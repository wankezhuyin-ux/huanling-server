#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""丹药炼制 / 套装转换 回归测试。

守这两条链路（之前都是空壳）：
  1. `/crafting/pill-craft` 客户端只传 recipeKey，老实现按 pillName 取名字——
     永远产出兜底名「定神丹」（还是旧名，客户端不认识），材料永远扣灵草×3。
     现在必须：按 recipeKey 查丹方、卡境界、按配方扣材料、按成功率出丹、批量、
     同一 operationId 重发不重复发货。
  2. `/equipment/suit-convert` 老实现完全不看 equipmentIds，只扣一颗客户端根本
     不存在的「套装转换石」。现在必须：3 件同名套装 → 消耗 → 随机部位 90%~110%。

    python3 tools/test_craft_suit.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import handlers_growth  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    flag = "OK " if got == want else "FAIL"
    if got != want:
        FAILED.append(name)
    print("[%s] %-52s got=%r want=%r" % (flag, name, got, want))


def check_true(name: str, cond) -> None:
    check(name, bool(cond), True)


def make_account(username: str, items: list[dict] | None = None,
                 equipment: list[dict] | None = None,
                 player_extra: dict | None = None) -> core.State:
    p = {"level": 100, "experience": 0, "gold": 1000, "name": "测丹侠",
         "realm": "凡人", "current_map": core.DEFAULT_MAP}
    p.update(player_extra or {})
    save = {
        "playerData": p,
        "inventoryData": {"items": items or [], "pet_equipment_items": [],
                          "warehouse_items": [], "xianmo_chests": []},
        "equipmentData": {"inventory": equipment or [], "equipped": {}},
        "gameSettings": {"current_map": core.DEFAULT_MAP},
        "martialArtsData": {"martial_arts": [], "equipped_ids": []},
        "petsData": {"pets": [], "active_pet_id": ""},
    }
    path = core.STATE_DIR / "accounts" / (core._safe_name(username) + ".json")
    path.write_text(json.dumps({"save": save, "meta": {}}, ensure_ascii=False), encoding="utf-8")
    return core.State(username)


def ctx_for(state: core.State, body: dict) -> core.Ctx:
    return core.Ctx(state, body, {}, {"username": state.username}, {})


def stack(name: str, count: int) -> dict:
    return {"id": "it_%s" % name, "name": name, "count": count,
            "item_type": "material", "custom_data": {}}


def suit_piece(pid: str, suit_id: int, slot: str, value: int, **over) -> dict:
    stat = handlers_growth.SUIT_SLOT_STAT[slot]
    item = {
        "id": pid, "name": "测试套装·%s" % handlers_growth.SUIT_SLOT_NAMES[slot],
        "slot": slot, "rarity": "suit",
        "attack": 0, "defense": 0, "hp": 0, "mp": 0, "agility": 0,
        "cultivation_req": 0, "base_cultivation_req": 0,
        "forge_count": 0, "forge_attr_counts": {},
        "equipped": False, "locked": False,
        "is_suit": True, "suit_id": suit_id, "suit_slot": slot,
        "is_sealed": False, "sealed_data": {},
        "is_pet_equipment": False, "pet_enhance_level": 0,
        "suit_enhance_level": 0, "suit_enhance_rolls": [],
        "pet_loot_source": "", "pet_chest_tier": 0,
    }
    item[stat] = value
    item.update(over)
    return item


class ForcedRoll:
    """把 random.random 固定住，让成功率可预期。"""

    def __init__(self, value: float) -> None:
        self.value = value

    def __enter__(self):
        self.orig = handlers_growth.random.random
        handlers_growth.random.random = lambda: self.value
        return self

    def __exit__(self, *exc):
        handlers_growth.random.random = self.orig
        return False


