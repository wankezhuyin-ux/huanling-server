#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宠物装备：同部位换装 / 卸下 / 强化 的回归测试。

玩家报障（2026-10-29 截图）：身上戴着「青铜坠（宠）」，从宠物装备背包里点
「银月坠（宠）」穿戴，提示换上了，但**原来的青铜坠直接消失** ——
既不在身上，也不回背包。

根因：客户端 `PetEquipmentManager.to_dict()` 落盘的键名是
`pet_accessory / pet_weapon / ...`，而服务端 `_norm_pet_equip_slot()` 归一后
读写的是 `accessory / weapon / ...`。老代码 `worn.get("accessory")` 在
客户端上传的存档里恒为 None → 「换下来的旧装备」这一支永远不执行 →
旧装备被覆盖掉。卸下（`worn.pop(slot)`）同样会报「该部位没有宠物装备」。

守这些事：
  A. 旧装备键是客户端写法（pet_accessory）时，换装后旧装备回背包、新装备上身
  B. 存档里同一部位存在 accessory + pet_accessory 两个键（历史脏数据）时清理干净
  C. 卸下对两种键名都有效，且一定回背包
  D. 同一件重复穿戴不产生副本（不在背包留第二份）
  E. 背包里已有同 id 的重复条目时，穿戴后一并清理
  F. 身上穿着的装备可以强化（客户端不带 slot 也不拿错件）
  G. 回包 saveData.inventoryData 里带着换下来的旧装备（客户端靠它刷背包）

    python3 tools/test_pet_equip_swap.py
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_swap_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_growth as hg  # noqa: E402

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


def item(iid: str, name: str, slot: str = "accessory", agility: int = 3) -> dict:
    return {
        "id": iid, "name": name, "slot": slot, "rarity": "common",
        "attack": 0, "defense": 0, "agility": agility, "hp": 10,
        "is_pet_equipment": True, "pet_enhance_level": 0,
        "enhance_level": 0, "equipped": False, "locked": False,
    }


def new_state(tag: str):
    st = core.State("u_" + tag)
    st.player["gold"] = 10_000_000
    return st


def equip(st, item_id: str, slot: str = "accessory") -> dict:
    ctx = core.Ctx(st, {"equipmentId": item_id, "slot": slot,
                        "operationId": "op_%s_%d" % (item_id, id(st))}, {}, None, {})
    return hg.pet_equipment_equip(ctx)


def unequip(st, slot: str = "accessory") -> dict:
    ctx = core.Ctx(st, {"slot": slot, "operationId": "un_%s" % id(st)}, {}, None, {})
    return hg.pet_equipment_unequip(ctx)


def bag_ids(st):
    return [str(i.get("id")) for i in st.inv.get("pet_equipment_items", []) if isinstance(i, dict)]


def worn_names(st):
    return {str(k): str((v or {}).get("name")) for k, v in
            (st.save.get("petEquipmentData", {}).get("equipped", {}) or {}).items()}


def client_reconcile(local_bag, worn, changed_item):
    """照抄客户端 `main_ui._reconcile_pet_equipment_bag()` 的逻辑。

    客户端收到回包后：先把「身上穿着的」从本地背包里删掉，
    再把「回包 item 里这件（已经不在身上）」补回背包。
    这里用它验证玩家实际看到的背包。
    """
    bag = [copy.deepcopy(x) for x in local_bag]
    worn_ids = {str((v or {}).get("id")) for v in worn.values() if isinstance(v, dict)}
    bag = [x for x in bag if str(x.get("id")) not in worn_ids]
    if isinstance(changed_item, dict):
        cid = str(changed_item.get("id", ""))
        if cid and cid not in worn_ids and not any(str(x.get("id")) == cid for x in bag):
            bag.append(copy.deepcopy(changed_item))
    return [str(x.get("id")) for x in bag]


print("== A. 换装：旧装备回背包（客户端键名 pet_accessory）==")
st = new_state("a")
st.save["petEquipmentData"] = {"equipped": {"pet_accessory": item("old", "青铜坠（宠）")}}
local_bag = [item("new", "银月坠（宠）", agility=9), item("other", "金鳞甲（宠）", slot="armor")]
st.inv["pet_equipment_items"] = copy.deepcopy(local_bag)
st.flush(force=True)
r = equip(st, "new")
check("穿戴成功", bool(r.get("success", True)) and "银月坠" in str(r.get("message")), str(r.get("message")))
check("新装备上身", worn_names(st) == {"pet_accessory": "银月坠（宠）"}, str(worn_names(st)))
check("旧装备回背包", "old" in bag_ids(st), str(bag_ids(st)))
check("新装备离开背包", "new" not in bag_ids(st), str(bag_ids(st)))
check("回包不下发宠装背包（避免清空本地）",
      "pet_equipment_items" not in (r.get("saveData", {}).get("inventoryData") or {}),
      str(list((r.get("saveData", {}).get("inventoryData") or {}).keys())))
check("回包 item 是被换下来的旧装备",
      isinstance(r.get("item"), dict) and str(r["item"].get("id")) == "old", str(r.get("item")))
check("回包 equippedItem 是刚穿上的",
      isinstance(r.get("equippedItem"), dict) and str(r["equippedItem"].get("id")) == "new",
      str(r.get("equippedItem")))
check("回包 equipped 用客户端键名", "pet_accessory" in (r.get("equipped") or {}), str(r.get("equipped")))
_client_bag = client_reconcile(local_bag, r.get("equipped") or {}, r.get("item"))
check("客户端背包结果 = 旧装备 + 原有的其它件",
      set(_client_bag) == {"old", "other"}, str(_client_bag))

