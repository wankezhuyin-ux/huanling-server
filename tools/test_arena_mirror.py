#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""竞技场镜像对战回归测试（2026-10-28 补齐结算后）。

流程：/arena/match/start 拿 token → /arena/challenge/start 拿双方快照+胜负 →
/arena/challenge/confirm 结算积分（幂等）。

用法：
    cd local-server && python3 tools/test_arena_mirror.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import handlers_arena  # noqa: E402,F401 触发路由注册
import handlers_growth  # noqa: E402,F401
from core import Ctx, State  # noqa: E402

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


def call(state: State, method: str, path: str, body: dict, username: str) -> dict:
    fn = core.ROUTES[(method, path)]
    return fn(Ctx(state, body, {}, {"username": username}, {}))


def cleanup() -> None:
    for user in ("zz_arena_a", "zz_arena_b"):
        path = core.STATE_DIR / "accounts" / (user + ".json")
        if path.exists():
            path.unlink()


def setup_account(user: str, level: int, points: int) -> State:
    st = core.STORE.get(user)
    core.STORE.accounts.setdefault(user, {"userId": abs(hash(user)) % 10 ** 6})
    st.meta["nickname"] = user
    st.player["level"] = level
    st.player["realm"] = "散仙"
    st.sync_final_stats()
    # 造一只出战宠
    pet = {
        "id": "pet_%s" % user, "pet_id": "kuxinggui", "name": "灵兽_%s" % user,
        "level": level, "experience": 0, "generation": 0,
        "growth_rates": {"attack": 2.0, "defense": 2.0, "agility": 2.0, "hp": 6.0},
        "stat_ratio": {"attack": 33.33, "defense": 33.33, "agility": 33.34},
        "base_stats": {"attack": 5.0, "defense": 5.0, "agility": 5.0, "hp": 30.0},
        "locked": False, "mutation_count": 0, "is_mutant": False,
        "is_evolution_pet": False, "lineage_seed_growth": 6.0,
    }
    st._recalc_pet_stats(pet)
    if not st.pets:
        st.pets.append(pet)
    st.save.setdefault("petsData", {})["active_pet_id"] = pet["id"]
    box = st.meta.setdefault("arena", {})
    import datetime
    box["week"] = datetime.datetime.now().isocalendar()[1]
    box["points"] = points
    st.bump()
    return st


def main() -> int:
    cleanup()
    a = setup_account("zz_arena_a", 500, 100)
    b = setup_account("zz_arena_b", 520, 120)

    print("== 1. 匹配 ==")
    r = call(a, "POST", "/arena/match/start", {}, "zz_arena_a")
    check("match/start 成功", bool(r.get("success")), str(r.get("error", "")))
    token = str(r.get("battleToken", ""))
    check("回 battleToken", len(token) >= 8, token[:8])
    check("回倒计时", int(r.get("countdownSeconds", 0)) >= 1)
    opp = r.get("opponent") or {}
    check("匹配到对手 zz_arena_b", opp.get("playerName") == "zz_arena_b", str(opp.get("playerName")))
    check("对手展示当周积分 120", int(opp.get("weeklyArenaPoints", -1)) == 120, str(opp))

    print("== 2. 开战 ==")
    r2 = call(a, "POST", "/arena/challenge/start", {"battleToken": token}, "zz_arena_a")
    check("challenge/start 成功", bool(r2.get("success")), str(r2.get("error", "")))
    atk = r2.get("attackerSnapshot") or {}
    dfn = r2.get("defenderSnapshot") or {}
    check("攻击方快照含 player", isinstance(atk.get("player"), dict) and bool(atk["player"]))
    check("攻击方快照含 pet", isinstance(atk.get("pet"), dict) and bool(atk["pet"]))
    check("防守方快照含 player", isinstance(dfn.get("player"), dict) and bool(dfn["player"]))
    check("快照带 final 战斗数值", "final_attack" in atk.get("player", {}), "")
    check("回 victory 布尔", isinstance(r2.get("victory"), bool), str(r2.get("victory")))
    check("battleMaxRounds=20", int(r2.get("battleMaxRounds", 0)) == 20)
    check("对手名回传", str(r2.get("opponentName", "")) == "zz_arena_b", str(r2.get("opponentName")))
    check("battleLog 为数组（空=客户端快进）", isinstance(r2.get("battleLog"), list))

    # 错误 token
    r_bad = call(a, "POST", "/arena/challenge/start", {"battleToken": "wrong"}, "zz_arena_a")
    check("错误 token 被拒", not r_bad.get("success"))

    print("== 3. 结算 ==")
    used_before = a.daily("arena_attempts")
    r3 = call(a, "POST", "/arena/challenge/confirm", {"battleToken": token}, "zz_arena_a")
    check("confirm 成功", bool(r3.get("success")), str(r3.get("error", "")))
    check("回 scoreDelta", "scoreDelta" in r3, str(r3.get("scoreDelta")))
    check("回 weeklyArenaPoints", "weeklyArenaPoints" in r3)
    check("回 myRank", int(r3.get("myRank", 0)) >= 1, str(r3.get("myRank")))
    check("回 remainingAttempts", int(r3.get("remainingAttempts", -1)) >= 0, str(r3.get("remainingAttempts")))
    check("消耗 1 次挑战次数", a.daily("arena_attempts") == used_before + 1)
    delta = int(r3.get("scoreDelta", 0))
    box = a.meta.get("arena", {})
    check("积分已入账", int(box.get("points", 0)) == 100 + delta, "%d (+%d)" % (box.get("points"), delta))

    # 幂等重放
    r4 = call(a, "POST", "/arena/challenge/confirm", {"battleToken": token}, "zz_arena_a")
    check("重复 confirm 幂等（同分不重复计）",
          bool(r4.get("success")) and int(r4.get("scoreDelta", -999)) == delta
          and int(a.meta.get("arena", {}).get("points", 0)) == 100 + delta, "")
    check("幂等不重复扣次数", a.daily("arena_attempts") == used_before + 1)

    print("== 4. 次数上限与开关 ==")
    cfg = core.RULES.m("arena")
    check("runtime-config arena.featureEnabled = true", bool(cfg.get("featureEnabled")))
    a.daily_inc("arena_attempts", int(cfg.get("dailyMaxAttempts", 10)))
    r5 = call(a, "POST", "/arena/match/start", {}, "zz_arena_a")
    check("次数用完后匹配被拒", not r5.get("success"), str(r5.get("error", "")))

    print("== 5. 排行榜含真实周积分 ==")
    r6 = call(a, "GET", "/arena/ranking", {}, "zz_arena_a")
    rows = r6.get("ranking") or []
    pts_rows = {str(x.get("username")): int(x.get("arenaPoints", 0)) for x in rows}
    check("榜上 zz_arena_a 积分 = 实际周分", pts_rows.get("zz_arena_a") == 100 + delta,
          str(pts_rows.get("zz_arena_a")))
    check("榜上 zz_arena_b 积分 120", pts_rows.get("zz_arena_b") == 120, str(pts_rows.get("zz_arena_b")))

    cleanup()
    print("\n通过 %d / 失败 %d" % (PASSED, FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
