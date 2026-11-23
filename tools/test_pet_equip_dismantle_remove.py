#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宠物装备分解 / 丢弃后，背包里那件必须真的消失。

玩家报障（2026-11-04）：「宠物装备分解后，装备还在没消失」。

根因：`_authority_save` 里有一条老保护 —— 服务端侧宠装背包为空时
**故意不下发** `pet_equipment_items` 键（老账号服务端那份一直是空的，
发空数组会把客户端整格背包 clear 掉）。但这条保护没区分「本来就是空的」
和「刚被这次分解删空的」：

  分解掉背包里**最后一件** → 服务端那份变空 → 键被抹掉 →
  客户端 `inventory_manager.from_dict` 的 `if data.has("pet_equipment_items")`
  为假 → 整段跳过 → 本地背包原样保留 → 玩家看到「分解成功」，装备还在。

修法：删除类接口在回包里带 `removedPetEquipIds`，客户端按 id 精确移除；
同时 `_authority_save` 在明确有删除集合时允许下发空表。

守这些事：
  A. 单件分解（还剩几件）→ 回包整包背包已不含该件
  B. 单件分解（最后一件）→ 回包带 `pet_equipment_items: []` + removedPetEquipIds
  C. 批量分解（全部）→ 同上
  D. 丢弃（最后一件）→ 同上
  E. 模拟客户端 from_dict：清空后本地背包为空
  F. 非删除操作（穿戴）仍然不下发空表（保护老账号，别把玩家背包清空）

    python3 tools/test_pet_equip_dismantle_remove.py
