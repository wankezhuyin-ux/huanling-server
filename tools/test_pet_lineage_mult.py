#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""幻化成长加成回归测试（2026-10-28 补 mutationMult 乘数后）。

覆盖：
1. 升级成长 _pet_roll_growth：代数加成 + 血统加成 × petMutation.lineageLevelUp 乘数
   （变异 1 次 ×2、2 次 ×3，与原版 pet.gd _resolve_level_up_lineage_multiplier 对齐）。
2. 进化成长 evo_bonus × petMutation.lineageEvolve.mutationMult
   （与原版 pet_park.gd _compute_evolve_lineage_bonus_for_predict 对齐）。
3. /pet/merge 幻化门槛与继承公式（300 级 / 同代 / 代数≤3 / H-L 公式 / 幻化符）。
4. 概率常量与原版（settings_hall VIP 表 / NobleBenefits.MERGE_BONUS / 幻化符 +15）对照。

用法：
    cd local-server && python3 tools/test_pet_lineage_mult.py
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import handlers_growth as hg  # noqa: E402,F402 触发路由注册
from core import Ctx, State  # noqa: E402

USER = "zz_selftest_lineage"
PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  OK   %s %s" % (name, detail))
    else:
        FAILED += 1
        print("  FAIL %s %s" % (name, detail))


def call_merge(state: State, body: dict) -> dict:
    return core.ROUTES[("POST", "/pet/merge")](
        Ctx(state, body, {}, {"username": USER}, {}))


def cleanup() -> None:
    path = core.STATE_DIR / "accounts" / (USER + ".json")
    if path.exists():
        path.unlink()


def make_pet(st: State, **kw) -> dict:
    pet = {
        "id": kw.get("id", "p%d" % random.randrange(10 ** 9)),
        "pet_id": kw.get("pet_id", "kuxinggui"),
        "name": kw.get("name", "测试宠"),
        "level": kw.get("level", 1),
        "experience": 0,
        "generation": kw.get("generation", 0),
        "growth_rates": dict(kw.get("growth_rates", {"attack": 1.0, "defense": 1.0, "agility": 1.0, "hp": 3.0})),
        "stat_ratio": kw.get("stat_ratio", {"attack": 33.33, "defense": 33.33, "agility": 33.34}),
        "base_stats": {"attack": 5.0, "defense": 5.0, "agility": 5.0, "hp": 30.0},
        "locked": False,
        "mutation_count": kw.get("mutation_count", 0),
        "is_mutant": kw.get("is_mutant", False),
        "is_evolution_pet": kw.get("is_evolution_pet", False),
        "lineage_seed_growth": kw.get("lineage_seed_growth", 3.0),
    }
    st._recalc_pet_stats(pet)
    return pet


LINEAGE = 25.0  # 血统 25 → lineage_base = sqrt(19)*0.001
LINEAGE_BASE = (max(0.0, LINEAGE - 6.0) ** 0.5) * 0.001


def roll_total_delta(st: State, mutation_count: int, generation: int = 0) -> float:
    pet = make_pet(st, generation=generation, mutation_count=mutation_count,
                   lineage_seed_growth=LINEAGE,
                   growth_rates={"attack": 0.0, "defense": 0.0, "agility": 0.0, "hp": 0.0})
    st._pet_roll_growth(pet)
    g = pet["growth_rates"]
    return g["attack"] + g["defense"] + g["agility"]