print("== B. 历史脏数据：accessory + pet_accessory 双键并存 ==")
st = new_state("b")
st.save["petEquipmentData"] = {"equipped": {
    "accessory": item("srv_old", "服务端旧写法"),
    "pet_accessory": item("cli_old", "客户端旧写法"),
}}
st.inv["pet_equipment_items"] = [item("new", "银月坠（宠）", agility=9)]
st.flush(force=True)
r = equip(st, "new")
check("只留一个饰品键", list(worn_names(st).keys()) == ["pet_accessory"], str(worn_names(st)))
check("当前饰品是新装备", worn_names(st) == {"pet_accessory": "银月坠（宠）"}, str(worn_names(st)))
check("两件旧装备都回背包", {"srv_old", "cli_old"} <= set(bag_ids(st)), str(bag_ids(st)))

print("== C. 卸下：两种键名都要能卸 ==")
for key in ("pet_accessory", "accessory"):
    st = new_state("c_" + key)
    st.save["petEquipmentData"] = {"equipped": {key: item("w1", "青铜坠（宠）")}}
    st.inv["pet_equipment_items"] = []
    st.flush(force=True)
    r = unequip(st, "accessory")
    check("键=%s 卸下成功" % key, "已卸下" in str(r.get("message", "")), str(r.get("message")))
    check("键=%s 卸下后回背包" % key, bag_ids(st) == ["w1"], str(bag_ids(st)))
    check("键=%s 卸下后槽位清空" % key, worn_names(st) == {}, str(worn_names(st)))

st = new_state("c_empty")
st.inv["pet_equipment_items"] = []
st.flush(force=True)
r = unequip(st, "accessory")
check("空槽位提示该部位没有装备", "没有宠物装备" in str(r.get("message", "")), str(r.get("message")))

print("== D. 重复穿戴同一件：不产生副本 ==")
st = new_state("d")
st.save["petEquipmentData"] = {"equipped": {"pet_accessory": item("same", "银月坠（宠）")}}
st.inv["pet_equipment_items"] = [item("same", "银月坠（宠）")]
st.flush(force=True)
equip(st, "same")
check("背包里不留副本", bag_ids(st) == [], str(bag_ids(st)))
check("身上还是那一件", worn_names(st) == {"pet_accessory": "银月坠（宠）"}, str(worn_names(st)))

print("== E. 背包里同 id 重复条目：穿戴后清干净 ==")
st = new_state("e")
st.save["petEquipmentData"] = {"equipped": {}}
dup = item("dup", "银月坠（宠）")
st.inv["pet_equipment_items"] = [copy.deepcopy(dup), copy.deepcopy(dup)]
st.flush(force=True)
equip(st, "dup")
check("重复条目清空", bag_ids(st) == [], str(bag_ids(st)))
check("身上只有一件", worn_names(st) == {"pet_accessory": "银月坠（宠）"}, str(worn_names(st)))

print("== F. 身上装备可以强化（客户端不带 slot）==")
st = new_state("f")
st.save["petEquipmentData"] = {"equipped": {"pet_helmet": item("h1", "金鳞盔（宠）", slot="helmet")}}
st.inv["pet_equipment_items"] = []
st.flush(force=True)
ctx = core.Ctx(st, {"equipmentId": "h1", "operationId": "enh1"}, {}, None, {})
r = hg.pet_equipment_enhance(ctx)
check("强化已穿戴装备成功", "强化成功" in str(r.get("message", "")), str(r.get("message")))
check("强化等级 +1", int(st.save["petEquipmentData"]["equipped"]["pet_helmet"]["enhance_level"]) == 1,
      str(st.save["petEquipmentData"]["equipped"]["pet_helmet"].get("enhance_level")))

print("== G. 五部位全换一遍：旧件全部回包 ==")
st = new_state("g")
slots = {"pet_weapon": ("w_old", "w_new", "weapon"),
         "pet_armor": ("a_old", "a_new", "armor"),
         "pet_boots": ("b_old", "b_new", "boots"),
         "pet_helmet": ("h_old", "h_new", "helmet"),
         "pet_accessory": ("y_old", "y_new", "accessory")}
st.save["petEquipmentData"] = {"equipped": {
    pk: item(old, "旧-" + pk, slot=sl) for pk, (old, _new, sl) in slots.items()}}
local_bag = [item(new, "新-" + pk, slot=sl) for pk, (_o, new, sl) in slots.items()]
st.inv["pet_equipment_items"] = copy.deepcopy(local_bag)
st.flush(force=True)
client_bag = copy.deepcopy(local_bag)
for pk, (_old, new, sl) in slots.items():
    r_i = equip(st, new, sl)
    client_bag = [{"id": x} for x in client_reconcile(client_bag, r_i.get("equipped") or {}, r_i.get("item"))]
check("五个旧件都在背包里",
      {v[0] for v in slots.values()} <= set(bag_ids(st)), str(bag_ids(st)))
check("五个新件都上身",
      set(worn_names(st).keys()) == set(slots.keys()), str(worn_names(st)))
check("背包里没有新品残留",
      not ({v[1] for v in slots.values()} & set(bag_ids(st))), str(bag_ids(st)))
_client_bag = [str(x.get("id")) for x in client_bag]
check("客户端逐次换装后背包里旧件齐全",
      {v[0] for v in slots.values()} <= set(_client_bag), str(_client_bag))
check("客户端背包里没有新品残留",
      not ({v[1] for v in slots.values()} & set(_client_bag)), str(_client_bag))

print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