"""
from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_dismantle_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_growth as hg  # noqa: E402
import handlers_extra as he  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def item(iid: str, name: str, slot: str = "accessory") -> dict:
    return {
        "id": iid, "name": name, "slot": slot, "rarity": "common",
        "attack": 0, "defense": 0, "agility": 3, "hp": 10,
        "is_pet_equipment": True, "pet_enhance_level": 0,
        "enhance_level": 0, "equipped": False, "locked": False,
    }


def new_state(tag: str, bag: list):
    st = core.State("u_" + tag)
    st.player["gold"] = 1_000_000
    st.inv["items"] = [{"id": "potion", "name": "金创药", "count": 5}]
    st.inv["pet_equipment_items"] = copy.deepcopy(bag)
    st.flush(force=True)
    return st


def dismantle(st, item_id: str) -> dict:
    ctx = core.Ctx(st, {"equipmentId": item_id, "operationId": "op_%s_%s" % (item_id, id(st))},
                   {}, None, {})
    return hg.pet_equipment_dismantle(ctx)


def dismantle_batch(st, ids: list) -> dict:
    ctx = core.Ctx(st, {"ids": ids, "operationId": "opb_%s" % id(st)}, {}, None, {})
    return he.pet_equipment_dismantle_batch(ctx)


def drop(st, item_id: str) -> dict:
    ctx = core.Ctx(st, {"equipmentId": item_id, "operationId": "opd_%s_%s" % (item_id, id(st))},
                   {}, None, {})
    return he.pet_equipment_drop(ctx)


def bag_ids(st):
    return [str(i.get("id")) for i in st.inv.get("pet_equipment_items", []) if isinstance(i, dict)]


def client_from_dict(local_bag: list, save_inventory: dict) -> list:
    """照抄客户端 `inventory_manager.from_dict` 的宠装分支。"""
    bag = [copy.deepcopy(x) for x in local_bag]
    if "pet_equipment_items" in save_inventory:
        bag = [copy.deepcopy(x) for x in (save_inventory.get("pet_equipment_items") or [])
               if isinstance(x, dict)]
    return bag


print("== A. 单件分解（背包还剩别的）==")
st = new_state("a", [item("a1", "青铜坠（宠）"), item("b1", "白银坠（宠）")])
local_bag = copy.deepcopy(st.inv["pet_equipment_items"])
r = dismantle(st, "a1")
check("分解成功", bool(r.get("success", True)) and "分解成功" in str(r.get("message")), str(r.get("message")))
check("服务端背包已删", bag_ids(st) == ["b1"], str(bag_ids(st)))
_inv = (r.get("saveData") or {}).get("inventoryData") or {}
check("回包整包背包已不含该件", "a1" not in [i.get("id") for i in (_inv.get("pet_equipment_items") or [])],
      str(_inv.get("pet_equipment_items")))
check("回包带 removedPetEquipIds", (r.get("saveData") or {}).get("removedPetEquipIds") == ["a1"],
      str((r.get("saveData") or {}).get("removedPetEquipIds")))
check("客户端背包 = 剩下那件", client_from_dict(local_bag, _inv) and
      [i.get("id") for i in client_from_dict(local_bag, _inv)] == ["b1"],
      str([i.get("id") for i in client_from_dict(local_bag, _inv)]))

print("== B. 单件分解（最后一件）==")
st = new_state("b", [item("only", "青铜坠（宠）")])
local_bag = copy.deepcopy(st.inv["pet_equipment_items"])
r = dismantle(st, "only")
check("分解成功", bool(r.get("success", True)) and "分解成功" in str(r.get("message")), str(r.get("message")))
check("服务端背包已清空", bag_ids(st) == [], str(bag_ids(st)))
_inv = (r.get("saveData") or {}).get("inventoryData") or {}
check("回包**仍然**带 pet_equipment_items 键（空数组）", "pet_equipment_items" in _inv,
      str(sorted(_inv.keys())))
check("该键是空数组", _inv.get("pet_equipment_items") == [], str(_inv.get("pet_equipment_items")))
check("回包带 removedPetEquipIds", (r.get("saveData") or {}).get("removedPetEquipIds") == ["only"],
      str((r.get("saveData") or {}).get("removedPetEquipIds")))
check("客户端背包被清空（这就是玩家报障的那一步）",
      [i.get("id") for i in client_from_dict(local_bag, _inv)] == [],
      str([i.get("id") for i in client_from_dict(local_bag, _inv)]))

print("== C. 批量分解（全部）==")
st = new_state("c", [item("c1", "青铜坠（宠）"), item("c2", "白银甲（宠）", slot="armor")])
local_bag = copy.deepcopy(st.inv["pet_equipment_items"])
r = dismantle_batch(st, ["c1", "c2"])
check("分解成功", "分解" in str(r.get("message", "")), str(r.get("message")))
check("服务端背包已清空", bag_ids(st) == [], str(bag_ids(st)))
_inv = (r.get("saveData") or {}).get("inventoryData") or {}
check("回包仍然带空数组", _inv.get("pet_equipment_items") == [], str(_inv.get("pet_equipment_items")))
check("removedPetEquipIds 含两件", sorted((r.get("saveData") or {}).get("removedPetEquipIds") or []) == ["c1", "c2"],
      str((r.get("saveData") or {}).get("removedPetEquipIds")))
check("客户端背包被清空",
      [i.get("id") for i in client_from_dict(local_bag, _inv)] == [],
      str([i.get("id") for i in client_from_dict(local_bag, _inv)]))

print("== D. 丢弃（最后一件）==")
st = new_state("d", [item("d1", "青铜坠（宠）")])
local_bag = copy.deepcopy(st.inv["pet_equipment_items"])
r = drop(st, "d1")
check("丢弃成功", "已丢弃" in str(r.get("message", "")), str(r.get("message")))
check("服务端背包已清空", bag_ids(st) == [], str(bag_ids(st)))
_inv = (r.get("saveData") or {}).get("inventoryData") or {}
check("回包仍然带空数组", _inv.get("pet_equipment_items") == [], str(_inv.get("pet_equipment_items")))
check("removedPetEquipIds 含该件", (r.get("saveData") or {}).get("removedPetEquipIds") == ["d1"],
      str((r.get("saveData") or {}).get("removedPetEquipIds")))
check("客户端背包被清空",
      [i.get("id") for i in client_from_dict(local_bag, _inv)] == [],
      str([i.get("id") for i in client_from_dict(local_bag, _inv)]))

print("== E. 分解不存在 / 已删过的 id ==")
st = new_state("e", [item("e1", "青铜坠（宠）")])
r = dismantle(st, "nope")
check("不存在的宠装报错", not bool(r.get("success", False)), str(r.get("error") or r.get("message")))
r = dismantle_batch(st, ["ghost1", "ghost2"])
check("批量全不存在报错", not bool(r.get("success", False)), str(r.get("error") or r.get("message")))
check("服务端背包未被误删", bag_ids(st) == ["e1"], str(bag_ids(st)))

print("== F. 非删除操作（穿戴）仍然不下发空表 ==")
st = new_state("f", [])
st.save["petEquipmentData"] = {"equipped": {}}
st.flush(force=True)
ctx = core.Ctx(st, {"equipmentId": "x", "slot": "accessory", "operationId": "op_f"}, {}, None, {})
r = hg.pet_equipment_equip(ctx)
check("服务端侧背包是空的（老账号形态）", bag_ids(st) == [], str(bag_ids(st)))
check("穿戴回包不下发 pet_equipment_items（保护客户端本地背包）",
      "pet_equipment_items" not in ((r.get("saveData") or {}).get("inventoryData") or {}),
      str(sorted(((r.get("saveData") or {}).get("inventoryData") or {}).keys())))
check("穿戴回包不带 removedPetEquipIds",
      "removedPetEquipIds" not in (r.get("saveData") or {}),
      str(list((r.get("saveData") or {}).keys())))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
