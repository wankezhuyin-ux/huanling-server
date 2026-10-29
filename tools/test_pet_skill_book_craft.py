#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技能书「2合1进阶 / 拆分」的回归测试。

玩家报障：背包里点【追击lv1技能书】的「2合1进阶」，确认后弹「合成失败 灵兽不存在」。
根因是服务端 /pet_skill/action 先查出战宠（客户端合成请求不带 petId），
且 synthetic/split 分支原本直接回「该技能书操作暂未开放」。

守这些事：
  1. lv1 ×2 → lv2 ×1（背包扣干净，产物进背包）
  2. 背包 1 + 仓库 1 也能合（客户端按钮按「背包+仓库」总数显示）
  3. lv5 不能再合、lv1 不能拆
  4. 数量不足时一本都不扣（不能吃掉一半）
  5. split lvN → lv1 × 2^(N-1)
  6. 同一个 operation_id 重复请求只生效一次

    python3 tools/test_pet_skill_book_craft.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lyskbook_"))

import handlers_growth as hg  # noqa: E402  （import 即注册路由）

PASS = 0
FAIL = 0
CHASE1 = "追击lv1技能书"
CHASE2 = "追击lv2技能书"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def new_state(account: str, bag: dict[str, int] | None = None,
              warehouse: dict[str, int] | None = None) -> core.State:
    st = core.State(account)
    st.items.clear()
    for name, n in (bag or {}).items():
        st.add_item(name, n, "pet_skill_book")
    inv = st.inv
    inv["warehouse_items"] = []
    for name, n in (warehouse or {}).items():
        inv["warehouse_items"].append({
            "id": "w_%s_%d" % (name, n), "name": name, "count": n,
            "item_type": "pet_skill_book", "custom_data": {},
        })
    return st


def call(st: core.State, action: str, book: str, *, count: int | None = 2,
         op_id: str = "") -> dict:
    params: dict = {"book_name": book}
    if count is not None:
        params["book_count"] = count
    if op_id:
        params["operation_id"] = op_id
    ctx = core.Ctx(st, {"action": action, "params": params}, {}, {}, {})
    return hg.pet_skill_action(ctx)


def bag_count(st: core.State, name: str) -> int:
    return sum(int(i.get("count", 0) or 0) for i in st.items
               if isinstance(i, dict) and str(i.get("name", "")) == name)


def wh_count(st: core.State, name: str) -> int:
    arr = st.inv.get("warehouse_items") or []
    return sum(int(i.get("count", 0) or 0) for i in arr
               if isinstance(i, dict) and str(i.get("name", "")) == name)


def main() -> int:
    print("① 背包里 2 本 lv1 → 1 本 lv2")
    st = new_state("probe_bag", {CHASE1: 2})
    out = call(st, "synthetic", CHASE1, count=2, op_id="op_bag_1")
    check("success=true", out.get("success") is True, str(out.get("message")))
    check("lv1 扣干净", bag_count(st, CHASE1) == 0, str(bag_count(st, CHASE1)))
    check("lv2 +1", bag_count(st, CHASE2) == 1, str(bag_count(st, CHASE2)))
    check("回包带 saveData", isinstance(out.get("saveData"), dict), type(out.get("saveData")).__name__)

    print("② 背包 1 + 仓库 1 也能合")
    st = new_state("probe_mixed", {CHASE1: 1}, {CHASE1: 1})
    out = call(st, "synthetic", CHASE1, count=2, op_id="op_mixed_1")
    check("success=true", out.get("success") is True, str(out.get("message")))
    check("背包清空", bag_count(st, CHASE1) == 0, str(bag_count(st, CHASE1)))
    check("仓库清空", wh_count(st, CHASE1) == 0, str(wh_count(st, CHASE1)))
    check("lv2 +1", bag_count(st, CHASE2) == 1, str(bag_count(st, CHASE2)))

    print("③ 数量不足时一本都不扣")
    st = new_state("probe_poor", {CHASE1: 1})
    out = call(st, "synthetic", CHASE1, count=2, op_id="op_poor_1")
    check("success=false", out.get("success") is False, str(out.get("message")))
    check("lv1 原样保留 1 本", bag_count(st, CHASE1) == 1, str(bag_count(st, CHASE1)))
    check("没有凭空出 lv2", bag_count(st, CHASE2) == 0, str(bag_count(st, CHASE2)))

    print("④ lv5 封顶")
    lv5 = "追击lv5技能书"
    st = new_state("probe_cap", {lv5: 2})
    out = call(st, "synthetic", lv5, count=2, op_id="op_cap_1")
    check("success=false", out.get("success") is False, str(out.get("message")))
    check("lv5 没被扣", bag_count(st, lv5) == 2, str(bag_count(st, lv5)))

    print("⑤ 拆分：lv3 → 4 本 lv1")
    lv3 = "追击lv3技能书"
    st = new_state("probe_split", {lv3: 1})
    out = call(st, "split", lv3, count=None, op_id="op_split_1")
    check("success=true", out.get("success") is True, str(out.get("message")))
    check("lv3 扣干净", bag_count(st, lv3) == 0, str(bag_count(st, lv3)))
    check("lv1 +4", bag_count(st, CHASE1) == 4, str(bag_count(st, CHASE1)))

    print("⑥ 拆分：lv1 不允许")
    st = new_state("probe_split_lv1", {CHASE1: 3})
    out = call(st, "split", CHASE1, count=None, op_id="op_split_lv1")
    check("success=false", out.get("success") is False, str(out.get("message")))
    check("lv1 原样保留 3 本", bag_count(st, CHASE1) == 3, str(bag_count(st, CHASE1)))

    print("⑦ 幂等：同一 operation_id 重复请求不重复扣")
    st = new_state("probe_idem", {CHASE1: 4})
    first = call(st, "synthetic", CHASE1, count=2, op_id="op_idem_1")
    second = call(st, "synthetic", CHASE1, count=2, op_id="op_idem_1")
    check("首次成功", first.get("success") is True, str(first.get("message")))
    check("重复标记 duplicate", second.get("duplicate") is True, str(second.get("duplicate")))
    check("lv1 只扣了 2 本", bag_count(st, CHASE1) == 2, str(bag_count(st, CHASE1)))
    check("lv2 只有 1 本", bag_count(st, CHASE2) == 1, str(bag_count(st, CHASE2)))

    print("⑧ 未知技能书不误扣")
    st = new_state("probe_unknown", {CHASE1: 2})
    out = call(st, "synthetic", "不存在的lv1技能书", count=2, op_id="op_unknown")
    check("success=false", out.get("success") is False, str(out.get("message")))
    check("原有书不动", bag_count(st, CHASE1) == 2, str(bag_count(st, CHASE1)))

    print("\n结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
