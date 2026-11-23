#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法宝系统服务端端到端回归（2026-11-04「对照原版看有没有生效」）。

客户端 `scripts/fabao/*` 只做渲染 —— 法宝实例（词条/数值）全由服务端产出。
所以「法宝到底有没有生效」要在这里验：

  A. 合成：扣 100 碎片 → 背包 +1 件、5 条词条、exp_pct 按档位给
  B. 合成材料不足要拒绝，且不能扣东西
  C. 装备/卸下：inventory ↔ equipped 互换，不会复制也不会丢
  D. 洗练两阶段：先 409 + token 并扣 1 颗石头；confirm(apply) 才算落地
  E. 洗练 confirm(false) 不改变词条（石头照样不退，符合原版）
  F. 锁定词条扣 (已锁数+1)×100 元宝，锁住的组洗练时不变
  G. 分解得 50 碎片，实例消失
  H. 难度：投胎不够要拒绝；够了才写 fabao.current_difficulty + gameSettings
  I. 战斗中真的读法宝经验：handlers_battle._fabao_exp_pct == 实例 exp_pct
  J. 战斗属性用客户端上传的 final_attack（已含法宝加成），不是 attack_base

    python3 tools/test_fabao_server.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_fabao_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_battle as hb  # noqa: E402
import handlers_fabao as hf  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("[OK]   " + name)
    else:
        FAIL += 1
        print("[FAIL] " + name + ("  → " + detail if detail else ""))


def account(username: str, reincarnation: int = 0) -> core.State:
    save = {
        "playerData": {
            "level": 500, "experience": 0, "gold": 1000,
            "current_map": core.DEFAULT_MAP,
            "reincarnation": reincarnation, "noble_level": 0,
            "attack_base": 100, "defense_base": 100, "agility_base": 100,
            "max_hp": 5000, "hp": 5000,
            # 客户端 to_dict 上传的是「已含法宝」的最终属性
            "final_attack": 8800, "final_defense": 6600,
            "final_agility": 4400, "final_max_hp": 99000,
            "element": "水", "use_fabao_player_element": True,
        },
        "inventoryData": {"items": [], "pet_equipment_items": [],
                          "warehouse_items": [], "xianmo_chests": []},
        "gameSettings": {"current_map": core.DEFAULT_MAP},
        "martialArtsData": {"martial_arts": [], "equipped_ids": []},
        "petsData": {"pets": [], "active_pet_id": ""},
    }
    path = core.STATE_DIR / "accounts" / (core._safe_name(username) + ".json")
    path.write_text(json.dumps({"save": save, "meta": {}}, ensure_ascii=False), encoding="utf-8")
    return core.State(username)


def ctx(state, body=None):
    return core.Ctx(state, body or {}, {}, None, {})


