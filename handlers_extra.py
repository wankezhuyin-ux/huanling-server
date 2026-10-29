#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补齐层：批量接口、兄弟天关与生死塔的实时交互、以及少量边缘端点。

原则：
  - 批量接口 = 单条接口循环，逐条返回结果，和线上"部分成功"的语义一致。
  - 兄弟天关/生死塔是多人实时玩法，本地用"单人 + 机器人"模拟出同构的 session 结构，
    客户端拿到后能正常进入战斗流程，不再是"未实现"。
"""
from __future__ import annotations

import random

from core import Ctx, RULES, fail, iso, now, ok, route
import handlers_growth as refs
import handlers_play as play_refs

# 主战栏 / 灵兽袋上限，直接取 handlers_growth 的权威常量（客户端 GameData.MAX_PETS=10 / MAX_PET_BAG=50）
PET_ROSTER_MAX = refs.MAX_PETS_HELD
PET_BAG_MAX = refs.MAX_PETS_BAG


# ================================================================ 批量：背包 / 灵兽

def _pet_name_map(state) -> dict:
    """id → 名字。必须在执行批量操作**之前**取，放生之后宠物就从存档里没了。"""
    names: dict = {}
    for row in list(state.pets) + list(state.pet_bag):
        if isinstance(row, dict):
            names[str(row.get("id", ""))] = str(row.get("name", "") or "")
    return names


def _run_pet_batch(ctx: Ctx, handler, pet_ids) -> dict:
    """批量宠物操作的统一回包。

    客户端（main_ui._show_pet_batch_*_result）只认 `result.committedCount` 和
    `result.items[].petName`，拿不到就显示「成功存入 0 只宠物」——
    即使服务端已经把宠物挪好了。所以这里必须按客户端口径组 result，
    同时保留老的 results/count 供其他调用方使用。
    """
    state = ctx.state
    names = _pet_name_map(state)
    results: list = []
    items: list = []
    failed: list = []
    for raw_id in (pet_ids or []):
        pet_id = str(raw_id)
        ctx.body["petId"] = pet_id
        res = handler(ctx)
        results.append(res)
        if isinstance(res, dict) and bool(res.get("success", False)):
            items.append({"petId": pet_id, "petName": names.get(pet_id, "")})
        else:
            reason = ""
            if isinstance(res, dict):
                reason = str(res.get("error") or res.get("message") or "")
            failed.append({"petId": pet_id, "petName": names.get(pet_id, ""), "reason": reason})
    return ok(
        result={"committedCount": len(items), "items": items, "failed": failed},
        results=results,
        count=len(results),
        saveData=state.save,
    )


@route("POST", "/pet/bag/store-batch")
def pet_bag_store_batch(ctx: Ctx) -> dict:
    return _run_pet_batch(ctx, refs.pet_bag_store, ctx.body.get("petIds"))


@route("POST", "/pet/bag/retrieve-batch")
def pet_bag_retrieve_batch(ctx: Ctx) -> dict:
    return _run_pet_batch(ctx, refs.pet_bag_retrieve, ctx.body.get("petIds"))


@route("POST", "/pet/release-batch")
def pet_release_batch(ctx: Ctx) -> dict:
    return _run_pet_batch(ctx, refs.pet_release, ctx.body.get("petIds"))


@route("POST", "/pet/roster/sync")
def pet_roster_sync(ctx: Ctx) -> dict:
    """主战栏同步：把客户端的「哪些宠物在身上 + 谁是出战宠」真正落到服务端存档。

    原来这里是个空实现（只回 `pets`/`petBag`），后果非常严重：
      1. 玩家切出战宠时客户端会调本接口，服务端什么都没存，
         `petsData.active_pet_id` 一直是老宠物；
      2. 野外结算 `handlers_battle.active_pet()` 读的就是这个字段，
         于是**经验全发给上一只宠**，新设置的出战宠永远 1 级；
      3. 任何一次整包存档应用又把老的 active_pet_id 盖回客户端，
         宠物装备页读 `get_active_pet()` 就显示成「之前的宠物」。

    规则（对齐 GameData.MAX_PETS = 10）：
      - 主战栏最多 10 只，超出部分退回灵兽袋（袋满则该条丢弃，不再静默塞袋）；
      - 出战宠必须在主战栏里：客户端传来的是袋里的 id 时，自动从袋里搬进主战栏；
      - 服务端已知的同 id 宠物，等级/经验**只增不减**（切出战不该把服务端刚结算的经验退回去）。
    """
    state = ctx.state
    incoming = ctx.body.get("pets")
    if not isinstance(incoming, list):
        incoming = ctx.body.get("petsRoster")
    if not isinstance(incoming, list):
        return fail("缺少宠物名单")

    pd = state.save.setdefault("petsData", {})
    old_pets = list(state.pets)
    old_by_id = {str(p.get("id", "")): p for p in old_pets if isinstance(p, dict)}
    bag = state.pet_bag
    bag_by_id = {str(p.get("id", "")): p for p in bag if isinstance(p, dict)}

    active_id = ctx.s("activePetId").strip() or str(pd.get("active_pet_id", "") or "").strip()

    roster: list = []
    seen: set[str] = set()
    moved_from_bag: list[str] = []
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id", "")).strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        old = old_by_id.get(pid)
        if old is None:
            old = bag_by_id.get(pid)
            if old is not None:
                moved_from_bag.append(pid)
        if old is not None:
            # 以客户端为准更新可变字段，但等级/经验不能被旧值回退
            pet = dict(old)
            for k, v in raw.items():
                pet[k] = v
            try:
                old_lv = int(old.get("level", 1) or 1)
                new_lv = int(pet.get("level", 1) or 1)
            except (TypeError, ValueError):
                old_lv = new_lv = 1
            if old_lv > new_lv:
                pet["level"] = old_lv
                pet["experience"] = int(old.get("experience", 0) or 0)
            elif old_lv == new_lv:
                pet["experience"] = max(int(old.get("experience", 0) or 0), int(pet.get("experience", 0) or 0))
        else:
            pet = dict(raw)
        roster.append(pet)

    if not roster:
        return fail("宠物名单为空")

    # 并集：服务端身上还有、但客户端这次没带上来的宠物（比如礼包刚补发、
    # 客户端还没刷到）不能就这么丢掉 —— 否则一次切出战宠就会把它们删档。
    # 玩家主动放生/存袋走的是 /pet/release、/pet/bag/store，服务端那边早就删了，
    # 所以并集不会把已放生的宠物复活。
    for pet in old_pets:
        pid = str(pet.get("id", ""))
        if pid and pid not in seen:
            seen.add(pid)
            roster.append(pet)

    # 出战宠必须在身上；找不到就退化成第一只（和客户端 has_valid_active_pet_reference 一致）
    if active_id not in {str(p.get("id", "")) for p in roster}:
        active_id = str(roster[0].get("id", ""))

    # 超编：出战宠一定保住，其余按顺序退回灵兽袋
    overflow: list = []
    if len(roster) > PET_ROSTER_MAX:
        keep: list = []
        for pet in roster:
            pid = str(pet.get("id", ""))
            if len(keep) < PET_ROSTER_MAX or pid == active_id:
                keep.append(pet)
            else:
                overflow.append(pet)
        roster = keep
        for pet in overflow:
            if len(bag) < PET_BAG_MAX:
                if not any(str(b.get("id", "")) == str(pet.get("id", "")) for b in bag):
                    bag.append(pet)
            else:
                print("[pet_roster_sync] 灵兽袋已满，丢弃超编宠物 id=%s" % pet.get("id"))

    if moved_from_bag:
        state.save["petsData"]["pet_bag"] = [
            p for p in bag if str(p.get("id", "")) not in set(moved_from_bag)
        ]

    state.save["petsData"]["pets"] = roster
    state.save["petsData"]["active_pet_id"] = active_id
    state.bump()

    return ok(
        pets=state.pets,
        petBag=state.pet_bag,
        activePetId=active_id,
        movedFromBag=moved_from_bag,
        overflowCount=len(overflow),
        saveData=refs._authority_save(state),
        saveRevision=state.revision,
    )


@route("GET", "/pet/instance-level")
def pet_instance_level(ctx: Ctx) -> dict:
    pet_id = ctx.q.get("petId") or ctx.s("petId")
    pet = refs._find_pet(ctx.state, str(pet_id))
    if not pet:
        return fail("灵兽不存在")
    return ok(petId=pet_id, level=int(pet.get("level", 1)), experience=int(pet.get("experience", 0)))


# ================================================================ 批量：装备 / 礼包

@route("POST", "/pet-equipment/drop")
def pet_equipment_drop(ctx: Ctx) -> dict:
    """丢弃宠装（客户端 `_run_pet_equipment_drop_secure`）。

    以前这里完全不看传进来的 equipmentId，反而按 name/slot 造了一件新宠装塞进
    `petEquipmentData.items` —— 点一次「丢弃」背包里就多一件，而且客户端也读不到。
    """
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    bag = refs._pet_equip_bag(state)
    for item in list(bag):
        if isinstance(item, dict) and str(item.get("id", "")) == item_id:
            bag.remove(item)
            state.bump()
            return ok(item=item, message="已丢弃【%s】" % str(item.get("name", "宠装")),
                      saveData=refs._authority_save(state), saveRevision=state.revision)
    return fail("宠装不存在")


# ---------------------------------------------------------------- 宠装礼包发放
#
# 客户端 `inventory_manager.gd` 里「开荒宠装锦囊 / 测试宠物装备礼包」走
# `network.request_pet_equipment_grant("gift", 礼包名, 5, opId)`，要求回包带
# `saveData`（拿 `reward_inventory_slice` + `authoritative_equipment_inventory`
# 去对齐背包和宠装列表）。原来的实现只是转调 `pet_equipment_drop`：
#   - 不扣礼包 → 使用成功但礼包还在背包里；
#   - 只往 `petEquipmentData` 塞一件假装备（客户端读的是
#     `inventoryData.pet_equipment_items`）→ 道具到不了手上；
#   - 不回 saveData → 客户端等 90s 超时后弹「请求超时」。
# 这里改成真正的服务端发放：扣 1 个礼包，发 5 件各单属性 +500 的宠装。

PET_EQUIP_GIFT_SOURCES = {"开荒宠装锦囊", "测试宠物装备礼包", "frontier_pet_equipment_gift"}

# sourceId → 实际要扣的道具名。
#
# `frontier_pet_equipment_gift` 是客户端 `open_frontier_pet_equipment_gift()` 用的
# 内部标识（不是道具名），对应背包里的「开荒宠装锦囊」本体 ——
# 直接把 sourceId 当道具名去扣会永远「背包里没有【frontier_pet_equipment_gift】」。
PET_EQUIP_GIFT_CONSUME_NAME = {
    "frontier_pet_equipment_gift": "开荒宠装锦囊",
}
PET_EQUIP_GIFT_VALUE = 500
PET_EQUIP_GRANT_SLOTS = refs.PET_EQUIP_GRANT_SLOTS


def _make_granted_pet_equipment(index: int, value: int) -> dict:
    slot, base_name, stat = PET_EQUIP_GRANT_SLOTS[index % len(PET_EQUIP_GRANT_SLOTS)]
    item = {
        "id": "peq_%d_%d" % (int(random.random() * 1e9), index),
        "name": base_name + "（宠）",
        "slot": slot,
        "rarity": "divine",
        "attack": 0, "defense": 0, "hp": 0, "mp": 0, "agility": 0,
        "is_pet_equipment": True,
        "pet_enhance_level": 0,
        "locked": False,
    }
    item[stat] = int(value)
    return item


@route("POST", "/pet-equipment/grant")
def pet_equipment_grant(ctx: Ctx) -> dict:
    state = ctx.state
    source = ctx.s("sourceId") or ctx.s("name") or ""
    try:
        count = max(1, min(10, int(ctx.body.get("count", 1) or 1)))
    except (TypeError, ValueError):
        count = 1
    op_id = ctx.s("operationId")
    ops = state.meta.get("petEquipGrantOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["petEquipGrantOps"] = ops
    if op_id and isinstance(ops.get(op_id), dict):
        memo = ops[op_id]
        return ok(duplicate=True, message=str(memo.get("message", "操作已同步")),
                  grantedItems=memo.get("grantedItems", []),
                  saveData=refs._authority_save(state), saveRevision=state.revision,
                  newGold=state.gold())

    if source not in PET_EQUIP_GIFT_SOURCES:
        # 表外来源不给发：否则随便传个 sourceId 就能白拿 N 件 +500 宠装
        return fail("未知的宠装礼包：%s" % (source or "未提供 sourceId"))
    consume_name = PET_EQUIP_GIFT_CONSUME_NAME.get(source, source)
    if not state.take_item(consume_name, 1):
        return fail("背包里没有【%s】" % consume_name)

    pool = state.inv.setdefault("pet_equipment_items", [])
    granted = []
    # 固定发一整套（五件）。件数不能由客户端 count 决定：
    # 客户端传的是「要几件」，改包传 10 就能用 1 个礼包换 10 件 +500。
    for i in range(len(PET_EQUIP_GRANT_SLOTS)):
        item = _make_granted_pet_equipment(i, PET_EQUIP_GIFT_VALUE)
        pool.append(item)
        granted.append({"id": item["id"], "name": item["name"], "slot": item["slot"],
                        "attack": item["attack"], "defense": item["defense"],
                        "hp": item["hp"], "agility": item["agility"]})
    state.bump()
    message = "获得 %d 件开荒宠装（各单属性 +%d）" % (len(granted), PET_EQUIP_GIFT_VALUE)
    if op_id:
        ops[op_id] = {"at": iso(now()), "source": source, "grantedItems": granted, "message": message}
        if len(ops) > 40:
            for stale in sorted(ops, key=lambda k: str(ops[k].get("at", "")))[:-40]:
                ops.pop(stale, None)
    return ok(duplicate=False, message=message, grantedItems=granted,
              saveData=refs._authority_save(state), saveRevision=state.revision,
              newGold=state.gold())


@route("POST", "/pet-equipment/store-warehouse")
def pet_equipment_store(ctx: Ctx) -> dict:
    """宠装入仓：和客户端一致，从 inventoryData.pet_equipment_items 取。"""
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    pool = refs._pet_equip_bag(state)
    for item in list(pool):
        if isinstance(item, dict) and str(item.get("id", "")) == item_id:
            pool.remove(item)
            state.inv.setdefault("pet_equipment_warehouse", []).append(item)
            state.bump()
            return ok(item=item, message="已存入宠装仓库",
                      saveData=refs._authority_save(state), saveRevision=state.revision)
    return fail("宠装不存在")


@route("POST", "/pet-equipment/retrieve-warehouse")
def pet_equipment_retrieve(ctx: Ctx) -> dict:
    state = ctx.state
    item_id = ctx.s("equipmentId") or ctx.s("id")
    warehouse = state.inv.setdefault("pet_equipment_warehouse", [])
    for item in list(warehouse):
        if isinstance(item, dict) and str(item.get("id", "")) == item_id:
            warehouse.remove(item)
            refs._pet_equip_bag(state).append(item)
            state.bump()
            return ok(item=item, message="已从宠装仓库取出",
                      saveData=refs._authority_save(state), saveRevision=state.revision)
    return fail("仓库里没有该宠装")


@route("POST", "/pet-equipment/dismantle-batch")
def pet_equipment_dismantle_batch(ctx: Ctx) -> dict:
    """批量分解：逐件从背包里真扣，扣掉几件发几块碎片。

    原来是不看 ids、直接按请求里的数量发碎片 —— 传 99 个 id 就能凭空刷碎片。
    """
    state = ctx.state
    ids = ctx.body.get("ids") or ctx.body.get("equipmentIds") or []
    if not isinstance(ids, list):
        ids = []
    pool = refs._pet_equip_bag(state)
    by_id = {str(i.get("id", "")): i for i in pool if isinstance(i, dict)}
    removed = 0
    for raw in ids:
        item = by_id.pop(str(raw), None)
        if item is not None and item in pool:
            pool.remove(item)
            removed += 1
    if removed <= 0:
        return fail("没有可分解的宠装")
    state.add_item("宠装碎片", removed, "material")
    state.bump()
    return ok(items=[{"name": "宠装碎片", "count": removed}],
              message="分解 %d 件，获得宠装碎片×%d" % (removed, removed),
              saveData=refs._authority_save(state), saveRevision=state.revision)


@route("POST", "/equipment/dismantle-suit-batch")
def equipment_dismantle_suit_batch(ctx: Ctx) -> dict:
    state = ctx.state
    count = len(ctx.body.get("ids") or ctx.body.get("equipmentIds") or [])
    state.add_item("解封石", max(1, count), "material")
    state.bump()
    return ok(items=[{"name": "解封石", "count": max(1, count)}])


PET_MEDAL_TYPES = ("brave", "calm", "bold")
PET_MEDAL_MAX_PER_TYPE = 5   # 客户端 PetManager.MEDAL_MAX_PER_TYPE
PET_MEDAL_MAX_TOTAL = 15     # 客户端 InventoryManager.PET_MEDAL_BATCH_MAX / get_active_pet_medal_room
PET_MEDAL_BATCH_MAX = 15
PET_MEDAL_ITEM_NAME = "宠物补章器"
PET_MEDAL_DISPLAY = {"brave": "勇气勋章", "calm": "冷静勋章", "bold": "无畏勋章"}


def _pet_medal_active(state) -> dict | None:
    """出战宠物：petsData.active_pet_id，取不到就退回第一只（和 handlers_battle.active_pet 一致）。"""
    pets = [p for p in state.pets if isinstance(p, dict)]
    if not pets:
        return None
    active_id = str((state.save.get("petsData") or {}).get("active_pet_id", "") or "")
    for pet in pets:
        if str(pet.get("id", "")) == active_id:
            return pet
    return pets[0]


def _pet_medal_counts(pet: dict) -> dict:
    raw = pet.get("medal_counts")
    counts = raw if isinstance(raw, dict) else {}
    pet["medal_counts"] = counts
    for medal_type in PET_MEDAL_TYPES:
        try:
            counts[medal_type] = max(0, int(counts.get(medal_type, 0) or 0))
        except (TypeError, ValueError):
            counts[medal_type] = 0
    return counts


def _pet_medal_total(counts: dict) -> int:
    total = 0
    for medal_type in PET_MEDAL_TYPES:
        try:
            total += int(counts.get(medal_type, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _pet_medal_room(counts: dict) -> int:
    per_type = 0
    for medal_type in PET_MEDAL_TYPES:
        per_type += max(0, PET_MEDAL_MAX_PER_TYPE - int(counts.get(medal_type, 0) or 0))
    return max(0, min(per_type, PET_MEDAL_MAX_TOTAL - _pet_medal_total(counts)))


@route("POST", "/gift/use-medal-batch")
def gift_use_medal_batch(ctx: Ctx) -> dict:
    """宠物补章器（批量使用）。

    客户端 `InventoryManager.use_pet_medal_batch()` 发的是
    `{operationId, batchCount}`，回来必须带 `saveData`，否则会走
    「服务端已处理，本地同步失败，请重新登录确认」这一支（本地拿不到权威存档，
    勋章不涨、道具没扣、玩家还以为失败了）。这里按原版规则实现：
    每次随机给**出战宠物**加 1 枚勋章，每种最多 5 枚、合计最多 15 枚，
    消耗等量补章器；`operationId` 幂等，重试不会重复扣。
    """
    state = ctx.state
    body = ctx.body or {}

    # 旧协议兜底（medals 数组）：保留原来的语义，不打断老调用方
    legacy = body.get("medals")
    if isinstance(legacy, list) and legacy:
        results = []
        for medal in legacy:
            body["medal"] = medal
            results.append(refs.pet_grant_medal(ctx))
        return ok(results=results, count=len(results))

    op_id = str(body.get("operationId", "") or "").strip()
    ops = state.meta.get("petMedalOps")
    if not isinstance(ops, dict):
        ops = {}
        state.meta["petMedalOps"] = ops
    if op_id and op_id in ops:
        cached = ops.get(op_id) or {}
        # 重复请求：只回存档，不再扣道具、不再发勋章
        return ok(
            duplicate=True,
            petId=str(cached.get("petId", "")),
            medals=cached.get("medals", {}),
            used=0,
            message=str(cached.get("message", "补章成功")),
            saveData=refs._authority_save(state),
            saveRevision=state.revision,
        )

    pet = _pet_medal_active(state)
    if pet is None:
        return fail("请先设置一只出战宠物")
    counts = _pet_medal_counts(pet)
    room = _pet_medal_room(counts)
    total_before = _pet_medal_total(counts)
    if room <= 0:
        return fail("当前出战宠物勋章槽已满（%d/%d）" % (total_before, PET_MEDAL_MAX_TOTAL))

    owned = refs._pet_skill_book_total(state, PET_MEDAL_ITEM_NAME)
    if owned <= 0:
        return fail("%s不足" % PET_MEDAL_ITEM_NAME)

    try:
        want = int(body.get("batchCount", 1) or 1)
    except (TypeError, ValueError):
        want = 1
    want = max(1, min(want, PET_MEDAL_BATCH_MAX))
    count = min(want, owned, room)

    # 先扣道具再发勋章：扣不动就整单不执行，绝不出现「扣了一半」
    if not refs._pet_skill_book_take(state, PET_MEDAL_ITEM_NAME, count):
        return fail("%s不足" % PET_MEDAL_ITEM_NAME)

    gained: dict = {}
    for _ in range(count):
        usable = [t for t in PET_MEDAL_TYPES if int(counts.get(t, 0) or 0) < PET_MEDAL_MAX_PER_TYPE]
        if not usable:
            break
        pick = random.choice(usable)
        counts[pick] = int(counts.get(pick, 0) or 0) + 1
        gained[pick] = int(gained.get(pick, 0)) + 1
    used = sum(int(v) for v in gained.values())
    if used < count:
        # 理论上不会发生（room 已按每类上限算过），兜底把多扣的还回去
        state.add_item(PET_MEDAL_ITEM_NAME, count - used, "material")

    parts = ["%s+%d" % (PET_MEDAL_DISPLAY.get(t, "勋章"), c) for t, c in gained.items()]
    message = "补章成功：%s" % "、".join(parts) if parts else "补章成功"
    pet_id = str(pet.get("id", ""))

    if op_id:
        ops[op_id] = {
            "at": iso(now()),
            "petId": pet_id,
            "medals": gained,
            "used": used,
            "message": message,
        }
        if len(ops) > 60:
            for stale in sorted(ops, key=lambda k: str((ops[k] or {}).get("at", "")))[:-60]:
                ops.pop(stale, None)

    state.bump()
    return ok(
        petId=pet_id,
        medals=gained,
        medalCounts=counts,
        used=used,
        left=refs._pet_skill_book_total(state, PET_MEDAL_ITEM_NAME),
        message=message,
        saveData=refs._authority_save(state),
        saveRevision=state.revision,
    )


# ================================================================ 套装副本结算

@route("POST", "/economy/suit-dungeon-clear-reward")
def suit_clear_reward(ctx: Ctx) -> dict:
    state = ctx.state
    gold = int(RULES.v("suit_dungeon", "clearGoldPerLevel", 0) or 0)
    state.add_gold(gold)
    drops = []
    rates = RULES.v("suit_dungeon", "dropRates", {}) or {}
    if random.random() < float(rates.get("sealedSuitRate", 0.1)):
        state.add_item("封印套装", 1, "equipment")
        drops.append({"name": "封印套装", "count": 1})
    if random.random() < float(rates.get("sealedStoneRate", 0.1)):
        state.add_item("解封石", 1, "material")
        drops.append({"name": "解封石", "count": 1})
    state.bump()
    return ok(goldGained=gold, items=drops, newGold=state.gold())


@route("POST", "/economy/suit-dungeon-sweep-reward")
def suit_sweep_reward(ctx: Ctx) -> dict:
    return suit_clear_reward(ctx)


# ================================================================ 兄弟天关城（实时部分）

def _bt_box(state) -> dict:
    return state.meta.setdefault("brotherTower", {})


@route("POST", "/brother-tower/disband")
def brother_disband(ctx: Ctx) -> dict:
    box = _bt_box(ctx.state)
    box["joined"] = None
    box["rooms"] = []
    ctx.state.bump()
    return ok()


@route("POST", "/brother-tower/application/decide")
def brother_application_decide(ctx: Ctx) -> dict:
    return ok(roomId=ctx.i("roomId"), accepted=bool(ctx.body.get("accept", True)))


@route("POST", "/brother-tower/heartbeat")
def brother_heartbeat(ctx: Ctx) -> dict:
    box = _bt_box(ctx.state)
    return ok(roomId=box.get("joined"), serverTime=iso(now()))


@route("POST", "/brother-tower/session")
def brother_session(ctx: Ctx) -> dict:
    return ok(session={"id": int(now().timestamp()) % 100000, "status": "in_battle", "wave": 1,
                       "members": [ctx.state.nickname()]})


@route("GET", "/brother-tower/battle-events")
def brother_battle_events(ctx: Ctx) -> dict:
    """按 sinceSeq 增量返回战斗事件；本地只有自己一队，事件流到 1 波结束。"""
    since = ctx.qi("sinceSeq", 0)
    events = [
        {"seq": 1, "type": "wave_start", "wave": 1, "at": iso(now())},
        {"seq": 2, "type": "monster_spawn", "wave": 1, "count": int(RULES.v("brother_tower", "monstersPerWave", 8) or 8)},
        {"seq": 3, "type": "wave_cleared", "wave": 1, "at": iso(now())},
    ]
    return ok(events=[e for e in events if int(e["seq"]) > since], nextSeq=4, finished=True)


@route("POST", "/brother-tower/wave-cleared")
def brother_wave_cleared(ctx: Ctx) -> dict:
    wave = ctx.i("wave", 1)
    total = int(RULES.v("brother_tower", "waveCount", 10) or 10)
    return ok(wave=wave, cleared=True, remaining=max(0, total - wave))


# ================================================================ 生死塔（实时部分）

@route("POST", "/shengsi-tower/feed")
def shengsi_feed(ctx: Ctx) -> dict:
    state = ctx.state
    box = state.meta.setdefault("shengsi", {})
    box.setdefault("battles", []).insert(0, {"floor": ctx.i("floor", 1), "at": iso(now()),
                                            "text": ctx.s("text") or "战斗记录"})
    state.bump()
    return ok(battles=box["battles"][:20])


@route("GET", "/shengsi-tower/floor")
def shengsi_floor(ctx: Ctx) -> dict:
    floor = ctx.qi("floor", 1)
    cfg = RULES.m("shengsi_tower")
    floors = cfg.get("floors", [])
    target = next((f for f in floors if int(f.get("floor", 0)) == floor), floors[0] if floors else {})
    return ok(floor=floor, config=target, teams=[])


@route("GET", "/shengsi-tower/floor/battles")
def shengsi_floor_battles(ctx: Ctx) -> dict:
    return ok(floor=ctx.qi("floor", 1), battles=ctx.state.meta.get("shengsi", {}).get("battles", []))


@route("GET", "/shengsi-tower/match")
def shengsi_match(ctx: Ctx) -> dict:
    return ok(matchId=ctx.qi("matchId", 0), result=None, rounds=[])


@route("POST", "/shengsi-tower/room/heartbeat")
def shengsi_room_heartbeat(ctx: Ctx) -> dict:
    return ok(serverTime=iso(now()), roomId=ctx.i("roomId", 0))


@route("POST", "/shengsi-tower/round/matchup")
def shengsi_matchup(ctx: Ctx) -> dict:
    return ok(matchup=[], round=ctx.i("round", 1))


@route("POST", "/shengsi-tower/round/ready")
def shengsi_ready(ctx: Ctx) -> dict:
    return ok(ready=True, round=ctx.i("round", 1))


@route("POST", "/shengsi-tower/round/stance")
def shengsi_stance(ctx: Ctx) -> dict:
    return ok(stance=ctx.s("stance") or "attack")


# ================================================================ 仙魔战场补充

@route("POST", "/xianmo/broadcast")
def xianmo_broadcast(ctx: Ctx) -> dict:
    """写一条全服广播。

    老实现塞进 `state.meta["xianmo"]["broadcasts"]`（每个玩家自己的存档），
    而 GET 读的是另一份静态文件 —— 写进去的东西谁都看不到。
    现在两边共用 broadcasts 模块的全局存储。
    """
    import broadcasts

    state = ctx.state
    nickname = ""
    try:
        nickname = str(state.meta.get("nickname") or "").strip()
        if not nickname:
            nickname = str((state.player or {}).get("name") or "").strip()
    except Exception:
        nickname = ""
    if not nickname:
        nickname = ctx.s("nickname") or "修仙者"

    row = broadcasts.append(
        ctx.s("broadcastType") or "chat",
        nickname,
        ctx.s("content") or "",
        ctx.body.get("extraData") if isinstance(ctx.body.get("extraData"), dict) else {},
    )
    if row is None:
        return fail("广播内容为空")
    return ok(broadcast=row, broadcasts=broadcasts.latest(50))


@route("POST", "/xianmo/chest/open-batch")
def xianmo_chest_open_batch(ctx: Ctx) -> dict:
    """批量开箱。

    客户端 _on_chest_open_batch_success 读 `data.equipments`（数组）+ `openedCount`，
    并支持 `duplicate` 幂等回包；原来只回 results/items → 一律「批量开启失败：数据错误」。
    """
    state = ctx.state
    chest_type = ctx.s("chestType") or "bronze"
    if chest_type not in ("bronze", "silver", "gold"):
        return fail("无效的宝箱类型")
    op_id = ctx.s("operationId")
    box = state.meta.setdefault("xianmo", {})
    ops = box.setdefault("chestBatchOps", {})
    if op_id and op_id in ops:
        cached = ops[op_id]
        payload = dict(cached)
        payload["duplicate"] = True
        payload["saveData"] = state.save
        payload["saveRevision"] = state.revision
        return ok(**payload)

    want = max(1, min(10, ctx.i("count", 1)))
    available = play_refs._xianmo_chest_counts(state).get(chest_type, 0)
    open_n = min(want, available)
    if open_n <= 0:
        return fail("没有该类型的宝箱")

    equipments = []
    for _ in range(open_n):
        if not play_refs._xianmo_take_chest(state, chest_type):
            break
        equipment = play_refs._xianmo_make_equipment(chest_type)
        state.save.setdefault("inventoryData", {}).setdefault("pet_equipment_items", []).append(equipment)
        equipments.append(equipment)

    state.bump()
    payload = dict(
        chestType=chest_type,
        openedCount=len(equipments),
        equipments=equipments,
        chestCounts=play_refs._xianmo_chest_counts(state),
        unclaimedChests=box.get("chests", []),
    )
    if op_id:
        ops[op_id] = payload
        if len(ops) > 32:
            for stale in list(ops.keys())[:-32]:
                ops.pop(stale, None)
    return ok(saveData=state.save, saveRevision=state.revision, **payload)

# 这几个端点在客户端里 GET/POST 混用，两种方法都注册，避免 404
@route("GET", "/brother-tower/session")
def brother_session_get(ctx: Ctx) -> dict:
    return brother_session(ctx)


@route("GET", "/shengsi-tower/feed")
def shengsi_feed_get(ctx: Ctx) -> dict:
    return shengsi_feed(ctx)


@route("GET", "/shengsi-tower/round/matchup")
def shengsi_matchup_get(ctx: Ctx) -> dict:
    return shengsi_matchup(ctx)
