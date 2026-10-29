#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩法层：天关、三大副本、竞技场、世界 Boss、生死塔、藏宝洞、兄弟天关、仙魔战场、签到、贵族。

所有时间窗、次数上限、权重、门槛都从 `data/runtime-config.json`（线上真实下发）读取，
不再硬编码；玩家侧次数与进度写在 `state/state.json` 的 meta 里，跨天自动归零。
"""
from __future__ import annotations

import json
import random
import threading
import time
from datetime import timedelta

from core import Ctx, RULES, STATE_DIR, STORE, fail, in_window, iso, now, ok, route, today
from core import capture_wild_pet
from handlers_battle import active_pet, combat_options, player_fighter, pet_fighter
import combat


# ================================================================ 无限天关

@route("GET", "/tower/info")
def tower_info(ctx: Ctx) -> dict:
    p = ctx.state.player
    return {
        "success": True,
        "towerHighest": int(p.get("tower_highest", 0)),
        "sweepCount": int(p.get("tower_sweep_count", 0)),
        "towerFloor": int(p.get("tower_floor", 1)),
        "openMaxFloor": int(RULES.v("tower", "openMaxFloor", 1899) or 1899),
        "openMaxFloorGlobal": int(RULES.v("tower", "openMaxFloorGlobal", 1899) or 1899),
        "openMaxFloorBypass": False,
        "maxFloor": int(RULES.v("tower", "maxFloor", 2000) or 2000),
    }


@route("POST", "/tower/update")
def tower_update(ctx: Ctx) -> dict:
    highest = ctx.i("highestFloor")
    p = ctx.state.player
    if highest > int(p.get("tower_highest", 0)):
        p["tower_highest"] = highest
        ctx.state.bump()
    return ok(towerHighest=int(p.get("tower_highest", 0)))


@route("POST", "/tower/sweep")
def tower_sweep(ctx: Ctx) -> dict:
    p = ctx.state.player
    highest = int(p.get("tower_highest", 0))
    if highest <= 0:
        return fail("尚未通关任何层，无法扫荡")
    # 扫荡直接推到最高层，奖励按每 100 层走邮件
    p["tower_floor"] = max(int(p.get("tower_floor", 1)), highest + 1)
    packs = [{"name": "%d关礼包" % floor, "count": 1} for floor in range(100, highest + 1, 100)]
    if packs:
        ctx.state.send_mail("天关扫荡 · 百关礼包", "您通过天关扫荡获得以下百关礼包，请查收附件。", packs)
    p["tower_sweep_count"] = int(p.get("tower_sweep_count", 0)) + 1
    ctx.state.bump()
    return ok(towerFloor=int(p["tower_floor"]), sweepCount=int(p["tower_sweep_count"]), items=packs)


@route("GET", "/tower/ranking")
def tower_ranking(ctx: Ctx) -> dict:
    return {"success": True, "rankings": STORE.rankings("tower")[:100]}


# ================================================================ 套装副本

def _suit_sweep_cap(state) -> int:
    """套装副本每日扫荡上限：配置值与 VIP 表取大。

    客户端 vip_benefits.suit_sweeps_max = [2,3,4,5]。以前这里只读配置
    （maxSweeps=2），黄金 VIP 点第 3 次会被服务端拒掉，界面上却显示还能扫。
    """
    base = int(RULES.v("suit_dungeon", "maxSweeps", 2) or 2)
    tier = int(state.vip_tier())
    table = state.VIP_SUIT_SWEEPS
    return max(base, int(table[max(0, min(tier, len(table) - 1))]))


@route("GET", "/suit-dungeon/info")
def suit_dungeon_info(ctx: Ctx) -> dict:
    p = ctx.state.player
    max_sweeps = _suit_sweep_cap(ctx.state)
    used = int(p.get("suit_dungeon_sweeps", 0))
    return {"success": True, "maxSweeps": max_sweeps, "sweepsLeft": max(0, max_sweeps - used)}


@route("POST", "/suit-dungeon/sweep")
def suit_dungeon_sweep(ctx: Ctx) -> dict:
    state = ctx.state
    p = state.player
    max_sweeps = _suit_sweep_cap(state)
    used = int(p.get("suit_dungeon_sweeps", 0))
    if used >= max_sweeps:
        return fail("今日扫荡次数已用完")
    p["suit_dungeon_sweeps"] = used + 1
    gold = int(RULES.v("suit_dungeon", "sweepGoldBase", 0) or 0)
    if gold:
        state.add_gold(gold)
    state.bump()
    return ok(sweepsLeft=max(0, max_sweeps - used - 1), newGold=state.gold())


@route("GET", "/suit-dungeon/ranking")
def suit_dungeon_ranking(ctx: Ctx) -> dict:
    return {"success": True, "rankings": STORE.rankings("suit_dungeon")[:100]}


# ================================================================ 五行副本

@route("GET", "/five-elements-dungeon/status")
def fe_status(ctx: Ctx) -> dict:
    state = ctx.state
    p = state.player
    daily_max = int(RULES.v("five_elements_dungeon", "dailyMax", 10) or 10)
    yuanbao_max = int(RULES.v("five_elements_dungeon", "yuanbaoDailyMax", 1) or 1)
    used = int(p.get("five_elements_dungeon_attempts", 0))
    yb_used = int(p.get("fe_yuanbao_attempts", 0)) if str(p.get("fe_yuanbao_attempts_date", "")) == today() else 0
    return {
        "success": True,
        "enabled": bool(RULES.v("five_elements_dungeon", "enabled", True)),
        "maxAttempts": daily_max,
        "remainingAttempts": max(0, daily_max - used),
        "yuanbaoMaxAttempts": yuanbao_max,
        "yuanbaoRemaining": max(0, yuanbao_max - yb_used),
    }


@route("POST", "/five-elements-dungeon/start")
def fe_start(ctx: Ctx) -> dict:
    state = ctx.state
    p = state.player
    daily_max = int(RULES.v("five_elements_dungeon", "dailyMax", 10) or 10)
    used = int(p.get("five_elements_dungeon_attempts", 0))
    if used >= daily_max:
        return fail("今日五行副本次数已用完")
    p["five_elements_dungeon_attempts"] = used + 1
    state.bump()
    return ok(remainingAttempts=max(0, daily_max - used - 1), waves=int(RULES.v("five_elements_dungeon", "monsters.waveCount", 10) or 10))


@route("POST", "/five-elements-dungeon/clear")
def fe_clear(ctx: Ctx) -> dict:
    state = ctx.state
    rewards = RULES.v("five_elements_dungeon", "rewards", []) or []
    got = []
    for row in rewards:
        chance = float(row.get("rate", 0) or 0)
        if random.random() < chance:
            state.add_item(str(row.get("name", "五行奖励")), 1, "item")
            got.append({"name": str(row.get("name", "")), "count": 1})
    state.bump()
    return ok(items=got, rewards=got, newGold=state.gold())


# ================================================================ 图鉴副本

@route("GET", "/atlas-dungeon/status")
def atlas_status(ctx: Ctx) -> dict:
    state = ctx.state
    tiers_cfg = RULES.v("atlas_dungeon", "tiers", {}) or {}
    tiers = {}
    for key, cfg in tiers_cfg.items():
        cap = int(cfg.get("dailyMax", 30))
        used = state.daily("atlas_%s" % key)
        tiers[key] = {
            "label": cfg.get("label", key),
            "usedAttempts": used,
            "maxAttempts": cap,
            "remainingAttempts": max(0, cap - used),
            "entryCost": int(cfg.get("entryCost", 0)),
            "waveCount": int(cfg.get("waveCount", 0)),
        }
    return {"success": True, "enabled": bool(RULES.v("atlas_dungeon", "enabled", True)), "tiers": tiers}


@route("POST", "/atlas-dungeon/start")
def atlas_start(ctx: Ctx) -> dict:
    state = ctx.state
    tier_key = ctx.s("tier") or "basic"
    cfg = RULES.v("atlas_dungeon", "tiers.%s" % tier_key, {}) or {}
    if not cfg:
        return fail("图鉴副本档位不存在")
    cap = int(cfg.get("dailyMax", 30))
    used = state.daily("atlas_%s" % tier_key)
    if used >= cap:
        return fail("今日该档次数已用完")
    cost = int(cfg.get("entryCost", 0))
    if state.gold() < cost:
        return fail("金币不足")
    state.add_gold(-cost)
    state.daily_inc("atlas_%s" % tier_key)
    state.bump()
    return ok(
        tier=tier_key,
        waveCount=int(cfg.get("waveCount", 0)),
        monstersPerWave=int(cfg.get("monstersPerWave", 5)),
        monsterLevel=int(cfg.get("monsterLevel", 100)),
        remainingAttempts=max(0, cap - used - 1),
        newGold=state.gold(),
    )


@route("POST", "/atlas-dungeon/finish")
def atlas_finish(ctx: Ctx) -> dict:
    """图鉴副本结算 + 按原版口径捕捉。

    客户端 game_manager `_on_atlas_dungeon_all_waves_cleared` 只认
    `summary.rosterCount / atlasCount / discardedCount` 和 `saveData`；
    旧实现回了个物种表里根本不存在的 `atlas_pet`，也没入包 ——
    图鉴副本打完等于白打，图鉴一只都不涨。
    """
    state = ctx.state
    op_id = ctx.s("operationId")
    ops = state.meta.setdefault("atlasFinishOps", {})
    if not isinstance(ops, dict):
        ops = {}
        state.meta["atlasFinishOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(duplicate=True, summary=memo.get("summary", {}), captured=[],
                  saveData=state.save, saveRevision=state.revision,
                  newGold=state.gold())

    tier_key = ctx.s("tier") or "basic"
    tier_cfg = RULES.v("atlas_dungeon", "tiers.%s" % tier_key, {}) or {}
    try:
        tier_waves = int(tier_cfg.get("waveCount", 0) or 0)
    except (TypeError, ValueError):
        tier_waves = 0
    if tier_waves <= 0:
        tier_waves = 20
    waves = ctx.i("wavesCleared") or ctx.i("wave") or tier_waves
    # 单局波数不能超过本档配置（防客户端塞超长）
    waves = max(0, min(int(waves or 0), tier_waves))
    gold = waves * 100000
    state.add_gold(gold)

    try:
        threshold = float(ctx.body.get("minGrowthThreshold", 0.0) or 0.0)
    except (TypeError, ValueError):
        threshold = 0.0
    raw_defeated = ctx.body.get("defeatedPetIds")
    defeated = raw_defeated if isinstance(raw_defeated, list) else []
    # 一局最多 waveCount × monstersPerWave，防客户端塞超长数组把结算拖死
    defeated = defeated[:1200]

    vip = state.vip_tier() > 0
    roster_n = atlas_n = discard_n = 0
    captured_rows: list = []
    for pid in defeated:
        pid_s = str(pid or "").strip()
        if not pid_s:
            continue
        r = capture_wild_pet(state, pid_s, "atlas_dungeon", threshold, vip)
        if r.get("pet"):
            roster_n += 1
            captured_rows.append({"petId": pid_s, "toAtlas": False,
                                  "name": str(r["pet"].get("name", ""))})
        elif r.get("reason") == "below_min_growth_to_collection":
            atlas_n += 1
            captured_rows.append({"petId": pid_s, "toAtlas": True})
        elif r.get("reason") in ("below_min_growth_discarded", "roster_full"):
            discard_n += 1

    summary = {
        "rosterCount": roster_n,
        "atlasCount": atlas_n,
        "discardedCount": discard_n,
        "successCount": roster_n + atlas_n,
        "defeatedCount": len(defeated),
    }
    state.bump()
    if op_id:
        ops[op_id] = {"at": iso(now()), "summary": summary}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(
        wavesCleared=waves,
        goldGained=gold,
        newGold=state.gold(),
        summary=summary,
        captured=captured_rows,
        saveData=state.save,
        saveRevision=state.revision,
    )


# ================================================================ 竞技场

def _arena_state(state) -> dict:
    box = state.meta.setdefault("arena", {})
    week = int(now().isocalendar().week)
    if int(box.get("week", 0)) != week:
        box["week"] = week
        box["points"] = 0
        box["wins"] = 0
    return box


@route("GET", "/arena/status")
def arena_status(ctx: Ctx) -> dict:
    state = ctx.state
    box = _arena_state(state)
    cfg = RULES.m("arena")
    cap = int(cfg.get("dailyMaxAttempts", 10))
    used = state.daily("arena_attempts")
    rows = ctx.state.ranking_rows("players")[:75]
    return {
        "success": True,
        "weekNumber": box["week"],
        "featureEnabled": bool(cfg.get("featureEnabled", True)),
        "inChallengeWindow": in_window(
            int(cfg.get("challengeStartHour", 8)), int(cfg.get("challengeStartMinute", 30)),
            int(cfg.get("challengeEndHour", 23)), int(cfg.get("challengeEndMinute", 30)),
        ),
        "remainingAttempts": max(0, cap - used),
        "dailyAttemptsUsed": used,
        "weeklyArenaPoints": int(box.get("points", 0)),
        "balanceArenaPoints": int(box.get("points", 0)),
        "spendableArenaPoints": int(box.get("points", 0)),
        "winStreak": int(box.get("streak", 0)),
        "myRank": int(box.get("rank", 0)),
        "hasMirror": False,
        "mirrorUpdatedAt": None,
        "mirrorSeedScore": 0,
        "config": cfg,
        "ranking": rows,
        "top3": rows[:3],
    }


@route("POST", "/arena/challenge")
def arena_challenge(ctx: Ctx) -> dict:
    state = ctx.state
    cfg = RULES.m("arena")
    cap = int(cfg.get("dailyMaxAttempts", 10))
    used = state.daily("arena_attempts")
    if used >= cap:
        return fail("今日竞技次数已用完")
    if not in_window(int(cfg.get("challengeStartHour", 8)), int(cfg.get("challengeStartMinute", 30)),
                     int(cfg.get("challengeEndHour", 23)), int(cfg.get("challengeEndMinute", 30))):
        return fail("当前不在竞技场开放时间")
    state.daily_inc("arena_attempts")
    box = _arena_state(state)
    player = player_fighter(ctx)
    pet_data = active_pet(ctx)
    pet = pet_fighter(pet_data) if pet_data else None
    opponents = [combat.make_monster("arena_opponent", "对手", int(state.player.get("level", 1)),
                                     random.choice(["金", "木", "水", "火", "土"]))]
    result = combat.resolve(player, pet, opponents, int(cfg.get("battleMaxRounds", 20)),
                            **combat_options(state, pet_data))
    win = result["victory"]
    delta = int(cfg.get("winScoreDelta", 10)) if win else int(cfg.get("loseScoreDelta", 2))
    box["points"] = int(box.get("points", 0)) + delta
    box["wins"] = int(box.get("wins", 0)) + (1 if win else 0)
    box["streak"] = int(box.get("streak", 0)) + 1 if win else 0
    state.bump()
    return ok(
        victory=win,
        scoreDelta=delta,
        weeklyArenaPoints=box["points"],
        winStreak=box["streak"],
        remainingAttempts=max(0, cap - used - 1),
        monsters=[m.public() for m in opponents],
        battleLog=result["battleLog"],
        playerName=player.name,
        petName=pet.name if pet else "",
    )


@route("GET", "/arena/shop/items")
def arena_shop_items(ctx: Ctx) -> dict:
    rows = RULES.arena_shop
    items = rows.get("items") if isinstance(rows, dict) else rows
    if not isinstance(items, list):
        items = RULES.v("arena", "shopItems", []) or []
    return {"success": True, "items": items}


@route("GET", "/arena/titles")
def arena_titles(ctx: Ctx) -> dict:
    rows = RULES.arena_titles
    titles = rows.get("titles") if isinstance(rows, dict) else rows
    if not isinstance(titles, list):
        titles = RULES.v("arena", "titleNames", []) or []
    return {"success": True, "titles": titles}


# ================================================================ 世界 Boss

def _boss_state(state) -> dict:
    cfg = RULES.m("world_boss")
    box = state.meta.setdefault("worldBoss", {})
    if box.get("date") != today():
        box["date"] = today()
        box["damage"] = 0
    return box


@route("GET", "/world-boss/status")
def world_boss_status(ctx: Ctx) -> dict:
    state = ctx.state
    cfg = RULES.m("world_boss")
    box = _boss_state(state)
    cap = int(cfg.get("dailyMaxAttempts", 3))
    used = state.daily("world_boss")
    return {
        "success": True,
        "eventDate": today(),
        "bossPetId": "shuizhizhangkong",
        "featureEnabled": bool(cfg.get("featureEnabled", True)),
        "inChallengeWindow": in_window(int(cfg.get("challengeStartHour", 9)), int(cfg.get("challengeStartMinute", 30)),
                                       int(cfg.get("challengeEndHour", 22)), int(cfg.get("challengeEndMinute", 30))),
        "remainingAttempts": max(0, cap - used),
        "myTotalDamage": int(box.get("damage", 0)),
        "myRank": int(box.get("rank", 0)),
        "config": cfg,
        "ranking": [],
    }


@route("POST", "/world-boss/challenge")
def world_boss_challenge(ctx: Ctx) -> dict:
    state = ctx.state
    cfg = RULES.m("world_boss")
    cap = int(cfg.get("dailyMaxAttempts", 3))
    used = state.daily("world_boss")
    if used >= cap:
        return fail("今日挑战次数已用完")
    if not in_window(int(cfg.get("challengeStartHour", 9)), int(cfg.get("challengeStartMinute", 30)),
                     int(cfg.get("challengeEndHour", 22)), int(cfg.get("challengeEndMinute", 30))):
        return fail("当前不在世界 Boss 开放时间")
    state.daily_inc("world_boss")
    pet_data = active_pet(ctx)
    rounds = int(cfg.get("battleMaxRounds", 10))
    attack = float(pet_data.get("final_attack", 100)) if pet_data else 100.0
    boss_def = float(cfg.get("bossDefense", 30000))
    damage = 0
    log = [{"type": "battle_start", "text": "战斗开始"}]
    for rnd in range(1, rounds + 1):
        log.append({"type": "round", "round": rnd, "text": "第 %d 回合" % rnd})
        hit = max(1, int(attack * random.uniform(0.9, 1.1) - boss_def * 0.3))
        damage += hit
        log.append({"type": "attack", "round": rnd, "attacker": str(pet_data.get("name", "灵兽")) if pet_data else "你",
                    "attackerKind": "pet", "attackerSide": "A", "attackerIndex": 0, "damage": hit,
                    "isCritical": False, "isPetAttack": True, "isEnemyAttack": False, "isArmorPierce": False,
                    "isCombo": False, "isJointAttack": False, "target": "世界Boss", "targetKind": "monster",
                    "targetSide": "B", "targetIndex": 0,
                    "targetHp": max(0, int(cfg.get("bossHp", 1)) - damage), "targetMaxHp": int(cfg.get("bossHp", 1))})
    log.append({"type": "victory", "text": "战斗结束"})
    box = _boss_state(state)
    box["damage"] = int(box.get("damage", 0)) + damage
    # 伤害达标发礼包（走邮件）
    gift = str(cfg.get("giftItemName", "世界首领锦囊"))
    state.send_mail("世界 Boss 参与礼包", "世界 Boss 挑战结算，请查收附件。", [{"name": gift, "count": 1}])
    state.bump()
    return ok(damage=damage, totalDamage=box["damage"], remainingAttempts=max(0, cap - used - 1),
              battleLog=log, items=[{"name": gift, "count": 1}])


# ================================================================ 生死塔

@route("GET", "/shengsi-tower/overview")
def shengsi_overview(ctx: Ctx) -> dict:
    cfg = RULES.m("shengsi_tower")
    floors = cfg.get("floors", [])
    return {
        "success": True,
        "featureEnabled": bool(cfg.get("featureEnabled", True)),
        "config": cfg,
        "floors": floors,
        "window": {
            "signupRangeText": cfg.get("signupRangeText", ""),
            "signupOpen": in_window(int(cfg.get("signupStartHour", 20)), int(cfg.get("signupStartMinute", 30)), 23, 59),
            "startHour": int(cfg.get("startHour", 20)),
        },
        "mySignup": ctx.state.meta.get("shengsi", {}).get("signup"),
    }


@route("GET", "/shengsi-tower/window")
def shengsi_window(ctx: Ctx) -> dict:
    cfg = RULES.m("shengsi_tower")
    return {
        "success": True,
        "signupOpen": in_window(int(cfg.get("signupStartHour", 20)), int(cfg.get("signupStartMinute", 30)), 23, 59),
        "signupRangeText": cfg.get("signupRangeText", ""),
        "startHour": int(cfg.get("startHour", 20)),
        "startMinute": int(cfg.get("startMinute", 40)),
    }


@route("POST", "/shengsi-tower/signup")
def shengsi_signup(ctx: Ctx) -> dict:
    state = ctx.state
    box = state.meta.setdefault("shengsi", {})
    if box.get("signup", {}).get("date") == today():
        return fail("今日已报名")
    floor = ctx.i("floor") or 1
    box["signup"] = {"date": today(), "floor": floor, "at": iso(now())}
    points = int(RULES.v("shengsi_tower", "participateFriendshipPoints", 5) or 5)
    state.add_friendship(points)
    gift = str(RULES.v("shengsi_tower", "participateGiftItem", "") or "")
    if gift and RULES.v("shengsi_tower", "participateGiftEnabled", False):
        state.send_mail("生死塔参与锦囊", "生死塔报名成功，请查收附件。", [{"name": gift, "count": 1}])
    state.bump()
    return ok(floor=floor, friendshipPoints=state.friendship())


@route("GET", "/shengsi-tower/room/list")
def shengsi_room_list(ctx: Ctx) -> dict:
    return {"success": True, "rooms": []}


@route("GET", "/shengsi-tower/my-battles")
def shengsi_my_battles(ctx: Ctx) -> dict:
    return {"success": True, "battles": ctx.state.meta.get("shengsi", {}).get("battles", [])}


# ================================================================ 藏宝洞

def _treasure_state(state) -> dict:
    box = state.meta.setdefault("treasure", {})
    if box.get("date") != today():
        box["date"] = today()
        box["join"] = 0
    return box


@route("GET", "/treasure/status")
def treasure_status(ctx: Ctx) -> dict:
    """藏宝洞状态：真实整点轮次（见 treasure_round.py），不再恒返回 round=null。"""
    import treasure_round

    return treasure_round.payload(ctx.state)


@route("POST", "/treasure/join")
def treasure_join(ctx: Ctx) -> dict:
    """择定秘箱入阵：一轮一人一箱，日内次数受 VIP 限制。"""
    import treasure_round

    chest_id = ctx.i("chestId") or ctx.i("chest_id") or ctx.i("chestNo")
    good, payload = treasure_round.join(ctx.state, chest_id)
    if not good:
        return fail(str(payload.get("error", "入阵失败")))
    return payload


def _treasure_gold_from_name(name: str) -> int:
    """「1亿金币」/「金币30亿」/「1000万金币」→ 金币数（藏宝匣奖池口径）。"""
    text = str(name or "").strip()
    if not text:
        return 0
    digits = ""
    unit = 0
    for ch in text:
        if ch.isdigit():
            digits += ch
        elif ch == "亿":
            unit = 100000000
            break
        elif ch == "万":
            unit = 10000
            break
    if not digits:
        return 0
    return int(digits) * (unit or 1)


@route("POST", "/treasure/chest/open")
def treasure_chest_open(ctx: Ctx) -> dict:
    """藏宝匣开奖（服务端权威）。

    原来只回 items，不回 saveData 也不扣箱子：客户端 `use()` 里
    「有 saveData 才 apply_cloud_save / 才 count -= 1」，于是
    弹「使用成功」但藏宝匣还在背包里。
    """
    state = ctx.state
    token = ctx.s("token") or ctx.s("treasureToken")
    op_id = ctx.s("operationId")
    treasure_meta = state.meta.setdefault("treasure", {})
    ops = treasure_meta.get("openOps")
    if not isinstance(ops, dict):
        ops = {}
        treasure_meta["openOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(duplicate=True, alreadyOpened=True, message=str(memo.get("message", "奖励已发放")),
                  reward=memo.get("reward", {}), items=memo.get("items", []),
                  saveData=state.save, saveRevision=state.revision, newGold=state.gold())

    # 扣掉对应的藏宝匣：客户端把 token 存在 custom_data.treasure_token 里
    removed = False
    for item in list(state.items):
        cd = item.get("custom_data")
        if isinstance(cd, dict) and str(cd.get("treasure_token", "")).strip() == str(token).strip() and token:
            cnt = max(0, int(item.get("count", 1) or 1))
            if cnt > 1:
                item["count"] = cnt - 1
            else:
                state.items.remove(item)
            removed = True
            break

    rewards = RULES.v("treasure", "rewards", []) or []
    row = RULES.roll(rewards)
    items = []
    reward: dict = {}
    message = "藏宝匣已开启"
    if row:
        name = str(row.get("name", "") or "")
        count = max(1, int(row.get("count", 1) or 1))
        rtype = str(row.get("type", "") or "")
        if rtype == "gold" or name.endswith("金币"):
            amount = _treasure_gold_from_name(name) * count
            if amount > 0:
                state.add_gold(amount)
                reward = {"type": "gold", "name": name, "count": amount}
                message = "获得 %s" % name
        if not reward and name:
            state.add_item(name, count, "item")
            items.append({"name": name, "count": count})
            reward = {"type": "item", "name": name, "count": count}
            message = "获得 %s x%d" % (name, count)
    if not reward:
        message = "藏宝匣已开启（本次没有获得道具）"

    state.bump()
    if op_id:
        ops[op_id] = {"at": iso(now()), "reward": reward, "items": items, "message": message}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)

    # 开箱出货播报（对齐原游戏 treasure_cave_open：恭喜玩家【大火花】开启藏宝匣，
    # 幸运获得【五阶宠物锦囊】！）。只播「开出了东西」的，空箱不播。
    if reward:
        rname = str(reward.get("name", "") or "")
        rcount = max(1, int(reward.get("count", 1) or 1))
        display = "%s x%d" % (rname, rcount) if rcount > 1 else rname
        import broadcasts as _broadcasts
        _broadcasts.append("treasure_cave_open", state.nickname(),
                           "恭喜玩家【%s】开启藏宝匣，幸运获得【%s】！" % (state.nickname(), display),
                           {"rewards": [display], "rewardCount": 1})

    return ok(duplicate=False, alreadyOpened=False, message=message, reward=reward, items=items,
              chestConsumed=removed,
              saveData=state.save, saveRevision=state.revision, newGold=state.gold())


@route("GET", "/treasure/battle-log")
def treasure_battle_log(ctx: Ctx) -> dict:
    """某个藏宝匣的逐场争夺战报（单败签表 + 每场战斗日志，可点开回放）。

    客户端 ui/city/treasure_cave_ui.gd:_show_battle_log_list_dialog 读的是
    `payload.matches`（数组，逐场一条）+ total/page/pageSize。
    这接口原来读的是个人 meta 里那个从来没人写的 matches，所以永远空、
    点了只弹「暂无战斗日志」；现在改成读服务端权威的整轮留档
    （treasure_round.battle_log：先查本轮，再查最近 8 轮的历史）。
    """
    import treasure_round

    chest_id = ctx.qi("chestId", 0) or ctx.qi("chest_id", 0) or ctx.qi("chestNo", 0)
    page = max(1, ctx.qi("page", 1))
    page_size = max(1, min(ctx.qi("pageSize", 10), 50))
    return treasure_round.battle_log(chest_id, page, page_size)


@route("GET", "/treasure/participants")
def treasure_participants(ctx: Ctx) -> dict:
    import treasure_round

    chest_id = ctx.qi("chestId", 0) or ctx.qi("chest_id", 0) or ctx.qi("chestNo", 0)
    return treasure_round.participants(ctx.state, chest_id)


@route("POST", "/treasure/chest/validate-tokens")
def treasure_validate_tokens(ctx: Ctx) -> dict:
    return ok(valid=True)


# ================================================================ 兄弟天关

@route("GET", "/brother-tower/status")
def brother_status(ctx: Ctx) -> dict:
    state = ctx.state
    cfg = RULES.m("brother_tower")
    box = state.meta.setdefault("brotherTower", {})
    p = state.player
    return {
        "success": True,
        "dailyRewardCount": state.daily("bt_reward"),
        "dailyRewardMax": int(cfg.get("dailyRewardMax", 1)),
        "dailyClearCount": state.daily("bt_clear"),
        # 原版：基础次数 + 贵族每级 +N 次（brother_tower_ui.gd:419-424）
        "dailyClearMax": int(cfg.get("dailyClearMax", 3))
        + state.noble_level() * int(cfg.get("nobleClearBonusPerLevel", 1) or 0),
        "friendshipPoints": state.friendship(),
        "reincarnation": int(p.get("reincarnation", 0)),
        "myRoomId": box.get("joined"),
        "pinnedSnapshot": box.get("pinned") or {"hasPinned": False},
        "difficulties": cfg.get("difficulties", []),
    }


@route("GET", "/brother-tower/list")
def brother_list(ctx: Ctx) -> dict:
    box = ctx.state.meta.setdefault("brotherTower", {})
    return {"success": True, "rooms": box.get("rooms", []), "myRoom": box.get("joined"),
            "myPendingApplicationRoomIds": []}


@route("POST", "/brother-tower/create")
def brother_create(ctx: Ctx) -> dict:
    state = ctx.state
    box = state.meta.setdefault("brotherTower", {})
    if box.get("joined"):
        return fail("已在房间中")
    room = {
        "id": int(now().timestamp()) % 100000,
        "owner": state.nickname(),
        "difficulty": ctx.s("difficulty") or "easy",
        "members": [state.nickname()],
        "status": "waiting",
        "createdAt": iso(now()),
    }
    box.setdefault("rooms", []).append(room)
    box["joined"] = room["id"]
    state.bump()
    return ok(room=room, roomId=room["id"])


@route("POST", "/brother-tower/join")
def brother_join(ctx: Ctx) -> dict:
    state = ctx.state
    box = state.meta.setdefault("brotherTower", {})
    room_id = ctx.i("roomId")
    for room in box.get("rooms", []):
        if int(room.get("id", 0)) == room_id:
            room.setdefault("members", []).append(state.nickname())
            box["joined"] = room_id
            state.bump()
            return ok(room=room, roomId=room_id)
    return fail("房间不存在")


@route("POST", "/brother-tower/leave")
def brother_leave(ctx: Ctx) -> dict:
    ctx.state.meta.setdefault("brotherTower", {})["joined"] = None
    return ok()


@route("POST", "/brother-tower/ready")
def brother_ready(ctx: Ctx) -> dict:
    return ok(ready=True)


@route("POST", "/brother-tower/start")
def brother_start(ctx: Ctx) -> dict:
    return ok(session={"id": int(now().timestamp()) % 100000, "status": "in_battle", "wave": 1})


@route("POST", "/brother-tower/complete")
def brother_complete(ctx: Ctx) -> dict:
    state = ctx.state
    cfg = RULES.m("brother_tower")
    box0 = state.meta.setdefault("brotherTower", {})
    op_id = ctx.s("operationId") or ctx.s("opId")
    # 幂等：客户端 complete 队列会重试，重放不能重复发奖、也不能重复刷战报
    if op_id and box0.get("lastCompleteOp") == op_id:
        return ok(**box0.get("lastCompleteResult", {}), duplicate=True)
    state.daily_inc("bt_clear")
    gold = 1000000
    state.add_gold(gold)
    points = int(cfg.get("friendshipPoints", 2) or 2)
    state.add_friendship(points)
    gift = str(cfg.get("giftItemName", ""))
    items = []
    if gift:
        state.send_mail("兄弟天关通关礼包", "兄弟天关通关奖励，请查收附件。", [{"name": gift, "count": 1}])
        items.append({"name": gift, "count": 1})

    # 通关战报（原游戏 brother_tower_clear）：「情比金坚，风雨同舟！\n
    # 恭喜【A】与【B】经过一番苦战联手通关【史诗】难度兄弟天关10层！」
    # 层数取配置 waveCount（原版 10 层），难度取房间难度。
    box = state.meta.setdefault("brotherTower", {})
    room = {}
    for r in box.get("rooms", []) or []:
        if int(r.get("id", 0)) == int(box.get("joined") or 0):
            room = r
            break
    members = [str(x) for x in (room.get("members") or []) if str(x).strip()]
    if not members:
        members = [state.nickname()]
    members = members[:2]
    difficulty = str(room.get("difficulty") or ctx.s("difficulty") or "easy")
    label = {"easy": "简单", "normal": "普通", "hard": "困难", "epic": "史诗"}.get(difficulty, difficulty)
    floors = int(cfg.get("waveCount", 10) or 10)
    if len(members) >= 2:
        body = "恭喜【%s】与【%s】经过一番苦战联手通关【%s】难度兄弟天关%d层！" % (
            members[0], members[1], label, floors)
    else:
        body = "恭喜【%s】经过一番苦战通关【%s】难度兄弟天关%d层！" % (members[0], label, floors)
    import broadcasts as _broadcasts
    _broadcasts.append("brother_tower_clear", state.nickname(),
                       "情比金坚，风雨同舟！\n" + body,
                       {"difficulty": difficulty, "difficultyLabel": label,
                        "playerNames": members, "floor": floors})

    state.bump()
    out = ok(goldGained=gold, friendshipPoints=state.friendship(), items=items)
    if op_id:
        box0["lastCompleteOp"] = op_id
        box0["lastCompleteResult"] = {"goldGained": gold, "friendshipPoints": state.friendship(),
                                      "items": items}
    return out


@route("POST", "/brother-tower/forfeit")
def brother_forfeit(ctx: Ctx) -> dict:
    return ok()


@route("GET", "/brother-tower/poll")
def brother_poll(ctx: Ctx) -> dict:
    box = ctx.state.meta.setdefault("brotherTower", {})
    return {
        "success": True,
        "rooms": box.get("rooms", []),
        "myRoom": box.get("joined"),
        "myPendingApplicationRoomIds": [],
        "status": brother_status(ctx),
        "session": {},
    }


@route("POST", "/brother-tower/pin-snapshot")
def brother_pin(ctx: Ctx) -> dict:
    state = ctx.state
    pet_data = active_pet(ctx)
    box = state.meta.setdefault("brotherTower", {})
    box["pinned"] = {
        "hasPinned": True,
        "pinnedAt": iso(now()),
        "playerName": state.nickname(),
        "playerLevel": int(state.player.get("level", 1)),
        "playerRealm": str(state.player.get("realm", "")),
        "playerReincarnation": int(state.player.get("reincarnation", 0)),
        "petName": str(pet_data.get("name", "")) if pet_data else "",
        "petLevel": int(pet_data.get("level", 1)) if pet_data else 1,
        "petId": str(pet_data.get("pet_id", "")) if pet_data else "",
    }
    state.bump()
    return ok(pinnedSnapshot=box["pinned"])


# ================================================================ 仙魔战场

# ---------------------------------------------------------------- 仙魔：周键 / 周结算 / 称号
#
# 原版节奏（客户端 scripts/xianmo/*.gd + 抓包）：
#   · 每周一 00:00（UTC+8）开新赛季：周积分归零、行动力回满、**阵营要重新选**；
#   · 上周按两阵营总积分定胜负，各阵营前 3 名发称号（7 天）+ 宝箱 + 金币 + 开悟丹，
#     其余参加过（有积分）的人发参与奖；
#   · 排行榜是「仙道榜 / 魔道榜」两个独立榜，按**周积分**排。
# 老实现只清了 score，faction 一直留着 → 玩家加入一次之后永远不用再选，
# 表现就是「默认都是仙道」；也没有任何结算，称号永远拿不到。
_XIANMO_SETTLE_LOCK = threading.RLock()
_XIANMO_SETTLE_FILE = STATE_DIR / "xianmo-settle.json"

# 称号表逐字来自 scripts/title/title_data.gd::_get_title_info
_XIANMO_TITLE_TABLE = {
    "xian": {
        "winner": {1: ("xian_zhizun", "仙道至尊", 1000),
                   2: ("xian_shengzhe", "仙道圣者", 800),
                   3: ("xian_zhenren", "仙道真人", 500)},
        "loser": {1: ("xian_xianfeng", "仙道先锋", 800),
                  2: ("xian_yongshi", "仙道勇士", 500),
                  3: ("xian_qiangzhe", "仙道强者", 300)},
    },
    "mo": {
        "winner": {1: ("mo_bazhu", "魔道霸主", 1000),
                   2: ("mo_tongling", "魔道统领", 800),
                   3: ("mo_mojiang", "魔道魔将", 500)},
        "loser": {1: ("mo_xianfeng", "魔道先锋", 800),
                  2: ("mo_yongshi", "魔道勇士", 500),
                  3: ("mo_qiangzhe", "魔道强者", 300)},
    },
}
# (宝箱类型, 个数, 金币, 开悟丹) —— 来自 scripts/xianmo/xianmo_reward.gd
_XIANMO_WIN_REWARDS = {1: ("gold", 2, 500_000_000, 5),
                       2: ("silver", 2, 300_000_000, 3),
                       3: ("bronze", 2, 200_000_000, 2)}
_XIANMO_LOSE_REWARDS = {1: ("gold", 1, 300_000_000, 3),
                        2: ("silver", 1, 200_000_000, 2),
                        3: ("bronze", 1, 100_000_000, 1)}
_XIANMO_PARTICIPATION = ("bronze", 1, 100_000_000, 1)
_XIANMO_TITLE_TTL = 7 * 86400


def _xianmo_week_key() -> int:
    """周编号：周起点 unix // 604800 + 1（和客户端 _calculate_week_number 同口径）。"""
    return int(_xianmo_week_bounds_unix()[0] // 604800) + 1


def _xianmo_title_items(box: dict) -> list:
    items = box.get("titleItems")
    if not isinstance(items, list):
        items = []
        box["titleItems"] = items
    return items


def _xianmo_grant_title(box: dict, faction: str, rank: int, is_winner: bool, week: int) -> dict | None:
    table = _XIANMO_TITLE_TABLE.get(faction, {}).get("winner" if is_winner else "loser", {})
    info = table.get(rank)
    if not info:
        return None
    title_id, title_name, bonus = info
    expire = int(time.time()) + _XIANMO_TITLE_TTL
    items = _xianmo_title_items(box)
    for row in items:
        if isinstance(row, dict) and row.get("title_id") == title_id:
            row.update({
                "title_name": title_name,
                "attack_bonus": bonus, "defense_bonus": bonus,
                "agility_bonus": bonus, "hp_bonus": bonus,
                "week_number": week, "expire_time_unix": expire, "expire_time": expire,
            })
            return row
    row = {
        "title_id": title_id, "title_name": title_name,
        "attack_bonus": bonus, "defense_bonus": bonus,
        "agility_bonus": bonus, "hp_bonus": bonus,
        "week_number": week, "expire_time_unix": expire, "expire_time": expire,
        "source": "xianmo",
    }
    items.append(row)
    del items[:-12]
    return row


def _xianmo_settle_due() -> dict:
    """惰性周结算：服务器不跑定时任务，靠玩家请求把上一周结掉（幂等）。

    只结算「当前周 - 1」，且全局只跑一次（marker 落盘）。玩家档案里若已
    `settlePaidWeek >= 上周` 则跳过，避免重启后重复发奖。
    """
    current = _xianmo_week_key()
    target = current - 1
    if target < 1:
        return {}
    with _XIANMO_SETTLE_LOCK:
        try:
            marker = json.loads(_XIANMO_SETTLE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = {}
        if int(marker.get("settledWeek", 0) or 0) >= target:
            return {}

        rows: list[dict] = []
        try:
            files = sorted((STATE_DIR / "accounts").glob("*.json"))
        except OSError:
            files = []
        for path in files:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            meta = raw.get("meta") or {}
            box = meta.get("xianmo")
            if not isinstance(box, dict):
                continue
            faction, score = "", 0
            box_week = int(box.get("week", 0) or 0)
            if box_week == target:
                faction = str(box.get("faction", "") or "")
                score = int(box.get("score", 0) or 0)
            elif box_week > target:
                last = box.get("lastWeek")
                if isinstance(last, dict) and int(last.get("week", 0) or 0) == target:
                    faction = str(last.get("faction", "") or "")
                    score = int(last.get("score", 0) or 0)
            if faction not in ("xian", "mo"):
                continue
            rows.append({
                "username": path.stem,
                "faction": faction,
                "score": max(0, score),
                "nickname": str(meta.get("nickname")
                                or ((raw.get("save") or {}).get("playerData") or {}).get("name")
                                or path.stem),
            })

        xian_total = sum(r["score"] for r in rows if r["faction"] == "xian")
        mo_total = sum(r["score"] for r in rows if r["faction"] == "mo")
        winner = "xian" if xian_total > mo_total else ("mo" if mo_total > xian_total else "draw")

        for faction in ("xian", "mo"):
            group = sorted([r for r in rows if r["faction"] == faction],
                           key=lambda r: r["score"], reverse=True)
            for idx, row in enumerate(group):
                row["rank"] = idx + 1
                _xianmo_pay_week_reward(row, winner, target)

        try:
            _XIANMO_SETTLE_FILE.write_text(json.dumps(
                {"settledWeek": target, "at": iso(now()),
                 "xianScore": xian_total, "moScore": mo_total, "winner": winner},
                ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        # 榜单/阵营统计都变了，缓存立刻失效
        _XIANMO_STATS_CACHE["at"] = 0.0
        _XIANMO_STATS_CACHE["data"] = {}
        _XIANMO_RANK_CACHE["at"] = 0.0
        _XIANMO_RANK_CACHE["data"] = {}
        return {"week": target, "winner": winner,
                "xianScore": xian_total, "moScore": mo_total, "players": len(rows)}


def _xianmo_pay_week_reward(row: dict, winner: str, week: int) -> None:
    """给单个玩家发上周奖励：称号直接进档，宝箱进仙魔宝箱栏，金币/丹药入账 + 邮件通知。"""
    try:
        st = STORE.get(row["username"])
    except Exception:
        return
    box = st.meta.setdefault("xianmo", {})
    if int(box.get("settlePaidWeek", 0) or 0) >= week:
        return
    faction = row["faction"]
    rank = int(row.get("rank", 0) or 0)
    is_winner = (winner == faction)
    lines = []
    # 先看有没有积分：0 分的人哪怕排进前 3（阵营人少时很常见）也不该拿名次奖
    if row["score"] > 0 and rank and rank <= 3:
        chest, chest_n, gold, pills = (_XIANMO_WIN_REWARDS if is_winner
                                       else _XIANMO_LOSE_REWARDS)[rank]
        title = _xianmo_grant_title(box, faction, rank, is_winner, week)
        if title:
            lines.append("称号：%s（7天，四维 +%d）" % (title["title_name"], title["attack_bonus"]))
    elif row["score"] > 0:
        chest, chest_n, gold, pills = _XIANMO_PARTICIPATION
    else:
        # 上周只报名、一次都没探险：不发参与奖，避免纯报名白领
        box["settlePaidWeek"] = week
        st.mark_dirty()
        return

    if gold:
        st.add_gold(gold)
        lines.append("金币：%d亿" % (gold // 100_000_000))
    if pills:
        st.add_item("开悟丹", pills, "item")
        lines.append("开悟丹 ×%d" % pills)
    if chest_n:
        for _ in range(chest_n):
            box.setdefault("chests", []).append(
                {"type": chest, "count": 1, "at": iso(now())})
        lines.append("%s宝箱 ×%d" % (_XIANMO_CHEST_LABEL.get(chest, chest), chest_n))

    box["settlePaidWeek"] = week
    box["lastSettle"] = {"week": week, "rank": rank, "faction": faction,
                         "winner": winner, "score": row["score"], "at": iso(now())}
    if lines:
        st.send_mail(
            "仙魔战场 · 第%d周结算" % week,
            "第%d周仙魔战场结算：%s阵营%s。\n你本周积分 %d，%s阵营第 %d 名。\n\n%s" % (
                week, "仙道" if winner == "xian" else ("魔道" if winner == "mo" else "双方"),
                "获胜" if winner in ("xian", "mo") else "战平",
                row["score"], "仙道" if faction == "xian" else "魔道", rank or 0,
                "\n".join("· " + x for x in lines)),
        )
    st.bump()
    st.flush()


def _xianmo(state) -> dict:
    """仙魔战场状态盒子：行动力按时间恢复（原版机制）。

    客户端 scripts/xianmo/xianmo_system.gd：
      - `action_points = int(player.get("actionPoints", 24))`，缺了这个字段永远 0；
      - 上限 VipBenefits.xianmo_max_action_points = 24/30/40/48（VIP0~3）；
      - 恢复间隔 VipBenefits.xianmo_recover_interval_seconds = 60/50/40/30 分钟 1 点；
      - 恢复只在客户端本地跑（last_recover_time 每次加载重置为 now），
        所以**离线期间的恢复必须服务端补**，否则玩家一下线就白等。
    """
    box = state.meta.setdefault("xianmo", {})
    # 周标识必须和客户端口径一致：周起点 unix // 604800 + 1（=2965）。
    # 老实现用 isocalendar().week（=44），导致 /xianmo/info 把 weekNumber=44
    # 回给客户端，把界面上的「第2965周」覆盖成「第44周」。
    week = _xianmo_week_key()
    if int(box.get("week", 0)) != week:
        old_week = int(box.get("week", 0) or 0)
        if old_week > 0:
            # 留一份上周成绩，供惰性结算读取（玩家本周已经点过界面、分数被清零了）
            box["lastWeek"] = {
                "week": old_week,
                "faction": str(box.get("faction", "") or ""),
                "score": int(box.get("score", 0) or 0),
                "totalScore": int(box.get("totalScore", 0) or 0),
                "at": iso(now()),
            }
        box["week"] = week
        box["score"] = 0
        box["actionPoints"] = int(state.VIP_XIANMO_POINTS[state.vip_tier()])
        box["lastRecoverAt"] = int(time.time())
        # 原版：新赛季要重新选仙道 / 魔道。不清的话玩家加入过一次就永远是那个阵营。
        box["faction"] = ""
        box["joinTime"] = ""
    _xianmo_regen(state, box)
    return box


def _xianmo_cap(state) -> int:
    return int(state.VIP_XIANMO_POINTS[state.vip_tier()])


def _xianmo_regen(state, box: dict | None = None) -> int:
    """结算行动力：按 VIP 恢复间隔补点，封顶；满值时把计时对齐到当前。"""
    box = box if isinstance(box, dict) else state.meta.setdefault("xianmo", {})
    vip = state.vip_tier()
    cap = int(state.VIP_XIANMO_POINTS[vip])
    interval = max(60, int(state.VIP_XIANMO_INTERVAL_MIN[vip]) * 60)
    now_ts = int(time.time())
    if int(box.get("actionPoints", -1)) < 0:
        box["actionPoints"] = cap
    points = max(0, min(cap, int(box.get("actionPoints", 0) or 0)))
    last = int(box.get("lastRecoverAt", 0) or 0)
    if last <= 0:
        box["lastRecoverAt"] = now_ts
        box["actionPoints"] = points
        return points
    if points >= cap:
        box["lastRecoverAt"] = now_ts
        box["actionPoints"] = cap
        return cap
    gained = (now_ts - last) // interval
    if gained > 0:
        points = min(cap, points + int(gained))
        box["lastRecoverAt"] = last + int(gained) * interval
    box["actionPoints"] = points
    return points


_XIANMO_EVENTS_CACHE: dict | None = None

# 宠装模板：与原客户端 scripts/xianmo/pet_equipment.gd 完全一致
# EQUIPMENT_TEMPLATES / BASE_STATS：青铜 300 / 白银 500 / 黄金 1000，
# 名称前缀 青铜 / 银月 / 金鳞，部位武器=攻击、防具=防御、饰品/头盔=生命、靴子=敏捷。
_XIANMO_EQ_TEMPLATE = {
    "bronze": (300, "青铜"),
    "silver": (500, "银月"),
    "gold": (1000, "金鳞"),
}
_XIANMO_EQ_SLOT = {
    "weapon": ("爪", "attack"),
    "armor": ("甲", "defense"),
    "accessory": ("坠", "hp"),
    "boots": ("靴", "agility"),
    "helmet": ("盔", "hp"),
}
# 2026-10-29：原来这里是 ["洗髓丹","灵兽经验丹","高级技能书"] / ["高级技能书","神兽蛋"]，
# 而客户端 game_data.gd ALL_ITEMS 里**根本没有这几个名字** → 邮件点「打开」直接报
# 「附件添加失败：未知物品：神兽蛋」，奖励永久卡死在邮箱（玩家截图实证）。
# 现在只在「客户端认得」的池子里抽，并在发之前用 data/client-item-names.json 兜底校验。
_CLIENT_ITEM_NAMES: set | None = None


def _client_item_names() -> set:
    """客户端 game_data.gd ALL_ITEMS 的键名快照（data/client-item-names.json）。"""
    global _CLIENT_ITEM_NAMES
    if _CLIENT_ITEM_NAMES is None:
        from core import client_item_names

        _CLIENT_ITEM_NAMES = client_item_names()
    return _CLIENT_ITEM_NAMES


def _client_has_item(name: str) -> bool:
    """客户端是否认识这个名字。白名单没加载成功时放行（不能让功能整体挂掉）。"""
    from core import client_has_item

    return client_has_item(name)


def _xianmo_pick_item(pool: list[str]) -> str:
    ok_pool = [n for n in pool if _client_has_item(n)]
    return random.choice(ok_pool or pool)


_XIANMO_RANDOM_ITEM_POOL = [
    "开悟丹", "强化石", "宠装强化石", "双倍经验符",
    "反打lv1技能书", "自复lv1技能书", "轻身lv1技能书", "长命lv1技能书", "力壮lv1技能书",
]
_XIANMO_RARE_ITEM_POOL = [
    "开悟丹", "五阶宠物蛋",
    "金纹蛋", "木纹蛋", "水纹蛋", "火纹蛋", "土纹蛋",
    "金纹异蛋", "木纹异蛋", "水纹异蛋", "火纹异蛋", "土纹异蛋",
]

# 捕获灵兽 → 发「仙魔礼包」到邮箱（原版线上口径：mail_manager 对这四个礼包
# 有专门的 xianmo_gift_growth 处理，开包走 /pet/gift 一类的服务端接口）。
# 以前直接 append 到 state.pet_bag，客户端宠物列表读不到这批（pet_id=xianmo_pet
# 没有模板），表现就是「提示捕获了灵兽，但哪儿都找不到」。
_XIANMO_PET_GIFT_BY_EVENT = {
    "spirit_beast": "仙魔灵兽锦囊",
    "spirit_beast_cub": "仙魔野生锦囊",
    "divine_beast_trace": "仙魔神兽锦囊",
    "mutant_beast": "仙魔变异锦囊",
    "spirit_beast_guardian": "仙魔灵兽锦囊",
    "pet_recognition": "仙魔灵兽锦囊",
}


def _xianmo_pet_gift(event_id: str, cfg: dict, growth: float) -> str:
    """按事件挑礼包；名字必须存在于客户端物品表，兜底用仙魔灵兽锦囊。"""
    gift = str(cfg.get("giftName") or _XIANMO_PET_GIFT_BY_EVENT.get(event_id, ""))
    if not gift or not _client_has_item(gift):
        gift = "仙魔灵兽锦囊"
    # 成长档位和礼包区间差太多时换更贴的一个（神兽/变异礼包成长更高）
    if growth >= 6.2 and _client_has_item("仙魔神兽锦囊"):
        gift = "仙魔神兽锦囊"
    elif growth >= 6.0 and _client_has_item("仙魔变异锦囊"):
        gift = "仙魔变异锦囊"
    elif growth < 5.0 and _client_has_item("仙魔野生锦囊"):
        gift = "仙魔野生锦囊"
    return gift


def _xianmo_events() -> dict:
    """仙魔事件表：从原客户端 scripts/xianmo/xianmo_event_data.gd 的 EVENTS 提取。"""
    global _XIANMO_EVENTS_CACHE
    if _XIANMO_EVENTS_CACHE is None:
        from core import DATA, load_json

        rows = load_json(DATA / "xianmo-events.json", {})
        if isinstance(rows, dict) and isinstance(rows.get("events"), dict):
            rows = rows["events"]
        _XIANMO_EVENTS_CACHE = rows if isinstance(rows, dict) else {}
    return _XIANMO_EVENTS_CACHE


def _xianmo_pick_event() -> tuple[str, dict]:
    """按原版 weight 加权抽一个事件（总权重含 35 个事件，覆盖宝箱/战斗/陷阱/机缘）。"""
    events = _xianmo_events()
    if not events:
        return "nothing_happened", {"type": "empty", "name": "无事发生", "weight": 1, "score": 0}
    weights = {k: max(1, int(v.get("weight", 1) or 1)) for k, v in events.items()}
    total = sum(weights.values())
    roll = random.uniform(0, total)
    acc = 0.0
    for event_id, weight in weights.items():
        acc += weight
        if roll <= acc:
            return event_id, events[event_id]
    event_id = list(events.keys())[-1]
    return event_id, events[event_id]


def _xianmo_make_equipment(quality: str) -> dict:
    """生成一件仙魔宝箱宠装（字段同时满足存档 schema 与开箱弹窗展示）。"""
    quality = quality if quality in _XIANMO_EQ_TEMPLATE else "bronze"
    base, prefix = _XIANMO_EQ_TEMPLATE[quality]
    slot = random.choice(list(_XIANMO_EQ_SLOT.keys()))
    suffix, stat = _XIANMO_EQ_SLOT[slot]
    value = int(round(base * random.uniform(0.9, 1.15)))
    name = "%s%s" % (prefix, suffix)
    eq = {
        # 存档字段（save.inventoryData.pet_equipment_items 用的 schema）
        "id": "xianmo_%s_%d" % (quality, int(random.random() * 1e10)),
        "name": "%s（宠）" % name,
        "slot": slot,
        "rarity": quality,
        "mp": 0,
        "attack": 0, "defense": 0, "agility": 0, "hp": 0,
        "equipped": False, "locked": False,
        "is_suit": False, "suit_id": 0, "suit_slot": "",
        "forge_count": 0, "forge_attr_counts": {},
        "is_sealed": False, "sealed_data": {},
        "pet_chest_tier": quality,
        "is_pet_equipment": True,
        "cultivation_req": 0, "base_cultivation_req": 0,
        "pet_enhance_level": 0,
        # 开箱弹窗读取的展示字段
        "equip_id": "",
        "equip_name": name,
        "quality": quality,
    }
    eq["equip_id"] = eq["id"]
    eq[stat] = value
    return eq


def _xianmo_chest_counts(state) -> dict:
    box = state.meta.setdefault("xianmo", {})
    counts = {"bronze": 0, "silver": 0, "gold": 0}
    for chest in box.get("chests", []) or []:
        ctype = str(chest.get("type", ""))
        if ctype in counts:
            counts[ctype] += max(0, int(chest.get("count", 1) or 1))
    return counts


def _xianmo_take_chest(state, chest_type: str) -> bool:
    """扣 1 个宝箱。

    注意宝箱在 meta.xianmo.chests 里是**堆叠**的（`{"type":"gold","count":2}`），
    原来整条 pop 掉：玩家有 2 个黄金匣、点批量开 2 个，
    `_xianmo_chest_counts` 算出来是 2（够），但第一次 take 就把整叠删了，
    第二次拿不到 → 实际只开 1 个、另一叠也跟着消失。
    """
    box = state.meta.setdefault("xianmo", {})
    chests = box.setdefault("chests", [])
    for idx, chest in enumerate(chests):
        if str(chest.get("type", "")) != chest_type:
            continue
        remain = max(0, int(chest.get("count", 1) or 1) - 1)
        if remain > 0:
            chest["count"] = remain
        else:
            chests.pop(idx)
        return True
    return False


def _xianmo_resolve_battle(ctx: Ctx, cfg: dict) -> tuple[bool, list, int, str]:
    """仙魔战场战斗结算：镜像对战 = 打一个与自己同档的对手，服务端算胜负。"""
    state = ctx.state
    faction = str(state.meta.get("xianmo", {}).get("faction", "xian") or "xian")
    enemy_faction = "mo" if faction == "xian" else "xian"
    opponent = "魔道修士" if enemy_faction == "mo" else "仙道修士"
    scale = random.uniform(0.86, 1.14)
    if str(cfg.get("difficulty", "")) == "hard":
        opponent = "魔修大能"
        scale = 1.15
    player = player_fighter(ctx)
    pet = active_pet(ctx)
    foe = combat.Fighter(
        name=opponent, kind="monster", side="B", index=0,
        hp=player.max_hp * scale, max_hp=player.max_hp * scale,
        attack=player.attack * scale, defense=player.defense * scale,
        agility=player.agility * scale, level=player.level,
    )
    result = combat.resolve(player, pet_fighter(pet) if pet else None,
                            [foe], max_rounds=10,
                            **combat_options(state, pet))
    return (
        bool(result.get("victory", False)),
        result.get("battleLog", []) or [],
        int(result.get("rounds", 0) or 0),
        opponent,
    )


def _xianmo_event_message(event_id: str, cfg: dict, gain: int, captured: bool | None = None) -> str:
    name = str(cfg.get("name", "仙魔奇遇"))
    etype = str(cfg.get("type", "empty"))
    if etype == "pet":
        return "你捕获了%s！" % name if captured else "你遇到了一只灵兽，但它逃脱了..."
    if etype == "treasure":
        return "你发现了【%s】！" % name
    if etype == "trap":
        return "你误入了【%s】，吃了点苦头..." % name
    if etype == "training":
        return "你在【%s】中静心修炼，修为有所精进" % name
    if etype == "fragment":
        return "你发现了藏宝图残片！"
    if etype == "fortune":
        return "你遇到了【%s】！" % name
    if etype == "empty":
        return "这里空无一物..."
    if gain != 0:
        return "%s（积分%+d）" % (name, gain)
    return name


_XIANMO_CHEST_LABEL = {"bronze": "青铜", "silver": "白银", "gold": "黄金"}
# 原游戏公屏里出现过的仙魔界战报类型（抓包 build/orig-capture/live.json）：
#   faction_confrontation / enlightenment / pet_recognition / ancient_cave /
#   formation_treasure / inheritance_memory / spirit_beast_cub / spirit_beast_market /
#   xianmo_battlefield / treasure_fragment
# 这里按事件 id 决定「要不要推、推什么类型」，文案逐字对齐原游戏模板。
_XIANMO_BROADCAST_EVENTS = {
    "ancient_cave": "ancient_cave",
    "immortal_relic": "ancient_cave",
    "heavenly_palace": "ancient_cave",
    "formation_treasure": "formation_treasure",
    "xianmo_battlefield": "xianmo_battlefield",
    "treasure_fragment": "treasure_fragment",
    "mysterious_fragment": "treasure_fragment",
    "enlightenment": "enlightenment",
    "enlightenment_stone": "enlightenment",
    "spirit_beast_market": "spirit_beast_market",
    "inheritance_memory": "inheritance_memory",
    "spirit_beast": "spirit_beast_cub",
    "spirit_beast_cub": "spirit_beast_cub",
    "divine_beast_trace": "spirit_beast_cub",
    "mutant_beast": "spirit_beast_cub",
    "spirit_beast_guardian": "spirit_beast_cub",
    "pet_recognition": "pet_recognition",
}


def _xianmo_broadcast(state, event_id: str, cfg: dict, rewards: list,
                      won: bool | None = None, opponent: str = "",
                      captured: bool | None = None, growth: float | None = None) -> None:
    """仙魔界探索结果推一条全服战报（对齐原游戏文案）。

    频率口径照原游戏：只播「有产出/打赢」的结果，空手、陷阱、失败一律不播，
    避免公屏被刷屏（原游戏 8 分钟 15 条就是这个量级）。
    """
    import broadcasts as _broadcasts

    nick = state.nickname()
    etype = str(cfg.get("type", "empty"))
    chest_type = ""
    item_name = ""
    for r in rewards or []:
        if not isinstance(r, dict):
            continue
        if not chest_type and str(r.get("type", "")) == "chest":
            chest_type = str(r.get("chestType", r.get("chest_type", "")) or "")
        if not item_name and str(r.get("type", "")) == "item":
            item_name = str(r.get("itemName", r.get("item_id", "")) or "")
    chest_label = _XIANMO_CHEST_LABEL.get(chest_type, "神秘")

    # 战斗：只在打赢敌对阵营修士时播（原游戏 faction_confrontation 文案）
    if etype == "battle":
        if event_id == "faction_confrontation" and won and opponent:
            _broadcasts.append(
                "faction_confrontation", nick,
                "【%s】在仙魔界击败了敌对阵营修士【%s】！" % (nick, opponent),
                {"type": "faction_confrontation", "opponent": opponent},
            )
        return

    btype = _XIANMO_BROADCAST_EVENTS.get(event_id)
    if not btype:
        return

    if btype == "ancient_cave":
        if not chest_type:
            return
        label = {"ancient_cave": "远古洞府", "immortal_relic": "仙府遗迹",
                 "heavenly_palace": "天宫遗址"}.get(event_id, str(cfg.get("name", "远古洞府")))
        content = "【%s】在仙魔界发现了%s，获得了%s宝箱！" % (nick, label, chest_label)
        extra = {"type": event_id, "chestType": chest_type}
    elif btype == "formation_treasure":
        if not chest_type:
            return
        content = "【%s】在仙魔界发现了阵法宝物，获得了%s宝箱！" % (nick, chest_label)
        extra = {"type": "formation_treasure", "chestType": chest_type}
    elif btype == "xianmo_battlefield":
        if not chest_type:
            return
        content = "【%s】在仙魔战场遗迹中发现了%s宝箱！" % (nick, chest_label)
        extra = {"type": "xianmo_battlefield", "chestType": chest_type}
    elif btype == "treasure_fragment":
        content = "【%s】在仙魔界发现了藏宝图残片！" % nick
        extra = {"type": "treasure_fragment"}
    elif btype == "enlightenment":
        amount = 0
        for r in rewards or []:
            if isinstance(r, dict) and str(r.get("type", "")) == "cultivation":
                amount = int(r.get("amount", 0) or 0)
        if amount <= 0:
            return
        content = "【%s】在仙魔界获得悟道机缘，修行值+%d！" % (nick, amount)
        extra = {"type": "enlightenment", "amount": amount}
    elif btype == "spirit_beast_market":
        if item_name != "建设材料":
            return
        content = "【%s】在仙魔界获得了【建设材料】！" % nick
        extra = {"type": "spirit_beast_market", "count": 1, "itemId": "建设材料"}
    elif btype == "inheritance_memory":
        if not item_name:
            return
        content = "【%s】在仙魔界发现了兄弟天关遗址，寻得【%s】！" % (nick, item_name)
        extra = {"type": "inheritance_memory", "count": 1, "itemId": item_name}
    elif btype in ("pet_recognition", "spirit_beast_cub"):
        if not captured:
            return
        g = float(growth or 0)
        if btype == "pet_recognition":
            content = "【%s】在仙魔界触发灵兽认主，获得了【仙魔灵兽锦囊】！（可开出成长%.2f）" % (nick, g)
        else:
            action = "发现灵兽幼崽" if event_id == "spirit_beast_cub" else "发现灵兽"
            content = "【%s】在仙魔界%s，获得了【仙魔灵兽锦囊】！（可开出成长%.2f）" % (nick, action, g)
        extra = {"type": btype, "growth": round(g, 2)}
    else:
        return

    _broadcasts.append(btype, nick, content, extra)


@route("GET", "/xianmo/info")
def xianmo_info(ctx: Ctx) -> dict:
    state = ctx.state
    _xianmo_settle_due()
    box = _xianmo(state)
    p = state.player
    return {
        "success": True,
        "joined": bool(box.get("faction")),
        "player": {
            "faction": str(box.get("faction", "") or ""),
            "joinTime": box.get("joinTime", ""),
            "weeklyScore": int(box.get("score", 0)),
            "totalScore": int(box.get("totalScore", 0)),
            "actionPoints": int(box.get("actionPoints", 0)),
            "maxActionPoints": _xianmo_cap(state),
            "recoverIntervalSeconds": int(state.VIP_XIANMO_INTERVAL_MIN[state.vip_tier()]) * 60,
            "weekNumber": int(box.get("week", 0)),
            "fragments": int(box.get("fragments", 0)),
            "titles": _xianmo_title_items(box),
            "activeTitle": str(box.get("activeTitle", "") or ""),
            "unclaimedChests": box.get("chests", []),
            "treasureMaps": box.get("treasureMaps", []),
            "lastSettle": box.get("lastSettle") or {},
        },
        "playerName": p.get("name", ""),
        "config": RULES.m("xianmo") or {},
    }


@route("POST", "/xianmo/join")
def xianmo_join(ctx: Ctx) -> dict:
    state = ctx.state
    _xianmo_settle_due()
    box = _xianmo(state)
    faction = str(ctx.s("faction") or "").strip().lower()
    if faction not in ("xian", "mo"):
        return fail("请选择仙道或魔道")
    # 门槛与原版客户端一致（修行 1000 / 金币 100 万），服务端把一道关，防改包绕过
    if int(state.player.get("cultivation", 0) or 0) < 1000:
        return fail("修行值需达到1000才能参与仙魔战场")
    if int(state.gold()) < 1_000_000:
        return fail("需要100万金币才能参与仙魔战场")
    if not _xianmo_registration_open():
        return fail("本周报名已截止，请等待下周")
    existing = str(box.get("faction", "") or "")
    if existing and existing != faction:
        return fail("本周已加入%s，下周可重新选择阵营" % ("仙道" if existing == "xian" else "魔道"))
    box["faction"] = faction
    box["joinTime"] = iso(now())
    if not box.get("actionPoints"):
        box["actionPoints"] = _xianmo_cap(state)
        box["lastRecoverAt"] = int(time.time())
    state.bump()
    return ok(faction=faction, actionPoints=int(box.get("actionPoints", 0)),
              maxActionPoints=_xianmo_cap(state), weekNumber=int(box.get("week", 0)))


def _xianmo_registration_open() -> bool:
    """报名截止：周日 22:00 之后不再接受报名（对齐客户端 _is_registration_open）。"""
    d = now()
    return not (d.weekday() == 6 and d.hour >= 22)


@route("GET", "/xianmo/faction/stats")
def xianmo_faction_stats(ctx: Ctx) -> dict:
    """阵营统计（全服真正的「仙道/魔道 人数 + 总积分」）。

    两个坑，2026-10-28 一起修掉：

    ① 客户端 `network.gd: xianmo_get_faction_stats()` **没带 Authorization**
       （只发了 Content-Type），而这条路由不在 PUBLIC_PATHS 里 → 服务端一律 401。
       401 的响应体里没有 xian/mo，客户端 `_on_xianmo_faction_stats_loaded`
       根本不会被触发，界面上「仙道 0人 0分 / 魔道 0人 0分」一直是初始值。
       所以这条路由现在走公开路径（见 server.PUBLIC_PATHS），这里也必须容忍
       ctx.state 为 None。

    ② 原来的人数/积分是**写死的**（61/63 人、1000+本周积分/980），
       跟真实玩家毫无关系。现在直接扫 `state/accounts/*.json` 里的
       `meta.xianmo`（faction / score / totalScore）做汇总，60 秒缓存。

    另外 `weekNumber` 必须按客户端口径给（= 周起点 unix // 604800 + 1，
    客户端 xianmo_system._calculate_week_number 就是这么算的）。老实现给的是
    `isocalendar().week`（44），一旦这条接口开始返回数据，标题会从「第2965周」
    变成「第44周」——那是修好一个坑又踩出一个坑。
    """
    _xianmo_settle_due()
    stats = _xianmo_global_stats()
    week_start, week_end = _xianmo_week_bounds_unix()
    return {
        "success": True,
        "weekNumber": int(week_start // 604800) + 1,
        "weekStartTime": int(week_start),
        "startTime": int(week_start),
        "endTime": int(week_end),
        "xian": stats["xian"],
        "mo": stats["mo"],
    }


_XIANMO_STATS_CACHE: dict = {"at": 0.0, "data": {}}


def _xianmo_global_stats(force: bool = False) -> dict:
    """扫全部账号存档里的 meta.xianmo，汇总两阵营的人数和积分（60s 缓存）。

    逐个走 STORE.get() 会把这些离线玩家的整份存档都读进内存（几十 MB），
    这里只读文件里的一个字段，读完就丢。
    """
    now_ts = time.time()
    cached = _XIANMO_STATS_CACHE
    if not force and cached["data"] and now_ts - float(cached["at"] or 0) < 60:
        return cached["data"]
    xian = {"count": 0, "totalScore": 0, "totalScoreAll": 0}
    mo = {"count": 0, "totalScore": 0, "totalScoreAll": 0}
    try:
        files = sorted((STATE_DIR / "accounts").glob("*.json"))
    except OSError:
        files = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            continue
        box = (raw.get("meta") or {}).get("xianmo")
        if not isinstance(box, dict):
            continue
        faction = str(box.get("faction", "") or "")
        if faction not in ("xian", "mo"):
            continue
        target = xian if faction == "xian" else mo
        target["count"] += 1
        target["totalScore"] += max(0, int(box.get("score", 0) or 0))
        target["totalScoreAll"] += max(0, int(box.get("totalScore", 0) or 0))
    data = {"xian": xian, "mo": mo}
    _XIANMO_STATS_CACHE["at"] = now_ts
    _XIANMO_STATS_CACHE["data"] = data
    return data


def _xianmo_week_bounds_unix() -> tuple[int, int]:
    """本周区间（UTC+8 周一 00:00 起的 7 天），返回 unix 秒。"""
    current = now()
    start = (current - timedelta(days=current.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int((start + timedelta(days=7)).timestamp())


def _xianmo_week_bounds() -> tuple[str, str]:
    """本周区间（周一 00:00 ~ 下周一 00:00，原服按 UTC+8 周日 24:00 结算）。"""
    current = now()
    start = current - timedelta(days=current.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    return iso(start), iso(end)


_XIANMO_RANK_CACHE: dict = {"at": 0.0, "week": 0, "data": {}}


def _xianmo_rank_rows(faction: str) -> list[dict]:
    """某一阵营的本周积分榜（原版 /xianmo/ranking?faction=xian|mo 的口径）。

    ★ 老实现直接 `ctx.state.ranking_rows("all")`：那是**等级总榜**，字段是
      user_id/level/nickname，没有 weekly_score，也不分阵营 ——
      所以客户端两个页签拿到同一批人、积分全是 0（玩家截图实证）。
    现在扫 state/accounts/*.json 里的 meta.xianmo：只读一个字段，按周积分排序，
    返回原版抓包里那套字段（user_id / faction / weekly_score / total_score / nickname）。
    """
    faction = faction if faction in ("xian", "mo") else ""
    week = _xianmo_week_key()
    now_ts = time.time()
    cached = _XIANMO_RANK_CACHE
    if (cached["data"] and int(cached["week"]) == week
            and now_ts - float(cached["at"] or 0) < 30
            and faction in cached["data"]):
        return cached["data"][faction]

    buckets: dict[str, list] = {"xian": [], "mo": []}
    try:
        files = sorted((STATE_DIR / "accounts").glob("*.json"))
    except OSError:
        files = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            continue
        meta = raw.get("meta") or {}
        box = meta.get("xianmo")
        if not isinstance(box, dict):
            continue
        row_faction = str(box.get("faction", "") or "")
        if row_faction not in ("xian", "mo"):
            continue
        username = path.stem
        nickname = str(meta.get("nickname")
                       or ((raw.get("save") or {}).get("playerData") or {}).get("name")
                       or username)
        buckets[row_faction].append({
            "user_id": int((STORE.accounts.get(username) or {}).get("userId", 0) or 0),
            "username": username,
            "faction": row_faction,
            "weekly_score": max(0, int(box.get("score", 0) or 0)),
            "total_score": max(0, int(box.get("totalScore", 0) or 0)),
            "nickname": nickname,
        })
    for rows in buckets.values():
        rows.sort(key=lambda r: (r["weekly_score"], r["total_score"]), reverse=True)
        del rows[60:]
    _XIANMO_RANK_CACHE["at"] = now_ts
    _XIANMO_RANK_CACHE["week"] = week
    _XIANMO_RANK_CACHE["data"] = buckets
    return buckets[faction] if faction else (buckets["xian"] + buckets["mo"])


@route("GET", "/xianmo/ranking")
def xianmo_ranking(ctx: Ctx) -> dict:
    # 客户端取榜单同样不带令牌（见 network.gd xianmo_get_ranking），
    # 这条路由走公开路径，ctx.state 可能是 None。
    faction = str(ctx.q.get("faction") or "").strip().lower()
    if faction not in ("xian", "mo"):
        faction = ""
    rows = _xianmo_rank_rows(faction)
    week = _xianmo_week_key()
    my_rank = 0
    if ctx.state is not None and faction:
        my_name = str(ctx.state.nickname())
        for i, row in enumerate(rows):
            if row.get("nickname") == my_name:
                my_rank = i + 1
                break
    return {
        "success": True,
        "weekNumber": week,
        "week_number": week,
        "faction": faction,
        "rankings": rows,
        "myRank": my_rank,
        "score": int(_xianmo(ctx.state).get("score", 0) or 0) if ctx.state is not None else 0,
    }


@route("GET", "/xianmo/week/info")
def xianmo_week_info(ctx: Ctx) -> dict:
    _xianmo_settle_due()
    box = _xianmo(ctx.state) if ctx.state is not None else {}
    start_u, end_u = _xianmo_week_bounds_unix()
    stats = _xianmo_global_stats()
    return {
        "success": True,
        "weekNumber": int(start_u // 604800) + 1,
        "weekStartTime": int(start_u),
        "startTime": int(start_u),
        "endTime": int(end_u),
        "score": int(box.get("score", 0) or 0),
        "xianScore": int(stats["xian"]["totalScore"]),
        "moScore": int(stats["mo"]["totalScore"]),
        # 上周结算摘要（奖励已由服务端入账 + 邮件通知；这里只给界面展示用）。
        # 注意不能返回 winner 键：客户端 _on_xianmo_week_info_loaded 一旦看到 winner
        # 就会调 xianmo_reward.apply_ranking_rewards()，而这个方法在重构时被删了，
        # 调用必报 Invalid call，把后面的周结算广播全打断。
        "lastSettle": box.get("lastSettle") or {},
        "faction": str(box.get("faction", "") or ""),
    }


@route("GET", "/xianmo/broadcasts")
def xianmo_broadcasts(ctx: Ctx) -> dict:
    """全服广播（江湖传闻里的战报流）。

    老实现直接回 `data/xianmo-broadcasts.json`（**原游戏抓包的 15 条**），
    还忽略 lastId，导致客户端每次进主城都把那些老消息当公告重弹一遍。
    现在读 broadcasts 模块的真实存储：
      - lastId<=0 → 返回最新 limit 条（客户端进主城时的历史回放）
      - lastId>0  → 只返回更新的（轮询）
    """
    import broadcasts

    last_id = ctx.qi("lastId", 0)
    limit = ctx.qi("limit", 20)
    rows = broadcasts.since(last_id, limit) if last_id > 0 else broadcasts.latest(limit)
    return {"success": True, "broadcasts": rows}


@route("GET", "/xianmo/shop/items")
def xianmo_shop_items(ctx: Ctx) -> dict:
    rows = RULES.xianmo_shop
    items = rows.get("items") if isinstance(rows, dict) else rows
    return {"success": True, "items": items or []}


@route("POST", "/xianmo/shop/buy")
def xianmo_shop_buy(ctx: Ctx) -> dict:
    """仙魔商店兑换：扣的是**累计积分**（客户端 xianmo_main_panel 用 total_score 判可买）。

    客户端 network.gd 识别成功靠 `data.has("newTotalScore")`，原来这里回的是
    `items=[...]` → 成功信号永远不发，界面一直转圈。改为回 newTotalScore/newWeeklyScore，
    并且**不含 items 字段**（避免被别的分支截胡）。
    """
    state = ctx.state
    box = _xianmo(state)
    op_id = ctx.s("operationId")
    ops = box.setdefault("shopOps", [])
    if op_id and op_id in ops:
        return ok(duplicate=True, newTotalScore=int(box.get("totalScore", 0)),
                  newWeeklyScore=int(box.get("score", 0)), saveRevision=state.revision)

    item_id = ctx.s("itemId") or ctx.s("id")
    items = xianmo_shop_items(ctx)["items"]
    for item in items:
        if str(item.get("id", item.get("itemId", ""))) != item_id:
            continue
        price = int(item.get("cost", item.get("price", 0)) or 0)
        name = str(item.get("name", item.get("itemName", "仙魔商品")))
        if item.get("type") != "chest" and not _client_has_item(name):
            # 客户端物品表里没有这个名字（例如老的「硬皮lv1技能书」），
            # 发了也进不了背包 → 宁可拒绝，也别白扣积分
            return fail("该商品暂不可兑换（道具数据缺失），请联系管理员")
        total_score = int(box.get("totalScore", 0))
        if total_score < price:
            return fail("累计积分不足")
        box["totalScore"] = total_score - price
        if item.get("type") == "chest" and item.get("chest_type"):
            # 宝箱进仙魔宝箱仓库（客户端宝箱面板读的是 unclaimedChests）
            box.setdefault("chests", []).append({"type": str(item["chest_type"]), "at": iso(now())})
        else:
            state.add_item(name, int(item.get("count", 1) or 1), "item")
        if op_id:
            ops.append(op_id)
            box["shopOps"] = ops[-64:]
        state.bump()
        return ok(itemName=name, cost=price, currency="score",
                  newTotalScore=int(box["totalScore"]),
                  newWeeklyScore=int(box.get("score", 0)),
                  unclaimedChests=box.get("chests", []),
                  saveRevision=state.revision)
    return fail("商品不存在")


@route("GET", "/xianmo/titles")
def xianmo_titles(ctx: Ctx) -> dict:
    """玩家**自己拥有**的仙魔称号（原版 /xianmo/titles 口径）。

    老实现返回的是 `data/runtime-config` 里的称号模板表（一直是空数组），
    而把玩家激活过的 id 塞在 `active` 里 —— 客户端 title_manager 读的是
    `data.titles`，于是「称号」页永远是「暂无称号」，周结算也发不出称号。
    """
    _xianmo_settle_due()
    box = _xianmo(ctx.state)
    now_ts = int(time.time())
    active = str(box.get("activeTitle", "") or "")
    rows = []
    for row in _xianmo_title_items(box):
        if not isinstance(row, dict):
            continue
        expire = int(row.get("expire_time_unix", row.get("expire_time", 0)) or 0)
        if expire and expire <= now_ts:
            continue
        out = dict(row)
        out["expire_time_unix"] = expire
        out["expire_time"] = expire
        out["is_active"] = 1 if str(out.get("title_id", "")) == active else 0
        rows.append(out)
    # 激活的那条已经过期 → 顺手卸下，免得人物面板一直挂个过期称号
    if active and not any(str(r.get("title_id", "")) == active for r in rows):
        box["activeTitle"] = ""
        active = ""
    return {"success": True, "titles": rows, "active": active}


@route("POST", "/xianmo/titles/activate")
def xianmo_title_activate(ctx: Ctx) -> dict:
    box = _xianmo(ctx.state)
    title_id = str(ctx.s("titleId") or ctx.s("title_id") or "").strip()
    now_ts = int(time.time())
    target = None
    for row in _xianmo_title_items(box):
        if isinstance(row, dict) and str(row.get("title_id", "")) == title_id:
            target = row
            break
    if target is None:
        return fail("你没有这个称号")
    expire = int(target.get("expire_time_unix", target.get("expire_time", 0)) or 0)
    if expire and expire <= now_ts:
        return fail("称号已过期")
    box["activeTitle"] = title_id
    ctx.state.bump()
    return ok(titleId=title_id, title_id=title_id, titles=_xianmo_title_items(box))


@route("POST", "/xianmo/titles/deactivate")
def xianmo_title_deactivate(ctx: Ctx) -> dict:
    box = _xianmo(ctx.state)
    box["activeTitle"] = ""
    ctx.state.bump()
    return ok(titleId="", title_id="", titles=_xianmo_title_items(box))


@route("GET", "/xianmo/mirror")
def xianmo_mirror(ctx: Ctx) -> dict:
    return {"success": True, "mirror": None, "hasMirror": False}


@route("POST", "/xianmo/chest/deposit")
def xianmo_chest_deposit(ctx: Ctx) -> dict:
    """背包里的宝箱 → 仙魔宝箱栏。

    客户端 inventory_manager 用掉「黄金匣」时走这里，然后**等回包里的 saveData**
    才把背包那一格抹掉（`if deposit_data.has("saveData") ... else: item.count -= 1`）。
    老实现既不动 state.items、也不回 saveData：
      * 服务端背包里那口箱子一直在 → 玩家下次 /save/download 又看到它，
        表现就是「黄金匣开了又有，一直能开」；
      * 客户端只能自己本地扣，一旦这次扣减被旧快照上传覆盖，箱子又回来了。
    所以这里必须由服务端扣掉背包里那口箱子，并把权威存档回给客户端。
    """
    state = ctx.state
    from handlers_growth import _authority_save
    box = _xianmo(state)
    op_id = ctx.s("operationId")
    ops = box.setdefault("depositOps", [])
    if op_id and op_id in ops:
        return ok(duplicate=True, unclaimedChests=box.get("chests", []),
                  chestCounts=_xianmo_chest_counts(state), saveRevision=state.revision,
                  saveData=_authority_save(state))
    chest_type = ctx.s("chestType") or "bronze"
    label = _XIANMO_CHEST_LABEL.get(chest_type)
    if not label:
        return fail("无效的宝箱类型")
    item_name = "%s宝箱" % label
    if not state.take_item(item_name, 1):
        # 服务端背包里没有这口箱子：要么客户端本地多出来的旧数据，要么已经存过。
        # 宁可让玩家重登同步，也不能白送一口（不然可以空手一直存）。
        return fail("背包里没有【%s】，请重新登录同步后再试" % item_name)
    box.setdefault("chests", []).append({"type": chest_type, "count": 1, "at": iso(now())})
    if op_id:
        ops.append(op_id)
        box["depositOps"] = ops[-64:]
    state.bump()
    return ok(unclaimedChests=box["chests"], chestCounts=_xianmo_chest_counts(state),
              newTotalScore=int(box.get("totalScore", 0)), saveRevision=state.revision,
              itemName=item_name, saveData=_authority_save(state))


@route("POST", "/xianmo/chest/open")
def xianmo_chest_open(ctx: Ctx) -> dict:
    """开宝箱。

    客户端 xianmo_main_panel._on_chest_open_success 读的是 `data.equipment`
    （network.gd 里 `data.has("equipment") and current_type == "xianmo_chest_open"`），
    原来这里只回 items/chest → 前端一律弹「开启宝箱失败：数据错误」。
    现在服务端开箱：扣宝箱、生成宠装写进 inventoryData.pet_equipment_items、
    回 equipment + saveData（客户端靠 saveData 把它并进背包）。
    """
    state = ctx.state
    _xianmo(state)
    chest_type = ctx.s("chestType") or "bronze"
    if chest_type not in ("bronze", "silver", "gold"):
        return fail("无效的宝箱类型")
    if not _xianmo_take_chest(state, chest_type):
        return fail("没有该类型的宝箱")
    equipment = _xianmo_make_equipment(chest_type)
    state.save.setdefault("inventoryData", {}).setdefault("pet_equipment_items", []).append(equipment)
    state.bump()
    return ok(
        chestType=chest_type,
        equipment=equipment,
        chestCounts=_xianmo_chest_counts(state),
        unclaimedChests=state.meta.get("xianmo", {}).get("chests", []),
        saveData=state.save,
        saveRevision=state.revision,
    )


@route("POST", "/xianmo/adventure")
def xianmo_adventure(ctx: Ctx) -> dict:
    """仙魔探险：按原版事件表（xianmo-events.json，35 个事件带 weight）真实结算。

    原版口径（scripts/xianmo/xianmo_event_data.gd + xianmo_main_panel.gd）：
      - 每次探险消耗 points（客户端固定传 1）；
      - 事件按 weight 加权抽取，`score` / `score_win` / `score_lose` 决定积分；
      - action_points_bonus / action_points_cost / action_points_loss 直接改行动力；
      - 金币、宝箱、残片、修为、经验、灵兽按事件字段发放（服务端权威，已落地的不再让客户端重复加）。
    """
    state = ctx.state
    box = _xianmo(state)
    op_id = ctx.s("operationId") or ctx.s("opId")
    ops = box.setdefault("adventureOps", {})
    if op_id and op_id in ops:
        return ok(**ops[op_id], duplicate=True)

    cost = ctx.i("points", 1) or 1
    cost = max(1, cost)
    if int(box.get("actionPoints", 0)) < cost:
        return fail("行动力不足")

    box["actionPoints"] = int(box.get("actionPoints", 0)) - cost

    p = state.player
    event_id, cfg = _xianmo_pick_event()
    event_type = str(cfg.get("type", "empty"))
    rewards: list[dict] = []
    reward_extra: dict = {}
    captured: bool | None = None
    battle_won: bool | None = None
    battle_opponent: str = ""
    pet_growth: float | None = None

    # ---- 积分与战斗
    gain = int(cfg.get("score", 0) or 0)
    if event_type == "battle":
        won, battle_log, rounds, opponent = _xianmo_resolve_battle(ctx, cfg)
        battle_won, battle_opponent = won, opponent
        gain = int(cfg.get("score_win", 4) if won else cfg.get("score_lose", 1))
        if not won and cfg.get("no_score_on_lose"):
            gain = 0
        reward_extra.update(
            won=won,
            battleLog=battle_log,
            rounds=rounds,
            opponent=opponent,
            is_mirror=bool(cfg.get("mirror_battle")),
            enemyFaction="mo" if str(box.get("faction", "xian")) == "xian" else "xian",
            message=("战斗胜利！你击败了%s" % opponent) if won
            else ("战斗失败，你被%s击败..." % opponent),
        )
    elif cfg.get("score_loss"):
        gain = -abs(gain)

    # ---- 奖励
    # 注意 `sent_via_mail` 只在**真的走 send_mail** 的那两条分支里标。
    # 这个字段被客户端 event_popup 直接拼到奖励文案后面（"… [已发送到邮件]"），
    # 以前无脑给每个奖励都标 true，于是玩家看到「行动力: -1 [已发送到邮件]」
    # 「藏宝图残片: +1 [已发送到邮件]」，其实东西都在存档里、邮件里啥也没有。
    gold_range = cfg.get("gold_range")
    if isinstance(gold_range, list) and len(gold_range) == 2:
        lo, hi = int(float(gold_range[0])), int(float(gold_range[1]))
        amount = random.randint(min(lo, hi), max(lo, hi))
        state.add_gold(amount)
        rewards.append({"type": "gold", "amount": amount})

    chest_type = cfg.get("chest_type")
    if chest_type:
        if str(chest_type) == "random":
            chest_type = random.choice(["bronze", "bronze", "silver", "gold"])
        box.setdefault("chests", []).append({"type": str(chest_type), "count": 1, "at": iso(now())})
        rewards.append({"type": "chest", "chestType": str(chest_type)})

    item_reward = cfg.get("item_reward")
    if item_reward:
        row = item_reward
        if isinstance(row, str):
            try:
                row = json.loads(row)
            except Exception:
                row = {}
        if isinstance(row, dict) and row.get("name"):
            name = str(row["name"])
            count = int(row.get("count", 1) or 1)
            if _client_has_item(name):
                state.send_mail(
                    "仙魔战场 · %s" % str(cfg.get("name", "探险")),
                    "你在仙魔战场获得【%s】×%d，请查收。" % (name, count),
                    [{"name": name, "count": count}],
                )
                rewards.append({"type": "item", "itemName": name, "count": count,
                                "sent_via_mail": True})

    for raw in (cfg.get("items") or []):
        name = str(raw).strip().strip('"').strip("'")
        if not name:
            continue
        if not _client_has_item(name):
            # 客户端物品表里没有这个名字（老数据里的占位符，如 cultivation_pill），
            # 发了也进不了背包，还刷一条假奖励，直接跳过。
            continue
        state.add_item(name, 1, "material")
        rewards.append({"type": "item", "itemName": name, "count": 1})

    if cfg.get("random_item"):
        name = _xianmo_pick_item(_XIANMO_RANDOM_ITEM_POOL)
        if _client_has_item(name):
            state.add_item(name, 1, "material")
        else:
            state.send_mail("仙魔战场 · 探险收获",
                            "你在仙魔战场获得【%s】×1，请查收。" % name,
                            [{"name": name, "count": 1}])
        rewards.append({"type": "item", "itemName": name, "count": 1})

    if cfg.get("rare_item"):
        name = _xianmo_pick_item(_XIANMO_RARE_ITEM_POOL)
        state.send_mail(
            "仙魔战场 · 天降祥瑞",
            "你在仙魔战场获得稀有道具【%s】×1，请查收。" % name,
            [{"name": name, "count": 1}],
        )
        rewards.append({"type": "item", "itemName": name, "count": 1, "sent_via_mail": True})

    # 藏宝图残片：直接进「藏宝图」系统（meta.xianmo.fragments），
    # 集满 10 张在藏宝图页兑换 1 张藏宝图（/xianmo/treasure/exchange）。
    # 这里不是发邮件，所以不能标 sent_via_mail；同时把合成提示带回去。
    if cfg.get("fragment") or cfg.get("fragment_count"):
        frag = int(cfg.get("fragment_count", 1) or 1)
        before = int(box.get("fragments", 0) or 0)
        box["fragments"] = before + frag
        rewards.append({"type": "fragment", "count": frag,
                        "total": int(box["fragments"]),
                        "ready": int(box["fragments"]) >= 10})

    if cfg.get("cultivation_bonus"):
        amount = int(cfg["cultivation_bonus"])
        p["cultivation"] = int(p.get("cultivation", 0)) + amount
        rewards.append({"type": "cultivation", "amount": amount})

    if cfg.get("exp_bonus"):
        base = int(cfg["exp_bonus"])
        amount = int(round(base * state.exp_multiplier()))
        state.grant_player_exp(amount)
        rewards.append({"type": "exp", "amount": amount})

    bonus_points = int(cfg.get("action_points_bonus", 0) or 0)
    if bonus_points:
        box["actionPoints"] = min(_xianmo_cap(state), int(box.get("actionPoints", 0)) + bonus_points)
        rewards.append({"type": "action_points", "amount": bonus_points})

    # 陷阱机关 / 幻阵困局：直接在行动力上扣（和「进入战场」那一扣是同一套账），
    # 不走邮件。客户端只会弹「⚡ 行动力: -1」并刷新行动力条。
    extra_cost = int(cfg.get("action_points_cost", 0) or 0) + int(cfg.get("action_points_loss", 0) or 0)
    if extra_cost:
        box["actionPoints"] = max(0, int(box.get("actionPoints", 0)) - extra_cost)
        rewards.append({"type": "action_points_loss", "amount": extra_cost,
                        "actionPoints": int(box.get("actionPoints", 0))})
        if event_type == "trap" and not reward_extra.get("message"):
            reward_extra["message"] = "你误入了【%s】，损失%d点行动力" % (
                str(cfg.get("name", "陷阱机关")), extra_cost)

    if event_type == "pet":
        rate = float(cfg.get("capture_rate", 0.5) or 0.5)
        captured = True if cfg.get("no_capture_roll") else (random.random() <= rate)
        growth_range = cfg.get("growth_range") or ["5.0", "5.5"]
        growth = random.uniform(float(growth_range[0]), float(growth_range[1]))
        if captured:
            pet_growth = round(growth, 2)
            gift = _xianmo_pet_gift(event_id, cfg, float(pet_growth))
            # 不再写 state.pet_bag：那批 pet_id=xianmo_pet 的宠物没有模板，
            # 客户端宠物列表根本不显示，玩家看到的就是「捕获了但没收到」。
            state.send_mail(
                "仙魔战场 · 灵兽收获",
                "你在仙魔界捕获了【%s】，已封装成【%s】寄给你，\n打开礼包可获得一只成长约 %.2f 的灵兽。" % (
                    str(cfg.get("name", "灵兽")), gift, pet_growth),
                [{"name": gift, "count": 1, "growth": pet_growth}],
            )
            rewards.append({"type": "pet", "captured": True, "growth": pet_growth,
                            "giftName": gift})
            rewards.append({"type": "item", "itemName": gift, "count": 1,
                            "growth": pet_growth, "sent_via_mail": True})
        else:
            rewards.append({"type": "pet", "captured": False})

    # ---- 积分落库（周积分本周清零，累计积分永久累加）
    weekly = max(0, int(box.get("score", 0)) + gain)
    total = max(0, int(box.get("totalScore", 0)) + gain)
    box["score"] = weekly
    box["totalScore"] = total

    reward = dict(reward_extra)
    reward["rewards"] = rewards
    if not reward.get("message"):
        reward["message"] = _xianmo_event_message(event_id, cfg, gain, captured)
    if gain > 0:
        # 客户端看到 factionScoreChange 会重新拉阵营统计
        reward["factionScoreChange"] = gain

    result = dict(
        eventId=event_id,
        eventType=event_type,
        eventName=str(cfg.get("name", "")),
        scoreGain=gain,
        actionPoints=int(box.get("actionPoints", 0)),
        maxActionPoints=_xianmo_cap(state),
        newWeeklyScore=weekly,
        newTotalScore=total,
        fragments=int(box.get("fragments", 0)),
        cost=cost,
        reward=reward,
        saveRevision=state.revision,
    )
    # 仙魔界探索结果进「江湖传闻」（原游戏同一条流），只播有产出/打赢的
    _xianmo_broadcast(state, event_id, cfg, rewards,
                      won=battle_won, opponent=battle_opponent,
                      captured=captured, growth=pet_growth)
    state.bump()
    if op_id:
        ops[op_id] = {k: v for k, v in result.items() if k != "saveRevision"}
        if len(ops) > 64:
            for stale in list(ops.keys())[:-64]:
                ops.pop(stale, None)
    return ok(**result)


@route("POST", "/xianmo/battle/result")
def xianmo_battle_result(ctx: Ctx) -> dict:
    """战斗结果上报：积分按**事件表**算，不采信客户端传的 scoreGain（防刷分）。"""
    state = ctx.state
    box = _xianmo(state)
    op_id = ctx.s("operationId")
    ops = box.setdefault("battleOps", {})
    if op_id and op_id in ops:
        return ok(**ops[op_id], duplicate=True)

    event_id = ctx.s("eventId") or "faction_confrontation"
    victory = bool(ctx.body.get("victory", False))
    cfg = _xianmo_events().get(event_id) or {}
    if victory:
        gain = int(cfg.get("score_win", 3) or 0)
    else:
        gain = 0 if cfg.get("no_score_on_lose") else int(cfg.get("score_lose", 1) or 0)
    gain = max(-10, min(10, gain))

    weekly = max(0, int(box.get("score", 0)) + gain)
    total = max(0, int(box.get("totalScore", 0)) + gain)
    box["score"] = weekly
    box["totalScore"] = total
    state.bump()

    result = dict(
        eventId=event_id,
        eventType=str(cfg.get("type", "battle")),
        scoreGain=gain,
        newWeeklyScore=weekly,
        newTotalScore=total,
        victory=victory,
        actionPoints=int(box.get("actionPoints", 0)),
        saveRevision=state.revision,
    )
    if op_id:
        ops[op_id] = {k: v for k, v in result.items() if k != "saveRevision"}
        if len(ops) > 64:
            for stale in list(ops.keys())[:-64]:
                ops.pop(stale, None)
    return ok(**result)


@route("POST", "/xianmo/treasure/exchange")
def xianmo_treasure_exchange(ctx: Ctx) -> dict:
    """藏宝图残片 10 → 1 张藏宝图 → 当场开奖。

    客户端「藏宝图」页（xianmo_main_panel._on_treasure_exchange）签的是：
      * 成功回包里必须有 `goldReward`（弹「获得 N 亿金币」）、
        `remainingFragments`（刷新「残片 x/10」）、`saveData`（金币才落地）；
      * 概率写在页面上：60% 得 1 亿 / 30% 得 5 亿 / 10% 得 10 亿。
    老实现只回 treasureMaps/fragments：兑换成功也看不到金币、残片数也不动，
    玩家会以为「碎片捡了没用」。
    """
    state = ctx.state
    from handlers_growth import _authority_save
    box = _xianmo(state)
    op_id = ctx.s("operationId")
    ops = box.get("treasureExchangeOps")
    if not isinstance(ops, list):
        ops = []
        box["treasureExchangeOps"] = ops
    if op_id and op_id in ops:
        last = box.get("lastTreasureExchange") or {}
        return ok(duplicate=True, goldReward=int(last.get("goldReward", 0)),
                  remainingFragments=int(box.get("fragments", 0)),
                  saveData=_authority_save(state), saveRevision=state.revision)
    frags = int(box.get("fragments", 0) or 0)
    if frags < 10:
        return fail("藏宝图残片不足（%d/10）" % frags)
    box["fragments"] = frags - 10
    box.setdefault("treasureMaps", []).append({"id": len(box["treasureMaps"]) + 1, "at": iso(now())})

    roll = random.random()
    gold = 100_000_000 if roll < 0.60 else (500_000_000 if roll < 0.90 else 1_000_000_000)
    state.add_gold(gold)
    state.bump()
    if op_id:
        ops.append(op_id)
        box["treasureExchangeOps"] = ops[-32:]
    box["lastTreasureExchange"] = {"at": iso(now()), "goldReward": gold}
    return ok(goldReward=gold, gold=gold,
              remainingFragments=int(box["fragments"]),
              fragments=int(box["fragments"]),
              treasureMaps=box["treasureMaps"],
              saveData=_authority_save(state), saveRevision=state.revision,
              newGold=int(state.player.get("gold", 0)))


@route("POST", "/xianmo/treasure/use")
def xianmo_treasure_use(ctx: Ctx) -> dict:
    state = ctx.state
    box = _xianmo(state)
    maps = box.setdefault("treasureMaps", [])
    if not maps:
        return fail("没有藏宝图")
    maps.pop(0)
    rewards = RULES.v("treasure", "rewards", []) or []
    row = RULES.roll(rewards)
    items = []
    if row:
        state.add_item(str(row.get("name", "")), 1, "item")
        items.append({"name": str(row.get("name", "")), "count": 1})
    state.bump()
    return ok(items=items, treasureMaps=maps)


@route("POST", "/xianmo/pet/exchange")
def xianmo_pet_exchange(ctx: Ctx) -> dict:
    state = ctx.state
    box = _xianmo(state)
    cost = 100
    if int(box.get("score", 0)) < cost:
        return fail("积分不足")
    box["score"] = int(box.get("score", 0)) - cost
    state.pet_bag.append({"id": "xianmo_pet_%d" % int(random.random() * 1e9), "pet_id": "xianmo_pet",
                          "name": "仙魔灵兽", "level": 1, "generation": 0})
    state.bump()
    return ok(pet={"name": "仙魔灵兽"})


# ================================================================ 七日签到
# 修复前的问题：info 只回名字表（缺 active/can_claim/finished/天状态），客户端
# 永远显示「活动未开启」；claim 扁平返回（客户端读不到 data），发了等于白发；
# 且无顺序/每日限制，可 day=7 抢领、day=0/-3 乱领，奖励一律是「签到礼包N」。
# 现在的规则：
#   * 常驻活动，严格按天顺序领：只能领「最小的未领天」，客户端传的 day 忽略
#   * 每自然日限领一次（服务端日期）
#   * 奖励按真实表发放（金币/元宝/道具），数量与界面宣传一致
#   * 老档乱数据（抢领过的 day=7、day=0/-3）读档时自动清洗
SIGNIN7_REWARDS = [
    {"day": 1, "name": "六阶可进化宠锦囊", "type": "item", "item_id": "六阶可进化宠锦囊", "amount": 1},
    {"day": 2, "name": "金币1亿", "type": "gold", "item_id": "金币", "amount": 100000000},
    {"day": 3, "name": "五行属性宠物蛋", "type": "item", "item_id": "五行属性宠物蛋", "amount": 1},
    {"day": 4, "name": "开悟丹", "type": "item", "item_id": "开悟丹", "amount": 1},
    {"day": 5, "name": "法宝碎片*20", "type": "item", "item_id": "法宝碎片", "amount": 20},
    {"day": 6, "name": "元宝100", "type": "yuanbao", "item_id": "元宝", "amount": 100},
    {"day": 7, "name": "十阶可进化宠锦囊", "type": "item", "item_id": "十阶可进化宠锦囊", "amount": 1},
]


def _signin7_clean(box: dict) -> list:
    days: list = []
    for d in box.get("days") or []:
        try:
            iv = int(d)
        except Exception:
            continue
        if 1 <= iv <= 7 and iv not in days:
            days.append(iv)
    days.sort()
    if days != list(box.get("days") or []):
        box["days"] = days
    return days


def _signin7_state(state) -> dict:
    box = state.meta.setdefault("signin7", {"days": [], "lastDate": ""})
    signed = _signin7_clean(box)
    next_day = next((r["day"] for r in SIGNIN7_REWARDS if r["day"] not in signed), None)
    claimed_today = str(box.get("lastDate") or "") == today()
    can_claim = next_day is not None and not claimed_today
    rows = []
    for r in SIGNIN7_REWARDS:
        row = dict(r)
        if r["day"] in signed:
            row["status"] = "claimed"
        elif r["day"] == next_day:
            row["status"] = "claimable" if can_claim else "waiting"
        else:
            row["status"] = "waiting"
        rows.append(row)
    return {"box": box, "signed": signed, "next_day": next_day,
            "can_claim": can_claim, "rows": rows, "finished": next_day is None}


def _signin7_payload(st: dict) -> dict:
    # 字段与 ui/city/signin7_ui.gd::_apply_info / city_ui._refresh_signin7_entry 对齐
    return {
        "title": "七日签到领好礼",
        "active": True,
        "finished": st["finished"],
        "can_claim": st["can_claim"],
        "start_text": "",
        "end_text": "",
        "days": st["rows"],
        "signedDays": st["signed"],
    }


@route("POST", "/signin7/info")
def signin7_info(ctx: Ctx) -> dict:
    cfg = RULES.festival if isinstance(RULES.festival, dict) else {}
    data = _signin7_payload(_signin7_state(ctx.state))
    data["config"] = cfg
    return {"ok": True, "success": True, "data": data}


@route("POST", "/signin7/claim")
def signin7_claim(ctx: Ctx) -> dict:
    state = ctx.state
    st = _signin7_state(state)
    if st["finished"]:
        return fail("本场奖励已全部领取")
    if not st["can_claim"]:
        return fail("今天已经领取过了，明天再来")
    day = st["next_day"]
    reward = next(r for r in SIGNIN7_REWARDS if r["day"] == day)
    granted: list = []
    if reward["type"] == "gold":
        state.add_gold(int(reward["amount"]))
    elif reward["type"] == "yuanbao":
        state.add_yuanbao(int(reward["amount"]))
    else:
        state.add_item(str(reward["item_id"]), int(reward["amount"]), "item")
        granted.append({"name": str(reward["item_id"]), "count": int(reward["amount"])})
    box = st["box"]
    box["days"] = sorted(set(st["signed"]) | {day})
    box["lastDate"] = today()
    state.bump()
    payload = _signin7_payload(_signin7_state(state))
    payload["message"] = "领取成功：%s" % reward["name"]
    payload["newGold"] = state.gold()
    payload["newYuanbao"] = state.yuanbao()
    if granted:
        payload["itemsGranted"] = granted
    payload["saveRevision"] = state.revision
    return {"ok": True, "success": True, "data": payload}


# ================================================================ 贵族

# `scripts/noble_benefits.gd` 的三张表，按贵族等级 0~15 逐档给。
# 之前这里把 benefits 写死成全 0，客户端 `_on_noble_info_loaded_for_cache` 会
# 直接吃 `petDamage = 0` —— 贵族宠物伤害加成永远不生效（经验那一项客户端有
# `EXP_BONUS` 兜底，宠物伤害没有）。
NOBLE_MERGE_BONUS = (0, 2, 4, 6, 8, 9, 10, 16, 23, 30, 37, 43, 50, 57, 63, 70)
NOBLE_MARTIAL_BONUS = (0, 3, 7, 10, 13, 17, 20, 23, 27, 30, 33, 37, 40, 43, 47, 50)
NOBLE_PET_DAMAGE = (0, 0, 0, 0, 0, 0, 3, 5, 7, 10, 13, 16, 19, 22, 25, 30)


def _noble_benefits(level: int) -> dict:
    lv = max(0, min(15, int(level or 0)))
    return {
        "mergeBonus": NOBLE_MERGE_BONUS[lv],
        "martialBonus": NOBLE_MARTIAL_BONUS[lv],
        "petDamage": NOBLE_PET_DAMAGE[lv],
        "expBonus": NOBLE_PET_DAMAGE[lv],
        # 原版 data.benefits 里还带这 6 个（本服相关玩法未开放，恒 false/0）
        "extremeEquip": 0,
        # 兄弟天关次数奖金：按配置的每级次数算，避免 benefits 与实际 dailyClearMax 对不上
        "brotherTowerClearBonus": lv * int(RULES.v("brother_tower", "nobleClearBonusPerLevel", 1) or 0),
        "canClaimExtremeEquip": False,
        "canClaimSpecialPet10": False,
        "canClaimSpecialPet15": False,
        "hasCollectionUnlock": False,
    }


@route("GET", "/noble/info")
def noble_info(ctx: Ctx) -> dict:
    state = ctx.state
    p = state.player
    # 老档 / 后台改档自愈：按累计消耗重新算等级
    state.sync_noble_level()
    level = int(p.get("noble_level", 0) or 0)
    from datetime import datetime as _dt, timezone as _tz
    total = int(p.get("noble_points", 0) or 0)
    # 下一档阈值（满级后固定最后一档，progress 由客户端判「已满级」）
    threshold = state.noble_next_threshold(level)
    _now = _dt.now(_tz.utc)
    # 原版口径：业务字段全在 data 里，外层再带一个 serverTime（ISO8601 UTC）。
    return {
        "success": True,
        "data": {
            "nobleLevel": level,
            "nobleName": str(p.get("noble_name", "平民")),
            "totalConsumed": total,
            "nextThreshold": threshold,
            "progress": "%d/%d" % (total, threshold),
            "maxLevel": len(state.NOBLE_THRESHOLDS),
            "nextLevelThreshold": threshold,
            "rankName": str(p.get("noble_name", "平民")),
            "benefits": _noble_benefits(level),
            "claims": state.meta.get("nobleClaims", {}) or {},
            "extremeEquipStatus": state.meta.get("nobleExtremeEquip") or {
                "mode": "per_level_independent",
                "claimedLevels": [],
                "claimedMap": {},
                "canClaimAtCurrentLevel": False,
            },
            "startTime": int(state.meta.get("nobleStartTime", 0) or 0),
        },
        "serverTime": _now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (_now.microsecond // 1000),
    }


@route("POST", "/noble/claim")
def noble_claim(ctx: Ctx) -> dict:
    state = ctx.state
    claim_type = ctx.s("claimType") or "daily"
    claimed = state.meta.setdefault("nobleClaimed", [])
    key = "%s:%s" % (claim_type, today())
    if key in claimed:
        return fail("今日已领取")
    claimed.append(key)
    state.add_item("贵族礼包", 1, "item")
    state.bump()
    return ok(items=[{"name": "贵族礼包", "count": 1}])


# ================================================================ 养宠赛

@route("GET", "/pet-raise-race/status")
def pet_raise_race_status(ctx: Ctx) -> dict:
    cfg = RULES.m("pet_raise_race")
    return {
        "success": True,
        "featureEnabled": bool(cfg.get("featureEnabled", True)),
        "config": cfg,
        "inSubmitWindow": in_window(int(cfg.get("submitStartHour", 8)), int(cfg.get("submitStartMinute", 0)),
                                    int(cfg.get("submitEndHour", 22)), int(cfg.get("submitEndMinute", 15))),
        "ranking": [],
        "mySubmission": ctx.state.meta.get("petRaiseRace", {}).get("submit"),
    }


@route("POST", "/pet-raise-race/submit")
def pet_raise_race_submit(ctx: Ctx) -> dict:
    state = ctx.state
    pet_id = ctx.s("petId")
    pet_data = None
    for pet in state.pets:
        if str(pet.get("id", "")) == pet_id:
            pet_data = pet
            break
    if pet_data is None:
        return fail("灵兽不存在")
    # 排行榜要按「成长与当日目标差值」排序，提交时必须把成长值和名字一起存下来，
    # 只存 petId 的话排行接口拿不到可比的数值。
    state.meta["petRaiseRace"] = {"submit": {
        "petId": pet_id,
        "petName": str(pet_data.get("name", "灵宠")),
        "petGrowth": float(pet_data.get("growth", 0) or 0),
        "at": iso(now()),
    }}
    state.bump()
    return ok(submission=state.meta["petRaiseRace"]["submit"])
