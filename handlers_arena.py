# -*- coding: utf-8 -*-
"""竞技场 / 藏宝阁接口。

客户端竞技场是**两阶段**流程（arena_ui.gd _run_match_flow）：
  1. POST /arena/match/start  → battleToken + 双方展示信息（头像/名字/当周积分）；
  2. POST /arena/challenge/start {battleToken} → 双方镜像快照 + 服务端判定的胜负，
     客户端用它组装回放战斗（battleLog 为空时直接快进到结果）；
  3. 战斗播完 POST /arena/challenge/confirm {battleToken} → 结算积分并回榜单名次。

2026-10-28 起：镜像对战结算已实现（combat.resolve 引擎），竞技开启
（runtime-config arena.featureEnabled=true）。
"""
from __future__ import annotations

import random
import uuid

import combat
from core import Ctx, RULES, STORE, fail, in_window, iso, now, ok, route
from handlers_battle import active_pet, combat_options, pet_fighter


def _arena_box(state) -> dict:
    box = state.meta.setdefault("arena", {})
    week = int(now().isocalendar().week)
    if int(box.get("week", 0)) != week:
        box["week"] = week
        box["points"] = 0
        box["wins"] = 0
    return box


def _player_snapshot(state) -> dict:
    """攻击/防守方的人物镜像：直接给 playerData（客户端 Player.from_dict 兼容）。"""
    try:
        state.sync_final_stats()
    except Exception:
        pass
    p = dict(state.player)
    p.setdefault("hp", p.get("final_max_hp", p.get("max_hp", 1000)))
    return p


def _pet_snapshot(state) -> dict:
    pet = None
    try:
        pets = state.pets
        active_id = str(state.save.get("petsData", {}).get("active_pet_id", ""))
        for cand in pets:
            if str(cand.get("id", "")) == active_id:
                pet = cand
                break
        pet = pet or (pets[0] if pets else None)
    except Exception:
        pet = None
    return dict(pet) if pet else {}


def _snapshot_of(state) -> dict:
    return {"player": _player_snapshot(state), "pet": _pet_snapshot(state)}


def _fighter_from_player_snap(p: dict) -> combat.Fighter:
    return combat.Fighter(
        name=str(p.get("name", "对手")),
        kind="player", side="B", index=0,
        hp=float(p.get("final_max_hp", p.get("max_hp", 1000)) or 1000),
        max_hp=float(p.get("final_max_hp", p.get("max_hp", 1000)) or 1000),
        attack=float(p.get("final_attack", p.get("attack_base", 100)) or 100),
        defense=float(p.get("final_defense", p.get("defense_base", 100)) or 100),
        agility=float(p.get("final_agility", p.get("agility_base", 100)) or 100),
        level=int(p.get("level", 1)),
        element=str(p.get("element", "")),
    )


def _pick_opponent(ctx: Ctx, box: dict) -> dict | None:
    """按当周积分带宽挑对手（matchScoreBand），没有近的就随机；排除自己。"""
    me = str(ctx.user.get("username", ""))
    my_pts = int(box.get("points", 0))
    band = max(0, int(RULES.v("arena", "matchScoreBand", 30) or 30))
    near: list[dict] = []
    far: list[dict] = []
    for username in list(STORE.accounts.keys()):
        if str(username) == me:
            continue
        try:
            st = STORE.get(username)
        except Exception:
            continue
        if st is None or not getattr(st, "player", None):
            continue
        ob = st.meta.get("arena") or {}
        if int(ob.get("week", 0)) != int(box.get("week", 0)):
            continue
        row = {
            "username": str(username),
            "nickname": str(st.nickname() or username),
            "characterId": int(st.player.get("player_character_id", 0) or 0),
            "weeklyArenaPoints": int(ob.get("points", 0) or 0),
        }
        (near if abs(int(ob.get("points", 0) or 0) - my_pts) <= band else far).append(row)
    pool = near or far
    return random.choice(pool) if pool else None


def _points_rank(ctx: Ctx) -> int:
    """按当周竞技点算我的名次。"""
    me = str(ctx.user.get("username", ""))
    my_pts = int(_arena_box(ctx.state).get("points", 0))
    higher = 0
    for username in STORE.accounts:
        if str(username) == me:
            continue
        try:
            st = STORE.get(username)
        except Exception:
            continue
        ob = st.meta.get("arena") or {}
        if int(ob.get("week", 0)) == int(now().isocalendar().week) and int(ob.get("points", 0) or 0) > my_pts:
            higher += 1
    return higher + 1


