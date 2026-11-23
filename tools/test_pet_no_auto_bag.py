#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统发放的宠物**不许**进宠物袋（只进宠物栏，栏满走邮件）。

玩家反馈：「获得的宠物直接进宠物袋了。宠物袋应该是玩家自己手动存进去的，
系统不能有自主获得进宠物袋的。」

本测试盯住三条产出链路：
  1. 开礼包 / 签到 / 活动发宠   handlers_growth._add_pet_to_state
  2. 仙魔积分兑换              handlers_play.xianmo_pet_exchange
  3. 野外捕捉                  core.capture_wild_pet（栏满放生，不写袋）

跑法：python3 tools/test_pet_no_auto_bag.py
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

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_petbag_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_growth as hg  # noqa: E402
import handlers_play as hp  # noqa: E402

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


def pet_row(idx: int) -> dict:
    return {
        "id": "test_pet_%d" % idx,
        "pet_id": "kuxinggui",
        "name": "测试灵兽%d" % idx,
        "level": 1,
        "experience": 0,
        "generation": 0,
        "total_growth": 5.5,
        "growth_rates": {},
        "mutation_count": 0,
    }


def account(username: str, roster: int = 0, bag: int = 0) -> core.State:
    save = {
        "playerData": {
            "level": 500, "experience": 0, "gold": 10000000,
            "current_map": core.DEFAULT_MAP, "realm": "凡人",
            "reincarnation": 0, "noble_level": 0,
            "attack_base": 100, "defense_base": 100, "agility_base": 100,
            "max_hp": 5000, "hp": 5000,
        },
        "inventoryData": {"items": [], "pet_equipment_items": []},
        "gameSettings": {"current_map": core.DEFAULT_MAP},
        "petsData": {
            "pets": [pet_row(i) for i in range(roster)],
            "pet_bag": [pet_row(1000 + i) for i in range(bag)],
            "active_pet_id": "test_pet_0" if roster else "",
        },
    }
    path = core.STATE_DIR / "accounts" / (core._safe_name(username) + ".json")
    path.write_text(json.dumps({"save": save, "meta": {}}, ensure_ascii=False), encoding="utf-8")
    return core.State(username)


def ctx(state, body=None):
    return core.Ctx(state, body or {}, {}, None, {})


def mails(state) -> list:
    return state.meta.get("mails") or []


def bag_ids(state) -> list:
    return [str(p.get("id", "")) for p in state.pet_bag]


