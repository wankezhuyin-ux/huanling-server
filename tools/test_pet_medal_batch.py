#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宠物补章器（/gift/use-medal-batch）回归测试。

玩家报障（2026-10-29 截图）：背包里点「宠物补章器」→ 弹
「使用失败 / 服务端已处理，本地同步失败，请重新登录确认」。

根因：这条路由原来只是个空壳 ——
    results = []
    for medal in (ctx.body.get("medals") or []):
        ...
    return ok(results=results, count=len(results))
客户端 `InventoryManager.use_pet_medal_batch()` 发的却是
`{operationId, batchCount}`（没有 medals 字段）→ 循环体一次都不执行，
既不扣补章器、也不发勋章，回包还没有 `saveData` →
`_apply_pet_medal_authority_save()` 返回 false →
「服务端已处理，本地同步失败，请重新登录确认」。

守这些事：
  A. 单个使用：勋章 +1、补章器 -1、回包带 saveData
  B. 批量使用：一次扣 N 个、发 N 枚，回包勋章明细正确
  C. 每种勋章上限 5，合计上限 15，超出部分不扣道具
  D. 槽满时报错且不扣道具
  E. 操作幂等：同 operationId 重放不重复扣、不重复发
  F. 补章器不够时报错，且一个都不扣
  G. 背包 + 仓库混放时能扣到
  H. 回包 saveData 里 petsData 的 medal_counts 与 inventoryData 的道具数量都已更新

    python3 tools/test_pet_medal_batch.py