def _match_info(row: dict | None, fallback_state, prefix: str) -> dict:
    if row:
        return {
            "characterId": int(row.get("characterId", 0) or 0),
            "playerName": str(row.get("nickname", "") or prefix),
            "weeklyArenaPoints": int(row.get("weeklyArenaPoints", 0) or 0),
        }
    return {
        "characterId": int(fallback_state.player.get("player_character_id", 0) or 0),
        "playerName": str(fallback_state.nickname() or prefix),
        "weeklyArenaPoints": 0,
    }


@route("POST", "/arena/match/start")
def arena_match_start(ctx: Ctx) -> dict:
    cfg = RULES.m("arena")
    if not bool(cfg.get("featureEnabled", False)):
        return fail("竞技场暂未开放")
    if not in_window(int(cfg.get("challengeStartHour", 8)), int(cfg.get("challengeStartMinute", 30)),
                     int(cfg.get("challengeEndHour", 23)), int(cfg.get("challengeEndMinute", 30))):
        return fail("当前不在竞技场开放时间")
    cap = int(cfg.get("dailyMaxAttempts", 10))
    if int(ctx.state.daily("arena_attempts")) >= cap:
        return fail("今日竞技次数已用完")

    box = _arena_box(ctx.state)
    opponent = _pick_opponent(ctx, box)
    token = uuid.uuid4().hex
    box["pending"] = {
        "token": token,
        "opponent": opponent,
        "at": iso(now()),
    }
    ctx.state.bump()
    self_info = _match_info(None, ctx.state, "我方")
    self_info["weeklyArenaPoints"] = int(box.get("points", 0))
    return ok(
        battleToken=token,
        countdownSeconds=3,
        opponentName=str(opponent["nickname"]) if opponent else "幻影修士",
        self=self_info,
        opponent=_match_info(opponent, ctx.state, "对手"),
    )


@route("POST", "/arena/challenge/start")
def arena_challenge_start(ctx: Ctx) -> dict:
    cfg = RULES.m("arena")
    if not bool(cfg.get("featureEnabled", False)):
        return fail("竞技场暂未开放")
    token = ctx.s("battleToken") or ctx.s("battle_token")
    box = _arena_box(ctx.state)
    pending = box.get("pending") or {}
    if not token or str(pending.get("token", "")) != token:
        return fail("匹配已失效，请重新匹配")
    if ctx.state.daily("arena_attempts") >= int(cfg.get("dailyMaxAttempts", 10)):
        return fail("今日竞技次数已用完")

    opp_row = pending.get("opponent")
    opp_state = None
    if isinstance(opp_row, dict) and opp_row.get("username"):
        try:
            opp_state = STORE.get(str(opp_row["username"]))
        except Exception:
            opp_state = None
    if opp_state is None or not getattr(opp_state, "player", None):
        # 没有可用对手：生成与自己同档的幻影对手（缩放 0.85–1.15）
        def scaled(state, factor: float) -> dict:
            snap = _snapshot_of(state)
            for unit in ("player", "pet"):
                d = snap.get(unit)
                if not isinstance(d, dict):
                    continue
                for key in ("final_max_hp", "max_hp", "final_attack", "attack",
                            "final_defense", "defense", "final_agility", "agility"):
                    if key in d:
                        try:
                            d[key] = round(float(d[key]) * factor, 2)
                        except (TypeError, ValueError):
                            pass
                if unit == "player":
                    d["name"] = "幻影修士"
                else:
                    d["name"] = "幻影灵兽"
            return snap
        defender_snap = scaled(ctx.state, random.uniform(0.85, 1.15))
        opponent_name = "幻影修士"
    else:
        defender_snap = _snapshot_of(opp_state)
        opponent_name = str((opp_row or {}).get("nickname") or opp_state.nickname() or "对手")

    attacker_snap = _snapshot_of(ctx.state)

    # 服务端判定胜负：combat.resolve（攻击方带完整加成，防守方取镜像白值）
    atk_player = _fighter_from_player_snap(attacker_snap["player"])
    atk_player.side = "A"
    atk_pet = None
    if attacker_snap.get("pet"):
        atk_pet = pet_fighter(attacker_snap["pet"])
        atk_pet.side = "A"
        atk_pet.index = 1
    def_player = _fighter_from_player_snap(defender_snap["player"])
    defenders = [def_player]
    if defender_snap.get("pet"):
        def_pet = pet_fighter(defender_snap["pet"])
        def_pet.side = "B"
        def_pet.index = 1
        defenders.append(def_pet)
    result = combat.resolve(atk_player, atk_pet, defenders,
                            int(cfg.get("battleMaxRounds", 20)),
                            **combat_options(ctx.state, attacker_snap.get("pet") or None))
    win = bool(result.get("victory", False))

    pending["result"] = {"victory": win, "at": iso(now())}
    box["pending"] = pending
    ctx.state.bump()
    return ok(
        battleToken=token,
        victory=win,
        battleMaxRounds=int(cfg.get("battleMaxRounds", 20)),
        opponentName=opponent_name,
        attackerSnapshot=attacker_snap,
        defenderSnapshot=defender_snap,
        battleLog=[],
    )