def main() -> int:
    accounts: list[Path] = []
    try:
        # ================================================== 一、丹药炼制
        print("---- 丹药炼制")

        # 未知丹方
        st = make_account("zzcraft_unknown", items=[stack("灵草", 99)])
        accounts.append(st.path)
        res = handlers_growth.pill_craft(ctx_for(st, {"recipeKey": "no_such_pill", "batchCount": 1}))
        check_true("未知 recipeKey 被拒", not res.get("success") and "未知丹方" in str(res.get("message")))

        # 境界不足：凡人炼「凝神丹」（散仙）
        st = make_account("zzcraft_realm", items=[stack("灵芝", 9), stack("天山雪莲", 9)])
        accounts.append(st.path)
        res = handlers_growth.pill_craft(ctx_for(st, {"recipeKey": "ningshen_pill", "batchCount": 1}))
        check_true("境界不足被拒", not res.get("success") and "境界不足" in str(res.get("message")))

        # 材料不足
        st = make_account("zzcraft_short", items=[stack("灵草", 4), stack("灵芝", 1)])
        accounts.append(st.path)
        res = handlers_growth.pill_craft(ctx_for(st, {"recipeKey": "juling_pill", "batchCount": 1}))
        check_true("材料不足被拒", not res.get("success") and "材料不足" in str(res.get("message")))
        check("材料不足不扣货", st.item_count("灵草"), 4)

        # 正常炼制：5 灵草 + 1 灵芝 → 聚灵丹 ×1
        st = make_account("zzcraft_ok", items=[stack("灵草", 5), stack("灵芝", 1)])
        accounts.append(st.path)
        with ForcedRoll(0.0):   # 必成功
            res = handlers_growth.pill_craft(ctx_for(st, {"recipeKey": "juling_pill", "batchCount": 1,
                                                          "operationId": "op_c1"}))
        check_true("聚灵丹炼制成功", res.get("success"))
        check("产物名", res.get("result", {}).get("resultItemName"), "聚灵丹")
        check("产物在客户端道具表", core.client_has_item("聚灵丹"), True)
        check("灵草扣 5", st.item_count("灵草"), 0)
        check("灵芝扣 1", st.item_count("灵芝"), 0)
        check("背包聚灵丹 ×1", st.item_count("聚灵丹"), 1)

        # 幂等：同一 operationId 再打一次，不能再扣也不能再给
        with ForcedRoll(0.0):
            res2 = handlers_growth.pill_craft(ctx_for(st, {"recipeKey": "juling_pill", "batchCount": 1,
                                                           "operationId": "op_c1"}))
        check_true("重发标记 duplicate", res2.get("duplicate"))
        check("重发不再给丹", st.item_count("聚灵丹"), 1)

        # 失败：材料照扣、不给丹
        st = make_account("zzcraft_fail", items=[stack("灵草", 5), stack("灵芝", 1)])
        accounts.append(st.path)
        with ForcedRoll(0.99):  # 必失败
            res = handlers_growth.pill_craft(ctx_for(st, {"recipeKey": "juling_pill", "batchCount": 1,
                                                          "operationId": "op_c2"}))
        check_true("失败也是成功回包", res.get("success"))
        check("失败不给丹", st.item_count("聚灵丹"), 0)
        check("失败仍扣材料", st.item_count("灵草"), 0)
        check("失败计数", res.get("result", {}).get("failed"), 1)

        # 批量：3 份材料 → ×3
        st = make_account("zzcraft_batch", items=[stack("灵草", 15), stack("灵芝", 3)])
        accounts.append(st.path)
        with ForcedRoll(0.0):
            res = handlers_growth.pill_craft(ctx_for(st, {"recipeKey": "juling_pill", "batchCount": 3,
                                                          "operationId": "op_c3"}))
        check("批量产出 3", st.item_count("聚灵丹"), 3)
        check("批量扣 15 灵草", st.item_count("灵草"), 0)
        check("批量 total", res.get("result", {}).get("total"), 3)

        # 批量被材料上限收缩：要 5 份但只够 2 份
        st = make_account("zzcraft_clamp", items=[stack("灵草", 11), stack("灵芝", 2)])
        accounts.append(st.path)
        with ForcedRoll(0.0):
            handlers_growth.pill_craft(ctx_for(st, {"recipeKey": "juling_pill", "batchCount": 5,
                                                     "operationId": "op_c4"}))
        check("批量按材料上限收缩", st.item_count("聚灵丹"), 2)

        # 三条丹方都要能产出客户端认识的东西
        for key, recipe in handlers_growth.PILL_RECIPES.items():
            check("丹方产物在客户端：%s" % key, core.client_has_item(recipe["result"]), True)
            for mat in recipe["materials"]:
                check("丹方材料在客户端：%s/%s" % (key, mat), core.client_has_item(mat), True)

        # ================================================== 二、套装转换
        print("---- 套装转换")

        table = handlers_growth.suit_stats_table()
        check_true("套装数值表已加载", len(table) >= 10)
        check("1 号套装名", (table.get("1") or {}).get("name"), "青云套装")
        check("青云武器基础攻击", (table.get("1") or {}).get("slot_stats", {}).get("weapon", {}).get("attack"), 420)

        base = lambda sid, slot: int(
            (table[str(sid)]["slot_stats"][slot].get(handlers_growth.SUIT_SLOT_STAT[slot], 0)))

        # 正常转换
        eq = [suit_piece("eq_a", 1, "weapon", 400), suit_piece("eq_b", 1, "armor", 400),
              suit_piece("eq_c", 1, "boots", 380)]
        st = make_account("zzsuit_ok", equipment=eq)
        accounts.append(st.path)
        res = handlers_growth.equipment_suit_convert(
            ctx_for(st, {"equipmentIds": ["eq_a", "eq_b", "eq_c"], "operationId": "sop_1"}))
        check_true("套装转换成功", res.get("success"))
        inv = st.equipment.get("inventory", [])
        check("3 件变 1 件", len(inv), 1)
        new_item = inv[0]
        check("新件套装 id 不变", new_item.get("suit_id"), 1)
        check_true("新件部位合法", new_item.get("suit_slot") in handlers_growth.SUIT_SLOT_STAT)
        slot = new_item["suit_slot"]
        stat = handlers_growth.SUIT_SLOT_STAT[slot]
        value = int(new_item.get(stat, 0))
        lo, hi = base(1, slot) * 0.9, base(1, slot) * 1.1
        check_true("新件数值在 90%%~110%%（实际 %d）" % value, lo - 1 <= value <= hi + 1)
        check("新件名 = 套装名·部位", new_item.get("name"), "青云套装·%s" % handlers_growth.SUIT_SLOT_NAMES[slot])
        check("新件未穿戴", new_item.get("equipped"), False)
        check("新件强化等级 0", new_item.get("suit_enhance_level"), 0)
        save = res.get("saveData") or {}
        check_true("回包带 equipmentData", isinstance(save.get("equipmentData"), dict))
        check_true("回包带 result.equipment", isinstance((res.get("result") or {}).get("equipment"), dict))

        # 幂等
        res2 = handlers_growth.equipment_suit_convert(
            ctx_for(st, {"equipmentIds": ["eq_a", "eq_b", "eq_c"], "operationId": "sop_1"}))
        check_true("重发标记 duplicate", res2.get("duplicate"))
        check("重发不再扣件", len(st.equipment.get("inventory", [])), 1)

        # 数量不对
        st = make_account("zzsuit_num", equipment=[suit_piece("d1", 1, "weapon", 400)])
        accounts.append(st.path)
        res = handlers_growth.equipment_suit_convert(
            ctx_for(st, {"equipmentIds": ["d1"], "operationId": "sop_2"}))
        check_true("不足 3 件被拒", not res.get("success") and "3 件" in str(res.get("message")))

        # 不同套装
        eq = [suit_piece("e1", 1, "weapon", 400), suit_piece("e2", 1, "armor", 400),
              suit_piece("e3", 2, "boots", 600)]
        st = make_account("zzsuit_mix", equipment=eq)
        accounts.append(st.path)
        res = handlers_growth.equipment_suit_convert(
            ctx_for(st, {"equipmentIds": ["e1", "e2", "e3"], "operationId": "sop_3"}))
        check_true("跨套装被拒", not res.get("success") and "同一套装" in str(res.get("message")))
        check("跨套装不扣件", len(st.equipment.get("inventory", [])), 3)

        # 违规材料：锁定 / 已穿戴 / 已强化 / 未解封
        for tag, over, want_kw in [
            ("locked", {"locked": True}, "已锁定"),
            ("equipped", {"equipped": True}, "穿戴"),
            ("enhanced", {"suit_enhance_level": 3}, "已强化"),
            ("sealed", {"is_sealed": True}, "解封"),
        ]:
            eq = [suit_piece("f1_%s" % tag, 1, "weapon", 400),
                  suit_piece("f2_%s" % tag, 1, "armor", 400),
                  suit_piece("f3_%s" % tag, 1, "boots", 400, **over)]
            st = make_account("zzsuit_%s" % tag, equipment=eq)
            accounts.append(st.path)
            res = handlers_growth.equipment_suit_convert(
                ctx_for(st, {"equipmentIds": [x["id"] for x in eq], "operationId": "sop_%s" % tag}))
            check_true("%s 的件被拒（%s）" % (tag, want_kw),
                       not res.get("success") and want_kw in str(res.get("message")))
            check("%s 不扣件" % tag, len(st.equipment.get("inventory", [])), 3)

        # 五部位数值区间（各造 60 件）
        print("---- 数值区间抽样")
        for slot in handlers_growth.SUIT_SLOT_STAT:
            ok_range = True
            for _ in range(60):
                item = handlers_growth._make_suit_piece(1, slot)
                stat = handlers_growth.SUIT_SLOT_STAT[slot]
                v = int(item.get(stat, 0))
                lo, hi = base(1, slot) * 0.9, base(1, slot) * 1.1
                if not (lo - 1 <= v <= hi + 1):
                    ok_range = False
                    break
            check_true("%s 数值恒在 90%%~110%%" % handlers_growth.SUIT_SLOT_NAMES[slot], ok_range)

        # 每件套装都只有一个主属性
        item = handlers_growth._make_suit_piece(2, "helmet")
        non_zero = [k for k in ("attack", "defense", "hp", "mp", "agility") if int(item.get(k, 0)) > 0]
        check("单属性装备（只有 hp）", non_zero, ["hp"])
    finally:
        for p in accounts:
            try:
                p.unlink()
            except OSError:
                pass

    print()
    if FAILED:
        print("❌ 失败 %d 项：%s" % (len(FAILED), "；".join(FAILED)))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