"""
from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_medal_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_extra as hx  # noqa: E402

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


def new_state(tag: str, medals: dict | None = None, bags: int = 0, warehouse: int = 0):
    st = core.State("u_" + tag)
    pet = {
        "id": "pet_" + tag,
        "pet_id": "lanniao",
        "name": "蓝鸟龙",
        "level": 300,
        "medal_counts": dict(medals or {"brave": 0, "calm": 0, "bold": 0}),
    }
    st.save["petsData"] = {"active_pet_id": pet["id"], "pets": [pet]}
    if bags > 0:
        st.add_item("宠物补章器", bags, "gift")
    if warehouse > 0:
        st.inv.setdefault("warehouse_items", []).append(
            {"id": "wh_medal", "name": "宠物补章器", "count": warehouse, "item_type": "gift"})
    st.flush(force=True)
    return st


def use(st, count: int = 1, op_id: str | None = None) -> dict:
    body = {"batchCount": count}
    if op_id:
        body["operationId"] = op_id
    ctx = core.Ctx(st, body, {}, None, {})
    return hx.gift_use_medal_batch(ctx)


def total_medals(pet_medals: dict) -> int:
    return sum(int(pet_medals.get(t, 0) or 0) for t in ("brave", "calm", "bold"))


def pet_of(st) -> dict:
    return st.save["petsData"]["pets"][0]


def owned(st) -> int:
    n = 0
    for arr in (st.items, st.inv.get("warehouse_items") or []):
        for it in arr:
            if isinstance(it, dict) and str(it.get("name")) == "宠物补章器":
                n += int(it.get("count", 0) or 0)
    return n


print("== A. 单个使用 ==")
st = new_state("a", bags=3)
r = use(st, 1, "op_a1")
check("回包成功", bool(r.get("success", True)) and not r.get("error"), str(r)[:200])
check("勋章 +1", total_medals(pet_of(st)["medal_counts"]) == 1, str(pet_of(st)["medal_counts"]))
check("补章器 -1", owned(st) == 2, str(owned(st)))
check("回包带 saveData", isinstance(r.get("saveData"), dict), str(type(r.get("saveData"))))
check("回包 saveData 里勋章已更新",
      total_medals((r.get("saveData", {}).get("petsData", {}).get("pets", [{}])[0] or {}).get("medal_counts", {})) == 1)
check("回包 saveData 里道具已扣",
      [i.get("count") for i in (r.get("saveData", {}).get("inventoryData", {}).get("items") or [])
       if i.get("name") == "宠物补章器"] == [2])
check("提示里有勋章名", "勋章" in str(r.get("message")), str(r.get("message")))

print("== B. 批量使用 ==")
st = new_state("b", bags=10)
r = use(st, 4, "op_b1")
check("扣 4 个", owned(st) == 6, str(owned(st)))
check("发 4 枚", total_medals(pet_of(st)["medal_counts"]) == 4, str(pet_of(st)["medal_counts"]))
check("used=4", int(r.get("used", 0)) == 4, str(r.get("used")))
check("明细之和=4", sum(int(v) for v in (r.get("medals") or {}).values()) == 4, str(r.get("medals")))

print("== C. 每类上限 5 / 合计 15 ==")
st = new_state("c", medals={"brave": 4, "calm": 5, "bold": 5}, bags=10)
r = use(st, 5, "op_c1")
check("只补到 brave 满（+1）", total_medals(pet_of(st)["medal_counts"]) == 15, str(pet_of(st)["medal_counts"]))
check("只扣了 1 个", owned(st) == 9, str(owned(st)))
check("每类都 ≤5",
      all(int(pet_of(st)["medal_counts"].get(t, 0)) <= 5 for t in ("brave", "calm", "bold")),
      str(pet_of(st)["medal_counts"]))

print("== D. 槽满报错且不扣 ==")
st = new_state("d", medals={"brave": 5, "calm": 5, "bold": 5}, bags=5)
r = use(st, 3, "op_d1")
check("返回失败", r.get("error") or r.get("success") is False, str(r)[:160])
check("道具没动", owned(st) == 5, str(owned(st)))
check("提示含「已满」", "已满" in str(r.get("error") or r.get("message")), str(r)[:160])

print("== E. 幂等（同 operationId 重放）==")
st = new_state("e", bags=5)
r1 = use(st, 2, "op_e1")
after1 = (owned(st), total_medals(pet_of(st)["medal_counts"]))
r2 = use(st, 2, "op_e1")
after2 = (owned(st), total_medals(pet_of(st)["medal_counts"]))
check("两次结果一致（不重复扣）", after1 == after2, "%s vs %s" % (after1, after2))
check("第二次标记 duplicate", bool(r2.get("duplicate", False)), str(r2)[:160])
check("第二次仍带 saveData", isinstance(r2.get("saveData"), dict))

print("== F. 补章器不足 ==")
st = new_state("f", bags=1)
r = use(st, 5, "op_f1")
check("只用掉 1 个", owned(st) == 0, str(owned(st)))
check("只发 1 枚", total_medals(pet_of(st)["medal_counts"]) == 1, str(pet_of(st)["medal_counts"]))
st2 = new_state("f2", bags=0)
r2 = use(st2, 3, "op_f2")
check("一个都没有时失败", bool(r2.get("error")), str(r2)[:160])
check("提示补章器不足", "补章器" in str(r2.get("error") or ""), str(r2)[:160])

print("== G. 背包 + 仓库混放 ==")
st = new_state("g", bags=1, warehouse=3)
r = use(st, 3, "op_g1")
check("总数扣 3（跨背包仓库）", owned(st) == 1, str(owned(st)))
check("发 3 枚", total_medals(pet_of(st)["medal_counts"]) == 3, str(pet_of(st)["medal_counts"]))

print("== H. 无出战宠物 ==")
st = core.State("u_h")
st.save["petsData"] = {"active_pet_id": "", "pets": []}
st.add_item("宠物补章器", 3, "gift")
st.flush(force=True)
r = use(st, 1, "op_h1")
check("返回失败", bool(r.get("error")), str(r)[:160])
check("提示设置出战宠物", "出战" in str(r.get("error") or ""), str(r)[:160])
check("道具没动", owned(st) == 3, str(owned(st)))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