@route("POST", "/arena/challenge/confirm")
def arena_challenge_confirm(ctx: Ctx) -> dict:
    cfg = RULES.m("arena")
    token = ctx.s("battleToken") or ctx.s("battle_token")
    box = _arena_box(ctx.state)
    if not token:
        return fail("缺少 battleToken")

    settled = box.setdefault("settled", {})
    memo = settled.get(token)
    cap = int(cfg.get("dailyMaxAttempts", 10))
    if isinstance(memo, dict):
        # 幂等重放：回同一次结算结果
        return ok(**memo)

    pending = box.get("pending") or {}
    if str(pending.get("token", "")) != token:
        return fail("没有待结算的战斗，请重新匹配")

    win = bool((pending.get("result") or {}).get("victory", False))
    delta = int(cfg.get("winScoreDelta", 10)) if win else int(cfg.get("loseScoreDelta", 2))
    box["points"] = max(int(cfg.get("minArenaPoints", 0) or 0), int(box.get("points", 0)) + delta)
    box["wins"] = int(box.get("wins", 0)) + (1 if win else 0)
    box["streak"] = int(box.get("streak", 0)) + 1 if win else 0
    used = ctx.state.daily_inc("arena_attempts")
    opponent_name = str((pending.get("opponent") or {}).get("nickname", "幻影修士"))
    box.setdefault("history", []).append({
        "at": iso(now()),
        "opponent": opponent_name,
        "victory": win,
        "scoreDelta": delta,
    })
    # 竞技场战报进「江湖传闻」（原游戏 arena 类型，只播打赢的，避免刷屏）
    if win:
        import broadcasts as _broadcasts
        attacker = ctx.state.nickname()
        _broadcasts.append(
            "arena", attacker,
            "竞技场战报！玩家【%s】技高一筹，不费吹灰之力，将玩家【%s】打得满地找牙！"
            % (attacker, opponent_name),
            {"victory": True, "attackerName": attacker,
             "defenderName": opponent_name, "arena_event": "battle"},
        )
    del box["history"][:-30]
    settled[token] = memo = {
        "victory": win,
        "scoreDelta": delta,
        "weeklyArenaPoints": int(box["points"]),
        "balanceArenaPoints": int(box["points"]),
        "spendableArenaPoints": int(box["points"]),
        "winStreak": int(box["streak"]),
        "remainingAttempts": max(0, cap - used),
        "myRank": _points_rank(ctx),
    }
    if len(settled) > 40:
        for key in sorted(settled, key=lambda k: str(settled[k].get("at", "")))[:-40]:
            settled.pop(key, None)
    box.pop("pending", None)
    ctx.state.bump()
    return ok(**memo)


@route("GET", "/arena/challenge/history")
def arena_challenge_history(ctx: Ctx) -> dict:
    limit = max(1, min(ctx.qi("limit", 20), 30))
    box = _arena_box(ctx.state)
    records = box.setdefault("history", [])
    return ok(records=list(reversed(records[-limit:])))


