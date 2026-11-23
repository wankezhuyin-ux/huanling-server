#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""武道馆「自创武功」链路回归（2026-11-04）。

背景：玩家反馈「自创武功一直失败」。客户端 `martial_arts_manager.learn_martial_art`
在服务端判定成功之后，还要走这一段才认：

    payload = net.last_martial_create_payload           # /martial/create 的整包
    save_data = payload.get("saveData")
    art_raw   = payload.get("art")
    use_server_art = bool(payload.get("serverAuthority", false)) and (art 或 saveData)

也就是说服务端**必须**回 `serverAuthority: true` + `art`（+ `saveData`），
否则 rolledSuccess=True 也会被客户端判成「服务端未返回武功数据」= 失败。

    python3 tools/test_martial_create.py
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_martial_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_growth as hg  # noqa: E402

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


def account(username: str, gold: int = 5000000, realm: str = "凡人", vip: int = 0) -> core.State:
    save = {
        "playerData": {
            "level": 500, "experience": 0, "gold": gold,
            "current_map": core.DEFAULT_MAP, "realm": realm,
            "reincarnation": 0, "noble_level": vip,
            "attack_base": 100, "defense_base": 100, "agility_base": 100,
            "max_hp": 5000, "hp": 5000,
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


# 客户端 learn_martial_art 的判定，原样搬运，专门用来卡「服务端说成功、客户端说失败」
def client_would_accept(payload: dict) -> tuple[bool, str]:
    if not bool(payload.get("rolledSuccess", False)):
        return False, "参悟失败，未能领悟新的武功..."
    save_data = payload.get("saveData", None)
    art_raw = payload.get("art", None)
    use_server_art = bool(payload.get("serverAuthority", False)) and (
        isinstance(art_raw, dict) or isinstance(save_data, dict))
    if not use_server_art:
        return False, "服务端未返回武功数据，请稍后重试或重新登录同步。"
    if not isinstance(save_data, dict) or not save_data:
        if not isinstance(art_raw, dict):
            return False, "服务端领悟数据异常，请重新登录同步。"
    return True, ""


def main() -> int:
    print("=== 武道馆 自创武功 端到端 ===")

    # ---------------------------------------------------------- A 回包契约
    # 成功率改成 0.3 之后，固定种子的第一次不一定命中——这里循环到出一次成功，
    # 只验证「成功那一发」的回包契约，不验证概率本身（概率在 B 段统计）。
    st = account("martial_a", gold=2000000000)
    r: dict = {}
    for attempt in range(40):
        st.save["martialArtsData"]["daily_create_count"] = 0
        random.seed(1000 + attempt)
        r = hg.martial_create(ctx(st, {"operationId": "op_a%d" % attempt}))
        if r.get("rolledSuccess"):
            break
    check("A1 接口 success", bool(r.get("success")), str(r)[:150])
    check("A2 有 createCount（客户端靠它识别 martial_create）",
          "createCount" in r, str(sorted(r.keys())))
    check("A3 有 rolledSuccess", "rolledSuccess" in r, str(sorted(r.keys())))
    check("A4 有 newGold", "newGold" in r, str(sorted(r.keys())))
    accepted, why = client_would_accept(r)
    check("A5 客户端认这条回包（serverAuthority + art/saveData）", accepted, why)
    check("A6 回包带 art 且是字典", isinstance(r.get("art"), dict), str(r.get("art"))[:120])
    art = r.get("art") or {}
    if accepted:
        check("A7 art 有客户端要的六个键",
              all(k in art for k in ("name", "effect_type", "base_value", "desc", "layer", "realm")),
              str(sorted(art.keys())))

    # ---------------------------------------------------------- B 成功必落地
    random.seed(11)
    st2 = account("martial_b", gold=2000000000)
    before_gold = st2.gold()
    okc = 0
    paid = 0
    for i in range(120):
        rr = hg.martial_create(ctx(st2, {"operationId": "op_b%d" % i}))
        if rr.get("error") and "武功栏已满" not in str(rr.get("error")):
            # 次数上限了，补次数继续测概率
            st2.save["martialArtsData"]["daily_create_count"] = 0
            rr = hg.martial_create(ctx(st2, {"operationId": "op_bx%d" % i}))
        if not rr.get("error"):
            paid += 1
        if rr.get("rolledSuccess"):
            okc += 1
    total = len(st2.save["martialArtsData"].get("martial_arts", []))
    check("B1 120 次里确有成功（概率>0）", okc > 0, "success=%d" % okc)
    check("B2 成功次数 == 存档武学条数", okc == total, "ok=%d saved=%d" % (okc, total))
    check("B3 每次扣 100 万金币", st2.gold() == before_gold - paid * 1000000,
          "%d -> %d（实扣 %d 次）" % (before_gold, st2.gold(), paid))
    check("B4 武功栏上限 15（满了要拒绝且不扣钱）", total <= 15, "arts=%d" % total)

    # ---------------------------------------------------------- C 次数/金币门禁
    st3 = account("martial_c", gold=500000)
    r3 = hg.martial_create(ctx(st3, {"operationId": "op_c1"}))
    check("C1 金币不足要拒绝", bool(r3.get("error")), str(r3)[:120])
    check("C2 金币不足不扣次数",
          int(st3.save["martialArtsData"].get("daily_create_count", 0)) == 0,
          str(st3.save["martialArtsData"].get("daily_create_count")))

    st4 = account("martial_d")
    st4.save["martialArtsData"]["daily_create_count"] = 99
    st4.save["martialArtsData"]["date"] = core.today()
    r4 = hg.martial_create(ctx(st4, {"operationId": "op_d1"}))
    check("C3 次数用完要拒绝", bool(r4.get("error")), str(r4)[:120])
    check("C4 次数用完不扣金币", st4.gold() == 5000000, str(st4.gold()))

    # ---------------------------------------------------------- D 每日重置
    st5 = account("martial_e")
    st5.save["martialArtsData"] = {"date": "2000-01-01", "daily_create_count": 9,
                                   "martial_arts": [], "equipped_ids": []}
    info = hg.martial_info(ctx(st5))
    check("D1 隔天次数归零", int(info.get("createCount", -1)) == 0, str(info.get("createCount")))
    check("D2 limit 按 VIP 档（VIP0=3）", int(info.get("dailyLimit", 0)) == 3,
          str(info.get("dailyLimit")))

    # ---------------------------------------------------------- E 境界池
    st6 = account("martial_f", gold=2000000000, realm="天仙")
    random.seed(3)
    r6 = None
    for i in range(200):
        st6.save["martialArtsData"]["daily_create_count"] = 0
        r6 = hg.martial_create(ctx(st6, {"operationId": "op_f%d" % i}))
        if r6.get("rolledSuccess"):
            break
    art6 = (r6 or {}).get("art") or {}
    check("E1 天仙自创落在天仙池", str(art6.get("realm", "")) == "天仙", str(art6.get("realm")))
    check("E2 天仙池 base_value>=9", float(art6.get("base_value", 0) or 0) >= 6,
          str(art6.get("base_value")))

    # ---------------------------------------------------------- F 幂等（同 operationId）
    st7 = account("martial_g", gold=20000000)
    random.seed(21)
    f1 = hg.martial_create(ctx(st7, {"operationId": "same_op"}))
    gold_after_first = st7.gold()
    f2 = hg.martial_create(ctx(st7, {"operationId": "same_op"}))
    check("F1 重放走 duplicate", bool(f2.get("duplicate")), str(f2.get("duplicate")))
    check("F2 重放不再扣金币", st7.gold() == gold_after_first,
          "%d -> %d" % (gold_after_first, st7.gold()))
    check("F3 重放不再扣次数",
          int(st7.save["martialArtsData"].get("daily_create_count", 0)) == 1,
          str(st7.save["martialArtsData"].get("daily_create_count")))
    check("F4 重放回包仍带 saveData/art 键",
          "saveData" in f2 and "art" in f2, str(sorted(f2.keys())))

    # ---------------------------------------------------------- G 技能书
    st8 = account("martial_h")
    st8.add_item("一箭三雕", 1, "martial_skill_book")
    g1 = hg.martial_skill_book_use(ctx(st8, {"bookName": "一箭三雕", "operationId": "op_g1"}))
    check("G1 技能书使用成功", bool(g1.get("success")) and not g1.get("error"), str(g1)[:120])
    check("G2 书被扣掉", st8.item_count("一箭三雕") == 0, str(st8.item_count("一箭三雕")))
    ga = g1.get("art") or {}
    check("G3 回包 art 有 name/effect_type", bool(ga.get("name")) and ga.get("effect_type") == "triple_shot",
          str(ga)[:120])
    check("G4 layer 落在 1~9", 1 <= int(ga.get("layer", 0)) <= 9, str(ga.get("layer")))
    check("G5 回包带 saveData.martialArtsData",
          isinstance(g1.get("saveData"), dict)
          and isinstance((g1["saveData"] or {}).get("martialArtsData"), dict), str(g1.get("saveData"))[:100])
    st8.add_item("一箭三雕", 1, "martial_skill_book")
    g2 = hg.martial_skill_book_use(ctx(st8, {"bookName": "不存在的书"}))
    check("G6 未知道具名要拒绝", bool(g2.get("error")), str(g2)[:100])
    check("G7 拒绝时不扣书", st8.item_count("一箭三雕") == 1, str(st8.item_count("一箭三雕")))

    # ---------------------------------------------------------- H 遗忘 / 布局
    st9 = account("martial_i")
    st9.add_item("一箭三雕", 1, "martial_skill_book")
    h1 = hg.martial_skill_book_use(ctx(st9, {"bookName": "一箭三雕"}))
    hid = str((h1.get("art") or {}).get("id", ""))
    check("H1 学到一条", len(st9.save["martialArtsData"]["martial_arts"]) == 1, str(h1)[:80])
    # 客户端发的是 artId
    h2 = hg.martial_forget(ctx(st9, {"artId": hid}))
    check("H2 artId 能删掉（字段名对齐客户端）",
          len(st9.save["martialArtsData"]["martial_arts"]) == 0, str(h2)[:100])
    # 布局：客户端发的是嵌套 martialArtsData
    h3 = hg.martial_sync_layout(ctx(st9, {"martialArtsData": {"equipped_ids": ["a1", "a2"]}}))
    check("H3 嵌套 equipped_ids 能存下来",
          st9.save["martialArtsData"].get("equipped_ids") == ["a1", "a2"], str(h3)[:100])
    h4 = hg.martial_sync_layout(ctx(st9, {"martialArtsData": {"martial_arts": [], "equipped_ids": []}}))
    check("H4 空布局不会把已有值顶成脏数据",
          st9.save["martialArtsData"].get("equipped_ids") == [], str(h4)[:100])

    # ---------------------------------------------------------- I 贵族成功率加成
    st10 = account("martial_j")
    st10.save["playerData"]["noble_level"] = 0
    info0 = hg.martial_info(ctx(st10))
    # 原版客户端 martial_arts_manager.gd 三处默认参数都是 0.3（30%）
    check("I1 平民基础成功率 0.3", abs(float(info0.get("serverSuccessRate", 0)) - 0.3) < 1e-6,
          str(info0.get("serverSuccessRate")))
    check("I2 平民加成 0", int(info0.get("nobleBonus", -1)) == 0, str(info0.get("nobleBonus")))

    st11 = account("martial_k")
    st11.save["playerData"]["noble_level"] = 15
    info15 = hg.martial_info(ctx(st11))
    rate15 = float(info15.get("serverSuccessRate", 0))
    check("I3 皇帝（贵族 15）加成 +50", int(info15.get("nobleBonus", 0)) == 50,
          str(info15.get("nobleBonus")))
    check("I4 皇帝成功率 = 0.3 + 0.5 = 0.8", abs(rate15 - 0.8) < 1e-6, str(rate15))
    check("I5 回包保留基础值", abs(float(info15.get("baseSuccessRate", 0)) - 0.3) < 1e-6,
          str(info15.get("baseSuccessRate")))

    print("\n通过 %d，失败 %d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
