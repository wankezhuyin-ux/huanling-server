#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""养成层：商店、抽奖、经济结算、灵兽、装备、玩家属性、武学、打造、礼包。

这些端点的共同点：都是"服务端收到操作 → 校验代价 → 改存档 → 回增量"。
本地一律照这个模式实现，代价和产物都从 runtime-config / shop-catalog 读，
不再有魔法数字。
"""
from __future__ import annotations

import random
import re
import time

from core import (COLLECTION_MAX_PER_SPECIES, Ctx, DATA, RULES, fail,
                  invest_pet_to_collection, load_json, iso, now, ok,
                  pet_hp_growth, route, today)
from handlers_battle import MAPS_DATA


# ================================================================ 商店

@route("GET", "/shop/catalog")
def shop_catalog(ctx: Ctx) -> dict:
    """商店目录 + 本玩家今日限购进度。

    客户端 ui/city/shop_ui.gd `_get_yuanbao_items_cfg()` 只认 `itemName` /
    `basePrice` / `finalPrice` / `currency` / `vipRequiredTier` / `dailyLimit` /
    `dailyRemaining`，限购剩余必须按玩家当日已购数量算，否则界面上永远显示满额。
    """
    state = ctx.state
    raw = RULES.shop_catalog if isinstance(RULES.shop_catalog, dict) else {}
    items = []
    for item in (raw.get("items") or []):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        name = str(row.get("itemName", "")).strip()
        limit = int(row.get("dailyLimit", 0) or 0)
        bought = state.daily("shop_%s" % name) if name else 0
        row["dailyBought"] = bought
        row["dailyRemaining"] = max(0, limit - bought) if limit > 0 else 0
        items.append(row)
    out = dict(raw)
    out["items"] = items
    out["serverUnixTime"] = int(time.time())
    out["vipTier"] = state.vip_tier()
    return out


def _shop_currency_key(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if text in ("gold", "金币"):
        return "gold"
    if text in ("yuanbao", "yuanbao_currency", "元宝"):
        return "yuanbao"
    if text in ("friendship", "friendship_points", "friend", "友情点"):
        return "friendship"
    if text in ("arena", "arena_points", "竞技点"):
        return "arena"
    return text


@route("POST", "/shop/buy")
def shop_buy(ctx: Ctx) -> dict:
    """商店购买（金币店 / 元宝店 / 友情点店 / 竞技点店 共用）。

    客户端 network.gd `shop_buy()` 的请求体只有 **itemName / quantity /
    operationId / currency**，从来不发 itemId。原实现按 itemId 查表，
    永远查不到 → 回「商品不存在」；更糟的是旧响应里带 `items` 字段，
    会被客户端 `_on_request_completed` 里 `data.has("items")` 的分支截胡，
    购买信号永远不发，界面 2.5 秒超时后反而提示「购买成功，请到邮件查收」。
    这里按 itemName 反查、按目录价扣款、走邮件发货，响应不再带 items。
    """
    state = ctx.state
    item_name = (ctx.s("itemName") or ctx.s("name") or ctx.s("itemId")).strip()
    if not item_name:
        return fail("商品名不能为空")
    try:
        quantity = max(1, min(999, int(ctx.body.get("quantity", ctx.body.get("count", 1)) or 1)))
    except (TypeError, ValueError):
        quantity = 1

    raw = RULES.shop_catalog if isinstance(RULES.shop_catalog, dict) else {}
    catalog = [it for it in (raw.get("items") or []) if isinstance(it, dict)]
    target = None
    for item in catalog:
        if str(item.get("itemName", "")).strip() == item_name:
            target = item
            break
    if target is None:
        return fail("商品不存在或已下架")

    # ---- 幂等：客户端超时重试会复用 operationId，不能重复扣款发货
    op_id = ctx.s("operationId")
    ops = state.meta.setdefault("shopOps", {})
    if not isinstance(ops, dict):
        ops = {}
        state.meta["shopOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(itemName=item_name, quantity=int(memo.get("quantity", quantity)),
                  cost=int(memo.get("cost", 0)), currency=str(memo.get("currency", "")),
                  duplicate=True, newGold=state.gold(), newYuanbao=state.yuanbao(),
                  newFriendshipPoints=state.friendship(),
                  saveData={"playerData": state.player}, saveRevision=state.revision)

    # ---- 限购 / VIP 门槛
    limit = int(target.get("dailyLimit", 0) or 0)
    if limit > 0 and state.daily("shop_%s" % item_name) + quantity > limit:
        left = max(0, limit - state.daily("shop_%s" % item_name))
        return fail("超出每日限购（今日还可买 %d 个）" % left)
    need_tier = int(target.get("vipRequiredTier", 0) or 0)
    if need_tier > 0 and state.vip_tier() < need_tier:
        return fail("需要 VIP%d 才能购买" % need_tier)

    # ---- 价格与货币（以目录为准，忽略客户端传来的 currency）
    base_price = int(target.get("basePrice", 0) or 0)
    unit_price = int(target.get("finalPrice", base_price) or base_price)
    if unit_price < 0:
        unit_price = base_price
    total = unit_price * quantity
    currency = _shop_currency_key(target.get("currency", "gold"))

    if currency == "gold":
        if state.gold() < total:
            return fail("金币不足（需要 %d，当前 %d）" % (total, state.gold()))
        state.add_gold(-total)
    elif currency == "yuanbao":
        if state.yuanbao() < total:
            return fail("元宝不足（需要 %d，当前 %d）" % (total, state.yuanbao()))
        state.add_yuanbao(-total)
    elif currency == "friendship":
        if state.friendship() < total:
            return fail("友情点不足（需要 %d，当前 %d）" % (total, state.friendship()))
        state.add_friendship(-total)
    elif currency == "arena":
        box = state.meta.setdefault("arena", {})
        points = int(box.get("points", 0) or 0)
        if points < total:
            return fail("竞技点不足（需要 %d，当前 %d）" % (total, points))
        box["points"] = points - total
    else:
        return fail("不支持的货币类型：%s" % currency)

    if limit > 0:
        state.daily_inc("shop_%s" % item_name, quantity)

    # ---- 发货：原服商店走邮件（客户端成功文案就是「请到邮件查收货物」）
    state.send_mail(
        "商店购买",
        "你在商店街购买了【%s】×%d，附件已随邮件送达。" % (item_name, quantity),
        items=[{"name": item_name, "count": quantity}],
    )
    if op_id:
        ops[op_id] = {"at": iso(now()), "itemName": item_name, "quantity": quantity,
                      "cost": total, "currency": currency}
        if len(ops) > 50:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-50]:
                ops.pop(stale, None)
    state.bump()
    return ok(
        itemName=item_name,
        quantity=quantity,
        cost=total,
        unitPrice=unit_price,
        currency=currency,
        duplicate=False,
        mailed=True,
        newGold=state.gold(),
        newYuanbao=state.yuanbao(),
        newFriendshipPoints=state.friendship(),
        dailyRemaining=(max(0, limit - state.daily("shop_%s" % item_name)) if limit > 0 else 0),
        saveRevision=state.revision,
    )


# ================================================================ 抽奖

# 「之一」类奖励的展开表：奖池配置里 `type=item_pool` 的行是**占位名**，
# 必须现场抽成具体道具再发，否则客户端 `inventory_manager.add_item()` 认不出
# 这个名字 —— 邮件点「打开」直接弹「附件添加失败：未知物品：普通技能书之一」，
# 奖励永久卡在邮箱里（玩家实测）。池内等概率，与客户端 probabilityText 一致。
LOTTERY_ITEM_POOLS = {
    "limit_equip": ["极限武器", "极限衣服", "极限鞋子", "极限血佩", "极限内带"],
    "element_egg": ["金纹蛋", "木纹蛋", "水纹蛋",
                    "火纹蛋", "土纹蛋"],
    "common_skill_book_pool": ["自复lv1技能书", "轻身lv1技能书", "挡伤lv1技能书",
                               "长命lv1技能书", "力壮lv1技能书", "凶性lv1技能书",
                               "追杀lv1技能书", "守主lv1技能书"],
    "rare_skill_book_pool": ["溅墨lv1技能书", "反打lv1技能书", "护主lv1技能书",
                             "连击lv1技能书", "饮血lv1技能书", "破防lv1技能书",
                             "挡伤lv1技能书", "致命lv1技能书", "挑衅lv1技能书"],
}


def expand_lottery_reward(row: dict) -> dict:
    """把 `item_pool` 行的占位名（普通技能书之一…）抽成具体道具。

    非 item_pool / 未知池原样返回（宁可按老路发出去，也别把奖励吞掉）。
    """
    kind = str(row.get("type", "") or "")
    if kind != "item_pool":
        return row
    options = LOTTERY_ITEM_POOLS.get(str(row.get("id", "") or ""))
    if not options:
        return row
    out = dict(row)
    out["poolName"] = str(row.get("name", "") or "")
    out["name"] = random.choice(options)
    out["type"] = "item"
    return out


def _pity(state) -> dict:
    return {"count": int(state.meta.get("lotteryPity", 0)),
            "threshold": int(RULES.v("lottery", "pity.threshold", 100) or 100),
            "options": RULES.v("lottery", "pity.options", []) or []}


@route("POST", "/lottery/draw")
def lottery_draw(ctx: Ctx) -> dict:
    """幸运转盘抽奖。

    客户端 ui/city/lucky_draw_ui.gd 期望：success + rewardName/rewardDesc +
    pityStatus + newGold/newYuanbao。响应里**绝对不能带 `items`**：
    network.gd `_on_request_completed` 里 `data.has("items")` 的分支排在
    `current_type == "lottery_draw"` 之前，会把它当成仙魔商店列表吃掉，
    `lottery_draw_success` 永远不发，全屏「抽奖进行中」遮罩就一直卡在那里。
    """
    state = ctx.state
    pool_key = ctx.s("pool") or "gold"
    pool = RULES.v("lottery", "pools.%s" % pool_key, {}) or {}
    if not pool:
        return fail("奖池不存在")
    try:
        times = max(1, min(10, int(ctx.body.get("times", 1) or 1)))
    except (TypeError, ValueError):
        times = 1
    cost = pool.get("cost", {}) or {}
    unit_cost = int(cost.get("amount", 0) or 0)
    amount = unit_cost * times
    currency = str(cost.get("type", "gold"))

    op_id = ctx.s("client_op_id") or ctx.s("operationId")
    ops = state.meta.setdefault("lotteryOps", {})
    if not isinstance(ops, dict):
        ops = {}
        state.meta["lotteryOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        # duplicate 回包必须带 saveData：客户端 _lottery_draw_response_ok() 在
        # duplicate=true 时会校验 saveData 非空，缺了会被判成「抽奖响应无效」。
        return ok(pool=pool_key, times=int(memo.get("times", times)),
                  rewardName=str(memo.get("rewardName", "")),
                  rewardDesc=str(memo.get("rewardDesc", "")),
                  rewards=memo.get("rewards", []),
                  duplicate=True,
                  newGold=state.gold(), newYuanbao=state.yuanbao(),
                  saveData={"playerData": state.player}, saveRevision=state.revision,
                  pityStatus=_pity_status(state))

    if currency == "gold":
        if state.gold() < amount:
            return fail("金币不足（本次需要 %d，当前 %d）" % (amount, state.gold()))
        state.add_gold(-amount)
    else:
        if state.yuanbao() < amount:
            return fail("元宝不足（本次需要 %d，当前 %d）" % (amount, state.yuanbao()))
        state.add_yuanbao(-amount)

    rewards_table = pool.get("rewards", []) or []
    results: list[dict] = []
    mail_items: list[dict] = []
    for _ in range(times):
        row = RULES.roll(rewards_table)
        if not row:
            continue
        # 「普通技能书之一」这类占位名要先抽成具体道具，否则邮件领不出来
        row = expand_lottery_reward(row)
        name = str(row.get("name", "奖励"))
        count = int(row.get("count", 1) or 1)
        kind = str(row.get("type", "item") or "item")
        if kind == "gold":
            # 金币类奖励直接加到余额（原来当成道具塞背包，抽到「5亿金币」等于白抽）
            gold_amount = _gold_reward_amount(name, count)
            state.add_gold(gold_amount)
            results.append({"name": name, "count": 1, "type": "gold", "gold": gold_amount})
        else:
            mail_items.append({"name": name, "count": count})
            results.append({"name": name, "count": count, "type": kind})

    if mail_items:
        summary = "、".join("%s×%d" % (it["name"], it["count"]) for it in mail_items)
        state.send_mail("幸运抽奖", "恭喜你在幸运抽奖中获得：%s，附件请查收。" % summary,
                        items=mail_items)

    if currency != "gold":
        state.meta["lotteryPity"] = int(state.meta.get("lotteryPity", 0)) + times
        state.player["lottery_yuanbao_pity_count"] = state.meta["lotteryPity"]

    state.bump()
    first = results[0] if results else {}
    total_items = sum(it["count"] for it in mail_items) or 1
    reward_name = str(first.get("name", "未知奖励"))
    if times > 1:
        reward_name = "%s 等 %d 件" % (reward_name, total_items if mail_items else times)
    reward_desc = ("奖励已通过邮件发放，请到邮件查收。" if mail_items
                   else "金币奖励已直接到账。")
    if op_id:
        ops[op_id] = {"at": iso(now()), "times": times, "rewardName": reward_name,
                      "rewardDesc": reward_desc, "rewards": results}
        if len(ops) > 50:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-50]:
                ops.pop(stale, None)
    return ok(
        rewards=results,
        pool=pool_key,
        times=times,
        cost=amount,
        currency=currency,
        rewardName=reward_name,
        rewardDesc=reward_desc,
        mailed=bool(mail_items),
        pityStatus=_pity_status(state),
        newGold=state.gold(),
        newYuanbao=state.yuanbao(),
        saveRevision=state.revision,
    )


def _gold_reward_amount(name: str, count: int = 1) -> int:
    """把「5亿金币 / 1亿金币 / 2亿金币 / 10亿金币」这类奖励名换算成数值。"""
    text = str(name or "")
    table = {"1亿金币": 100_000_000, "2亿金币": 200_000_000, "5亿金币": 500_000_000,
             "10亿金币": 1_000_000_000}
    for key, amount in table.items():
        if key in text:
            return amount * max(1, int(count or 1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*亿", text)
    if m:
        return int(float(m.group(1)) * 100_000_000) * max(1, int(count or 1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*万", text)
    if m:
        return int(float(m.group(1)) * 10_000) * max(1, int(count or 1))
    return 0


def _pity_status(state) -> dict:
    pity = _pity(state)
    count = int(state.meta.get("lotteryPity", 0))
    return {
        "count": count,
        "threshold": pity["threshold"],
        "remaining": max(0, pity["threshold"] - count),
        "canClaim": count >= pity["threshold"],
        "options": pity["options"],
        "description": "",
    }



@route("GET", "/lottery/pity/status")
def lottery_pity_status(ctx: Ctx) -> dict:
    return {"success": True, "pityStatus": _pity_status(ctx.state)}


@route("POST", "/lottery/pity/claim")
def lottery_pity_claim(ctx: Ctx) -> dict:
    state = ctx.state
    pity = _pity(state)
    if int(state.meta.get("lotteryPity", 0)) < pity["threshold"]:
        return fail("保底次数未满")
    pick = ctx.s("rewardName") or ctx.s("option") or (pity["options"][0] if pity["options"] else "保底奖励")
    if pity["options"] and pick not in pity["options"]:
        return fail("奖励选项不合法")
    # 走邮件发放（客户端文案：保底奖励已发放，请到邮件查收）
    state.send_mail("幸运抽奖保底", "保底奖励已发放，请在附件中查收。", items=[{"name": pick, "count": 1}])
    state.meta["lotteryPity"] = 0
    state.player["lottery_yuanbao_pity_count"] = 0
    state.bump()
    return ok(rewardName=pick, mailed=True, pityStatus=_pity_status(state))


# ================================================================ 经济结算

@route("GET", "/economy/drop-config")
def drop_config(ctx: Ctx) -> dict:
    return RULES.drop


@route("GET", "/gameplay/runtime-config")
def gameplay_config(ctx: Ctx) -> dict:
    payload = {snake_name: value for snake_name, value in RULES.modules.items()}
    # offline 是本服自己的离线结算参数，客户端不认识也不需要，别混进玩法配置
    payload.pop("offline", None)
    payload["success"] = True
    return payload


@route("GET", "/festival/runtime-config")
def festival_config(ctx: Ctx) -> dict:
    return RULES.festival


def snake_name(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_" + ch.lower())
        else:
            out.append(ch)
    return "".join(out).lstrip("_")


@route("POST", "/economy/offline-gold")
def offline_gold(ctx: Ctx) -> dict:
    # 客户端 economy_offline_gold 只传 operationId，没有 minutes；
    # 原来按 minutes*10000 发钱既发不出东西，又多开了一个刷金币的口子。
    # 离线收益统一走 /economy/offline-claim 的权威结算。
    return offline_claim(ctx)


# ================================================================ 离线收益
# 客户端契约（network.gd economy_offline_claim / game_manager.gd
# `run_post_login_offline_claim_async`）：
#   登录后立刻 POST /economy/offline-claim {"operationId": "..."}
#   ← success / effectiveSeconds / exp / gold / petGifts / vipTier
#     playerData / inventoryData / petsData
#     playerLevelBefore|After / petLevelBefore|After / petName
# 只有 effectiveSeconds > 60 客户端才弹「离线奖励」窗。
# playerData 会整包覆盖客户端本地存档（offline_claim_authoritative=true），
# 所以必须回传结算后的**完整** playerData，不能只回增量。

_OFFLINE_DEFAULT = {
    "enabled": True,
    "minSeconds": 60,
    # VIP 特权表（ui/city/settings_hall.gd 离线时长行）：非VIP / 青铜 / 白银 / 黄金
    "maxHoursByVip": [8, 12, 16, 24],
    # 离线折算成「每小时打多少场」；地图收益取 data/maps.json 里的原服实测均值。
    #
    # 原版标定（用玩家登录原版服的截图反推，不是拍的数）：
    #   截图面板「离线时间 8.0 小时 → 金币 13116491」，同一张图人物 733 级升到 1125 级。
    #   1) 金币侧：13116491 / 8h = 1,639,561 金币/小时。原版该账号当时在
    #      「石壁洞（25,32）」(data/maps.json avgGold=911.7)，反解得 1798.6 场/小时。
    #   2) 经验侧：733→1125 需 5.2 亿经验，÷8h÷1800 场 ≈ 41000 经验/场，
    #      和「石壁洞」avgPlayerExp=41025 对得上。
    #   3) 节奏侧：原版战斗录屏实测「战斗开始 → 战斗胜利 + 结算 ≈ 2 秒」，
    #      3600 / 2 = 1800 场/小时 —— 也就是说**原版离线收益就是按在线同速结算**，
    #      并不是打了折扣。原来这里填 12，等于离线只有在线的 1/150，
    #      玩家体感就是「挂机一晚上什么都没长」。
    #   取 1800（而不是 1798.6）：它正好等于「2 秒一场」的在线速度，
    #   金币误差 0.09%（13,128,480 vs 原版 13,116,491），属于四舍五入级别。
    "battlesPerHour": 1800,
    "petExpRatio": 0.6,
    # 同上表的「离线礼包概率」行
    "petGiftChanceByVip": [0.25, 0.40, 0.45, 0.50],
    # 地图查不到 / 该地图原本就不给奖励（冷泉洞系列 avgPlayerExp=0）时的兜底。
    # 这些图在线本来就不掉经验，离线给 0 才对；这里保留「每小时 0.35 级」只是
    # 一个避免玩家以为系统坏掉的小保底，不要按主路径的量级去理解它。
    "fallbackExpPerHourRatio": 0.35,
    "fallbackGoldPerHourBase": 500,
    "fallbackGoldPerLevelPerHour": 100,
}


def _offline_cfg() -> dict:
    cfg = dict(_OFFLINE_DEFAULT)
    raw = RULES.m("offline")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if value is not None:
                cfg[key] = value
    return cfg


def _vip_index(player: dict) -> int:
    try:
        tier = int(player.get("vip_level", 0) or 0)
    except (TypeError, ValueError):
        tier = 0
    return max(0, min(3, tier))


def _offline_hourly_rates(state) -> tuple[float, float]:
    """返回 (每小时经验, 每小时金币)。

    优先用当前地图的原服实测均值：`data/maps.json` 里 avgPlayerExp / avgGold
    是线上抓包统计出来的「每场战斗平均玩家经验/金币」，离线按 `battlesPerHour`
    场折算，高等级地图的离线收益会自然更高（和在线收益同源）。

    `battlesPerHour` = 1800 的原版依据见 `_OFFLINE_DEFAULT` 里的标定注释：
    原版战斗 2 秒一场 → 3600/2 = 1800，即**离线与在线同速**，不是打折结算。

    地图查不到、或该地图原服本来就不给奖励（冷泉洞系列 avgPlayerExp=0），
    回退到按当前等级需求估算（只给一个象征性保底，见 fallbackExpPerHourRatio）。
    """
    cfg = _offline_cfg()
    p = state.player
    save = state.save
    gs = save.get("gameSettings") if isinstance(save.get("gameSettings"), dict) else {}
    map_id = str(gs.get("current_map", "") or "")
    map_cfg = MAPS_DATA.get(map_id) if isinstance(MAPS_DATA, dict) else None
    battles = float(cfg.get("battlesPerHour", 1800) or 1800)
    avg_exp = float((map_cfg or {}).get("avgPlayerExp", 0) or 0)
    avg_gold = float((map_cfg or {}).get("avgGold", 0) or 0)
    if avg_exp > 0:
        return avg_exp * battles, max(0.0, avg_gold) * battles
    level = int(p.get("level", 1) or 1)
    exp_ratio = float(cfg.get("fallbackExpPerHourRatio", 0.35) or 0.35)
    gold_base = float(cfg.get("fallbackGoldPerHourBase", 500) or 500)
    gold_per_level = float(cfg.get("fallbackGoldPerLevelPerHour", 100) or 100)
    return state.required_exp(level) * exp_ratio, gold_base + gold_per_level * level


def _offline_empty(state, seconds: int = 0) -> dict:
    """没有离线收益（时长不足/未开启/重复请求）时的完整响应。"""
    p = state.player
    level = int(p.get("level", 1) or 1)
    return ok(
        effectiveSeconds=seconds,
        exp=0,
        gold=0,
        petGifts=0,
        vipTier=state.vip_tier(),
        newGold=state.gold(),
        playerData=p,
        inventoryData=state.inv,
        petsData=state.save.get("petsData", {}),
        playerLevelBefore=level,
        playerLevelAfter=level,
        petLevelBefore=0,
        petLevelAfter=0,
        petName="",
    )


def _offline_recent_replay(state, min_seconds: int):
    """5 分钟内刚结算过这一次离线 → 回放上次展示数据（不重复发奖）。

    客户端同一次登录会对 `/economy/offline-claim` 打两次（下载存档后 + 主界面），
    第一次已经把 offlinePending 消费掉了，第二次一定算出 0 秒。如果不回放，
    玩家就看不到「离线奖励」弹窗 —— 这正是「下线再上线没有离线经验提示」的根因。
    """
    last = state.meta.get("offlineLastClaim")
    if not isinstance(last, dict):
        return None
    # 只回放「同一次登录」里的第二次请求。换一次登录（loginSeq 变了）就不再回放，
    # 否则玩家下线 30 秒再上来会被上一次的离线弹窗糊一脸。
    if int(last.get("loginSeq", -1)) != int(state.meta.get("loginSeq", -2)):
        return None
    try:
        claimed_at = int(last.get("claimedAt", 0) or 0)
    except (TypeError, ValueError):
        return None
    if claimed_at <= 0 or int(time.time()) - claimed_at > 300:
        return None
    stored = last.get("payload")
    if not isinstance(stored, dict):
        return None
    if int(stored.get("effectiveSeconds", 0) or 0) <= min_seconds:
        return None
    out = ok(**{k: v for k, v in stored.items() if k != "duplicate"})
    out["duplicate"] = True
    out["newGold"] = state.gold()
    # 必须带当前最新存档：客户端 `_merge_offline_claim_response_into_save_data`
    # 在 playerData 缺失时会用本地旧存档 + newGold 覆盖，刚结算的等级会被盖回去。
    out["playerData"] = state.player
    out["inventoryData"] = state.inv
    out["petsData"] = state.save.get("petsData", {})
    return out


def _offline_has_character(state) -> bool:
    """这个账号是不是已经建过角色了（规则同 handlers_account._has_character）。

    新号存档是从种子复制的，种子里的名字是「新人」，所以不能用「名字非空」判断。
    """
    if str(state.meta.get("nickname") or "").strip():
        return True
    name = str(state.player.get("name") or "").strip()
    return name not in ("", "新人")


@route("POST", "/economy/offline-claim")
def offline_claim(ctx: Ctx) -> dict:
    """离线收益结算：按距上次在线时长发经验/金币/宠物经验/离线宠物锦囊。"""
    state = ctx.state
    p = state.player
    cfg = _offline_cfg()
    op_id = ctx.s("operationId")

    # 还没建角色（刚注册、正停在「创建角色」面板）的账号一律不结算离线收益。
    # 种子档默认停在后期地图（data/maps.json 里 avgPlayerExp 上万），按
    # battlesPerHour=1800 折算，二十几分钟就能把 1 级新号顶到 70 级 ——
    # 实际表现就是「注册完还没建角色，先弹了一个 50 级升 70 级的离线奖励」。
    if not _offline_has_character(state):
        state.meta.pop("offlinePending", None)
        return _offline_empty(state)

    # 幂等：客户端网络重试会复用同一个 operationId，绝不能重复发奖
    claims = state.meta.get("offlineClaims")
    if not isinstance(claims, dict):
        # 先不挂回 meta：登录后客户端会连打两次 claim，其中一次必然走
        # 「秒数不够 → 回空包」的早退分支。早退时往 meta 里塞一个空 dict
        # 也算「存档变了」，会让本该纯读的请求触发一次整档落盘。
        # 真要写记录时（下方 claims[op_id] = ...）再挂回去。
        claims = {}
    if op_id and isinstance(claims.get(op_id), dict):
        prev = claims[op_id]
        payload = _offline_empty(state, int(prev.get("effectiveSeconds", 0) or 0))
        payload["duplicate"] = True
        return payload

    if not bool(cfg.get("enabled", True)):
        return _offline_empty(state)

    seconds = state.peek_offline_seconds()
    min_seconds = int(cfg.get("minSeconds", 60) or 60)

    # 一次离线结算可能被调用两次：game_manager `_on_cloud_save_downloaded`
    # 拿到存档后先领一次，主界面 `run_post_login_offline_claim_async` 再领一次
    # （两次 operationId 不同）。第一次已经把 offlinePending 清了、奖也发了，
    # 第二次必然算出 0 秒 → 回空包 → 客户端 `if eff > 60` 不成立 → 离线奖励弹窗
    # 永远不出现。这里在 5 分钟内回放上一份结果（只回展示数据，不重复发奖）。
    if seconds <= min_seconds:
        replay = _offline_recent_replay(state, min_seconds)
        if replay is not None:
            return replay
        state.meta.pop("offlinePending", None)
        return _offline_empty(state)

    # 结算一次就把登录快照清掉，避免同一次离线被反复领
    state.meta.pop("offlinePending", None)

    vip = state.vip_tier()
    hours_table = cfg.get("maxHoursByVip") or [8, 12, 16, 24]
    try:
        cap_hours = float(hours_table[min(vip, len(hours_table) - 1)])
    except (TypeError, ValueError, IndexError):
        cap_hours = 8.0
    effective = min(int(seconds), int(cap_hours * 3600))
    if effective <= min_seconds:
        replay = _offline_recent_replay(state, min_seconds)
        if replay is not None:
            return replay
        return _offline_empty(state)
    hours = effective / 3600.0

    exp_per_hour, gold_per_hour = _offline_hourly_rates(state)
    base_exp = float(exp_per_hour) * hours
    gold = int(round(gold_per_hour * hours))

    # 经验倍率：VIP（1/1.3/1.6/2.0）+ 每种生效中的双倍经验符 +100%。
    # 双倍丹只对落在它自身有效期内的那段离线时长生效，过期部分不翻倍；
    # 三种丹各自计时，所以这里是逐通道叠加，而不是只看最晚的那一个。
    now_ts = int(time.time())
    start_ts = now_ts - effective
    exp_mult = float(state.VIP_EXP_MULT[vip])
    for _name, until in state.double_exp_channels().items():
        until = int(until or 0)
        if until <= start_ts:
            continue
        overlap = min(until, now_ts) - start_ts
        exp_mult += max(0.0, min(1.0, overlap / float(max(1, effective))))
    exp = int(round(base_exp * exp_mult))

    level_before = int(p.get("level", 1) or 1)
    if exp > 0:
        state.grant_player_exp(exp)
    if gold > 0:
        state.add_gold(gold)
    level_after = int(p.get("level", 1) or 1)

    # 宠物：服务端直接结算并下发 petsData，客户端看到 petsData 就不会再叠加
    # petOfflineExp（game_manager._merge_offline_claim_response_into_save_data），
    # 所以这里 petOfflineExp 必须回空，否则宠物经验双发。
    pet_before = 0
    pet_after = 0
    pet_name = ""
    pets = state.pets
    if pets:
        pet_box = state.save.get("petsData") if isinstance(state.save.get("petsData"), dict) else {}
        active_id = str(pet_box.get("active_pet_id", "") or "")
        pet = next((x for x in pets if str(x.get("id", "")) == active_id), pets[0])
        if isinstance(pet, dict):
            pet_before = int(pet.get("level", 1) or 1)
            pet_name = str(pet.get("name", "灵兽"))
            pet_exp = int(round(exp * float(cfg.get("petExpRatio", 0.6) or 0.6)))
            if pet_exp > 0:
                state.grant_pet_exp(pet, pet_exp)
            pet_after = int(pet.get("level", 1) or 1)

    # 离线宠物锦囊：按 VIP 概率 roll
    gifts = 0
    chances = cfg.get("petGiftChanceByVip") or [0.25, 0.40, 0.45, 0.50]
    try:
        chance = float(chances[min(vip, len(chances) - 1)])
    except (TypeError, ValueError, IndexError):
        chance = 0.25
    if chance > 0 and random.random() < chance:
        gifts = 1
        state.add_item("离线宠物锦囊", 1, "gift")

    p["timestamp"] = int(time.time())
    state.bump()

    # 只保留最近 20 条 operationId，别让 meta 无限膨胀
    if op_id:
        state.meta["offlineClaims"] = claims
        claims[op_id] = {"effectiveSeconds": effective, "exp": exp, "gold": gold, "at": iso(now())}
        if len(claims) > 20:
            for key in sorted(claims, key=lambda k: str(claims[k].get("at", "")))[:-20]:
                claims.pop(key, None)

    payload = ok(
        effectiveSeconds=effective,
        exp=exp,
        gold=gold,
        petGifts=gifts,
        vipTier=vip,
        duplicate=False,
        newGold=state.gold(),
        playerData=p,
        inventoryData=state.inv,
        petsData=state.save.get("petsData", {}),
        playerLevelBefore=level_before,
        playerLevelAfter=level_after,
        petLevelBefore=pet_before,
        petLevelAfter=pet_after,
        petName=pet_name,
        offlineSeconds=int(seconds),
    )
    # 缓存一份「本次离线的展示数据」，供 5 分钟内第二次 claim 回放（见 _offline_recent_replay）。
    # 只存展示字段，不含 playerData/inventoryData/petsData 这类大块存档，
    # 避免 meta 膨胀，也避免回放时把旧存档盖到客户端内存里。
    state.meta["offlineLastClaim"] = {
        "claimedAt": now_ts,
        "loginSeq": int(state.meta.get("loginSeq", 0) or 0),
        "payload": {
            "effectiveSeconds": effective,
            "exp": exp,
            "gold": gold,
            "petGifts": gifts,
            "vipTier": vip,
            "playerLevelBefore": level_before,
            "playerLevelAfter": level_after,
            "petLevelBefore": pet_before,
            "petLevelAfter": pet_after,
            "petName": pet_name,
        },
    }
    return payload

@route("POST", "/economy/quest-claim")
def quest_claim(ctx: Ctx) -> dict:
    state = ctx.state
    quest_id = ctx.s("questId") or ctx.s("id")
    gold = ctx.i("gold") or 100000
    state.add_gold(gold)
    state.bump()
    return ok(goldGained=gold, newGold=state.gold(), questId=quest_id)


# 金币袋（赞助者命名道具）。key 与金币数全部取自客户端
# scripts/inventory_manager.gd `const GOLD_BAG_CONFIG`（amount 一律 100000，
# server_key 只是历史命名，别拿 key 里的 10k/50k 当金额用）。
_GOLD_BAG_RULES = {
    # 2026-10 钱袋合并：14 种赞助钱袋 → 单一「钱币袋」。旧档由 core.State._migrate_coin_bags
    # 在读档时改名，下面这些旧 key 只留给迁移前的请求兜底。
    "coin_bag": (("钱币袋",), 100000),
    "gold_bag_10k_zui": (("醉剑", "1万金币袋（醉）"), 100000),
    "gold_bag_50k_shen": (("神的遗愿",), 100000),
    "gold_bag_100k_fengge": (("风歌夜曲",), 100000),
    "gold_bag_100k_bailan": (("摆烂松",), 100000),
    "gold_bag_100k_renjian": (("人间大炮",), 100000),
    "gold_bag_100k_wodelixiang": (("我的理想",), 100000),
    "gold_bag_100k_banzhuan": (("板砖",), 100000),
    "gold_bag_100k_liyu": (("立羽",), 100000),
    "gold_bag_100k_mingdada": (("铭大大",), 100000),
    "gold_bag_100k_changkong": (("长空秋雁",), 100000),
    "gold_bag_100k_dajudali": (("大橘大利",), 100000),
    "gold_bag_100k_renjianshi": (("人间事",), 100000),
    "gold_bag_100k_sinian": (("思念",), 100000),
    # 开发期「测试锦囊」：客户端 inventory_manager.gd 4879 直接要 1 亿金币、不扣道具
    "test_gift_100m": ((), 100000000),
}


@route("POST", "/economy/whitelist-gold")
def whitelist_gold(ctx: Ctx) -> dict:
    """金币袋在线发放（服务端权威）。

    客户端 use_gold_bag_batch / 单次使用都要求回包带 `saveData`
    （inventory_manager.gd 2034/2974 `batch_data.has("saveData")`），
    拿 `reward_inventory_slice` 去对齐背包并扣掉金币袋。
    原来这里只回 `newGold`：客户端一律弹「服务器返回异常，金币袋使用未完成」，
    而且道具根本不会被扣 —— 这就是金币袋用不了的直接原因。
    """
    state = ctx.state
    key = ctx.s("grantKey")
    try:
        count = max(1, int(ctx.body.get("consumeCount", 1) or 1))
    except (TypeError, ValueError):
        count = 1
    op_id = ctx.s("operationId")

    ops = state.meta.get("goldBagOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["goldBagOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(grantKey=key, itemName=memo.get("itemName", ""), consumed=0,
                  goldGained=0, duplicate=True, newGold=state.gold(),
                  saveData=_authority_save(state), saveRevision=state.revision)

    rule = _GOLD_BAG_RULES.get(key)
    if rule is not None:
        names, unit = rule
    else:
        # 表外 key：仍然要求带道具名，金额以服务端为准（客户端传的 amount 只用来兜底）
        name = ctx.s("itemName") or ctx.s("name")
        unit = max(0, ctx.i("amount"))
        if not name or unit <= 0:
            return fail("未知的金币袋：%s" % (key or "未提供 grantKey"))
        names = (name,)

    picked = None
    for name in names:
        if state.item_count(name) >= count:
            picked = name
            break
    if not names:
        # 表项明确写了「不扣道具」（如 test_gift_100m）
        picked = ""
        count = 1
    elif picked is None:
        return fail("道具数量不足")

    if picked:
        state.take_item(picked, count)
    gold = int(unit) * count
    state.add_gold(gold)
    state.bump()
    if op_id:
        ops[op_id] = {"at": iso(now()), "key": key, "itemName": picked, "count": count}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(grantKey=key, itemName=picked, consumed=count, goldGained=gold,
              duplicate=False, newGold=state.gold(),
              saveData=_authority_save(state), saveRevision=state.revision)


def _authority_save(state, include_pet_equip_bag: bool = True) -> dict:
    """道具使用类接口的权威存档回包。

    **必须带 inventoryData**：客户端 game_manager.gd `apply_cloud_save` 里
    背包分支是 `if save_data.has("inventoryData")` —— 只回 playerData 时
    背包压根不刷新。原来的表现就是：服务端已经把双倍丹扣了、界面提示「使用成功」，
    但背包里那颗丹还在；紧接着客户端一次 `save_game` 把旧背包整包传回来，
    服务端扣掉的道具也跟着复活。同理 petsData / equipmentData 也必须带上，
    否则开礼包开出来的宠物、装备下一轮上传又被抹掉。

    `include_pet_equip_bag=False` 供「宠物装备穿戴/卸下」用：这两条路径上
    服务端侧的宠装背包只是**局部副本**（老账号的整套宠装只在客户端，
    服务端第一次见到某件装备还得靠请求里的快照补登），把它整包下发会让
    客户端 `inventory_manager.from_dict` 先 clear 再用这份不完整的列表填充，
    玩家背包里其余几件当场消失。宠装背包的增删由客户端
    `_reconcile_pet_equipment_bag()` 按回包的 `equipped` / `item` 自己对齐，
    服务端只要保证 `item` 是「这次从身上换下来的那件」就够了。
    """
    inv = state.inv
    if not include_pet_equip_bag:
        inv = {k: v for k, v in inv.items() if k != "pet_equipment_items"}
    else:
        pet_equip_bag = inv.get("pet_equipment_items") if isinstance(inv, dict) else None
        if isinstance(pet_equip_bag, list) and not pet_equip_bag:
            # 空数组也不能发。客户端 `inventory_manager.from_dict` 是
            # `if data.has("pet_equipment_items"): pet_equipment_items.clear(); ...`，
            # 收到一个空数组就会把本地整格宠物装备背包清空 —— 玩家点一次「穿戴」，
            # 背包里剩下的几件就凭空消失了。
            # 老账号的服务端侧这份背包是空的（客户端一直在紧凑上传里把它 erase 掉），
            # 这种时候宁可不发这个键，让客户端保留自己那份。
            inv = {k: v for k, v in inv.items() if k != "pet_equipment_items"}
    return {
        "playerData": state.player,
        "inventoryData": inv,
        "petsData": state.save.get("petsData", {}),
        "equipmentData": state.equipment,
        # 魂魄道具（/pet/soul/use-item）加的五行点数存在这里。
        # 客户端 apply_cloud_save 只有拿到 petCollectionData 才会走
        # `pet_soul_manager.from_dict(...)`（game_manager.gd 420~428），
        # 少这一个键的表现就是「使用成功、魂魄点数不涨」。
        "petCollectionData": state.save.get("petCollectionData", {}),
        # 已穿戴的宠物装备（petEquipmentData.equipped，全局一套）。
        # 客户端 `apply_cloud_save` 的宠装分支是
        # `if save_data.has("petEquipmentData")` → `pet_equipment_manager.from_dict(pe)`，
        # 回包里没有这个键就整块跳过，本地 `equipped` 保持原样（空的）。
        # 少这一个键的表现（玩家实测）：点「穿戴」弹「已穿戴【开荒鞋子（宠）】」，
        # 但宠物装备页的槽位还是空的、「查看加成」全是 0、
        # 出战宠物的战斗属性也没有装备加成 —— 服务端明明已经穿上去了。
        "petEquipmentData": state.save.get("petEquipmentData", {}),
    }


# 服务端权威消耗（/economy/whitelist-item-consume）。
# 客户端只传 consumeKey（network.gd economy_whitelist_item_consume 的请求体
# 只有 operationId/consumeKey/batchCount），**从来不传道具名**；
# 原来按 itemName 扣道具，客户端调过来 name 是空串，必然「道具不足」——
# 双倍经验符、修行丹、经验丹这些高价值道具全部用不了。
# 这里按 key 反查真实道具名，扣多少、给什么由服务端说了算。
#
# key 全部取自客户端源码（scripts/inventory_manager.gd 里的 economy_whitelist_item_consume 调用）：
#   double_exp_day / double_exp_week / double_exp_month  → 三种双倍经验符
#   wudao_pill_gift                                      → 开悟丹锦囊（每包 50 颗开悟丹）
#   limit_equipment_gift                                 → 极限装备锦囊（每包 5 件：武器/衣服/鞋子/血佩/内带）
#   player_upgrade_dan                                   → 人物经验丹（直接顶到满级）
#   pet_upgrade_dan                                      → 宠物经验丹（出战宠顶到满级）
_CONSUME_RULES = {
    # key: (道具名, 处理类型)
    "double_exp_day": ("双倍经验符", "double_exp"),
    "double_exp_week": ("双倍经验符·周", "double_exp"),
    "double_exp_month": ("双倍经验符·月", "double_exp"),
    "wudao_pill_gift": ("开悟丹锦囊", "wudao"),
    "limit_equipment_gift": ("极限装备锦囊", "limit_equip"),
    "player_upgrade_dan": ("人物经验丹", "player_dan"),
    "pet_upgrade_dan": ("宠物经验丹", "pet_dan"),
}

# 修行丹（蓄灵丹/定神丹/开悟丹）：客户端 CULTIVATION_PILL_WHITELIST_KEYS 的三个 key。
# 原来这里没有对应规则，落进「表外 key」分支后又拿不到道具名（客户端只传 consumeKey），
# 于是三颗修行丹一用就报「该道具暂未接入服务端权威消耗」。
_CULTIVATION_PILL_RULES = {
    "cultivation_pill_juling": ("蓄灵丹", 100),
    "cultivation_pill_ningshen": ("定神丹", 200),
    "cultivation_pill_wudao": ("开悟丹", 500),
}
MAX_CULTIVATION_PILLS_PER_LIFE = 5
for _ckey, (_cname, _cbonus) in _CULTIVATION_PILL_RULES.items():
    _CONSUME_RULES[_ckey] = (_cname, "cultivation")


# 客户端 inventory_manager.gd 常量：
#   LIMIT_EQUIPMENT_GIFT_GRANTED_NAMES / WUDAO_PILL_GIFT_GRANTED_NAMES
WUDAO_PILL_NAME = "开悟丹"
WUDAO_PILL_PER_GIFT = 50
LIMIT_EQUIP_NAMES = ("极限武器", "极限衣服", "极限鞋子", "极限血佩", "极限内带")
LIMIT_EQUIP_BATCH_MAX = 5


def _active_pet_of(state) -> dict | None:
    """出战灵兽（与 handlers_battle.active_pet 同口径，这里避免循环导入）。"""
    pets = state.pets
    if not pets:
        return None
    active_id = str(state.save.get("petsData", {}).get("active_pet_id", ""))
    for pet in pets:
        if str(pet.get("id", "")) == active_id:
            return pet
    return pets[0]


def _apply_player_upgrade_dan(state) -> int:
    """人物经验丹：直接把人物顶到满级（1200）。

    客户端 inventory_manager.gd 3300 一系只认回包 playerData.level，文案是
    「恭喜！您已升级到 %d 级（满级）」，所以这里必须真的把 level 写满，
    并按原版升级口径补属性点 / max_hp +5 / 地仙以上修行值 +1
    （player.gd on_level_up + core.grant_player_exp 同一套）。
    """
    p = state.player
    level = int(p.get("level", 1) or 1)
    cap = int(state.PLAYER_MAX_LEVEL)
    if level >= cap:
        return 0
    ups = 0
    while level < cap:
        level += 1
        ups += 1
        points = state._points_per_level()
        if bool(p.get("auto_allocation_enabled", False)):
            p["pending_auto_points"] = int(p.get("pending_auto_points", 0) or 0) + points
        else:
            p["points"] = int(p.get("points", 0) or 0) + points
        p["max_hp"] = int(p.get("max_hp", 100) or 100) + 5
        if str(p.get("realm", "")) in ("地仙", "天仙", "大罗金仙", "幻灵天神"):
            p["cultivation"] = int(p.get("cultivation", 0) or 0) + 1
    p["level"] = cap
    p["experience"] = 0
    p["hp"] = int(p.get("max_hp", 100) or 100)
    state.auto_allocate_pending_points()
    return ups


def _apply_pet_upgrade_dan(state, pet: dict) -> int:
    """宠物经验丹：出战宠顶到自己的等级上限（进化宠 1200 / 普通宠 1500）。

    每级都按 pet.gd `_level_up_stats()` 的口径 roll 成长并重算属性，
    不能只改 level —— 只改等级会让面板数值不动。
    """
    cap = state.pet_max_level(pet)
    level = int(pet.get("level", 1) or 1)
    if level >= cap:
        return 0
    ups = 0
    while level < cap:
        level += 1
        ups += 1
        pet["level"] = level
        state._pet_roll_growth(pet)
        state._recalc_pet_stats(pet)
    pet["experience"] = 0
    return ups


@route("POST", "/economy/whitelist-item-consume")
def whitelist_item_consume(ctx: Ctx) -> dict:
    state = ctx.state
    p = state.player
    key = ctx.s("consumeKey")
    try:
        count = max(1, int(ctx.body.get("batchCount", 1) or 1))
    except (TypeError, ValueError):
        count = 1
    op_id = ctx.s("operationId")

    # 幂等：客户端重试会复用 operationId，不能重复扣道具/叠加时长
    ops = state.meta.get("consumeOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["consumeOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(consumed=0, duplicate=True,
                  itemName=memo.get("itemName", ""),
                  result=memo.get("result", {}),
                  newGold=state.gold(),
                  doubleExpUntil=int(p.get("double_exp_until", 0) or 0),
                  doubleExpChannels=state.double_exp_channels(),
                  activeChannels=state.double_exp_active_count(),
                  saveData=_authority_save(state), saveRevision=state.revision)

    rule = _CONSUME_RULES.get(key)
    if rule is None:
        # 兼容仍有带道具名的老调用
        name = ctx.s("itemName") or ctx.s("name")
        if not name:
            return fail("该道具暂未接入服务端权威消耗：%s" % (key or "未知道具"))
        if not state.take_item(name, count):
            return fail("道具不足")
        state.bump()
        return ok(consumed=count, duplicate=False, itemName=name, newGold=state.gold(),
                  saveData=_authority_save(state), saveRevision=state.revision)

    item_name, kind = rule

    # 先做「能不能用」的前置校验，避免扣了道具才发现用不了
    if kind == "player_dan" and int(p.get("level", 1) or 1) >= int(state.PLAYER_MAX_LEVEL):
        return fail("您已经达到满级（%d级），无法再使用升级丹" % int(state.PLAYER_MAX_LEVEL))
    pet_for_dan = None
    if kind == "pet_dan":
        pet_for_dan = _active_pet_of(state)
        if pet_for_dan is None:
            return fail("请先设置一只出战宠物")
        if int(pet_for_dan.get("level", 1) or 1) >= state.pet_max_level(pet_for_dan):
            return fail("您的出战宠物已经达到满级，无法再使用升级丹")
    if kind == "limit_equip":
        # 客户端 LIMIT_EQUIPMENT_GIFT_BATCH_MAX = 5，单次最多开 5 个
        count = max(1, min(count, LIMIT_EQUIP_BATCH_MAX))
    if kind == "cultivation":
        # 每世限用 5 颗（player.gd MAX_CULTIVATION_PILLS_PER_LIFE）
        used_life = max(0, int(p.get("cultivation_pills_used", 0) or 0))
        remain_life = max(0, MAX_CULTIVATION_PILLS_PER_LIFE - used_life)
        if remain_life <= 0:
            return fail("本世修行丹药已用完，投胎后可再次使用")
        # 同时按「本世还能吃几颗」和「包里还有几颗」夹一次。
        # 只按 remain_life 夹的话，玩家只剩 1 颗却点批量 3 颗，会拿到 count=2
        # 然后 take_item 失败 → 弹「道具数量不足」但一颗都没用上，白点一次。
        owned_dan = state.item_count(item_name)
        if owned_dan <= 0:
            return fail("道具数量不足：需要 %s×1" % item_name)
        count = max(1, min(count, remain_life, owned_dan))

    if not state.take_item(item_name, count):
        return fail("道具数量不足：需要 %s×%d" % (item_name, count))

    result: dict = {"itemName": item_name, "count": count}
    if kind == "double_exp":
        # 三种双倍丹各自独立计时：只有「同一种」才在它自己的到期时间上顺延，
        # 不同种类互不影响（原版就是这样，三丹可以同时生效、时效分开算）。
        seconds = int(state.DOUBLE_EXP_CHANNELS.get(item_name, 24 * 3600))
        now_ts = int(time.time())
        channels = dict(state.double_exp_channels())
        base = max(now_ts, int(channels.get(item_name, 0) or 0))
        channels[item_name] = base + seconds * count
        p["double_exp_channels"] = channels
        state.sync_double_exp_fields()
        result["seconds"] = seconds * count
    elif kind == "wudao":
        gain = WUDAO_PILL_PER_GIFT * count
        state.add_item(WUDAO_PILL_NAME, gain, "cultivation_pill")
        result["grantedItems"] = [{"name": WUDAO_PILL_NAME, "count": gain}]
    elif kind == "limit_equip":
        granted = []
        for _ in range(count):
            for equip_name in LIMIT_EQUIP_NAMES:
                # 客户端 from_dict 认 item_type == "limit_equip" → material + is_limit_equip
                state.add_item(equip_name, 1, "material", {"is_limit_equip": True})
                granted.append({"name": equip_name, "count": 1})
        result["grantedItems"] = granted
    elif kind == "player_dan":
        result["levelUps"] = _apply_player_upgrade_dan(state)
        result["playerLevel"] = int(p.get("level", 1))
    elif kind == "cultivation":
        bonus = int(_CULTIVATION_PILL_RULES.get(key, (item_name, 100))[1])
        p["cultivation"] = int(p.get("cultivation", 0) or 0) + bonus * count
        p["cultivation_pills_used"] = max(0, int(p.get("cultivation_pills_used", 0) or 0)) + count
        result["cultivation"] = int(p["cultivation"])
        result["cultivationBonus"] = bonus * count
        result["cultivationPillsUsed"] = int(p["cultivation_pills_used"])
        result["remaining"] = state.item_count(item_name)
    elif kind == "pet_dan":
        result["petId"] = str(pet_for_dan.get("id", ""))
        result["petName"] = str(pet_for_dan.get("name", ""))
        result["petLevelUps"] = _apply_pet_upgrade_dan(state, pet_for_dan)
        result["petLevel"] = int(pet_for_dan.get("level", 1))
        result["learnedSkill"] = {}

    state.bump()
    if op_id:
        ops[op_id] = {"at": iso(now()), "key": key, "count": count,
                      "itemName": item_name, "result": result}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(
        consumed=count,
        duplicate=False,
        itemName=item_name,
        result=result,
        doubleExpUntil=int(p.get("double_exp_until", 0) or 0),
        doubleExpChannels=state.double_exp_channels(),
        activeChannels=state.double_exp_active_count(),
        newGold=state.gold(),
        saveData=_authority_save(state),
        saveRevision=state.revision,
    )


@route("POST", "/economy/xianmo-misc-gold")
def xianmo_misc_gold(ctx: Ctx) -> dict:
    state = ctx.state
    amount = ctx.i("amount")
    state.add_gold(amount)
    state.bump()
    return ok(newGold=state.gold())


@route("POST", "/economy/pet-refine-gold")
def pet_refine_gold(ctx: Ctx) -> dict:
    state = ctx.state
    amount = ctx.i("amount") or 100000
    if state.gold() < amount:
        return fail("金币不足")
    state.add_gold(-amount)
    state.bump()
    return ok(newGold=state.gold(), cost=amount)


@route("POST", "/events/batch")
@route("POST", "/api/events/batch")
def events_batch(ctx: Ctx) -> dict:
    """事件批处理：客户端上报一串事件，服务端逐条确认。"""
    events = ctx.body.get("events")
    results = []
    if isinstance(events, list):
        for event in events:
            results.append({"eventId": (event or {}).get("eventId", ""), "accepted": True})
    return ok(results=results, accepted=len(results))


@route("GET", "/player/buffs")
@route("GET", "/api/player/buffs")
def player_buffs(ctx: Ctx) -> dict:
    state = ctx.state
    weekday = now().isoweekday()
    days = RULES.v("weekly_buff", "days", []) or []
    active = [d for d in days if int(d.get("weekday", 0)) == weekday and d.get("enabled", True)]
    catchup = RULES.m("gameplay").get("reincarnationCatchup", {}) or {}
    return {
        "success": True,
        "weekday": weekday,
        "enabled": bool(RULES.v("weekly_buff", "enabled", True)),
        "buffs": active,
        "usage": RULES.v("weekly_buff", "usage", {}) or {},
        "double_exp_until": int(state.player.get("double_exp_until", 0) or 0),
        "newbie_exp_until": int(state.player.get("newbie_exp_until", 0) or 0),
        "catchup_enabled": bool(catchup.get("enabled", False)),
        "catchup_gap": int(catchup.get("gap", 0) or 0),
        "catchup_day": catchup.get("day", ""),
        "catchup_field_max_level_ups": int(catchup.get("field_max_level_ups", 0) or 0),
        "catchup_first_reincarnation": int(catchup.get("first_reincarnation", 0) or 0),
        "serverUnixTime": int(now().timestamp()),
    }


# ================================================================ 灵兽

def _find_pet(state, pet_id: str) -> dict | None:
    for pet in state.pets:
        if str(pet.get("id", "")) == pet_id:
            return pet
    for pet in state.pet_bag:
        if str(pet.get("id", "")) == pet_id:
            return pet
    return None


@route("POST", "/pet/lock")
def pet_lock(ctx: Ctx) -> dict:
    pet = _find_pet(ctx.state, ctx.s("petId"))
    if not pet:
        return fail("灵兽不存在")
    pet["locked"] = not bool(pet.get("locked", False))
    ctx.state.bump()
    return ok(petId=pet.get("id"), locked=pet.get("locked"))


@route("POST", "/pet/release")
def pet_release(ctx: Ctx) -> dict:
    state = ctx.state
    pet_id = ctx.s("petId")
    for bucket in (state.pets, state.pet_bag):
        for pet in list(bucket):
            if str(pet.get("id", "")) == pet_id:
                if pet.get("locked"):
                    return fail("灵兽已锁定，无法放生")
                bucket.remove(pet)
                state.add_friendship(10)
                # 放生也要记一笔消耗：客户端手里那份旧快照（切后台前、上传失败
                # 留下的）还带着这只宠，不记的话下次整包上传又被 petsData 并集
                # 塞回来 —— 和图鉴投入是同一个坑。
                state.mark_pet_consumed(pet_id)
                state.bump()
                return ok(petId=pet_id, friendshipPoints=state.friendship())
    return fail("灵兽不存在")


# ---------------------------------------------------------------- 灵兽幻化 / 进化
# 全部数值来自原版包：
#   ui/city/pet_park.gd  _MERGE_WEIGHTS_BY_TIER / _apply_merge_perfect_bonus_to_weights /
#                        _predict_merge_growth_outcomes / _predict_evolve_growth_range
#   scripts/noble_benefits.gd  MERGE_BONUS
#   scripts/pet_manager.gd     can_merge / can_evolve
MERGE_WEIGHTS_BY_VIP = (
    {"A": 5, "B": 15, "C": 30, "D": 50},
    {"A": 15, "B": 20, "C": 30, "D": 35},
    {"A": 20, "B": 30, "C": 40, "D": 10},
    {"A": 30, "B": 60, "C": 10, "D": 0},
)
NOBLE_MERGE_BONUS = (0, 2, 4, 6, 8, 9, 10, 16, 23, 30, 37, 43, 50, 57, 63, 70)
MERGE_CHARM_ITEM = "幻化符"
MERGE_CHARM_BONUS = 15
MERGE_GRADE_NAMES = {"A": "完美", "B": "优良", "C": "普通", "D": "平均"}
EVOLVE_LINEAGE_BASELINE = 6.0
EVOLVE_LINEAGE_COEFF = 0.25
EVOLVE_MAX_LEVEL = 1200
_PET_CATALOG_CACHE: dict | None = None
_EVOLUTION_CHAINS_CACHE: dict | None = None


def _pet_catalog() -> dict:
    global _PET_CATALOG_CACHE
    if _PET_CATALOG_CACHE is None:
        _PET_CATALOG_CACHE = load_json(DATA / "pet-catalog.json", {})
    return _PET_CATALOG_CACHE


def _evolution_chains() -> dict:
    global _EVOLUTION_CHAINS_CACHE
    if _EVOLUTION_CHAINS_CACHE is None:
        _EVOLUTION_CHAINS_CACHE = load_json(DATA / "evolution-chains.json", {})
    return _EVOLUTION_CHAINS_CACHE


def _resolve_evolution_origin(chains: dict, pet_id: str) -> str:
    if not pet_id:
        return ""
    for origin, data in chains.items():
        for key in ("normal", "mutant"):
            chain = data.get(key)
            if isinstance(chain, list) and pet_id in chain:
                return str(origin)
    return ""


def _pet_growth_total(pet: dict) -> float:
    growth = pet.get("growth_rates") if isinstance(pet.get("growth_rates"), dict) else {}
    try:
        return float(growth.get("attack", 0) or 0) + float(growth.get("defense", 0) or 0) + float(growth.get("agility", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _pet_growth_ratio(pet: dict) -> dict:
    ratio = pet.get("stat_ratio") if isinstance(pet.get("stat_ratio"), dict) else {}
    out = {}
    for key in ("attack", "defense", "agility"):
        try:
            out[key] = max(0.0, float(ratio.get(key, 33.33) or 33.33))
        except (TypeError, ValueError):
            out[key] = 33.33
    if sum(out.values()) <= 0.0001:
        out = {"attack": 33.33, "defense": 33.33, "agility": 33.34}
    return out


def _pet_set_growth_by_ratio(pet: dict, new_total: float) -> None:
    """把三项成长总和控制为 new_total，按 stat_ratio 重新分配（幻化用）。"""
    ratio = _pet_growth_ratio(pet)
    denom = sum(ratio.values()) or 100.0
    growth = pet.get("growth_rates") if isinstance(pet.get("growth_rates"), dict) else {}
    acc = 0.0
    keys = ("attack", "defense", "agility")
    for key in keys[:-1]:
        value = round(float(new_total) * ratio[key] / denom, 3)
        growth[key] = value
        acc += value
    # 最后一项补齐，保证三项之和精确等于 new_total
    growth[keys[-1]] = round(float(new_total) - acc, 3)
    pet["growth_rates"] = growth
    pet["total_growth"] = round(sum(float(growth[k]) for k in keys), 3)


def _pet_scale_growth_to_total(pet: dict, new_total: float) -> None:
    """按现有三项比例整体放大/缩小到 new_total（进化用，保持原有成长结构）。"""
    growth = pet.get("growth_rates") if isinstance(pet.get("growth_rates"), dict) else {}
    cur = {}
    for key in ("attack", "defense", "agility"):
        try:
            cur[key] = max(0.0, float(growth.get(key, 0) or 0))
        except (TypeError, ValueError):
            cur[key] = 0.0
    total = sum(cur.values())
    if total <= 0.0001:
        _pet_set_growth_by_ratio(pet, new_total)
        return
    scale = float(new_total) / total
    acc = 0.0
    keys = ("attack", "defense", "agility")
    for key in keys[:-1]:
        value = round(cur[key] * scale, 3)
        growth[key] = value
        acc += value
    growth[keys[-1]] = round(float(new_total) - acc, 3)
    pet["growth_rates"] = growth
    pet["total_growth"] = round(sum(float(growth[k]) for k in keys), 3)


def _merge_apply_bonus(weights: dict, bonus: int) -> dict:
    """复刻 _apply_merge_perfect_bonus_to_weights：D→C→B 依次补给 A。"""
    out = {key: max(0, int(weights.get(key, 0) or 0)) for key in ("A", "B", "C", "D")}
    remaining = max(0, int(bonus or 0))
    for bucket in ("D", "C", "B"):
        if remaining <= 0:
            break
        take = min(remaining, out[bucket])
        out[bucket] -= take
        out["A"] += take
        remaining -= take
    if out["A"] > 100:
        overflow = out["A"] - 100
        out["A"] = 100
        out["B"] = max(0, out["B"] - overflow)
    return out


def _roll_weighted_grade(weights: dict) -> str:
    total = sum(max(0, int(weights.get(k, 0) or 0)) for k in ("A", "B", "C", "D"))
    if total <= 0:
        return "D"
    roll = random.uniform(0.0, float(total))
    acc = 0.0
    for key in ("A", "B", "C", "D"):
        acc += max(0, int(weights.get(key, 0) or 0))
        if roll <= acc:
            return key
    return "D"


def _pet_save_snapshot(state) -> dict:
    """宠物类操作回包的 saveData：客户端会直接 apply_cloud_save。"""
    return {
        "playerData": state.player,
        "petsData": state.save.get("petsData", {}),
        "inventoryData": state.inv,
    }


@route("POST", "/pet/merge")
def pet_merge(ctx: Ctx) -> dict:
    """宠物幻化：复刻 pet_manager.gd can_merge + pet_park.gd 的继承公式。

    原版规则（以 recovered-godot-project 为准）：
      - 主/副宠都在队伍里、不是同一只、都 ≥300 级、同代、代数 <3、都没锁定；
      - 新代数 = max(主,副)+1（≤3），主宠回到 1 级，保留成长与技能，副宠被消耗；
      - 成长继承按三项成长（攻+防+敏）之和的 H/L：
          完美 A = H、优良 B = 0.8H+0.2L、普通 C = 0.7H+0.3L、平均 D = (主+副)/2
      - 概率权重按 VIP 档 + 贵族等级 + 幻化符（+15 完美），D→C→B 依次转给 A。
    之前这里只是给 lineage_seed_growth 加 0.05 的占位实现，幻化完全不生效。
    """
    state = ctx.state
    op_id = ctx.s("operationId")
    ops = state.meta.get("petMergeOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["petMergeOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(duplicate=True, result=memo.get("result", {}),
                  saveData=_pet_save_snapshot(state), saveRevision=state.revision)

    main_id = ctx.s("mainPetId") or ctx.s("targetPetId")
    sub_id = ctx.s("subPetId") or ctx.s("petId")
    use_charm = bool(ctx.body.get("useTransformCharm"))
    if not main_id or not sub_id:
        return fail("请选择主宠与副宠")
    if main_id == sub_id:
        return fail("不能选择同一只宠物")
    main = _find_pet(state, main_id)
    sub = _find_pet(state, sub_id)
    if not main:
        return fail("主灵兽不存在")
    if not sub:
        return fail("副灵兽不存在")

    realm = str(state.player.get("realm", "凡人") or "凡人")
    if realm not in ("凡人", "散仙", "地仙", "天仙", "大罗金仙", "幻灵天神"):
        return fail("境界要求未达到")
    for label, pet in (("主宠", main), ("副宠", sub)):
        if int(pet.get("level", 1) or 1) < 300:
            return fail("%s等级需达到300级" % label)
        if int(pet.get("generation", 0) or 0) >= 3:
            return fail("%s是3代，无法参与幻化" % label)
        if bool(pet.get("locked", False)):
            return fail("%s已锁定" % label)
    if int(main.get("generation", 0) or 0) != int(sub.get("generation", 0) or 0):
        return fail("仅允许同代宠物进行幻化")

    new_generation = max(int(main.get("generation", 0) or 0), int(sub.get("generation", 0) or 0)) + 1
    if new_generation > 3:
        return fail("幻化后代数不能超过3")

    if use_charm and state.item_count(MERGE_CHARM_ITEM) < 1:
        return fail("幻化符数量不足")

    main_total = _pet_growth_total(main)
    sub_total = _pet_growth_total(sub)
    high = max(main_total, sub_total)
    low = min(main_total, sub_total)
    outcomes = {
        "A": high,
        "B": high * 0.8 + low * 0.2,
        "C": high * 0.7 + low * 0.3,
        "D": (main_total + sub_total) / 2.0,
    }

    vip = state.vip_tier()
    weights = dict(MERGE_WEIGHTS_BY_VIP[min(vip, len(MERGE_WEIGHTS_BY_VIP) - 1)])
    try:
        noble = int(state.player.get("noble_level", 0) or 0)
    except (TypeError, ValueError):
        noble = 0
    weights = _merge_apply_bonus(weights, NOBLE_MERGE_BONUS[min(max(0, noble), len(NOBLE_MERGE_BONUS) - 1)])
    if use_charm:
        state.take_item(MERGE_CHARM_ITEM, 1)
        weights = _merge_apply_bonus(weights, MERGE_CHARM_BONUS)
    # 周常「周一 幻化完美几率 +5%」：同样走 D→C→B 补给 A，每日 20 次上限
    weekly_bonus = state.weekly_merge_bonus()
    if weekly_bonus:
        weights = _merge_apply_bonus(weights, weekly_bonus)
        state.weekly_mark_used("mergeUsed")

    grade = _roll_weighted_grade(weights)
    new_total = round(outcomes[grade], 3)

    # 主宠：成长替换为继承值（按 stat_ratio 分配到三项），代数+1，回到 1 级
    _pet_set_growth_by_ratio(main, new_total)
    main["generation"] = new_generation
    main["level"] = 1
    main["experience"] = 0
    main["lineage_seed_growth"] = round(new_total, 3)
    state._recalc_pet_stats(main)

    # 副宠被消耗
    for container in (state.pets, state.pet_bag):
        try:
            container.remove(sub)
        except ValueError:
            pass
    # 记进消耗名单：否则客户端旧快照上传时并集又把副宠带回来（喂图鉴同款坑）
    state.mark_pet_consumed(sub_id)
    if str(state.save.get("petsData", {}).get("active_pet_id", "")) == sub_id:
        state.save.setdefault("petsData", {})["active_pet_id"] = main_id

    state.bump()
    result = {
        "newPetName": str(main.get("name", "灵兽")),
        "newGrowth": new_total,
        "newGeneration": new_generation,
        "mergeType": grade,
        "mergeName": MERGE_GRADE_NAMES.get(grade, ""),
        "mainPetId": main_id,
        "subPetId": sub_id,
        "consumedCharm": use_charm,
        "inheritedSkills": main.get("skill_slots", []) if isinstance(main.get("skill_slots"), list) else [],
        "weeklyBonus": weekly_bonus,
    }
    if op_id:
        ops[op_id] = {"result": result, "at": iso(now())}
        if len(ops) > 20:
            for key in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-20]:
                ops.pop(key, None)
    return ok(result=result, saveData=_pet_save_snapshot(state), saveRevision=state.revision)


@route("POST", "/pet/evolve")
def pet_evolve(ctx: Ctx) -> dict:
    """宠物进化：按 evolution_data.gd 的进化链推进形态。

    原版（pet_manager.can_evolve / _can_evolve_special）：
      - 只有进化链上的宠物能进化，等级 ≥500，未到终极形态；
      - 进化只是换形态 + 加成长，不重置等级；
      - 成长增量 = rand(基础区间) × (1+代数×0.1) + sqrt(max(0, 血统-6))×0.25，
        区间按当前阶段与是否满级取（满级更高）。
    """
    state = ctx.state
    op_id = ctx.s("operationId")
    ops = state.meta.get("petEvolveOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["petEvolveOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        return ok(duplicate=True, pet=ops[op_id].get("pet", {}),
                  saveData=_pet_save_snapshot(state), saveRevision=state.revision)

    pet = _find_pet(state, ctx.s("petId"))
    if not pet:
        return fail("灵兽不存在")

    chains = _evolution_chains()
    origin = str(pet.get("evolution_origin", "") or "").strip()
    if not origin:
        origin = _resolve_evolution_origin(chains, str(pet.get("pet_id", "")))
    if not origin or origin not in chains:
        return fail("该宠物无法进化")

    data = chains[origin]
    # 进化变异：原版 petMutation.evolveMutateChance=1%，周常「周五 进化变异几率翻倍」×2
    pm_cfg = RULES.m("pet_mutation")
    weekly_mult = 1.0
    mutate_chance = 0.0
    if isinstance(pm_cfg, dict) and bool(pm_cfg.get("enabled", True)):
        try:
            mutate_chance = max(0.0, float(pm_cfg.get("evolveMutateChance", 0) or 0))
        except (TypeError, ValueError):
            mutate_chance = 0.0
        weekly_mult = state.weekly_evolve_mult()
        mutate_chance = min(1.0, mutate_chance * weekly_mult)
    already_mutant = bool(pet.get("is_mutant", False))
    rolled_mutant = bool(not already_mutant and mutate_chance > 0 and random.random() < mutate_chance)
    is_mutant = already_mutant or bool(ctx.body.get("useMutant")) or rolled_mutant
    chain = data.get("mutant") if is_mutant and data.get("mutant") else data.get("normal")
    if not isinstance(chain, list) or len(chain) < 2:
        return fail("进化数据无效")

    cur_id = str(pet.get("pet_id", ""))
    if cur_id in chain:
        stage = chain.index(cur_id)
    else:
        stage = max(0, min(int(pet.get("evolution_stage", 0) or 0), len(chain) - 1))
    if stage >= len(chain) - 1:
        return fail("该宠物已是终极形态")

    level = int(pet.get("level", 1) or 1)
    if level < 500:
        return fail("宠物等级需达到 500 级才能进化")

    gen = int(pet.get("generation", 0) or 0)
    is_max = level >= EVOLVE_MAX_LEVEL
    stage_now = max(0, min(2, stage))
    ranges = {
        0: ((1.5, 2.0) if is_max else (1.0, 1.5)),
        1: ((1.8, 2.5) if is_max else (1.2, 1.8)),
        2: ((2.0, 3.0) if is_max else (1.5, 2.0)),
    }
    base_min, base_max = ranges[stage_now]
    try:
        lineage = float(pet.get("lineage_seed_growth", pet.get("initial_total_growth", 3.0)) or 3.0)
    except (TypeError, ValueError):
        lineage = 3.0
    evo_bonus = (max(0.0, lineage - EVOLVE_LINEAGE_BASELINE) ** 0.5) * EVOLVE_LINEAGE_COEFF
    # 复刻 pet_park.gd _compute_evolve_lineage_bonus_for_predict：
    # 血统加成再乘 petMutation.lineageEvolve 的 baseMult × mutationMult[变异次数]。
    pm_cfg = RULES.m("pet_mutation")
    if pm_cfg.get("enabled"):
        ev = pm_cfg.get("lineageEvolve", {}) or {}
        mm = ev.get("mutationMult", {}) or {}
        try:
            mc = max(0, min(2, int(pet.get("mutation_count", 0) or 0)))
        except (TypeError, ValueError):
            mc = 0
        evo_bonus *= float(ev.get("baseMult", 1.0) or 1.0) * float(mm.get(mc, mm.get(str(mc), 1.0)) or 1.0)
    gain = random.uniform(base_min, base_max) * (1.0 + float(gen + 1) * 0.1) + evo_bonus
    new_total = round(_pet_growth_total(pet) + gain, 3)

    new_id = str(chain[stage + 1])
    cat = _pet_catalog().get(new_id) or {}
    pet["pet_id"] = new_id
    if cat:
        pet["name"] = cat.get("name") or pet.get("name")
        pet["element"] = cat.get("element") or pet.get("element")
        pet["type"] = cat.get("type") or pet.get("type")
        pet["base_stats"] = {
            "hp": cat.get("hp", 0),
            "attack": cat.get("attack", 0),
            "defense": cat.get("defense", 0),
            "agility": cat.get("agility", 0),
        }
        ratio = cat.get("ratio") or [33, 33, 34]
        pet["stat_ratio"] = {"attack": ratio[0], "defense": ratio[1], "agility": ratio[2]}
    pet["evolution_origin"] = origin
    if rolled_mutant:
        pet["mutation_count"] = max(0, int(pet.get("mutation_count", 0) or 0)) + 1
    pet["is_mutant"] = is_mutant
    pet["evolution_stage"] = stage + 1
    pet["is_evolution_pet"] = True
    pet["form"] = ("幼年", "一进", "二进", "终极")[min(3, stage + 1)]
    _pet_scale_growth_to_total(pet, new_total)
    state._recalc_pet_stats(pet)
    state.bump()

    if weekly_mult > 1.0:
        state.weekly_mark_used("evolveUsed")
    if op_id:
        ops[op_id] = {"pet": pet, "at": iso(now())}
        if len(ops) > 20:
            for key in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-20]:
                ops.pop(key, None)
    return ok(pet=pet, saveData=_pet_save_snapshot(state), saveRevision=state.revision,
              mutated=rolled_mutant, mutationCount=int(pet.get("mutation_count", 0) or 0),
              evolveChanceMult=weekly_mult, skillLearnChanceMult=weekly_mult)


@route("POST", "/pet/reincarnate")
def pet_reincarnate(ctx: Ctx) -> dict:
    state = ctx.state
    pet = _find_pet(state, ctx.s("petId"))
    if not pet:
        return fail("灵兽不存在")
    pet["reincarnation_count"] = int(pet.get("reincarnation_count", 0)) + 1
    pet["level"] = 1
    state.bump()
    return ok(pet=pet)


@route("POST", "/pet/reincarnate-inherit")
def pet_reincarnate_inherit(ctx: Ctx) -> dict:
    return pet_reincarnate(ctx)


@route("POST", "/pet/grant-starter")
def pet_grant_starter(ctx: Ctx) -> dict:
    state = ctx.state
    if state.pets:
        return fail("已有初始灵兽")
    pet = {"id": "starter_pet", "pet_id": "starter", "name": "灵兽", "level": 1, "generation": 0, "form": "幼年"}
    state.pets.append(pet)
    state.bump()
    return ok(pet=pet)


@route("POST", "/pet/refine")
def pet_refine(ctx: Ctx) -> dict:
    state = ctx.state
    pet = _find_pet(state, ctx.s("petId"))
    if not pet:
        return fail("灵兽不存在")
    pet["stat_ratio"] = round(float(pet.get("stat_ratio", 1.0) or 1.0) * 1.05, 4)
    state.bump()
    return ok(pet=pet)


@route("POST", "/pet/refine/validate")
def pet_refine_validate(ctx: Ctx) -> dict:
    return ok(valid=True, cost={"gold": 100000})


@route("POST", "/pet/bag/store")
def pet_bag_store(ctx: Ctx) -> dict:
    state = ctx.state
    pet_id = ctx.s("petId")
    for pet in list(state.pets):
        if str(pet.get("id", "")) == pet_id:
            if len(state.pet_bag) >= 60:
                return fail("灵兽袋已满")
            state.pets.remove(pet)
            state.pet_bag.append(pet)
            state.bump()
            return ok(petId=pet_id, toBag=True)
    return fail("灵兽不在身上")


@route("POST", "/pet/bag/retrieve")
def pet_bag_retrieve(ctx: Ctx) -> dict:
    state = ctx.state
    pet_id = ctx.s("petId")
    for pet in list(state.pet_bag):
        if str(pet.get("id", "")) == pet_id:
            state.pet_bag.remove(pet)
            state.pets.append(pet)
            state.bump()
            return ok(petId=pet_id, toBag=False)
    return fail("灵兽不在袋中")


@route("POST", "/pet/assist/equip")
def pet_assist_equip(ctx: Ctx) -> dict:
    state = ctx.state
    assists = state.save.setdefault("petsData", {}).setdefault("assist_pets", [])
    pet_id = ctx.s("petId")
    if pet_id and pet_id not in assists:
        assists.append(pet_id)
    state.bump()
    return ok(assistPets=assists)


@route("POST", "/pet/assist/unequip")
def pet_assist_unequip(ctx: Ctx) -> dict:
    state = ctx.state
    assists = state.save.setdefault("petsData", {}).setdefault("assist_pets", [])
    pet_id = ctx.s("petId")
    if pet_id in assists:
        assists.remove(pet_id)
    state.bump()
    return ok(assistPets=assists)


# ---------------------------------------------------------------- 宠物技能
# 客户端 scripts/network.gd 发的是 {"action": "...", "params": {...}}，
# 参数**嵌在 params 里**；而老实现只读扁平 petId/skill，于是
# ctx.s("petId") 永远是空串 → _find_pet 拿不到宠 → 玩家点技能书必弹
# 「学习失败：灵兽不存在」（2026-10-28 玩家报障）。
#
# 另外存档里的字段是 `petsData.pets[].skill_slots`
# （`{slot, skill_id, skill_key, skill_name, level}`），
# 老实现往一个根本不存在的 `pet["skills"]` 列表里塞字符串，客户端永远看不到。

def _pet_skill_defs() -> dict:
    defs = RULES.v("gameplay", "skillDefinitions", {}) or {}
    return defs if isinstance(defs, dict) else {}


def _skill_def_level(key: str, row: dict) -> int:
    try:
        level = int(row.get("level", 0) or 0)
    except (TypeError, ValueError):
        level = 0
    if level > 0:
        return level
    m = re.search(r"_lv(\d+)$", str(key or ""))
    return int(m.group(1)) if m else 0


def _skill_book_map() -> dict:
    """道具名「自复lv1技能书」→ skill_key（如 `self_heal_lv1`）。"""
    out: dict[str, str] = {}
    for key, row in _pet_skill_defs().items():
        if not isinstance(row, dict):
            continue
        cname = str(row.get("skill_name", "") or "").strip()
        level = _skill_def_level(str(key), row)
        if cname and level > 0:
            out["%slv%d技能书" % (cname, level)] = str(key)
    return out


def _pet_slots(pet: dict) -> list:
    """把 skill_slots 规整成客户端认的结构（缺字段补齐，空档补 3 格）。"""
    slots = pet.get("skill_slots")
    if not isinstance(slots, list) or not slots:
        slots = []
        pet["skill_slots"] = slots
    for idx in range(len(slots)):
        row = slots[idx]
        if not isinstance(row, dict):
            row = {}
            slots[idx] = row
        row.setdefault("slot", idx + 1)
        row.setdefault("skill_id", "")
        row.setdefault("skill_key", "")
        row.setdefault("skill_name", "")
        row.setdefault("level", 0)
    while len(slots) < 3:
        slots.append({"slot": len(slots) + 1, "skill_id": "", "skill_key": "",
                      "skill_name": "", "level": 0})
    return slots


def _slot_key(row: dict) -> str:
    key = str(row.get("skill_key", "") or "").strip()
    if key:
        return key
    sid = str(row.get("skill_id", "") or "").strip()
    level = int(row.get("level", 0) or 0)
    if sid and level > 0:
        return "%s_lv%d" % (sid, level)
    return ""


def _pet_skill_param(ctx: Ctx, *names: str, default=None):
    """从 params 里取参数，取不到再退回扁平 body（两种协议都兼容）。"""
    body = ctx.body if isinstance(ctx.body, dict) else {}
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    for name in names:
        for src in (params, body):
            if isinstance(src, dict) and name in src:
                value = src.get(name)
                if value is not None and value != "":
                    return value
    return default


def _pet_learn_book(state, pet: dict, book_name: str) -> tuple[bool, str, dict]:
    """学一本技能书。返回 (ok, message, extra)。

    规则与原版客户端 pet_manager.learn_skill_book_from_active_pet 对齐：
      * 同 key 已学 → 拒绝（否则同一本书能无限叠）；
      * 同系（skill_id 相同）已有更低阶 → 就地升阶（高阶书覆盖低阶）；
      * 同系已有更高阶 → 拒绝并提示先卸载；
      * 有空栏 → 占空栏；空栏也没有 → 拒绝。
    """
    books = _skill_book_map()
    key = books.get(book_name)
    if not key:
        return False, "未知宠物技能书：%s" % book_name, {}
    cfg = _pet_skill_defs().get(key) or {}
    sid = str(cfg.get("skill_id", "") or key.rsplit("_lv", 1)[0]).strip()
    cname = str(cfg.get("skill_name", "") or "").strip() or sid
    level = _skill_def_level(key, cfg)
    if level <= 0:
        return False, "技能配置错误", {}
    if state.item_count(book_name) <= 0:
        return False, "技能书不足", {}

    slots = _pet_slots(pet)
    for row in slots:
        if _slot_key(row) == key:
            return False, "该灵兽已经学会【%s lv%d】" % (cname, level), {}

    for row in slots:
        rk = _slot_key(row)
        if not rk:
            continue
        rcfg = _pet_skill_defs().get(rk) or {}
        rsid = str(rcfg.get("skill_id", "") or rk.rsplit("_lv", 1)[0]).strip()
        if rsid != sid:
            continue
        old_level = int(row.get("level", 0) or 0)
        if level <= old_level:
            return False, "同系已有【%slv%d】，请先卸载后再使用高阶技能书" % (cname, old_level), {}
        row.update({"skill_id": sid, "skill_key": key, "skill_name": cname, "level": level})
        return True, "【%s】提升至 lv%d" % (cname, level), {
            "upgraded": True, "slot": int(row.get("slot", 0) or 0), "level": level,
            "skillKey": key, "skillName": cname, "oldLevel": old_level,
        }

    for row in slots:
        if _slot_key(row):
            continue
        row.update({"skill_id": sid, "skill_key": key, "skill_name": cname, "level": level})
        return True, "灵兽学会了【%s lv%d】" % (cname, level), {
            "upgraded": False, "slot": int(row.get("slot", 0) or 0), "level": level,
            "skillKey": key, "skillName": cname, "oldLevel": 0,
        }
    return False, "技能栏已满（%d 格），请先卸载一个技能再用书" % len(slots), {}


def _pet_skill_book_arrays(state) -> list:
    """技能书可能躺在背包（inventoryData.items）或仓库（inventoryData.warehouse_items）。"""
    inv = state.inv
    bag = state.items
    wh = inv.get("warehouse_items")
    if not isinstance(wh, list):
        wh = []
        inv["warehouse_items"] = wh
    return [bag, wh]


def _pet_skill_book_total(state, name: str) -> int:
    total = 0
    for arr in _pet_skill_book_arrays(state):
        for it in arr:
            if not isinstance(it, dict) or str(it.get("name", "")) != name:
                continue
            try:
                total += int(it.get("count", 0) or 0)
            except (TypeError, ValueError):
                continue
    return total


def _pet_skill_book_take(state, name: str, count: int) -> bool:
    """先扣背包再扣仓库；总量不够就一本都不扣（不能让玩家「扣了一半」）。"""
    if count <= 0 or _pet_skill_book_total(state, name) < count:
        return False
    need = count
    for arr in _pet_skill_book_arrays(state):
        for it in list(arr):
            if need <= 0:
                break
            if not isinstance(it, dict) or str(it.get("name", "")) != name:
                continue
            try:
                have = int(it.get("count", 0) or 0)
            except (TypeError, ValueError):
                have = 0
            if have <= 0:
                continue
            take = min(have, need)
            it["count"] = have - take
            need -= take
            if int(it["count"]) <= 0:
                arr.remove(it)
    return need == 0


def _pet_skill_book_craft(ctx: Ctx, state, action: str, op_id: str) -> dict:
    """技能书「2合1进阶」/「拆分」。

    客户端 main_ui 背包详情页的两个按钮走这里（network.request_pet_skill_synthetic /
    request_pet_skill_split）：
        synthetic: params{book_name, book_count=2, operation_id}
        split:     params{book_name, operation_id}
    规则（与客户端 UI 文案一致）：
        合成 lvN × 2 → lv(N+1) × 1，lv5 封顶；
        拆分 lvN × 1 → lv1 × 2^(N-1)。
    这两条操作和具体灵兽无关，所以调用方必须在「查出战宠」之前分流 —— 否则客户端
    不带 petId 时会先撞「灵兽不存在」，按钮永远点不动。
    """
    book_name = str(_pet_skill_param(ctx, "book_name", "bookName", "book") or "").strip()
    books = _skill_book_map()
    key = books.get(book_name)
    if not key:
        return fail("未知技能书：%s" % (book_name or "（空）"))
    cfg = _pet_skill_defs().get(key) or {}
    sid = str(cfg.get("skill_id", "") or key.rsplit("_lv", 1)[0]).strip()
    cname = str(cfg.get("skill_name", "") or "").strip() or sid
    level = _skill_def_level(key, cfg)
    if level <= 0:
        return fail("未知技能书：%s" % book_name)

    ops = state.meta.get("petSkillOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["petSkillOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        extra = memo.get("extra") if isinstance(memo.get("extra"), dict) else {}
        return ok(message=str(memo.get("message", "操作已处理")), duplicate=True,
                  **extra, saveData=_authority_save(state), saveRevision=state.revision)

    if action == "split":
        if level < 2:
            return fail("lv1 技能书不能拆分")
        gain = 1 << (level - 1)
        lv1_name = "%slv1技能书" % cname
        if lv1_name not in books:
            return fail("该技能书没有对应的 lv1 版本")
        if not _pet_skill_book_take(state, book_name, 1):
            return fail("【%s】数量不足" % book_name)
        state.add_item(lv1_name, gain, "pet_skill_book")
        message = "消耗 1 本【%s】，拆成 %d 本【%s】" % (book_name, gain, lv1_name)
        extra = {"bookName": book_name, "targetBookName": lv1_name,
                 "gainCount": gain, "bookNameLeft": _pet_skill_book_total(state, book_name)}
    else:
        try:
            book_count = int(_pet_skill_param(ctx, "book_count", "bookCount", "count", default=2) or 2)
        except (TypeError, ValueError):
            book_count = 2
        if book_count < 2 or book_count % 2 != 0:
            return fail("合成数量必须是 2 的倍数")
        if level >= 5:
            return fail("【%s】已是最高等级（lv5），无法继续合成" % book_name)
        target_name = "%slv%d技能书" % (cname, level + 1)
        if target_name not in books:
            return fail("【%s】没有更高等级的技能书" % book_name)
        craft = book_count // 2
        if not _pet_skill_book_take(state, book_name, book_count):
            return fail("【%s】数量不足（需要 %d 本）" % (book_name, book_count))
        state.add_item(target_name, craft, "pet_skill_book")
        message = "消耗 %d 本【%s】，合成 %d 本【%s】" % (
            book_count, book_name, craft, target_name)
        extra = {"bookName": book_name, "targetBookName": target_name,
                 "gainCount": craft, "bookNameLeft": _pet_skill_book_total(state, book_name)}

    state.bump()
    if op_id:
        ops[op_id] = {"at": iso(now()), "message": message, "extra": extra}
        if len(ops) > 60:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-60]:
                ops.pop(stale, None)
    return ok(message=message, **extra,
              saveData=_authority_save(state), saveRevision=state.revision)


@route("POST", "/pet_skill/action")
def pet_skill_action(ctx: Ctx) -> dict:
    state = ctx.state
    action = str(_pet_skill_param(ctx, "action", "act", default="") or "").strip()
    pet_id = str(_pet_skill_param(ctx, "pet_id", "petId", "petInstanceId", default="") or "").strip()
    op_id = str(_pet_skill_param(ctx, "operation_id", "operationId", default="") or "").strip()

    # 技能书合成/拆分与具体灵兽无关，必须放在查宠之前分流
    if action in ("synthetic", "synthesize", "merge", "split"):
        return _pet_skill_book_craft(ctx, state, action, op_id)

    pet = _find_pet(state, pet_id) if pet_id else None
    if pet is None:
        # 老协议（learn/replace/forget + 扁平 skill）没有 pet_id，只在宠物栏里找不到
        # 特定宠时才报错；这里把 id 明确回给客户端，免得玩家只看到「灵兽不存在」。
        return fail("灵兽不存在" + ("（%s）" % pet_id if pet_id else ""))

    if action in ("sync_pet_skills", "sync"):
        slots = _pet_slots(pet)
        return ok(pet_id=str(pet.get("id", "")), skill_slots=slots,
                  saveRevision=state.revision)

    if action in ("learn_skill", "learn", "level_up_skill", "level_up", "replace"):
        book_name = str(_pet_skill_param(ctx, "book_name", "bookName", "book", default="") or "").strip()
        if not book_name:
            return fail("缺少技能书名称")
        ops = state.meta.get("petSkillOps")
        if not isinstance(ops, dict):
            ops = {}
            state.meta["petSkillOps"] = ops
        if op_id and isinstance(ops.get(op_id), dict):
            memo = ops[op_id]
            return ok(pet_id=str(pet.get("id", "")), skill_slots=_pet_slots(pet),
                      message=str(memo.get("message", "操作已处理")), duplicate=True,
                      **{k: v for k, v in memo.get("extra", {}).items()},
                      saveData=_authority_save(state), saveRevision=state.revision)

        ok_flag, message, extra = _pet_learn_book(state, pet, book_name)
        if not ok_flag:
            return fail(message)
        state.take_item(book_name, 1)
        state.bump()
        slots = _pet_slots(pet)
        if op_id:
            ops[op_id] = {"at": iso(now()), "message": message, "extra": extra}
            if len(ops) > 60:
                for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-60]:
                    ops.pop(stale, None)
        return ok(pet_id=str(pet.get("id", "")), skill_slots=slots,
                  message=message, bookName=book_name, **extra,
                  saveData=_authority_save(state), saveRevision=state.revision)

    if action in ("unlearn_skill", "unlearn", "forget", "remove"):
        slot_index = _pet_skill_param(ctx, "slot_index", "slotIndex", "slot", default=None)
        slots = _pet_slots(pet)
        row = None
        if slot_index not in (None, ""):
            try:
                want = int(slot_index)
            except (TypeError, ValueError):
                want = -1
            # 客户端 slot_index 是 0 基，存档里的 slot 字段是 1 基
            if 0 <= want < len(slots):
                row = slots[want]
            else:
                for cand in slots:
                    if int(cand.get("slot", 0) or 0) == want:
                        row = cand
                        break
        if row is None:
            return fail("技能栏位不存在")
        if str(row.get("locked", False)).lower() in ("true", "1") or bool(row.get("locked", False)):
            return fail("该技能栏已锁定，请先解锁")
        removed = str(row.get("skill_name", "") or "")
        row.update({"skill_id": "", "skill_key": "", "skill_name": "", "level": 0})
        state.bump()
        return ok(pet_id=str(pet.get("id", "")), skill_slots=slots,
                  message="已卸载【%s】" % removed if removed else "已清空该技能栏",
                  saveData=_authority_save(state), saveRevision=state.revision)

    if action in ("lock_skill_slot", "unlock_skill_slot"):
        slot_index = _pet_skill_param(ctx, "slot_index", "slotIndex", "slot", default=None)
        slots = _pet_slots(pet)
        try:
            want = int(slot_index)
        except (TypeError, ValueError):
            return fail("技能栏位不存在")
        row = slots[want] if 0 <= want < len(slots) else None
        if row is None:
            for cand in slots:
                if int(cand.get("slot", 0) or 0) == want:
                    row = cand
                    break
        if row is None:
            return fail("技能栏位不存在")
        row["locked"] = action == "lock_skill_slot"
        state.bump()
        return ok(pet_id=str(pet.get("id", "")), skill_slots=slots,
                  saveData=_authority_save(state), saveRevision=state.revision)

    # split / synthetic 已在函数开头分流（那两个操作不需要 petId）
    return fail("不支持的技能操作：%s" % (action or "（空）"))


@route("POST", "/pet/soul/commit")
def pet_soul_commit(ctx: Ctx) -> dict:
    state = ctx.state
    pet = _find_pet(state, ctx.s("petId"))
    if not pet:
        return fail("灵兽不存在")
    chance = float(RULES.v("pet_soul", "rebirthChance", 0.1) or 0.1)
    success = random.random() < chance
    if success:
        low = float(RULES.v("pet_soul", "rebirthGrowthMin", 6) or 6)
        high = float(RULES.v("pet_soul", "rebirthGrowthMax", 8) or 8)
        pet["soul_growth"] = round(random.uniform(low, high), 3)
    state.bump()
    return ok(success=success, pet=pet, growth=pet.get("soul_growth"))


@route("POST", "/pet/soul/commit-batch")
def pet_soul_commit_batch(ctx: Ctx) -> dict:
    return pet_soul_commit(ctx)


SOUL_ELEMENTS = ("金", "木", "水", "火", "土")
SOUL_ITEM_POINTS = 50


@route("POST", "/pet/soul/use-item")
def pet_soul_use_item(ctx: Ctx) -> dict:
    """魂魄道具（金/木/水/火/土魂魄）：扣 1 个，对应五行魂魄 +50 点。

    原来是「扣掉道具直接 ok()」——客户端拿不到 result.addedPoints/element
    （读的是 saveData.petCollectionData.pet_soul_data），于是提示「使用成功」但
    魂魄点数一点都不涨。
    """
    state = ctx.state
    name = ctx.s("itemName")
    op_id = ctx.s("operationId")
    element = ""
    for e in SOUL_ELEMENTS:
        if str(name).startswith(e):
            element = e
            break
    if not element:
        return fail("无效的魂魄道具")

    ops = state.meta.get("petSoulUseOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["petSoulUseOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(result=memo.get("result", {}), duplicate=True,
                  saveData=_authority_save(state), saveRevision=state.revision)

    if not state.take_item(name, 1):
        return fail("道具不足")

    pc = state.save.setdefault("petCollectionData", {})
    soul = pc.setdefault("pet_soul_data", {})
    pts = soul.get("element_points")
    if not isinstance(pts, dict):
        pts = {}
        soul["element_points"] = pts
    for k in SOUL_ELEMENTS:
        pts[k] = max(0, int(pts.get(k, 0) or 0))
    pts[element] = int(pts.get(element, 0)) + SOUL_ITEM_POINTS
    state.bump()
    result = {"element": element, "addedPoints": SOUL_ITEM_POINTS,
              "elementPoints": dict(pts), "itemName": name}
    if op_id:
        ops[op_id] = {"at": iso(now()), "result": result}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(result=result, element=element, addedPoints=SOUL_ITEM_POINTS,
              saveData=_authority_save(state), saveRevision=state.revision, newGold=state.gold())


@route("POST", "/pet/collection/invest")
def pet_collection_invest(ctx: Ctx) -> dict:
    """灵兽投入图鉴（客户端「加入图鉴 / 变异升华」按钮）。

    这里原来是个空桩 —— 只回 `{success, points:10}`，既不收走宠物也不回存档。
    客户端收到回包后自己在本地删宠、本地补图鉴进度，再整包上传，但
    `/save/upload` 的 `_merge_authoritative_progress` 会拿 `petsData.pets/pet_bag`
    **按 id 求并集**（为了保住服务端刚发的宠），服务端旧档里那只宠又原样塞回去：
    玩家喂一只、重登一次它就回到列表，喂十只回来十只。

    所以必须由服务端真正吃掉这只宠：
      * 从 pets / pet_bag 删掉，清空 active_pet_id；
      * 记进 `meta.consumedPetIds`（`/save/upload` 落库前按这个名单剔除，
        否则客户端手里任何一份旧快照上传都能让它复活）；
      * 图鉴进度 +1（普通进度 / 变异层数），并按档位累加属性加成；
      * 回 saveData，客户端不用再靠本地补记。

    入参：`petInstanceId`（必填，形如 `linlin_339830451`）、
    `speciesId` / `investKind`（可选，客户端没传就按宠物记录和实例 id 推断）。
    """
    state = ctx.state
    pet_id = ctx.s("petInstanceId") or ctx.s("petId")
    op_id = ctx.s("operationId") or ctx.s("client_op_id")
    if not pet_id:
        return fail("缺少宠物 ID")

    ops = state.meta.get("petInvestOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["petInvestOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(result=memo.get("result", {}), duplicate=True,
                  message=str(memo.get("message", "图鉴升级成功")),
                  saveData=_authority_save(state), saveRevision=state.revision)

    pet = _find_pet(state, pet_id)
    if pet is None:
        # 服务端权威存档里没有这只宠：要么是重复提交（op_id 没带或换了新的），
        # 要么干脆是伪造 id 刷图鉴。以前空桩时这里直接 +1，等于图鉴可以无限刷，
        # 所以一律拒绝 —— 客户端本地多出来的宠请重新登录同步后再操作。
        return fail("灵兽不存在，请重新登录后再试")
    species = str(pet.get("pet_id", "") or ctx.s("speciesId") or "").strip()
    if not species:
        # 实例 id 形如 `linlin_339830451`：最后一段下划线之前就是物种键
        species = pet_id.rsplit("_", 1)[0] if "_" in pet_id else pet_id
    if not species:
        return fail("无法识别灵兽种类")

    pc = state.save.setdefault("petCollectionData", {})
    prog = pc.get("progress")
    if not isinstance(prog, dict):
        prog = {}
        pc["progress"] = prog
    mprog = pc.get("mutation_progress")
    if not isinstance(mprog, dict):
        mprog = {}
        pc["mutation_progress"] = mprog
    try:
        old_count = int(prog.get(species, 0) or 0)
    except (TypeError, ValueError):
        old_count = 0
    try:
        old_mut = int(mprog.get(species, 0) or 0)
    except (TypeError, ValueError):
        old_mut = 0

    # 变异升华：普通图鉴已满 50 且宠物本身是变异品质（客户端按钮的判定口径）。
    # 客户端传了 investKind 就听它的，省得两边进度一时不同步对不上。
    mut_count = int(pet.get("mutation_count", 0) or 0)
    is_mutation = (str(ctx.s("investKind") or "") == "mutation"
                   or (mut_count >= 1 and old_count >= COLLECTION_MAX_PER_SPECIES))
    if old_count >= COLLECTION_MAX_PER_SPECIES and not is_mutation:
        # 满 50 只后原版是「直接丢弃」，但那样会白吃玩家一只宠（界面上还写着
        # 「图鉴已满」）。这里改成拒绝，宠物留在栏里，让玩家先养出变异品质。
        return fail("该灵兽图鉴已满 %d 只，需要变异品质才能继续升华"
                    % COLLECTION_MAX_PER_SPECIES)

    for bucket in (state.pets, state.pet_bag):
        for row in list(bucket):
            if str(row.get("id", "")) == pet_id:
                bucket.remove(row)
    pd = state.save.setdefault("petsData", {})
    if str(pd.get("active_pet_id", "") or "") == pet_id:
        pd["active_pet_id"] = ""
    state.mark_pet_consumed(pet_id)

    if is_mutation:
        new_count = old_count
        mprog[species] = old_mut + 1
        message = "变异升华成功"
    else:
        # 图鉴满 50 时原版直接丢弃（invest_pet_to_collection 返回 False）
        new_count = old_count + 1 if invest_pet_to_collection(state.save, species) else old_count
        message = "图鉴升级成功"

    state.bump()
    result = {
        "speciesId": species,
        "oldCount": old_count,
        "newCount": new_count,
        "oldMutationCount": old_mut,
        "mutationCount": int(mprog.get(species, 0) or 0),
        "investKind": "mutation" if is_mutation else "normal",
        "petName": str((pet or {}).get("name", "") or species),
    }
    if op_id:
        ops[op_id] = {"at": iso(now()), "result": result, "message": message}
        if len(ops) > 60:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-60]:
                ops.pop(stale, None)
    return ok(result=result, message=message, petCollectionData=pc,
              saveData=_authority_save(state), saveRevision=state.revision)


@route("POST", "/pet/max-level-learn")
def pet_max_level_learn(ctx: Ctx) -> dict:
    """灵兽满级随机领悟一个技能。

    客户端 pet_manager.queue_max_level_skill_learn_if_needed 在灵兽升到
    `max_level` 时置 `pending_skill_learn_sync` 并打这个接口，成功后弹
    「满级领悟」。老实现直接转给 pet_skill_action，既不看等级也不发技能，
    灵兽升级后永远学不到技能（而且会往不存在的 `skills` 字段里塞东西）。
    """
    state = ctx.state
    pet_id = ctx.s("petId") or ctx.s("pet_id")
    pet = _find_pet(state, pet_id)
    if pet is None:
        return fail("灵兽不存在")
    op_id = ctx.s("operationId")
    ops = state.meta.get("petMaxLevelLearnOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["petMaxLevelLearnOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(petId=str(pet.get("id", "")), learned=bool(memo.get("learned", False)),
                  learnedSkill=memo.get("learnedSkill", {}), duplicate=True,
                  saveData=_authority_save(state), saveRevision=state.revision)

    try:
        cap = int(pet.get("max_level", 0) or 0) or int(RULES.v("pet_level", "maxLevel", 1500) or 1500)
    except (TypeError, ValueError):
        cap = 1500
    level = int(pet.get("level", 1) or 1)
    if level < cap:
        return fail("灵兽未满级，无法领悟技能")
    if int(pet.get("last_max_level_skill_learn_cap", 0) or 0) >= cap:
        return fail("本级已领悟过技能")

    slots = _pet_slots(pet)
    free = [row for row in slots if not _slot_key(row)]
    if not free:
        return fail("技能栏已满，无法领悟新技能")

    known = {_slot_key(row) for row in slots if _slot_key(row)}
    pool = []
    for key, row in _pet_skill_defs().items():
        if not isinstance(row, dict):
            continue
        if not str(key).endswith("_lv1") or str(key) in known:
            continue
        pool.append(str(key))
    if not pool:
        return fail("暂无可领悟的技能")
    key = random.choice(pool)
    cfg = _pet_skill_defs().get(key) or {}
    sid = str(cfg.get("skill_id", "") or key.rsplit("_lv", 1)[0]).strip()
    cname = str(cfg.get("skill_name", "") or "").strip() or sid
    slot = free[0]
    slot.update({"skill_id": sid, "skill_key": key, "skill_name": cname, "level": 1})
    pet["last_max_level_skill_learn_cap"] = cap
    pet["pending_skill_learn_sync"] = False
    pet["pending_skill_learn_prev_level"] = 0
    state.bump()

    learned = {"skill_key": key, "skillKey": key, "skill_id": sid,
               "skill_name": cname, "skillName": cname, "level": 1,
               "slot": int(slot.get("slot", 0) or 0)}
    if op_id:
        ops[op_id] = {"at": iso(now()), "learned": True, "learnedSkill": learned}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(petId=str(pet.get("id", "")), learned=True, learnedSkill=learned,
              pet_id=str(pet.get("id", "")), skill_slots=slots,
              message="灵兽领悟了【%slv1】" % cname,
              saveData=_authority_save(state), saveRevision=state.revision)


@route("POST", "/pet/grant-medal")
def pet_grant_medal(ctx: Ctx) -> dict:
    state = ctx.state
    pet = _find_pet(state, ctx.s("petId"))
    if not pet:
        return fail("灵兽不存在")
    medals = pet.setdefault("medal_counts", {"bold": 0, "brave": 0, "calm": 0})
    key = ctx.s("medal") or "bold"
    medals[key] = int(medals.get(key, 0)) + 1
    state.bump()
    return ok(pet=pet, medals=medals)


# ================================================================ 装备（人物 / 灵兽）

def _find_equipment(state, item_id: str) -> dict | None:
    for item in state.equipment.get("inventory", []):
        if str(item.get("id", "")) == item_id:
            return item
    return None


@route("POST", "/equipment/equip")
def equipment_equip(ctx: Ctx) -> dict:
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    pool = state.equipment.setdefault("inventory", [])
    item = _find_equipment(state, item_id)
    if item is None:
        # 服务端这份装备可能在上一次紧凑存档上传时被清空过（历史 bug）。
        # 客户端穿的时候会带完整快照，这里直接补登再穿，玩家不用重开礼包。
        snapshot = ctx.body.get("equipment")
        if isinstance(snapshot, dict) and str(snapshot.get("id", "")) == item_id:
            item = dict(snapshot)
            pool.append(item)
        else:
            return fail("装备不存在")
    slot = str(item.get("slot", item.get("type", "weapon")))
    equipped = state.equipment.setdefault("equipped", {})
    old = equipped.get(slot)
    if item in pool:
        pool.remove(item)
    if old:
        pool.append(old)
    equipped[slot] = item
    state.bump()
    return ok(
        equipped=equipped,
        slot=slot,
        item=item,
        message="已装备【%s】" % str(item.get("name", "装备")),
        saveData=_authority_save(state),
        saveRevision=state.revision,
    )


@route("POST", "/equipment/unequip")
def equipment_unequip(ctx: Ctx) -> dict:
    state = ctx.state
    slot = ctx.s("slot")
    equipped = state.equipment.setdefault("equipped", {})
    item = equipped.pop(slot, None)
    if item is None:
        return fail("该部位没有装备")
    state.equipment.setdefault("inventory", []).append(item)
    state.bump()
    return ok(
        equipped=equipped,
        item=item,
        slot=slot,
        message="已卸下【%s】" % str(item.get("name", "装备")),
        saveData=_authority_save(state),
        saveRevision=state.revision,
    )


@route("POST", "/equipment/drop")
def equipment_drop(ctx: Ctx) -> dict:
    """丢弃人物装备（客户端 equipment_manager.request_drop → /equipment/drop）。

    原来这里不看 equipmentId，直接按 name/slot **造一件新装备塞进背包** ——
    点一次「丢弃」反而多一件，等于无限刷装备。现在按 id 真删。
    """
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    pool = state.equipment.setdefault("inventory", [])
    for item in list(pool):
        if isinstance(item, dict) and str(item.get("id", "")) == item_id:
            pool.remove(item)
            state.bump()
            return ok(item=item, message="已丢弃【%s】" % str(item.get("name", "装备")),
                      saveData=_authority_save(state), saveRevision=state.revision)
    # 已装备的也允许直接丢弃（客户端会带上它的 id）
    equipped = state.equipment.setdefault("equipped", {})
    for slot, item in list(equipped.items()):
        if isinstance(item, dict) and str(item.get("id", "")) == item_id:
            equipped.pop(slot, None)
            state.bump()
            return ok(item=item, slot=slot, message="已丢弃【%s】" % str(item.get("name", "装备")),
                      saveData=_authority_save(state), saveRevision=state.revision)
    return fail("装备不存在")


# 单手属性套装发放（新手锦囊 / 先锋锦囊等）
#
# 客户端 `_grant_single_attr_set_via_server` 走 `/equipment/grant`，回包必须是
# `{saveData, result:{grantedItems}, message}`：
# 它拿 `saveData` + `authoritative_equipment_inventory` 覆盖本地装备背包。
# 原来是 `equipment_drop` 的转发——不扣礼包、不给整套装备、不回 saveData，
# 于是「点了使用提示成功，礼包还在包里，装备也没进背包」。
EQUIP_GRANT_SETS = {
    "新手锦囊": {"value": 50, "prefix": "新手", "rarity": "rare"},
    "先锋锦囊": {"value": 100, "prefix": "内测先锋·", "rarity": "legendary"},
    "新人扶持锦囊": {"value": 100, "prefix": "新人扶持·", "rarity": "legendary"},
    "开荒锦囊": {"value": 400, "prefix": "开荒·", "rarity": "legendary"},
}
EQUIP_SET_SLOTS = [
    ("weapon", "武器", "attack"),
    ("armor", "衣服", "defense"),
    ("helmet", "头盔", "hp"),
    ("boots", "鞋子", "agility"),
    ("accessory", "饰品", "mp"),
]


def _make_single_attr_equipment(slot: str, label: str, stat: str, name: str, value: int, rarity: str) -> dict:
    item = {
        "id": "eq_%d_%s" % (int(random.random() * 1e9), slot),
        "name": name,
        "slot": slot,
        "rarity": rarity,
        "attack": 0, "defense": 0, "hp": 0, "mp": 0, "agility": 0,
        "cultivation_req": 0,
        "base_cultivation_req": 0,
        "forge_count": 0,
        "forge_attr_counts": {},
        "equipped": False,
        "locked": False,
        "is_suit": False,
        "is_sealed": False,
        "is_pet_equipment": False,
    }
    item[stat] = int(value)
    return item


@route("POST", "/equipment/grant")
def equipment_grant(ctx: Ctx) -> dict:
    state = ctx.state
    source = ctx.s("sourceId") or ctx.s("name") or ""
    op_id = ctx.s("operationId")
    ops = state.meta.get("equipGrantOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["equipGrantOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(duplicate=True, message=str(memo.get("message", "操作已同步")),
                  result={"grantedItems": memo.get("grantedItems", [])},
                  saveData=_authority_save(state), saveRevision=state.revision,
                  newGold=state.gold())

    cfg = EQUIP_GRANT_SETS.get(source)
    if cfg is None:
        # 表外来源不再发东西：原来这里会「按名字生成一件同名装备」，
        # 传个任意 sourceId 就能白拿装备，而且不扣任何礼包。
        return fail("未知的装备礼包：%s" % (source or "未提供 sourceId"))

    if not state.take_item(source, 1):
        return fail("背包里没有【%s】" % source)

    pool = state.equipment.setdefault("inventory", [])
    granted = []
    for slot, label, stat in EQUIP_SET_SLOTS:
        item = _make_single_attr_equipment(slot, label, stat, str(cfg["prefix"]) + label,
                                           int(cfg["value"]), str(cfg["rarity"]))
        pool.append(item)
        granted.append({"id": item["id"], "name": item["name"], "slot": slot, "stat": stat,
                        "value": int(cfg["value"])})
    state.bump()
    message = "获得一套单属性+%d装备（%d 件）" % (int(cfg["value"]), len(granted))
    if op_id:
        ops[op_id] = {"at": iso(now()), "source": source, "grantedItems": granted, "message": message}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(duplicate=False, message=message, result={"grantedItems": granted},
              saveData=_authority_save(state), saveRevision=state.revision,
              newGold=state.gold())


@route("POST", "/equipment/store-warehouse")
def equipment_store(ctx: Ctx) -> dict:
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    item = _find_equipment(state, item_id)
    if item is None:
        return fail("装备不存在")
    state.equipment["inventory"].remove(item)
    state.inv.setdefault("equipment_warehouse", []).append(item)
    state.bump()
    return ok(item=item)


@route("POST", "/equipment/retrieve-warehouse")
def equipment_retrieve(ctx: Ctx) -> dict:
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    warehouse = state.inv.setdefault("equipment_warehouse", [])
    for item in list(warehouse):
        if str(item.get("id", "")) == item_id:
            warehouse.remove(item)
            state.equipment.setdefault("inventory", []).append(item)
            state.bump()
            return ok(item=item)
    return fail("仓库里没有该装备")


@route("POST", "/equipment/dismantle-suit")
def equipment_dismantle_suit(ctx: Ctx) -> dict:
    state = ctx.state
    state.add_item("解封石", 1, "material")
    state.bump()
    return ok(items=[{"name": "解封石", "count": 1}])


@route("POST", "/equipment/unseal-suit")
def equipment_unseal_suit(ctx: Ctx) -> dict:
    state = ctx.state
    if not state.take_item("解封石", 1):
        return fail("解封石不足")
    state.bump()
    return ok(unsealed=True)


@route("POST", "/equipment/suit-convert")
def equipment_suit_convert(ctx: Ctx) -> dict:
    state = ctx.state
    if not state.take_item("套装转换石", 1):
        return fail("套装转换石不足")
    state.bump()
    return ok(converted=True)


# ---- 宠物装备 -------------------------------------------------------
#
# 客户端 `PetEquipmentManager` 的存档口径和这里以前假设的完全不是一回事：
#   * 背包里的宠装       → inventoryData.pet_equipment_items（InventoryManager 管）
#   * 已穿戴的宠装       → petEquipmentData.equipped = {slot: item}（全局一套，不分宠物）
# 老代码读写的是 `petEquipmentData.items` 这个**客户端从来不读**的幽灵字段，
# 而且要求请求里带 petId —— 客户端 `request_pet_equipment_equip` 只发
# equipmentId/slot/operationId，于是 `_find_pet(state, "")` 恒为 None，
# 每次点「穿戴」都是「灵兽不存在」。

PET_EQUIP_SLOT_ALIASES = {
    "weapon": "weapon", "pet_weapon": "weapon", "武器": "weapon",
    "armor": "armor", "pet_armor": "armor", "防具": "armor", "衣服": "armor",
    "helmet": "helmet", "pet_helmet": "helmet", "头盔": "helmet",
    "boots": "boots", "pet_boots": "boots", "鞋子": "boots",
    "accessory": "accessory", "pet_accessory": "accessory", "饰品": "accessory",
}

# 宠装五件套的权威定义（handlers_extra 的礼包发放也复用这一份，避免两处漂移）
PET_EQUIP_GRANT_SLOTS = [
    ("weapon", "开荒武器", "attack"),
    ("armor", "开荒护甲", "defense"),
    ("boots", "开荒鞋子", "agility"),
    ("helmet", "开荒血佩", "hp"),
    ("accessory", "开荒内带", "hp"),
]


def _norm_pet_equip_slot(raw) -> str:
    key = str(raw or "").strip().lower()
    return PET_EQUIP_SLOT_ALIASES.get(key, key or "weapon")


def _pet_equip_bag(state) -> list:
    """宠装背包：和客户端一致，放在 inventoryData.pet_equipment_items。"""
    return state.inv.setdefault("pet_equipment_items", [])


def _pet_equip_worn(state) -> dict:
    """已穿戴的宠装：petEquipmentData.equipped（全局一套）。"""
    return state.save.setdefault("petEquipmentData", {}).setdefault("equipped", {})


# 客户端 `PetEquipmentManager` 的 equipped 键名就是下面这套（`SLOTS` 的键）：
#   pet_weapon / pet_armor / pet_boots / pet_helmet / pet_accessory
# 而服务端老代码归一后写的是 `weapon / armor / boots / helmet / accessory`。
# 两边键名不同 → 换装时 `worn.get("accessory")` 取不到客户端上传的
# `pet_accessory` → 身上那件旧装备既不回背包也不在身上，凭空消失。
# 现在统一按客户端键名落盘，读取时再按归一化部位名兼容两种写法。
PET_EQUIP_WORN_KEYS = {
    "weapon": "pet_weapon",
    "armor": "pet_armor",
    "boots": "pet_boots",
    "helmet": "pet_helmet",
    "accessory": "pet_accessory",
}


def _pet_equip_worn_key(slot) -> str:
    canon = _norm_pet_equip_slot(slot)
    return PET_EQUIP_WORN_KEYS.get(canon, "pet_" + canon)


def _pet_equip_find_worn(worn: dict, slot):
    """按部位取已穿戴的装备（只读，兼容 pet_accessory / accessory 两种键）。"""
    canon = _norm_pet_equip_slot(slot)
    for key in worn.keys():
        if not str(key or "").strip():
            continue
        if _norm_pet_equip_slot(key) != canon:
            continue
        value = worn.get(key)
        if isinstance(value, dict) and value:
            return value
    return None


def _pet_equip_take_worn(worn: dict, slot) -> list:
    """从 equipped 里摘掉某部位的装备，并把同部位的所有别名键一起清掉。

    返回列表：正常只有 1 件；存档里同时留着 `accessory`（服务端旧写法）和
    `pet_accessory`（客户端上传）时会有 2 件，这类脏数据必须都还给玩家，
    不能只留一个键、另一件凭空蒸发。
    """
    canon = _norm_pet_equip_slot(slot)
    taken: list = []
    for key in list(worn.keys()):
        if not str(key or "").strip():
            continue
        if _norm_pet_equip_slot(key) != canon:
            continue
        value = worn.pop(key)
        if isinstance(value, dict) and value:
            taken.append(value)
    return taken


def _find_pet_equip(bag: list, item_id: str):
    for item in bag:
        if isinstance(item, dict) and str(item.get("id", "")) == item_id:
            return item
    return None


@route("POST", "/pet-equipment/equip")
def pet_equipment_equip(ctx: Ctx) -> dict:
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    bag = _pet_equip_bag(state)
    item = _find_pet_equip(bag, item_id)
    if item is None:
        # 服务端背包里没有，但客户端带了完整快照（历史存档被紧凑上传清空过）→ 补登
        snapshot = ctx.body.get("equipment")
        if isinstance(snapshot, dict) and str(snapshot.get("id", "")) == item_id:
            item = dict(snapshot)
        else:
            return fail("宠装不存在")
    else:
        bag.remove(item)
    slot = _norm_pet_equip_slot(item.get("slot") or ctx.s("slot"))
    worn = _pet_equip_worn(state)
    # 换下来的旧装备（含双键脏数据）全部回背包。
    # 同一件重复穿戴：只把它从背包里拿出来，别把副本塞回背包。
    swapped_out: list = []
    for old in _pet_equip_take_worn(worn, slot):
        if str(old.get("id", "")).strip() == str(item.get("id", "")).strip():
            continue
        old["equipped"] = False
        bag.append(old)
        swapped_out.append(old)
    # 背包里如果还有同 id 的重复条目（历史脏数据），一并清掉
    for dup in list(bag):
        if dup is not item and isinstance(dup, dict) and str(dup.get("id", "")) == str(item.get("id", "")):
            bag.remove(dup)
    item["slot"] = slot
    item["equipped"] = True
    worn[_pet_equip_worn_key(slot)] = item
    state.bump()
    # 回包 `item` 给「换下来的那件」而不是刚穿上的：
    # 客户端 `_sync_pet_equipment_from_authority()` → `_reconcile_pet_equipment_bag(
    # worn, data.item)` 的语义就是「这件不在身上了，背包里没有就补回去」。
    # 给新装备的话它会被判定成「已穿戴」直接跳过，换下来的旧装备就没人补回背包。
    # 新装备本身通过 `equipped` 已经落到槽位上，客户端不依赖 item。
    return ok(
        item=(swapped_out[0] if swapped_out else item),
        equippedItem=item,
        slot=slot,
        petId=ctx.s("petId"),
        equipped=worn,
        message="已穿戴【%s】" % str(item.get("name", "宠物装备")),
        saveData=_authority_save(state, include_pet_equip_bag=False),
        saveRevision=state.revision,
    )


@route("POST", "/pet-equipment/unequip")
def pet_equipment_unequip(ctx: Ctx) -> dict:
    state = ctx.state
    slot = _norm_pet_equip_slot(ctx.s("slot"))
    worn = _pet_equip_worn(state)
    taken = _pet_equip_take_worn(worn, slot)
    if not taken:
        return fail("该部位没有宠物装备")
    item = taken[0]
    item["equipped"] = False
    _pet_equip_bag(state).append(item)
    # 脏数据（同部位两个键）多出来的那件也还给玩家
    for extra in taken[1:]:
        extra["equipped"] = False
        _pet_equip_bag(state).append(extra)
    state.bump()
    return ok(
        item=item,
        slot=slot,
        equipped=worn,
        message="已卸下【%s】" % str(item.get("name", "宠物装备")),
        saveData=_authority_save(state, include_pet_equip_bag=False),
        saveRevision=state.revision,
    )


@route("POST", "/pet-equipment/enhance")
def pet_equipment_enhance(ctx: Ctx) -> dict:
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    worn = _pet_equip_worn(state)
    item = _find_pet_equip(_pet_equip_bag(state), item_id)
    if item is None:
        # 身上穿着的也能强化：按 id 在 equipped 里找，别用部位猜
        # （客户端强化时不一定带 slot，猜成 weapon 会拿错件）。
        for key in worn.keys():
            value = worn.get(key)
            if isinstance(value, dict) and str(value.get("id", "")) == item_id:
                item = value
                break
    if not isinstance(item, dict) or str(item.get("id", "")) != item_id:
        return fail("宠装不存在")
    cost = 100000
    if state.gold() < cost:
        return fail("金币不足")
    state.add_gold(-cost)
    level = int(item.get("enhance_level", 0) or 0) + 1
    item["enhance_level"] = level
    state.bump()
    return ok(
        level=level,
        item=item,
        newGold=state.gold(),
        message="强化成功，当前 +%d" % level,
        saveData=_authority_save(state),
        saveRevision=state.revision,
    )


@route("POST", "/pet-equipment/dismantle")
def pet_equipment_dismantle(ctx: Ctx) -> dict:
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    bag = _pet_equip_bag(state)
    item = _find_pet_equip(bag, item_id)
    if item is None:
        return fail("宠装不存在")
    bag.remove(item)
    state.add_item("宠装碎片", 1, "material")
    state.bump()
    return ok(
        items=[{"name": "宠装碎片", "count": 1}],
        message="分解成功，获得宠装碎片×1",
        saveData=_authority_save(state),
        saveRevision=state.revision,
    )


@route("POST", "/pet-equipment/synthesize-fragments")
def pet_equipment_synthesize(ctx: Ctx) -> dict:
    state = ctx.state
    if not state.take_item("宠装碎片", 10):
        return fail("碎片不足")
    bag = _pet_equip_bag(state)
    slot, _label, stat = random.choice(PET_EQUIP_GRANT_SLOTS)
    item = {
        "id": "peq_%d_syn" % int(random.random() * 1e9),
        "name": "合成·%s（宠）" % _label,
        "slot": slot,
        "rarity": "legendary",
        "attack": 0, "defense": 0, "hp": 0, "mp": 0, "agility": 0,
        "enhance_level": 0,
        "equipped": False,
        "is_pet_equipment": True,
    }
    item[stat] = 500
    bag.append(item)
    state.bump()
    return ok(
        items=[{"name": item["name"], "count": 1}],
        item=item,
        message="合成成功：获得【%s】" % item["name"],
        saveData=_authority_save(state),
        saveRevision=state.revision,
    )


@route("POST", "/suit-equipment/enhance")
def suit_enhance(ctx: Ctx) -> dict:
    state = ctx.state
    cost = 500000
    if state.gold() < cost:
        return fail("金币不足")
    state.add_gold(-cost)
    state.bump()
    return ok(level=1, newGold=state.gold(), needConfirm=True)


@route("POST", "/suit-equipment/enhance/confirm")
def suit_enhance_confirm(ctx: Ctx) -> dict:
    return ok(confirmed=True)


# ================================================================ 玩家

_ATTR_STAT_FIELD = {
    "vitality": "vitality",
    "magic": "magic",
    "attack": "attack_base",
    "defense": "defense_base",
    "agility": "agility_base",
}


@route("POST", "/player/attr/spend-points")
def attr_spend(ctx: Ctx) -> dict:
    """手动加点：和 player.gd 的 add_vitality/add_attack/... 完全一致。

    原来这里消耗的是 permanent_points（转生永久点）、加成写进 talent_*（天赋）；
    客户端点「+」走的是 points → attack_base 这套基础属性，两边根本不是一回事，
    结果就是玩家手动加点也看不到变化。
    """
    state = ctx.state
    p = state.player
    stat = (ctx.s("stat") or "attack").strip()
    field = _ATTR_STAT_FIELD.get(stat)
    if not field:
        return fail("无效的属性")
    try:
        count = max(1, int(ctx.body.get("count", 1) or 1))
    except (TypeError, ValueError):
        count = 1
    points = int(p.get("points", 0) or 0)
    if points < count:
        return fail("属性点不足")
    p["points"] = points - count
    p[field] = int(p.get(field, 0) or 0) + count
    state.sync_final_stats()
    state.bump()
    return ok(
        attr=stat,
        count=count,
        points=int(p.get("points", 0) or 0),
        attrSlice=state.attr_slice(),
        saveRevision=state.revision,
    )


# 客户端 player.gd `_auto_allocation_plan_total()` 把这五项相加当「一轮方案」，
# 并要求 1 <= 总和 <= 10，超过 10 客户端直接不分配（auto_allocate_pending_points）。
_ATTR_PLAN_KEYS = ("vitality", "attack", "defense", "magic", "agility")


def _norm_auto_plan(raw) -> dict:
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for key in _ATTR_PLAN_KEYS:
        try:
            out[key] = max(0, min(10, int(raw.get(key, 0) or 0)))
        except (TypeError, ValueError):
            out[key] = 0
    total = sum(out.values())
    if total <= 0 or total > 10:
        return {}
    return out


def _apply_attr_point(p: dict, key: str) -> None:
    """落点与客户端 player.gd 的 add_vitality/add_magic/add_attack/... 完全一致。"""
    field = {
        "vitality": "vitality",
        "magic": "magic",
        "attack": "attack_base",
        "defense": "defense_base",
        "agility": "agility_base",
    }[key]
    p[field] = int(p.get(field, 0) or 0) + 1


def _refresh_final_stats(p: dict) -> None:
    """同步存档里的 final_* 快照。

    客户端 get_attack() = 10 + attack_base + 装备/套装/心法/帮会加成，
    服务端拿不到装备明细，这里只写基础部分；客户端加载后仍会按自身逻辑重算，
    这些字段主要供排行榜/他人详情展示。
    """
    p["final_attack"] = 10 + int(p.get("attack_base", 0) or 0)
    p["final_defense"] = 10 + int(p.get("defense_base", 0) or 0)
    p["final_agility"] = 10 + int(p.get("agility_base", 0) or 0)
    p["final_max_hp"] = int(p.get("max_hp", 0) or 0) + int(p.get("vitality", 0) or 0) * 10
    p["final_max_mp"] = int(p.get("max_mp", 0) or 0) + int(p.get("magic", 0) or 0) * 10


@route("POST", "/player/attr/auto-allocate")
def attr_auto_allocate(ctx: Ctx) -> dict:
    """按玩家设置的方案消耗 pending_auto_points。

    两点必须对齐客户端：
      1. 阈值 —— main_ui `_player_attr_meets_auto_allocate_threshold()` 只在
         pending >= 100 时才发这个请求，服务端也要按同一阈值分配；
      2. 回包 —— 客户端 `_extract_attr_slice_from_rpc_data()` 只认
         `attrSlice` / `saveData.playerData`，缺了就不写内存（属性看着没变）。
    """
    state = ctx.state
    p = state.player
    if not bool(p.get("auto_allocation_enabled", False)):
        return fail("未开启自动加点")
    plan = _norm_auto_plan(p.get("auto_allocation_plan"))
    if not plan:
        return fail("加点方案无效（每轮 1~10 点）")
    total_plan = sum(plan.values())
    pending = int(p.get("pending_auto_points", 0) or 0)
    if pending <= 0:
        return fail("没有待分配属性点")
    if pending < total_plan:
        return fail("待分配点数不足一整轮方案")
    # 必须 force=True：原版「立即分配」按钮走的是 player.gd auto_allocate_pending_points()，
    # 里面**没有** AUTO_ALLOCATE_THRESHOLD(100) 检查 —— 100 点是 on_level_up_for_auto_allocate()
    # 里的自动触发阈值。之前这里走默认 force=False，pending<100 时直接返回 0，
    # 回包变成「待分配点数不足一整轮方案」，客户端把它当「同步中」静默重试 → 按钮完全没反应。
    allocated = state.auto_allocate_pending_points(force=True)
    if allocated <= 0:
        # 客户端把这两句当「同步中」静默重试（_is_player_attr_auto_allocate_sync_pending_error）
        return fail("待分配点数不足一整轮方案")
    state.bump()
    remaining = int(p.get("pending_auto_points", 0) or 0)
    cycles = allocated // total_plan if total_plan else 0
    return ok(
        allocatedPoints=allocated,
        cycles=cycles,
        points=remaining,
        pendingAutoPoints=remaining,
        attrSlice=state.attr_slice(),
        saveRevision=state.revision,
        messages=["自动分配了 %d 点属性（%d轮方案）" % (allocated, cycles)],
    )


@route("POST", "/player/attr/auto-plan")
def attr_auto_plan(ctx: Ctx) -> dict:
    """保存自动加点开关与方案。

    原实现只 return 一个固定比例，enabled/plan 都不落盘 —— 重登后
    auto_allocation_enabled 又是 false，玩家看到的就是「选了自动加点但不生效」。
    """
    state = ctx.state
    p = state.player
    enabled = bool(ctx.body.get("enabled", False))
    plan = _norm_auto_plan(ctx.body.get("plan"))

    if enabled and not plan:
        return fail("加点方案无效（每轮 1~10 点）")

    p["auto_allocation_enabled"] = enabled
    if plan:
        p["auto_allocation_plan"] = plan
    elif not isinstance(p.get("auto_allocation_plan"), dict):
        p["auto_allocation_plan"] = {}

    # 与客户端 _route_points_to_pending_auto_if_enabled() 一致：
    # 开启自动加点时把手上还没加的可用点并入待分配池。
    if enabled:
        free = int(p.get("points", 0) or 0)
        if free > 0:
            p["pending_auto_points"] = int(p.get("pending_auto_points", 0) or 0) + free
            p["points"] = 0
        # 不足 100 点不落点（原版阈值），这里只做"满额就顺手分配"的检查
        state.auto_allocate_pending_points()
    state.bump()
    return ok(
        enabled=enabled,
        plan=p.get("auto_allocation_plan", {}),
        points=int(p.get("points", 0) or 0),
        pendingAutoPoints=int(p.get("pending_auto_points", 0) or 0),
        autoAllocationEnabled=enabled,
        autoAllocationPlan=p.get("auto_allocation_plan", {}),
        attrSlice=state.attr_slice(),
        saveRevision=state.revision,
    )


@route("POST", "/player/talent/allocate")
def talent_allocate(ctx: Ctx) -> dict:
    state = ctx.state
    p = state.player
    field = "talent_%s" % (ctx.s("talent") or "attack")
    p[field] = int(p.get(field, 0)) + 1
    state.bump()
    return ok(talent=field, value=p[field])


@route("POST", "/player/talent/reset")
def talent_reset(ctx: Ctx) -> dict:
    state = ctx.state
    p = state.player
    refund = 0
    for attr in ("attack", "defense", "hp", "agility"):
        field = "talent_%s" % attr
        refund += int(p.get(field, 0))
        p[field] = 0
    p["permanent_points"] = int(p.get("permanent_points", 0)) + refund
    state.bump()
    return ok(points=p["permanent_points"], refunded=refund)


_REALMS = ("凡人", "散仙", "地仙", "天仙", "大罗金仙", "幻灵天神")

# 客户端 player.gd to_dict() 里「境界转换会改变」的字段白名单。
# 服务端照单全收（境界转换是客户端权威操作，与其它属性接口同口径），
# 白名单外的字段（金币、VIP、背包、宠物等）一律不碰，避免客户端旧快照覆盖服务端。
_REALM_TRANSITION_SNAPSHOT_KEYS = (
    "realm", "level", "experience", "points", "pending_auto_points",
    "permanent_points", "cultivation_pills_used", "reincarnation",
    "vitality", "magic", "attack_base", "defense_base", "agility_base",
    "max_hp", "max_mp", "hp", "mp",
    "attr_reset_guard_level",
)


@route("POST", "/player/realm/transition")
def realm_transition(ctx: Ctx) -> dict:
    """境界转换（飞升 ascend / 投胎 reincarnate）。

    客户端已在本地完成转换（player.gd `ascend()` / `reincarnate()`：境界推进、
    等级重置 1、经验清零、属性点重置；投胎另把转生次数 +1），然后把**转换后**
    的 playerData 快照发上来。服务端职责：落库 + 幂等 + 回权威存档。

    旧实现是占位桩：不看 transitionType、不落 playerData、一律 reincarnation+1。
    后果（2026-10-28 玩家实测）：
      1. 飞升被误加转生次数（投胎则双重 +1）；
      2. 服务端存档境界/等级不变，客户端随后的云同步 / 重登把旧等级盖回来
         → 「飞升成功成为散仙，但等级没变回 1」；
      3. 等级仍是 1000+，门槛检查继续通过 → 可以连点连升（散仙→地仙→天仙）。
    """
    state = ctx.state
    p = state.player
    transition = (ctx.s("transitionType") or "ascend").strip().lower()
    if transition not in ("ascend", "reincarnate"):
        return fail("未知的境界转换类型：%s" % (transition or "空"))

    pd = ctx.body.get("playerData")
    if not isinstance(pd, dict) or not pd:
        return fail("缺少转换后的 playerData 快照")
    realm = str(pd.get("realm", "") or "").strip()
    if realm not in _REALMS:
        return fail("未知的境界：%s" % realm)
    try:
        level = int(pd.get("level", 1))
    except (TypeError, ValueError):
        level = 1
    level = max(1, min(level, 1200))  # 客户端 GameData.MAX_LEVEL_EVOLUTION

    # ---- 幂等：同 operationId 重放直接回当前权威存档，不产生二次效果
    op_id = ctx.s("operationId")
    ops = state.meta.get("realmTransitionOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["realmTransitionOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        return ok(duplicate=True, transitionType=transition,
                  reincarnation=int(p.get("reincarnation", 0)),
                  saveData=_authority_save(state), saveRevision=state.revision)

    # ---- 采用客户端转换后的快照（白名单字段）
    for key in _REALM_TRANSITION_SNAPSHOT_KEYS:
        if key in pd:
            p[key] = pd[key]
    p["realm"] = realm
    p["level"] = level
    if transition == "reincarnate":
        # 投胎重开：塔层回 1（转生次数以客户端快照为准，客户端已 +1，不重复加）
        p["tower_floor"] = 1

    state.bump()
    if op_id:
        ops[op_id] = {"at": iso(now()), "transitionType": transition,
                      "realm": realm, "level": level}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(duplicate=False, transitionType=transition,
              reincarnation=int(p.get("reincarnation", 0)),
              saveData=_authority_save(state), saveRevision=state.revision)


@route("POST", "/player/set-current-map")
def set_current_map(ctx: Ctx) -> dict:
    """记录玩家当前所在地图。

    两个坑（原来都有）：
    1. 客户端 network.gd:2810 发的字段是 `mapName`，原来读 mapId/map，永远取到空串；
    2. 客户端恢复存档读的是 `gameSettings.current_map`，原来写进 `playerData.current_map`，
       于是每次重登都被"空 -> 默认第一张图"覆盖回去。
    """
    state = ctx.state
    map_name = (ctx.s("mapName") or ctx.s("mapId") or ctx.s("map")).strip()
    if not map_name:
        return fail("缺少地图名")

    settings = state.save.setdefault("gameSettings", {})
    prev = str(settings.get("current_map", "") or "").strip()
    settings["current_map"] = map_name
    if prev and prev != map_name:
        settings["previous_map"] = prev
    elif not str(settings.get("previous_map", "") or "").strip():
        settings["previous_map"] = map_name

    # playerData.current_map 是历史遗留字段，保持同步，避免两处取值不一致
    state.player["current_map"] = map_name

    recent = state.player.get("recent_maps")
    if isinstance(recent, list):
        cleaned = [map_name] + [str(x) for x in recent if str(x) and str(x) != map_name]
        state.player["recent_maps"] = cleaned[:10]

    state.bump()
    return ok(currentMap=map_name, previousMap=settings.get("previous_map", ""))


@route("POST", "/player/capture-settings")
def capture_settings(ctx: Ctx) -> dict:
    state = ctx.state
    state.save.setdefault("gameSettings", {})["capture_settings"] = ctx.body
    state.bump()
    return ok(settings=ctx.body)


PLAYER_SWITCH_COST_GOLD = 500000000
PLAYER_CHARACTER_MAX = 6


@route("POST", "/player/switch-character")
def switch_character(ctx: Ctx) -> dict:
    """切换人物形象（角色 0~6），消耗 5 亿金币。

    客户端 economy_player_switch_character 回包读的是
    playerCharacterId / newGold / saveData / saveRevision。
    早先的实现只往 playerData 里写了个 player_character_id 就 ok(characterId=...)：
      · 一分钱不扣 —— 界面写着「切换需消耗 5 亿金币」，实际白嫖
      · 不回 newGold/saveData —— 客户端金币栏和外形只能靠本地猜
    这里改成和别的经济接口一致：扣费 + operationId 幂等 + 回 saveData 切片。
    """
    state = ctx.state
    try:
        cid = int(ctx.body.get("characterId", -1))
    except (TypeError, ValueError):
        return fail("角色参数不合法")
    if cid < 0 or cid > PLAYER_CHARACTER_MAX:
        return fail("角色参数不合法")
    op_id = ctx.s("operationId")

    ops = state.meta.get("playerSwitchOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["playerSwitchOps"] = ops

    def current_id() -> int:
        try:
            return int(state.player.get("player_character_id", 0) or 0)
        except (TypeError, ValueError):
            return 0

    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops.get(op_id) or {}
        done_id = int(memo.get("characterId", cid))
        return ok(characterId=done_id, playerCharacterId=done_id, cost=0,
                  duplicate=True, newGold=state.gold(),
                  saveData=_authority_save(state), saveRevision=state.revision)

    if cid == current_id():
        return fail("当前已是该角色")

    cost = PLAYER_SWITCH_COST_GOLD
    if state.gold() < cost:
        return fail("金币不足，切换角色需要 %d 金币" % cost)
    state.add_gold(-cost)
    state.player["player_character_id"] = cid
    state.bump()
    if op_id:
        ops[op_id] = {"at": iso(now()), "characterId": cid, "cost": cost}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(characterId=cid, playerCharacterId=cid, cost=cost, duplicate=False,
              newGold=state.gold(), saveData=_authority_save(state),
              saveRevision=state.revision)


# ================================================================ 武学

@route("GET", "/martial/info")
def martial_info(ctx: Ctx) -> dict:
    """武道馆面板。

    客户端 network.gd 里靠 `data.has("createCount")` 识别这个接口
    （`martial_info_loaded.emit(create_count, is_today, server_success_rate, noble_bonus)`）。
    缺了 createCount 客户端就永远停在「同步今日次数中.../今日剩余 0 次」。
    """
    state = ctx.state
    data = state.save.setdefault("martialArtsData", {})
    if str(data.get("date", "")) != today():
        data["date"] = today()
        data["daily_create_count"] = 0
    limit = int(state.VIP_MARTIAL_DAILY[state.vip_tier()])
    used = int(data.get("daily_create_count", 0) or 0)
    return {
        "success": True,
        "martialArts": data.get("martial_arts", []),
        "equippedIds": data.get("equipped_ids", []),
        "createCount": used,
        "dailyLimit": limit,
        "remaining": max(0, limit - used),
        "isToday": True,
        "serverSuccessRate": float(RULES.v("martial_create", "serverSuccessRate", 0.4) or 0.4),
        "nobleBonus": 0,
        "baseSuccessRate": float(RULES.v("martial_create", "serverSuccessRate", 0.4) or 0.4),
    }


_MARTIAL_POOL: dict | None = None


def martial_pool() -> dict:
    """武学命名池（data/martial-arts.json）。

    以画法命名：凡人用常法、天仙以上用玄法，六境界各一组。
    取不到文件时回退到最小池，不能让创建接口整体挂掉。
    """
    global _MARTIAL_POOL
    if _MARTIAL_POOL is None:
        raw = load_json(DATA / "martial-arts.json", {}) or {}
        realms = raw.get("realms") if isinstance(raw, dict) else None
        books = raw.get("skill_books") if isinstance(raw, dict) else None
        _MARTIAL_POOL = {
            "realms": realms if isinstance(realms, dict) else {},
            "skill_books": books if isinstance(books, dict) else {},
        }
    return _MARTIAL_POOL


def roll_martial_art(realm: str, owned: set) -> dict:
    """按境界抽一条武学，避开该玩家已拥有的同名条目。

    客户端 `MartialArt.from_dict` 只认 name / effect_type / base_value /
    desc / layer / realm 六个键；少发 effect_type 会导致武学装上后属性加成
    恒为 0，因此这里必须整条下发。
    """
    pools = martial_pool()["realms"]
    realm_name = str(realm or "凡人")
    pool = pools.get(realm_name)
    if not pool:
        pool = pools.get("凡人") or []
    if not pool:
        return {"name": "自创武学", "effect_type": "all_stats", "base_value": 1, "desc": ""}

    fresh = [row for row in pool if str(row.get("name", "")) not in owned]
    row = dict(random.choice(fresh or pool))
    row.setdefault("realm", realm_name)
    row["layer"] = 1
    row["level"] = 1
    return row


@route("POST", "/martial/create")
def martial_create(ctx: Ctx) -> dict:
    """自创武功：每日次数按 VIP（3/4/5/6 次），每次 100 万金币，失败也扣次数与金币。"""
    state = ctx.state
    data = state.save.setdefault("martialArtsData", {})
    if str(data.get("date", "")) != today():
        data["date"] = today()
        data["daily_create_count"] = 0
    limit = int(state.VIP_MARTIAL_DAILY[state.vip_tier()])
    used = int(data.get("daily_create_count", 0) or 0)
    if used >= limit:
        return fail("今日自创次数已用完（%d/%d）" % (used, limit))
    cost = int(RULES.v("martial_create", "goldCost", 1000000) or 1000000)
    if state.gold() < cost:
        return fail("金币不足（自创武功需要 %d 金币）" % cost)
    rate = float(RULES.v("martial_create", "serverSuccessRate", 0.4) or 0.4)

    state.add_gold(-cost)
    data["daily_create_count"] = used + 1
    rolled = random.random() <= rate
    art = None
    if rolled:
        owned = {str(a.get("name", "")) for a in data.get("martial_arts", []) if isinstance(a, dict)}
        art = {"id": "martial_%d" % int(random.random() * 1e9)}
        art.update(roll_martial_art(str(state.player.get("realm", "凡人") or "凡人"), owned))
        data.setdefault("martial_arts", []).append(art)
    state.bump()
    return ok(
        createCount=int(data["daily_create_count"]),
        remaining=max(0, limit - int(data["daily_create_count"])),
        isToday=True,
        serverSuccessRate=rate,
        nobleBonus=0,
        rolledSuccess=rolled,
        martialArt=art,
        martialArts=data.get("martial_arts", []),
        newGold=state.gold(),
        cost=cost,
        saveRevision=state.revision,
    )


@route("POST", "/martial/skill_book_use")
def martial_skill_book_use(ctx: Ctx) -> dict:
    state = ctx.state
    book = ctx.s("itemName") or ctx.s("bookName")
    if book and not state.take_item(book, 1):
        return fail("技能书不足")
    data = state.save.setdefault("martialArtsData", {})
    book_key = str(ctx.s("bookName") or ctx.s("skillName") or book or "")
    books = martial_pool()["skill_books"]
    tpl = books.get(book_key)
    if tpl is None:
        # 客户端传过来的是书名，按名字反查模板；查不到就按三目标技处理
        tpl = books.get("一击三伤")
    art = {"id": "skill_%d" % int(random.random() * 1e9)}
    art.update(dict(tpl or {}))
    art["layer"] = max(1, min(9, ctx.i("layer", 1)))
    art["level"] = art["layer"]
    data.setdefault("martial_arts", []).append(art)
    state.bump()
    return ok(martialArt=art, martialArts=data["martial_arts"])


@route("POST", "/martial/forget")
def martial_forget(ctx: Ctx) -> dict:
    state = ctx.state
    data = state.save.setdefault("martialArtsData", {})
    art_id = ctx.s("martialId") or ctx.s("id")
    data["martial_arts"] = [a for a in data.get("martial_arts", []) if str(a.get("id", "")) != art_id]
    state.bump()
    return ok(martialArts=data["martial_arts"])


@route("POST", "/martial/sync-layout")
def martial_sync_layout(ctx: Ctx) -> dict:
    state = ctx.state
    data = state.save.setdefault("martialArtsData", {})
    data["equipped_ids"] = ctx.body.get("equippedIds", data.get("equipped_ids", []))
    state.bump()
    return ok(equippedIds=data["equipped_ids"])


# ================================================================ 打造 / 炼制

@route("POST", "/forge/preview")
def forge_preview(ctx: Ctx) -> dict:
    return ok(cost={"gold": 1000000}, possible=["极限装备", "封印套装"], successRate=0.3)


@route("POST", "/forge/execute")
def forge_execute(ctx: Ctx) -> dict:
    state = ctx.state
    cost = 1000000
    if state.gold() < cost:
        return fail("金币不足")
    state.add_gold(-cost)
    success = random.random() < 0.3
    items = []
    if success:
        state.add_item("极限装备", 1, "equipment")
        items.append({"name": "极限装备", "count": 1})
    state.bump()
    return ok(success=success, items=items, newGold=state.gold())


@route("POST", "/crafting/pill-craft")
def pill_craft(ctx: Ctx) -> dict:
    state = ctx.state
    name = ctx.s("pillName") or "定神丹"
    if not state.take_item("墨草", 3):
        return fail("墨草不足")
    state.add_item(name, 1, "item")
    state.bump()
    return ok(items=[{"name": name, "count": 1}])


# ================================================================ 背包 / 礼包

@route("POST", "/inventory/store-warehouse")
def inventory_store(ctx: Ctx) -> dict:
    state = ctx.state
    name = ctx.s("itemName") or ctx.s("name")
    count = max(1, ctx.i("count", 1))
    if not state.take_item(name, count):
        return fail("道具不足")
    warehouse = state.inv.setdefault("warehouse_items", [])
    for item in warehouse:
        if str(item.get("name")) == name:
            item["count"] = int(item.get("count", 0)) + count
            break
    else:
        warehouse.append({"id": "w_%d" % int(random.random() * 1e9), "name": name, "count": count,
                          "item_type": "material", "custom_data": {}})
    state.bump()
    return ok(items=[{"name": name, "count": count}])


@route("POST", "/inventory/drop")
def inventory_drop(ctx: Ctx) -> dict:
    state = ctx.state
    name = ctx.s("itemName") or ctx.s("name")
    count = max(1, ctx.i("count", 1))
    if not state.take_item(name, count):
        return fail("道具不足")
    state.bump()
    return ok()


@route("POST", "/inventory/synthesize-seal-stone")
def synthesize_seal_stone(ctx: Ctx) -> dict:
    state = ctx.state
    state.add_item("解封石", 1, "material")
    state.bump()
    return ok(items=[{"name": "解封石", "count": 1}])


@route("POST", "/gift/frontier/use")
def gift_frontier_use(ctx: Ctx) -> dict:
    """开荒锦囊：5 亿金币 + 7 只成长 6.0 的赤翅蛾 + 一套单属性 +400 装备。

    原实现是往背包塞一个「边境礼包」的占位桩：客户端开了礼包只拿到一个查不到
    图鉴的道具，金币/宠物/装备全没有，开荒锦囊本身也不扣（「使用成功但礼包还在」）。
    """
    state = ctx.state
    op_id = ctx.s("operationId")
    ops = state.meta.get("frontierGiftOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["frontierGiftOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(duplicate=True, message=str(memo.get("message", "操作已同步")),
                  result=memo.get("result", {}),
                  saveData=_authority_save(state), saveRevision=state.revision,
                  newGold=state.gold())

    if state.item_count("开荒锦囊") <= 0:
        return fail("背包里没有【开荒锦囊】")
    state.take_item("开荒锦囊", 1)

    bag: dict = {"gold": 0, "pets": [], "items": [], "lines": []}
    bag["gold"] = 500000000
    bag["lines"].append("获得 5 亿金币")
    _grant_pets_by_spec(state, {"pet_id": "linlin", "growth": (6.0, 6.0)}, bag, 7)
    for slot, label, stat in EQUIP_SET_SLOTS:
        item = _make_single_attr_equipment(slot, label, stat, "开荒·" + label, 400, "legendary")
        state.equipment.setdefault("inventory", []).append(item)
        bag["items"].append({"id": item["id"], "name": item["name"], "slot": slot})
        bag["lines"].append("获得【%s】" % item["name"])
    if bag["gold"] > 0:
        state.add_gold(int(bag["gold"]))

    mailed = [{"name": p.get("name", ""), "growth": p.get("growth", 0)} for p in bag["pets"] if p.get("mailed")]
    result = {
        "grantedItems": bag["items"],
        "grantedPets": [{"name": p.get("name", ""), "growth": p.get("growth", 0)} for p in bag["pets"]],
        "mailedPets": mailed,
        "grantedGold": bag["gold"],
    }
    message = "\n".join(bag["lines"])
    state.bump()
    if op_id:
        ops[op_id] = {"at": iso(now()), "result": result, "message": message}
        if len(ops) > 20:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-20]:
                ops.pop(stale, None)
    return ok(duplicate=False, message=message, result=result,
              saveData=_authority_save(state), saveRevision=state.revision,
              newGold=state.gold())


# ================================================================ 宠物礼包 / 宠物蛋
#
# 客户端 `/gift/open-pet`（network.gd request_pet_gift_open）和
# `/pet/open-egg`（request_pet_egg_open）的契约完全一样：
#   { result: {grantedPets:[{name,growth,mutation_count,mailed}], grantedItems, grantedGold},
#     saveData, message, duplicate }
# 客户端 `_await_server_auth_pet_reward_open` 靠 saveData 里的 petsData 入包
# （apply_granted_pets_from_reward_result 是空实现，别指望它补宠物），
# 所以**宠物必须真写进 state.save["petsData"]**，否则礼包开了、宠物没进列表。

# 客户端把这两个礼包走 `/gift/open-pet`
# （inventory_manager.gd `_request_server_auth_pet_reward_open("gift", name)`），
# **不是** `/equipment/grant`。原来 open-pet 只认宠物礼包 / 掉率表 / 天关锦囊，
# 这两个名字一律落进「该礼包暂不支持开启」—— 玩家拿到礼包点了完全没反应。
# desc：1 亿金币 + 1 只成长 6.0 赤翅蛾 + 一套单属性 +100 人物装备。
GIFT_EQUIP_PACKS = {
    "先锋锦囊": {"gold": 100000000, "pets": 1, "pet_id": "linlin", "growth": 6.0},
    "新人扶持锦囊": {"gold": 100000000, "pets": 1, "pet_id": "linlin", "growth": 6.0},
}

# 注意：不要和上面 `_PET_CATALOG_CACHE` 共用同一个全局变量。
# 上面那个读 data/pet-catalog.json（288 条），这个读 data/pets.json（337 条，含进化链），
# 共用一个变量会出现「谁先调用谁决定内容」的串表问题。
_PETS_ALL_CACHE: dict | None = None
# 主战栏 10 格：必须和客户端 GameData.MAX_PETS（10）一致。
# 原来是 20，比客户端多一倍 —— 第 11~20 只客户端 UI 根本不渲染，
# 玩家看到的只是「领了礼包但没见到宠」，而野外捕获又按 10 判满，两套口径打架。
MAX_PETS_HELD = 10
# 必须和客户端 GameData.MAX_PET_BAG（50）一致。
# 以前这里是 60，野外自动捕捉又完全不查上限，宠物袋能被塞到 55/50 这种
# 「显示超编」的状态，玩家看着就是「莫名多了一堆宠」。
MAX_PETS_BAG = 50


def _pet_catalog_all() -> dict:
    global _PETS_ALL_CACHE
    if _PETS_ALL_CACHE is None:
        raw = load_json(DATA / "pets.json", {})
        pets = raw.get("pets") if isinstance(raw, dict) else None
        _PETS_ALL_CACHE = pets if isinstance(pets, dict) else {}
    return _PETS_ALL_CACHE


def _pet_pool(kind: str, element: str = "") -> list:
    """按礼包类型挑宠物池。

    `evolution` 只取一阶（幼年）形态：高阶形态由进化系统获得，
    礼包/蛋直接开二进/终极会让「进化」玩法失效。
    """
    rows = []
    for pid, data in _pet_catalog_all().items():
        if not isinstance(data, dict):
            continue
        is_evo = bool(data.get("is_evolution_pet", False))
        if kind == "evolution":
            if not is_evo or int(data.get("evolution_stage", 0) or 0) != 0:
                continue
        elif kind == "non_evolution":
            if is_evo:
                continue
        if element and str(data.get("element", "")) != element:
            continue
        rows.append((str(pid), data))
    return rows


def _hp_growth_for_catalog(data: dict, growth: float) -> float:
    """物种 hp_growth × (本次总成长 / 物种基础成长)。缺数据时退回总成长。"""
    if not isinstance(data, dict):
        return growth
    species_hp = float(data.get("hp_growth", 0) or 0)
    species_total = float(data.get("growth", 0) or 0)
    if species_hp <= 0 or species_total <= 0:
        return growth
    ratio = max(0.05, min(100.0, float(growth) / species_total))
    return round(species_hp * ratio, 3)


def _make_pet_from_catalog(state, pet_id: str, data: dict, growth: float, mutation: int = 0) -> dict:
    """按 pet.gd `create_from_data` + pet_manager `create_pet_from_tower` 造一只 1 级宠。

    - growth_rates 按 pet_data.ratio 分 attack/defense/agility 三段，hp 通道取整份 growth；
    - total_growth = growth（原版就是「总量」，不是三项相加）；
    - 变异只体现在 is_mutant/mutation_count 与 final_* 倍率上（_recalc_pet_stats 会沿用倍率）。
    """
    ratio = data.get("ratio") if isinstance(data.get("ratio"), list) else [33, 33, 34]
    r_atk = float(ratio[0]) if len(ratio) > 0 else 33.0
    r_def = float(ratio[1]) if len(ratio) > 1 else 33.0
    r_agi = float(ratio[2]) if len(ratio) > 2 else 34.0
    growth = round(max(0.1, float(growth)), 3)
    is_evo = bool(data.get("is_evolution_pet", False))
    base = {
        "hp": float(data.get("hp", 30.0) or 30.0),
        "attack": float(data.get("attack", 5.0) or 5.0),
        "defense": float(data.get("defense", 5.0) or 5.0),
        "agility": float(data.get("agility", 5.0) or 5.0),
    }
    pet = {
        "id": "%s_%d" % (pet_id, int(random.random() * 1e9)),
        "pet_id": pet_id,
        "name": str(data.get("name", pet_id)),
        "level": 1,
        "experience": 0,
        "generation": 0,
        "locked": False,
        "is_elite": False,
        "is_mutant": bool(int(mutation) > 0),
        "mutation_count": int(mutation),
        "element": str(data.get("element", "")),
        "type": str(data.get("type", "")),
        "form": str(data.get("form", "幼年")),
        "max_level": int(data.get("max_level", 1500) or 1500),
        "capture_map": "gift",
        "base_stats": base,
        "stat_ratio": {"attack": r_atk, "defense": r_def, "agility": r_agi},
        "growth_rates": {
            "attack": round(growth * r_atk / 100.0, 3),
            "defense": round(growth * r_def / 100.0, 3),
            "agility": round(growth * r_agi / 100.0, 3),
            # 生命成长按物种 hp_growth（原版口径），不再拿总成长顶替
            "hp": _hp_growth_for_catalog(data, growth),
        },
        "total_growth": growth,
        "lineage_seed_growth": growth,
        "is_evolution_pet": is_evo,
        "evolution_origin": str(data.get("evolution_origin", "")),
        "evolution_stage": int(data.get("evolution_stage", 0) or 0),
        "skill_slots": [
            {"slot": 1, "level": 0, "skill_id": "", "skill_key": "", "skill_name": ""},
            {"slot": 2, "level": 0, "skill_id": "", "skill_key": "", "skill_name": ""},
            {"slot": 3, "level": 0, "skill_id": "", "skill_key": "", "skill_name": ""},
        ],
        "medal_counts": {"bold": 0, "calm": 0, "brave": 0},
        "reincarnation_count": 0,
        "pet_raise_race_used": False,
        "pending_medal_sync": False,
        "pending_medal_sync_reason": "",
        "pending_skill_learn_sync": False,
        "pending_skill_learn_prev_level": 0,
        "last_max_level_medal_cap": 0,
        "last_max_level_skill_learn_cap": 0,
    }
    state._recalc_pet_stats(pet)
    return pet


def _add_pet_to_state(state, pet: dict) -> str:
    """入包：宠物栏 → 灵兽袋 → 邮件。返回值对应客户端的 mailed 标记。"""
    pets = state.pets
    if len(pets) < MAX_PETS_HELD:
        pets.append(pet)
        return "pets"
    bag = state.pet_bag
    if len(bag) < MAX_PETS_BAG:
        bag.append(pet)
        return "bag"
    state.send_mail(
        "宠物礼包补发",
        "宠物栏已满，为您补发【%s】（成长%.3f）" % (pet.get("name", "灵兽"), float(pet.get("total_growth", 0) or 0)),
        attachments={"pets": [pet]},
    )
    return "mail"


# ---------------------------------------------------------------- 礼包掉率表
# data/gift-rewards.json 是原版掉率表的逐条抄录。
# 客户端 inventory_manager.gd `SERVER_AUTH_PET_GIFT_NAMES`（74 个名字）走
# /gift/open-pet，原来服务端只认「天关锦囊」「N关礼包」和 _pet_reward_spec
# 的正则族，塔1~5 / 生死塔参与 / 兄弟天关 / 世界boss / 冠军·亚军·季军·十强
# 这 12 个一律 fail「该礼包暂不支持开启」—— 玩家拿到礼包也开不了。
PET_OPEN_BATCH_MAX = 20
_GIFT_REWARDS_CACHE: dict | None = None


def _gift_rewards() -> dict:
    global _GIFT_REWARDS_CACHE
    if _GIFT_REWARDS_CACHE is None:
        raw = load_json(DATA / "gift-rewards.json", {})
        _GIFT_REWARDS_CACHE = raw if isinstance(raw, dict) else {}
    return _GIFT_REWARDS_CACHE


def _gold_from_name(name: str) -> int:
    """「1亿金币」/「金币30亿」/「5,000,000,000金币」→ 金币数。

    塔表写「1亿金币」、世界boss 表写「金币1亿」、竞技场写「5,000,000,000金币」，
    三种写法都要认：先查 gold_unit，再退化成去掉「金币」后解析亿/万。
    """
    text = str(name or "").strip()
    if not text:
        return 0
    unit = _gift_rewards().get("gold_unit")
    if isinstance(unit, dict) and text in unit:
        try:
            return int(unit[text])
        except (TypeError, ValueError):
            return 0
    body = text.replace(",", "").replace("，", "").replace("金币", "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?)(亿|万)?$", body)
    if not m:
        return 0
    value = float(m.group(1))
    suffix = m.group(2) or ""
    if suffix == "亿":
        value *= 100000000
    elif suffix == "万":
        value *= 10000
    return int(round(value))


def _skill_book_name(skill_key: str) -> str:
    """skill_key → 道具名（`splash_lv1` →「溅墨lv1技能书」）。

    中文名只存在于 runtime-config gameplay.skillDefinitions，现查不写死；
    查不到返回空串，由调用方兜底。
    """
    key = str(skill_key or "").strip()
    if not key:
        return ""
    defs = RULES.v("gameplay", "skillDefinitions", {}) or {}
    row = defs.get(key) if isinstance(defs, dict) else None
    if not isinstance(row, dict):
        return ""
    cname = str(row.get("skill_name", "") or "")
    level = int(row.get("level", 0) or 0)
    if not cname or level <= 0:
        return ""
    return "%slv%d技能书" % (cname, level)


def _lv1_pet_skill_books() -> list:
    """全部 lv1 宠物技能书道具名（世界首领锦囊「lv1宠物技能书之一」）。"""
    defs = RULES.v("gameplay", "skillDefinitions", {}) or {}
    out = []
    for key in (defs.keys() if isinstance(defs, dict) else []):
        if not str(key).endswith("_lv1"):
            continue
        name = _skill_book_name(key)
        if name:
            out.append(name)
    return out


def _roll_weighted_row(rows: list) -> dict | None:
    """按 weight 加权抽 1 条（weight 不必凑满 100）。"""
    clean: list = []
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            weight = float(row.get("weight", 0) or 0)
        except (TypeError, ValueError):
            weight = 0.0
        if weight <= 0:
            continue
        clean.append((weight, row))
        total += weight
    if total <= 0:
        return None
    roll = random.random() * total
    acc = 0.0
    for weight, row in clean:
        acc += weight
        if roll <= acc:
            return row
    return clean[-1][1]


def _gift_pool_pick(pool: str) -> dict | None:
    """礼包里的「之一」类奖励 → 具体产出。

    返回 `{"kind":"item","name":...,"meta":...}` 或
         `{"kind":"pet","spec":{...}}`。
    池子全部来自 gift-rewards.json（原版 pool 定义）。
    """
    data = _gift_rewards()
    if pool == "element_mutant_egg":
        eggs = data.get("element_mutant_egg_pool") or []
        if not eggs:
            return None
        return {"kind": "item", "name": str(random.choice(eggs)), "meta": {}}
    if pool == "limit_equip":
        names = data.get("limit_equip_pool") or list(LIMIT_EQUIP_NAMES)
        if not names:
            return None
        # 客户端 from_dict 认 item_type == "limit_equip" → material + is_limit_equip
        return {"kind": "item", "name": str(random.choice(names)),
                "meta": {"is_limit_equip": True}}
    if pool == "pet_skill_book_lv1":
        books = _lv1_pet_skill_books()
        if not books:
            return None
        return {"kind": "item", "name": str(random.choice(books)), "meta": {}}
    if pool == "world_boss_pet":
        ids = [pid for pid in (data.get("world_boss_pet_pool") or [])
               if pid in _pet_catalog_all()]
        if not ids:
            return None
        lo = float(RULES.v("worldBoss", "petGrantGrowthMin", 5.0) or 5.0)
        hi = float(RULES.v("worldBoss", "petGrantGrowthMax", 6.0) or 6.0)
        if hi < lo:
            lo, hi = hi, lo
        return {"kind": "pet",
                "spec": {"pool": "fixed", "pet_id": str(random.choice(ids)),
                         "growth": (lo, hi), "mutation": 0}}
    return None


def _grant_pets_by_spec(state, spec: dict, bag: dict, times: int = 1) -> bool:
    """按 spec 发 `times` 只宠，入宠物栏 → 灵兽袋 → 邮件。"""
    granted = False
    for _ in range(max(1, int(times))):
        rolled = _roll_pet_for_spec(spec)
        if rolled is None:
            continue
        pet = _make_pet_from_catalog(state, rolled["pet_id"], rolled["data"],
                                     rolled["growth"], rolled["mutation"])
        where = _add_pet_to_state(state, pet)
        bag["pets"].append({
            "name": pet["name"], "growth": pet["total_growth"],
            "mutation_count": pet["mutation_count"],
            "mailed": where == "mail",
        })
        bag["lines"].append("获得【%s】（成长%.3f）%s" % (
            pet["name"], pet["total_growth"],
            "，宠物栏已满，已发送到邮件" if where == "mail" else ""))
        granted = True
    return granted


def _grant_gift_row(state, row: dict, bag: dict, times: int = 1) -> bool:
    """按掉率表里的一行发奖。`times` 是「这一行被抽中几次」。"""
    if not isinstance(row, dict) or times <= 0:
        return False
    rtype = str(row.get("type", "item") or "item")
    name = str(row.get("name", "") or "")
    try:
        unit = max(1, int(row.get("count", 1) or 1))
    except (TypeError, ValueError):
        unit = 1
    count = unit * times

    if rtype == "gold":
        amount = _gold_from_name(name) * times
        if amount <= 0:
            return False
        bag["gold"] += amount
        bag["lines"].append("获得金币 %d" % amount)
        return True

    if rtype == "pool":
        for _ in range(times):
            picked = _gift_pool_pick(str(row.get("pool", "") or ""))
            if picked is None:
                return False
            if picked["kind"] == "pet":
                _grant_pets_by_spec(state, picked["spec"], bag, 1)
            else:
                state.add_item(picked["name"], unit, "gift", picked.get("meta") or None)
                bag["items"].append({"name": picked["name"], "count": unit})
                bag["lines"].append("获得【%s】×%d" % (picked["name"], unit))
        return True

    if rtype == "pet_skill_book":
        book = _skill_book_name(str(row.get("skill_key", ""))) or name
        if not book:
            return False
        state.add_item(book, count, "pet_skill_book")
        bag["items"].append({"name": book, "count": count})
        bag["lines"].append("获得【%s】×%d" % (book, count))
        return True

    if not name:
        return False
    item_type = "martial_skill_book" if rtype == "martial" else "gift"
    state.add_item(name, count, item_type)
    bag["items"].append({"name": name, "count": count})
    bag["lines"].append("获得【%s】×%d" % (name, count))
    return True


def _gift_table_kind(item_name: str) -> str:
    """礼包是否在掉率表里：`fixed`（固定打包）/ `random`（加权抽 1）。"""
    data = _gift_rewards()
    fixed = data.get("fixed_gifts")
    if isinstance(fixed, dict) and isinstance(fixed.get(item_name), dict):
        return "fixed"
    gifts = data.get("gifts")
    rows = gifts.get(item_name) if isinstance(gifts, dict) else None
    return "random" if isinstance(rows, list) and rows else ""


def _grant_gift_table(state, item_name: str, count: int, kind: str, bag: dict) -> None:
    data = _gift_rewards()
    if kind == "fixed":
        cfg = data.get("fixed_gifts", {}).get(item_name, {})
        gold = int(cfg.get("gold", 0) or 0) * count
        if gold > 0:
            bag["gold"] += gold
            bag["lines"].append("获得金币 %d" % gold)
        for row in (cfg.get("rewards") or []):
            if isinstance(row, dict):
                _grant_gift_row(state, row, bag, count)
        return
    rows = data.get("gifts", {}).get(item_name) or []
    for _ in range(count):
        picked = _roll_weighted_row(rows)
        if picked is not None:
            _grant_gift_row(state, picked, bag, 1)


def _pet_reward_spec(item_name: str) -> dict | None:
    """礼包/蛋 → 成长区间 + 宠物池。

    数值来源（原版 `runtime-config.json petOpenItems` 45 条 desc，
    `inventory_manager.gd _open_pet_egg`，`game_data.gd` 道具 desc）：

      - 普通蛋 5.0~6.0；五行属性宠物蛋 4.0~7.0（`_open_pet_egg`）；
      - `金/木/水/火/土纹异蛋` 6.0~7.9，1 级 0 代 变异 1 本属性；
      - `五阶宠物锦囊` 5.0~5.9，但 `五阶宠物蛋` 是 5.0~6.0（两个 desc 不一样，别合并）；
      - `N+可进化宠礼包` N.0~N.9（12+ / 13+ 是 N.0~N.99）；
      - `N+不可进化宠礼包` N.0~N.5；
      - `N+可进化宠礼包(可能变异)` N.0~N.9，约 10% 变异 1；
      - `N+不可进化宠礼包(可能变异)` N.0~**N.9**（比不可变异宽），约 10% 变异 1；
      - `10-13变异可进化/不可进化宠礼包` 10.0~13.9，变异 1；
      - `每日变异宠物蛋` 6.0~7.0，变异 1。

    变异率放在 spec 的 `mutation_rate` 里，由 `_roll_pet_for_spec` 逐只 roll ——
    不能在这里 roll：批量开 20 个时在这里 roll 会让 20 只共用同一个结果。
    """
    name = str(item_name or "").strip()
    if not name:
        return None

    # 1) gift-rewards.json 里显式登记的礼包（仙魔四件套 / 7-9 固定宠 / 10-13 变异）
    table = _gift_rewards().get("pet_gifts")
    row = table.get(name) if isinstance(table, dict) else None
    if isinstance(row, dict):
        growth = row.get("growth") or [5.0, 6.0]
        spec = {
            "growth": (float(growth[0]), float(growth[-1])),
            "pool": str(row.get("pool", "any") or "any"),
            "mutation": int(row.get("mutation", 0) or 0),
        }
        if row.get("pet_id"):
            spec["pet_id"] = str(row["pet_id"])
        return spec

    m = re.match(r"^(金|木|水|火|土)(变异)?宠物蛋$", name)
    if m:
        element, mutated = m.group(1), bool(m.group(2))
        return {"growth": (6.0, 7.9) if mutated else (4.0, 7.0),
                "pool": "any", "element": element,
                "mutation": 1 if mutated else 0}
    # `金纹蛋` 这一族（不带「变异」二字）也是 open-egg 路由，成长 4.0~7.0
    m = re.match(r"^(金|木|水|火|土)属性宠物蛋$", name)
    if m:
        return {"growth": (4.0, 7.0), "pool": "any",
                "element": m.group(1), "mutation": 0}
    if name == "五行属性宠物蛋":
        return {"growth": (4.0, 7.0), "pool": "any", "random_element": True}
    if name == "每日变异宠物蛋":
        return {"growth": (6.0, 7.0), "pool": "any", "mutation": 1}
    if name == "五阶宠物锦囊":
        return {"growth": (5.0, 5.9), "pool": "any"}
    if name == "五阶宠物蛋":
        return {"growth": (5.0, 6.0), "pool": "any"}
    m = re.match(r"^(\d+)\+可进化宠礼包(\(可能变异\))?$", name)
    if m:
        base = float(m.group(1))
        # 12+ / 13+ 的 desc 是 N.0~N.99，其余是 N.0~N.9
        upper = base + (0.99 if base >= 12 else 0.9)
        spec = {"growth": (base, upper), "pool": "evolution"}
        if m.group(2):
            spec["mutation_rate"] = 0.10
        return spec
    m = re.match(r"^(\d+)\+不可进化宠礼包(\(可能变异\))?$", name)
    if m:
        base = float(m.group(1))
        if m.group(2):
            return {"growth": (base, base + 0.9), "pool": "non_evolution",
                    "mutation_rate": 0.10}
        return {"growth": (base, base + 0.5), "pool": "non_evolution"}
    m = re.match(r"^(\d+)-(\d+)变异(可进化|不可进化)宠礼包$", name)
    if m:
        pool = "evolution" if m.group(3) == "可进化" else "non_evolution"
        return {"growth": (float(m.group(1)), float(m.group(2)) + 0.9),
                "pool": pool, "mutation": 1}
    if name == "离线宠物锦囊":
        # 原版 desc：「打开可获得一只极品宠物，宠物来自离线挂机地图」
        # 成长取普通捕捉区间（宠物池由离线地图决定，客户端另行处理）。
        return {"growth": (5.0, 6.0), "pool": "any"}
    return None


def _roll_pet_for_spec(spec: dict) -> dict | None:
    fixed_id = str(spec.get("pet_id", "") or "")
    if fixed_id:
        data = _pet_catalog_all().get(fixed_id)
        if not isinstance(data, dict):
            return None
        pid, data = fixed_id, data
    else:
        element = str(spec.get("element", "") or "")
        if spec.get("random_element"):
            element = random.choice(("金", "木", "水", "火", "土"))
        pool = _pet_pool(str(spec.get("pool", "any")), element)
        if not pool:
            pool = _pet_pool("any")
        if not pool:
            return None
        pid, data = random.choice(pool)
    lo, hi = spec.get("growth", (5.0, 6.0))
    growth = round(random.uniform(float(lo), float(hi)), 3)
    mutation = int(spec.get("mutation", 0) or 0)
    rate = float(spec.get("mutation_rate", 0.0) or 0.0)
    if rate > 0 and random.random() < rate:
        mutation = max(1, mutation)
    return {"pet_id": pid, "data": data, "growth": growth, "mutation": mutation}


def _open_pet_reward(ctx: Ctx, default_kind: str = "gift") -> dict:
    """宠物礼包 / 宠物蛋统一开箱（服务端权威）。"""
    state = ctx.state
    item_name = (ctx.s("giftName") or ctx.s("itemName") or ctx.s("eggName")).strip()
    op_id = ctx.s("operationId")
    try:
        count = max(1, int(ctx.body.get("batchCount", ctx.body.get("count", 1)) or 1))
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, PET_OPEN_BATCH_MAX))

    ops = state.meta.get("petOpenOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["petOpenOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        out = ok(duplicate=True, result=memo.get("result", {}),
                 message=memo.get("message", ""),
                 summaryLines=memo.get("summaryLines", []),
                 saveData=_authority_save(state), saveRevision=state.revision)
        return out

    if not item_name:
        return fail("未指定要开启的礼包")

    # 礼包本体必须在背包里（客户端已扣过一次本地数量时，服务端仍是权威）
    owned = state.item_count(item_name)
    if owned <= 0:
        return fail("背包里没有【%s】" % item_name)
    count = min(count, owned)

    bag: dict = {"gold": 0, "pets": [], "items": [], "lines": []}
    handled = False

    # ---- 天关锦囊：runtime-config tower.towerGift = 100 万金币 + 5% 额外 五阶宠物锦囊
    if item_name == "天关锦囊":
        cfg = RULES.v("tower", "towerGift", {}) or {}
        unit_gold = int(cfg.get("goldAmount", 1000000) or 1000000)
        extra_rate = float(cfg.get("extraPetGiftRate", 0.0) or 0.0)
        extra_name = str(cfg.get("extraPetGiftName", "") or "")
        state.take_item(item_name, count)
        bag["gold"] += unit_gold * count
        bag["lines"].append("开启天关锦囊 x%d，获得金币 %d" % (count, unit_gold * count))
        for _ in range(count):
            if extra_name and random.random() < extra_rate:
                state.add_item(extra_name, 1, "gift")
                bag["items"].append({"name": extra_name, "count": 1})
                bag["lines"].append("获得【%s】" % extra_name)
        handled = True
    # ---- N关礼包：runtime-config tower.floorGiftRewards 掉落表（宠物直接发放 / 蛋落背包）
    elif item_name.endswith("关礼包") and item_name != "天关锦囊" and item_name[:-3].isdigit():
        table = RULES.v("tower", "floorGiftRewards", []) or []
        table = [row for row in table if isinstance(row, dict) and float(row.get("rate", 0) or 0) > 0]
        state.take_item(item_name, count)
        for _ in range(count):
            if not table:
                break
            roll = random.random()
            acc = 0.0
            picked = table[-1]
            for row in table:
                acc += float(row.get("rate", 0) or 0)
                if roll <= acc:
                    picked = row
                    break
            picked_name = str(picked.get("name", ""))
            spec = _pet_reward_spec(picked_name)
            if spec is not None:
                if not _grant_pets_by_spec(state, spec, bag, 1):
                    continue
            elif picked_name:
                state.add_item(picked_name, 1, "gift")
                bag["items"].append({"name": picked_name, "count": 1})
                bag["lines"].append("获得【%s】" % picked_name)
        handled = True
    # ---- 内测先锋 / 新人扶持：1 亿金币 + 1 只成长 6.0 赤翅蛾 + 一套单属性 +100 装备
    elif item_name in GIFT_EQUIP_PACKS:
        cfg_pack = GIFT_EQUIP_PACKS[item_name]
        equip_cfg = EQUIP_GRANT_SETS.get(item_name) or {"value": 100, "prefix": item_name, "rarity": "legendary"}
        state.take_item(item_name, count)
        bag["gold"] += int(cfg_pack["gold"]) * count
        bag["lines"].append("获得金币 %d" % (int(cfg_pack["gold"]) * count))
        for _ in range(count):
            _grant_pets_by_spec(state, {"pet_id": str(cfg_pack["pet_id"]),
                                        "growth": (float(cfg_pack["growth"]), float(cfg_pack["growth"]))},
                                bag, int(cfg_pack["pets"]))
            for slot, label, stat in EQUIP_SET_SLOTS:
                equip = _make_single_attr_equipment(slot, label, stat,
                                                    str(equip_cfg["prefix"]) + label,
                                                    int(equip_cfg["value"]), str(equip_cfg["rarity"]))
                state.equipment.setdefault("inventory", []).append(equip)
                bag["items"].append({"id": equip["id"], "name": equip["name"], "slot": slot})
                bag["lines"].append("获得【%s】" % equip["name"])
        handled = True
    else:
        # ---- 掉率表礼包（塔1~5 / 生死塔参与 / 兄弟天关 / 世界boss / 竞技场四包）
        table_kind = _gift_table_kind(item_name)
        if table_kind:
            state.take_item(item_name, count)
            _grant_gift_table(state, item_name, count, table_kind, bag)
            handled = True
        else:
            # ---- 宠物礼包 / 宠物蛋（单只起开，spec 决定成长区间与宠物池）
            spec = _pet_reward_spec(item_name)
            if spec is not None:
                state.take_item(item_name, count)
                if not _grant_pets_by_spec(state, spec, bag, count):
                    return fail("宠物数据缺失，无法开启【%s】" % item_name)
                handled = True

    if not handled:
        return fail("该礼包暂不支持开启：%s" % item_name)

    if bag["gold"] > 0:
        state.add_gold(int(bag["gold"]))

    granted_pets = bag["pets"]
    granted_items = bag["items"]
    granted_gold = bag["gold"]
    lines = bag["lines"]
    result = {
        "grantedPets": granted_pets,
        "grantedItems": granted_items,
        "grantedGold": granted_gold,
        "opened": count,
        "kind": default_kind,
    }
    message = "\n".join(lines)
    state.bump()
    if op_id:
        ops[op_id] = {"at": iso(now()), "item": item_name, "count": count,
                      "result": result, "message": message, "summaryLines": lines}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(duplicate=False, result=result, message=message, summaryLines=lines,
              saveData=_authority_save(state), saveRevision=state.revision)


@route("POST", "/pet/open-egg")
def pet_open_egg(ctx: Ctx) -> dict:
    return _open_pet_reward(ctx, "egg")


@route("POST", "/gift/open-pet")
def gift_open_pet(ctx: Ctx) -> dict:
    return _open_pet_reward(ctx, "gift")


@route("POST", "/gift/use-medal")
def gift_use_medal(ctx: Ctx) -> dict:
    state = ctx.state
    medal = ctx.s("medal") or "bold"
    pet_id = ctx.s("petId")
    ctx.body["medal"] = medal
    ctx.body["petId"] = pet_id
    return pet_grant_medal(ctx)