@route("GET", "/arena/ranking")
def arena_ranking(ctx: Ctx) -> dict:
    limit = max(1, min(ctx.qi("limit", 100), 100))
    box = _arena_box(ctx.state)
    me = str(ctx.user.get("username", ""))
    rows: list[dict] = []
    week = int(now().isocalendar().week)
    for row in STORE.rankings("players"):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        username = str(item.get("username", ""))
        pts = 0
        if username:
            try:
                ob = STORE.get(username).meta.get("arena") or {}
                if int(ob.get("week", 0)) == week:
                    pts = int(ob.get("points", 0) or 0)
            except Exception:
                pts = 0
        item["arenaPoints"] = pts
        item["isSelf"] = username == me
        rows.append(item)
    mine = {
        "user_id": int(ctx.user.get("userId", 0) or 0),
        "username": me,
        "nickname": str(ctx.state.nickname()),
        "level": int(ctx.state.player.get("level", 1) or 1),
        "arenaPoints": int(box.get("points", 0)),
        "battle_wins": int(box.get("wins", 0)),
        "isSelf": True,
    }
    if not any(r.get("username") == me for r in rows):
        rows.append(mine)
    rows.sort(key=lambda r: (-int(r.get("arenaPoints", 0)), -int(r.get("level", 1))))
    for idx, row in enumerate(rows, 1):
        row["rank"] = idx
    my_rank = next((r["rank"] for r in rows if r.get("username") == me), 0)
    return ok(ranking=rows[:limit], myRank=my_rank, total=len(rows))


@route("GET", "/arena/mirror")
def arena_mirror(ctx: Ctx) -> dict:
    box = _arena_box(ctx.state)
    return ok(hasMirror=bool(box.get("mirrorUpdatedAt")), mirrorUpdatedAt=box.get("mirrorUpdatedAt"))


@route("POST", "/arena/mirror/update")
def arena_mirror_update(ctx: Ctx) -> dict:
    box = _arena_box(ctx.state)
    box["mirrorUpdatedAt"] = iso(now())
    box["mirror"] = _snapshot_of(ctx.state)
    ctx.state.bump()
    return ok(hasMirror=True, mirrorUpdatedAt=box["mirrorUpdatedAt"])


@route("POST", "/arena/shop/buy")
def arena_shop_buy(ctx: Ctx) -> dict:
    item_id = ctx.s("itemId") or ctx.s("item_id")
    try:
        qty = max(1, min(int(ctx.body.get("quantity", 1) or 1), 99))
    except (TypeError, ValueError):
        qty = 1
    rows = RULES_SHOP.get("items") if isinstance(RULES_SHOP, dict) else RULES_SHOP
    if not isinstance(rows, list):
        rows = []
    item = next((i for i in rows if isinstance(i, dict) and str(i.get("id", "")) == item_id), None)
    if item is None:
        return fail("商品不存在")
    cost = int(item.get("cost", 0) or 0) * qty
    box = _arena_box(ctx.state)
    points = int(box.get("points", 0))
    if points < cost:
        return fail("竞技点不足（需要 %d，当前 %d）" % (cost, points))
    box["points"] = points - cost
    name = str(item.get("itemName", item.get("name", "")) or "竞技奖励")
    ctx.state.add_item(name, qty, "consumable")
    ctx.state.bump()
    return ok(itemName=name, quantity=qty, cost=cost,
              weeklyArenaPoints=int(box.get("points", 0)),
              balanceArenaPoints=int(box.get("points", 0)),
              spendableArenaPoints=int(box.get("points", 0)))


@route("POST", "/arena/titles/activate")
def arena_title_activate(ctx: Ctx) -> dict:
    return fail("暂无可用竞技称号")


# ---- 藏宝洞镜像（客户端会拉，缺了会报 404） ------------------------------

@route("GET", "/treasure/mirror")
def treasure_mirror(ctx: Ctx) -> dict:
    """镜像 = 人物 + 当前出战宠的快照（见 treasure_mirror.py）。"""
    import treasure_mirror

    return treasure_mirror.current(ctx.state)


@route("POST", "/treasure/mirror/update")
def treasure_mirror_update(ctx: Ctx) -> dict:
    """按当前阵容重拍镜像；入阵后本轮仍是入阵时锁定的那份。"""
    import treasure_mirror

    return treasure_mirror.update(ctx.state)


def _load_shop() -> dict:
    from core import DATA, load_json
    raw = load_json(DATA / "arena-shop-items.json", {}) or {}
    return raw if isinstance(raw, dict) else {}


RULES_SHOP = _load_shop()