def main() -> int:
    cleanup()
    st = core.STORE.get(USER)
    st.player["realm"] = "凡人"

    print("== 1. 升级成长血统乘数（lineageLevelUp） ==")
    lu = core.RULES.m("pet_mutation").get("lineageLevelUp", {})
    check("runtime-config 的 lineageLevelUp 已加载",
          lu.get("mutationMult", {}).get("2") == 3, str(lu))
    check("petMutation.enabled = true",
          bool(core.RULES.m("pet_mutation").get("enabled")))

    # 真实上限 = 0.003 + lineage_base×乘数（基础项 0.003 不参与乘数）；
    # 三项成长各自 round(·,3)，总和量化到 0.001，可观测最大值 ≈ upper 向下取整。
    n = 6000
    samples = {mc: sorted(roll_total_delta(st, mc) for _ in range(n)) for mc in (0, 1, 2)}
    uppers = {mc: 0.003 + LINEAGE_BASE * float(mult) for mc, mult in ((0, 1.0), (1, 2.0), (2, 3.0))}
    for mc in (0, 1, 2):
        upper = uppers[mc]
        mx = samples[mc][-1]
        # 三项各自 round(·,3)，总和舍入溢出 ≤ 3×0.0005
        check("变异%d 最大值 ≤ 上限+舍入容差(%.5f)" % (mc, upper + 0.0016),
              mx <= upper + 0.0016, "实际 %.5f" % mx)
        near = sum(1 for v in samples[mc] if v >= upper - 0.002)
        check("变异%d 有采样进入上限邻域" % mc, near > n * 0.02, "%d/%d" % (near, n))
    mean_0 = sum(samples[0]) / n
    mean_2 = sum(samples[2]) / n
    check("变异2 均值 ≥ 变异0 均值 ×2.2（乘数 3 扣除共同基础项）",
          mean_2 >= mean_0 * 2.2, "%.5f vs %.5f" % (mean_2, mean_0))
    check("变异1 均值介于两者之间",
          sum(samples[1]) / n > mean_0 > 0, "%.5f" % (sum(samples[1]) / n))

    g3 = roll_total_delta(st, 0, generation=3)
    g3_upper = 0.003 + 3 * 0.001 + LINEAGE_BASE
    check("3 代宠升级成长上限含代数项(0.003+3×0.001+血统)", g3 <= g3_upper + 1e-6,
          "%.5f vs %.5f" % (g3, g3_upper))

    print("== 2. 进化血统乘数（lineageEvolve） ==")
    ev = core.RULES.m("pet_mutation").get("lineageEvolve", {})
    check("lineageEvolve.mutationMult[2] == 3", float((ev.get("mutationMult") or {}).get("2", 1.0)) == 3.0, str(ev))
    check("进化血统系数 0.25（EVOLUTION_INITIAL_GROWTH_COEFF）", hg.EVOLVE_LINEAGE_COEFF == 0.25)
    base_bonus = (max(0.0, LINEAGE - hg.EVOLVE_LINEAGE_BASELINE) ** 0.5) * hg.EVOLVE_LINEAGE_COEFF
    check("基础加成 ×3（变异2）与原版公式一致",
          abs(base_bonus * 3.0 - base_bonus * float((ev.get("mutationMult") or {}).get("2", 1.0))) < 1e-9)

    print("== 3. /pet/merge 幻化门槛与继承 ==")
    m = make_pet(st, level=299, generation=0,
                 growth_rates={"attack": 2.0, "defense": 2.0, "agility": 2.0, "hp": 6.0})
    s1 = make_pet(st, level=300, generation=0,
                  growth_rates={"attack": 1.0, "defense": 1.0, "agility": 1.0, "hp": 3.0})
    st.pets.append(m)
    st.pets.append(s1)
    r = call_merge(st, {"mainPetId": m["id"], "subPetId": s1["id"],
                        "operationId": "op%d" % random.randrange(10 ** 9)})
    check("主宠 299 级被拒", not r.get("success"), str(r.get("error", "")))

    m["level"] = 300
    r = call_merge(st, {"mainPetId": m["id"], "subPetId": s1["id"],
                        "operationId": "op%d" % random.randrange(10 ** 9)})
    check("300 级同代可幻化", bool(r.get("success")), str(r.get("error", "")))
    res = r.get("result", {})
    check("新代数 = 1", res.get("newGeneration") == 1, str(res))
    total = m["growth_rates"]["attack"] + m["growth_rates"]["defense"] + m["growth_rates"]["agility"]
    allowed = {6.0, 6.0 * 0.8 + 3.0 * 0.2, 6.0 * 0.7 + 3.0 * 0.3, (6.0 + 3.0) / 2.0}
    check("继承成长 = 完美/优良/普通/平均 四公式之一",
          any(abs(total - a) < 0.01 for a in allowed), "实际 %.3f" % total)
    check("主宠回 1 级", m["level"] == 1)
    check("副宠被消耗", s1 not in st.pets)

    g3a = make_pet(st, level=300, generation=3)
    g3b = make_pet(st, level=300, generation=3)
    st.pets.append(g3a)
    st.pets.append(g3b)
    r = call_merge(st, {"mainPetId": g3a["id"], "subPetId": g3b["id"],
                        "operationId": "op%d" % random.randrange(10 ** 9)})
    check("3 代宠无法参与幻化", not r.get("success"))

    g0 = make_pet(st, level=300, generation=0)
    g1 = make_pet(st, level=300, generation=1)
    st.pets.append(g0)
    st.pets.append(g1)
    r = call_merge(st, {"mainPetId": g0["id"], "subPetId": g1["id"],
                        "operationId": "op%d" % random.randrange(10 ** 9)})
    check("跨代被拒", not r.get("success"))

    lk = make_pet(st, level=300, generation=0)
    lk["locked"] = True
    lk2 = make_pet(st, level=300, generation=0)
    st.pets.append(lk)
    st.pets.append(lk2)
    r = call_merge(st, {"mainPetId": lk["id"], "subPetId": lk2["id"],
                        "operationId": "op%d" % random.randrange(10 ** 9)})
    check("锁定主宠被拒", not r.get("success"))

    # 幻化符
    if st.find_item("幻化符") is None:
        st.add_item("幻化符", 1)
    st.take_item("幻化符", st.item_count("幻化符"))
    c1 = make_pet(st, level=300, generation=0)
    c2 = make_pet(st, level=300, generation=0)
    st.pets.append(c1)
    st.pets.append(c2)
    r = call_merge(st, {"mainPetId": c1["id"], "subPetId": c2["id"],
                        "operationId": "op%d" % random.randrange(10 ** 9),
                        "useTransformCharm": True})
    check("无幻化符时勾选被拒", not r.get("success"), str(r.get("error", "")))
    st.add_item("幻化符", 2)
    r = call_merge(st, {"mainPetId": c1["id"], "subPetId": c2["id"],
                        "operationId": "op%d" % random.randrange(10 ** 9),
                        "useTransformCharm": True})
    check("有幻化符可幻化且扣 1 张", bool(r.get("success")) and st.item_count("幻化符") == 1,
          "success=%s 剩余=%d" % (r.get("success"), st.item_count("幻化符")))

    # 幂等
    if r.get("success"):
        op_id = None
        ops = st.meta.get("petMergeOps", {})
        check("operationId 已登记", len(ops) >= 1, str(len(ops)))

    print("== 4. 概率常量与原版对照 ==")
    check("VIP 概率表 4 档", len(hg.MERGE_WEIGHTS_BY_VIP) == 4)
    check("无会员 = A5/B15/C30/D50", hg.MERGE_WEIGHTS_BY_VIP[0] == {"A": 5, "B": 15, "C": 30, "D": 50})
    check("青铜 = A15/B20/C30/D35", hg.MERGE_WEIGHTS_BY_VIP[1] == {"A": 15, "B": 20, "C": 30, "D": 35})
    check("白银 = A20/B30/C40/D10", hg.MERGE_WEIGHTS_BY_VIP[2] == {"A": 20, "B": 30, "C": 40, "D": 10})
    check("黄金 = A30/B60/C10/D0（原版 settings_hall 表）",
          hg.MERGE_WEIGHTS_BY_VIP[3] == {"A": 30, "B": 60, "C": 10, "D": 0})
    check("贵族加成数组 = 原版 NobleBenefits.MERGE_BONUS",
          list(hg.NOBLE_MERGE_BONUS) == [0, 2, 4, 6, 8, 9, 10, 16, 23, 30, 37, 43, 50, 57, 63, 70])
    check("幻化符 = +15 完美", hg.MERGE_CHARM_BONUS == 15)
    w = hg._merge_apply_bonus({"A": 5, "B": 15, "C": 30, "D": 50}, 15)
    check("加成 15：D 先出 15", w == {"A": 20, "B": 15, "C": 30, "D": 35}, str(w))
    w = hg._merge_apply_bonus({"A": 30, "B": 60, "C": 10, "D": 0}, 70)
    check("黄金+贵族70：D/C/B 依次补给 A", w["A"] == 100 and w["D"] == 0, str(w))

    cleanup()
    print("\n通过 %d / 失败 %d" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