def main() -> int:
    print("=== 法宝服务端端到端 ===")

    # ---------------------------------------------------------------- A 合成
    st = account("fabaoprobe1")
    st.add_item("普通法宝碎片", 100, "material")
    r = hf.fabao_synthesize(ctx(st, {"tier": "normal"}))
    box = st.save.get("fabao") or {}
    inv = box.get("inventory") or []
    inst = inv[0] if inv else {}
    check("A1 合成成功", bool(r.get("success", True)) and not r.get("error"), str(r)[:120])
    check("A2 碎片扣掉 100", st.item_count("普通法宝碎片") == 0,
          str(st.item_count("普通法宝碎片")))
    check("A3 背包多一件法宝", len(inv) == 1, str(len(inv)))
    check("A4 词条 5 条", len(inst.get("affixes") or []) == 5,
          str(len(inst.get("affixes") or [])))
    check("A5 exp_pct = 2.5（普通档）", abs(float(inst.get("exp_pct", 0)) - 2.5) < 1e-6,
          str(inst.get("exp_pct")))
    check("A6 词条数值在 combatCap 内", all(
        (a.get("group") in ("player_element", "evolve_mutate"))
        or 1 <= int(a.get("value", 0)) <= 5 for a in (inst.get("affixes") or [])
    ), str(inst.get("affixes")))

    # ---------------------------------------------------------------- B 材料不足
    st2 = account("fabaoprobe2")
    st2.add_item("普通法宝碎片", 99, "material")
    r2 = hf.fabao_synthesize(ctx(st2, {"tier": "normal"}))
    check("B1 碎片不足被拒", bool(r2.get("error")), str(r2)[:100])
    check("B2 不足时碎片没被扣", st2.item_count("普通法宝碎片") == 99,
          str(st2.item_count("普通法宝碎片")))

    # ---------------------------------------------------------------- C 装备/卸下
    fid = str(inst.get("id", ""))
    r3 = hf.fabao_equip(ctx(st, {"fabaoId": fid}))
    box = st.save.get("fabao") or {}
    check("C1 装备成功", isinstance(box.get("equipped"), dict)
          and str(box["equipped"].get("id")) == fid, str(r3)[:120])
    check("C2 装备后从背包移除", len(box.get("inventory") or []) == 0,
          str(len(box.get("inventory") or [])))
    r4 = hf.fabao_unequip(ctx(st))
    box = st.save.get("fabao") or {}
    check("C3 卸下回背包", box.get("equipped") is None
          and len(box.get("inventory") or []) == 1, str(r4)[:120])
    hf.fabao_equip(ctx(st, {"fabaoId": fid}))

    # ---------------------------------------------------------------- I 战斗读法宝经验
    pct = hb._fabao_exp_pct(st)
    check("I1 战斗经验读法宝 exp_pct", abs(pct - 2.5) < 1e-6, str(pct))

    # 换传说档验证档位差
    st_legend = account("fabaoprobe3")
    st_legend.add_item("传说法宝碎片", 100, "material")
    hf.fabao_synthesize(ctx(st_legend, {"tier": "legend"}))
    lg = (st_legend.save["fabao"]["inventory"] or [{}])[0]
    hf.fabao_equip(ctx(st_legend, {"fabaoId": lg["id"]}))
    check("I2 传说法宝经验 12.5%", abs(hb._fabao_exp_pct(st_legend) - 12.5) < 1e-6,
          str(hb._fabao_exp_pct(st_legend)))
    check("I3 传说档词条上限 20", all(
        int(a.get("value", 0)) <= 20 for a in lg.get("affixes", [])
        if a.get("group") not in ("player_element", "evolve_mutate", "armor_pierce_rate")
    ), str(lg.get("affixes")))

    # ---------------------------------------------------------------- J 战斗属性
    f = hb.player_fighter(ctx(st))
    check("J1 战斗攻击取 final_attack（含法宝）", int(f.attack) == 8800, str(f.attack))
    check("J2 战斗血量取 final_max_hp", int(f.max_hp) == 99000, str(f.max_hp))

    # ---------------------------------------------------------------- D 洗练两阶段
    st.add_item("法宝洗练石", 1, "material")
    # 洗练只能对背包里的法宝，先卸下
    hf.fabao_unequip(ctx(st))
    before = [dict(a) for a in (st.save["fabao"]["inventory"][0]["affixes"])]
    r5 = hf.fabao_reroll(ctx(st, {"fabaoId": fid}))
    status = r5.get("_status", 200)
    pending = r5.get("pending") or {}
    check("D1 洗练返回 409 待确认", status == 409 and bool(pending.get("token")),
          str(status) + " " + str(r5)[:80])
    check("D2 洗练扣 1 颗石头", st.item_count("法宝洗练石") == 0,
          str(st.item_count("法宝洗练石")))
    check("D3 预览条数与当前一致",
          len(pending.get("previewAffixes") or []) == len(before),
          str(len(pending.get("previewAffixes") or [])))
    token = str(pending.get("token", ""))
    r6 = hf.fabao_reroll_confirm(ctx(st, {"token": token, "apply": False}))
    after_keep = st.save["fabao"]["inventory"][0]["affixes"]
    check("D4 放弃洗练：词条不变", after_keep == before, str(after_keep))

    # 再来一次，这次应用
    st.add_item("法宝洗练石", 1, "material")
    r7 = hf.fabao_reroll(ctx(st, {"fabaoId": fid}))
    token2 = str((r7.get("pending") or {}).get("token", ""))
    preview = (r7.get("pending") or {}).get("previewAffixes") or []
    r8 = hf.fabao_reroll_confirm(ctx(st, {"token": token2, "apply": True}))
    after_apply = st.save["fabao"]["inventory"][0]["affixes"]
    check("D5 应用洗练：词条换成预览", after_apply == preview, str(r8)[:100])
    check("D6 洗练后词条种类不变",
          sorted(str(a.get("group")) for a in after_apply) ==
          sorted(str(a.get("group")) for a in before), str(after_apply))

    # ---------------------------------------------------------------- F 锁定
    st.add_yuanbao(1000)
    target_group = ""
    for a in after_apply:
        if hf._is_lockable_group(str(a.get("group", ""))):
            target_group = str(a.get("group"))
            break
    r9 = hf.fabao_affix_lock(ctx(st, {"fabaoId": fid, "group": target_group}))
    check("F1 锁定扣 100 元宝", st.yuanbao() == 900, str(st.yuanbao()))
    check("F2 lockedGroups 记上", target_group in (
        st.save["fabao"]["inventory"][0].get("lockedGroups") or []),
        str(st.save["fabao"]["inventory"][0].get("lockedGroups")))
    # 锁定后洗练该组不变
    st.add_item("法宝洗练石", 1, "material")
    locked_value = next(a for a in st.save["fabao"]["inventory"][0]["affixes"]
                        if str(a.get("group")) == target_group)
    r10 = hf.fabao_reroll(ctx(st, {"fabaoId": fid}))
    prev = (r10.get("pending") or {}).get("previewAffixes") or []
    kept = next((a for a in prev if str(a.get("group")) == target_group), {})
    check("F3 锁定词条洗练时保持不变", kept == locked_value, str(kept) + " vs " + str(locked_value))
    hf.fabao_reroll_confirm(ctx(st, {"token": str((r10.get("pending") or {}).get("token", "")),
                                     "apply": True}))
    r11 = hf.fabao_affix_unlock(ctx(st, {"fabaoId": fid, "group": target_group}))
    check("F4 解锁不退款", st.yuanbao() == 900, str(st.yuanbao()))

    # ---------------------------------------------------------------- G 分解
    frag_before = st.item_count("普通法宝碎片")
    r12 = hf.fabao_dismantle(ctx(st, {"fabaoId": fid}))
    check("G1 分解得 50 碎片", st.item_count("普通法宝碎片") - frag_before == 50,
          str(st.item_count("普通法宝碎片") - frag_before))
    check("G2 实例已消失", all(str(x.get("id")) != fid
                              for x in (st.save["fabao"].get("inventory") or [])),
          str(st.save["fabao"].get("inventory")))

    # 已装备的不许分解
    hf.fabao_equip(ctx(st_legend, {"fabaoId": lg["id"]}))
    r13 = hf.fabao_dismantle(ctx(st_legend, {"fabaoId": lg["id"]}))
    check("G3 已装备的不能分解", bool(r13.get("error")), str(r13)[:80])

    # ---------------------------------------------------------------- H 难度
    st3 = account("fabaoprobe4", reincarnation=0)
    r14 = hf.fabao_set_difficulty(ctx(st3, {"tier": "hard"}))
    check("H1 投胎 0 次不能开困难（需 100）", bool(r14.get("error")), str(r14)[:100])
    check("H2 被拒时难度没变",
          (st3.save.get("fabao") or {}).get("current_difficulty", "normal") == "normal",
          str((st3.save.get("fabao") or {}).get("current_difficulty")))
    st4 = account("fabaoprobe5", reincarnation=150)
    r15 = hf.fabao_set_difficulty(ctx(st4, {"tier": "hard"}))
    check("H3 投胎 150 次可开困难",
          not r15.get("error")
          and st4.save["fabao"]["current_difficulty"] == "hard", str(r15)[:100])
    check("H4 同时写 gameSettings（重登不回落）",
          st4.save["gameSettings"].get("current_map_difficulty") == "hard",
          str(st4.save["gameSettings"].get("current_map_difficulty")))
    r16 = hf.fabao_set_difficulty(ctx(st4, {"tier": "wushang"}))
    check("H5 无上需投胎 1000 次，150 次被拒", bool(r16.get("error")), str(r16)[:100])

    # ---------------------------------------------------------------- 收尾
    print("")
    print("通过 %d / 失败 %d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