def main() -> int:
    print("=== 系统发放宠物不进宠物袋 ===")

    # ---------------------------------------------------------- A 礼包开宠
    st = account("petbag_a")
    where = hg._add_pet_to_state(st, pet_row(1))
    check("A1 宠物栏有空位 → 进宠物栏", where == "pets", where)
    check("A2 宠物栏 +1", len(st.pets) == 1, str(len(st.pets)))
    check("A3 宠物袋没变", bag_ids(st) == [], str(bag_ids(st)))

    st2 = account("petbag_b", roster=10)
    before_bag = bag_ids(st2)
    where2 = hg._add_pet_to_state(st2, pet_row(2))
    check("A4 宠物栏满 → 走邮件（不是宠物袋）", where2 == "mail", where2)
    check("A5 宠物栏仍是 10 只", len(st2.pets) == 10, str(len(st2.pets)))
    check("A6 宠物袋一只没多", bag_ids(st2) == before_bag, str(bag_ids(st2)))
    ms = mails(st2)
    check("A7 发出了补发邮件", len(ms) == 1, str(len(ms)))
    if ms:
        att = ms[0].get("attachments") or {}
        pets_att = att.get("pets") or []
        check("A8 邮件附件里带着这只宠", len(pets_att) == 1,
              json.dumps(att, ensure_ascii=False)[:160])
        check("A9 邮件标题点明是宠物补发", "宠物" in str(ms[0].get("title", "")),
              str(ms[0].get("title")))

    # 宠物袋本来就满的情况也不许被塞（以前是「栏满→袋满→邮件」）
    st3 = account("petbag_c", roster=10, bag=50)
    where3 = hg._add_pet_to_state(st3, pet_row(3))
    check("A10 栏满袋满 → 仍然只走邮件", where3 == "mail", where3)
    check("A11 宠物袋仍是 50 只", len(st3.pet_bag) == 50, str(len(st3.pet_bag)))

    # ---------------------------------------------------------- B 仙魔兑换
    # _xianmo() 会在周切换时把积分清零，测试里先把周键写成当前周
    st4 = account("petbag_d")
    box4 = st4.meta.setdefault("xianmo", {})
    box4["week"] = hp._xianmo_week_key()
    box4["score"] = 500
    before_bag4 = bag_ids(st4)
    r4 = hp.xianmo_pet_exchange(ctx(st4))
    check("B1 仙魔兑换成功", bool(r4.get("success")), json.dumps(r4, ensure_ascii=False)[:160])
    check("B2 兑换不写宠物袋", bag_ids(st4) == before_bag4, str(bag_ids(st4)))
    check("B3 兑换扣了 100 积分", int(st4.meta.get("xianmo", {}).get("score", 0)) == 400,
          str(st4.meta.get("xianmo", {}).get("score")))
    check("B4 兑换发的是礼包邮件", len(mails(st4)) >= 1, str(len(mails(st4))))
    if mails(st4):
        att = mails(st4)[0].get("attachments") or {}
        names = [str(i.get("name", "")) for i in (att.get("items") or [])]
        check("B5 邮件里是仙魔灵兽礼包", any("仙魔" in n for n in names), str(names))

    st5 = account("petbag_e")
    box5 = st5.meta.setdefault("xianmo", {})
    box5["week"] = hp._xianmo_week_key()
    box5["score"] = 50
    r5 = hp.xianmo_pet_exchange(ctx(st5))
    check("B6 积分不足时拒绝", not r5.get("success"), json.dumps(r5, ensure_ascii=False)[:120])
    check("B7 拒绝时不扣积分", int(st5.meta.get("xianmo", {}).get("score", 0)) == 50,
          str(st5.meta.get("xianmo", {}).get("score")))
    check("B8 拒绝时不发邮件", len(mails(st5)) == 0, str(len(mails(st5))))

    # ---------------------------------------------------------- C 野外捕捉
    st6 = account("petbag_f", roster=10)
    before_bag6 = bag_ids(st6)
    # 捕获本身有 10% 命中率，这里把概率钉死成必中，只验证「满栏时的去向」
    species = core.pet_species_table()
    species_id = next(iter(species.keys())) if species else "kuxinggui"
    old_rate = core.capture_rate
    core.capture_rate = lambda: 1.0
    try:
        res = core.capture_wild_pet(st6, species_id, "测试图", 0.0, False)
    finally:
        core.capture_rate = old_rate
    reason = str(res.get("reason", ""))
    check("C1 宠物栏满时捕捉返回 roster_full", reason == "roster_full", reason)
    check("C2 捕捉没往宠物袋塞", bag_ids(st6) == before_bag6, str(bag_ids(st6)))
    check("C3 宠物栏还是 10 只", len(st6.pets) == 10, str(len(st6.pets)))

    # ---------------------------------------------------------- D 源码守卫
    # 上面 A~C 只盯住已修的三条链路。新写的发宠代码要是又偷偷 append 到 pet_bag，
    # 这里直接让 CI 红掉：全仓扫一遍 pet_bag.append 出现在哪些函数里。
    import ast

    def bag_append_funcs(fname: str) -> set:
        path = ROOT / fname
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: set = set()

        def walk(node, stack):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, stack + [child.name])
                    continue
                func = getattr(child, "func", None)
                if (isinstance(func, ast.Attribute) and func.attr == "append"
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr == "pet_bag"):
                    found.add(stack[-1] if stack else "<module>")
                walk(child, stack)

        walk(tree, [])
        return found

    check("D1 core.py 没有自动塞袋", bag_append_funcs("core.py") == set(),
          str(bag_append_funcs("core.py")))
    check("D2 handlers_play.py 没有自动塞袋", bag_append_funcs("handlers_play.py") == set(),
          str(bag_append_funcs("handlers_play.py")))
    check("D3 handlers_battle.py 没有自动塞袋", bag_append_funcs("handlers_battle.py") == set(),
          str(bag_append_funcs("handlers_battle.py")))
    check("D4 handlers_growth.py 只剩玩家手动的 /pet/bag/store",
          bag_append_funcs("handlers_growth.py") == {"pet_bag_store"},
          str(bag_append_funcs("handlers_growth.py")))

    gm_src = (ROOT / "gm.py").read_text(encoding="utf-8")
    check("D5 GM 后台没有直接 pet_bag.append", "pet_bag.append" not in gm_src)
    n_alias = gm_src.count("bag.append(pet)")
    check("D6 GM 里唯一一处写袋（add_pet）", n_alias == 1, str(n_alias))
    if n_alias == 1:
        at = gm_src.index("bag.append(pet)")
        window = gm_src[max(0, at - 900):at]
        check("D7 那处写袋挂在 to_bag 显式开关下", "to_bag" in window,
              window[-100:].strip())
    check("D8 新手锦囊不再把宠塞袋",
          "starter_pack" in gm_src and "state.pet_bag.append(pet)" not in gm_src)

    add_src = __import__("inspect").getsource(hg._add_pet_to_state)
    check("D9 _add_pet_to_state 里已经不提 pet_bag", "pet_bag" not in add_src,
          add_src.strip()[:120])

    print("\n通过 %d，失败 %d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
