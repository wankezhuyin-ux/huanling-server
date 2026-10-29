#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程内自检：/player/realm/transition（飞升 / 投胎）。

背景 bug：旧实现是占位桩 —— 不看 transitionType、不落转换后的 playerData、
一律 reincarnation+1。后果：
  1. 飞升被误加转生次数；
  2. 服务端存档里「境界 / 等级」不变，客户端随后的云同步 / 重登把旧等级盖回来
     → 玩家看到「飞升成功成为散仙，但等级没变回 1」；
  3. 等级仍是 1000+，门槛检查继续通过 → 可以连点连升（散仙→地仙→天仙）。

正确行为（与客户端 player.gd 的 ascend()/reincarnate() 对齐）：
  - 飞升：采用客户端转换后快照（新境界、等级 1、经验 0、属性点重置），
    转生次数不变、塔层不动；
  - 投胎：同样采用快照（凡人、等级 1、转生次数 = 客户端已 +1 的值），塔层回 1；
  - operationId 幂等：重复请求不产生二次效果；
  - 回包带 saveData（playerData 全量）+ saveRevision，客户端走
    realm_transition_slice 合并。

用法：
    cd local-server && python3 tools/test_realm_transition.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import handlers_growth  # noqa: E402,F401  触发路由注册
from core import Ctx, State  # noqa: E402

USER = "zz_selftest_realm"
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


def call(state: State, body: dict) -> dict:
    fn = core.ROUTES[("POST", "/player/realm/transition")]
    return fn(Ctx(state, body, {}, {"username": USER}, {}))


def cleanup() -> None:
    path = core.STATE_DIR / "accounts" / (USER + ".json")
    if path.exists():
        path.unlink()


def make_player(state: State) -> None:
    p = state.player
    p.update({
        "realm": "凡人",
        "level": 1000,
        "experience": 500,
        "points": 30,
        "vitality": 50,
        "magic": 40,
        "attack_base": 40,
        "defense_base": 35,
        "agility_base": 30,
        "max_hp": 900,
        "max_mp": 800,
        "hp": 900,
        "mp": 800,
        "reincarnation": 0,
        "cultivation_pills_used": 3,
        "tower_floor": 7,
    })
    state.flush()


def ascend_snapshot(realm: str, reincarnation: int = 0) -> dict:
    """模拟客户端 player.ascend() 之后的 to_dict() 关键字段。"""
    return {
        "realm": realm,
        "level": 1,
        "experience": 0,
        "points": 20,
        "pending_auto_points": 0,
        "vitality": 10,
        "magic": 10,
        "attack_base": 10,
        "defense_base": 10,
        "agility_base": 10,
        "max_hp": 100,
        "max_mp": 100,
        "hp": 100,
        "mp": 100,
        "reincarnation": reincarnation,
        "cultivation_pills_used": 3,
    }


# ================================================================ 飞升
def test_ascend() -> None:
    print("\n== 飞升：境界推进 / 等级重置 / 转生次数不变 ==")
    cleanup()
    state = State(USER)
    make_player(state)

    resp = call(state, {
        "transitionType": "ascend",
        "playerData": ascend_snapshot("散仙"),
        "operationId": "op_ascend_1",
    })
    check("飞升请求成功", bool(resp.get("success")), str(resp.get("message", ""))[:40])
    check("回包带 saveData", isinstance(resp.get("saveData"), dict) and bool(resp.get("saveData")))
    check("回包带 saveRevision", "saveRevision" in resp)
    check("服务端境界=散仙", state.player.get("realm") == "散仙",
          "=%r" % state.player.get("realm"))
    check("服务端等级=1（bug 时会保留 1000）", state.player.get("level") == 1,
          "=%r" % state.player.get("level"))
    check("经验清零", int(state.player.get("experience", -1)) == 0)
    check("转生次数不变=0（bug 时会被+1）", int(state.player.get("reincarnation", -1)) == 0,
          "=%r" % state.player.get("reincarnation"))
    check("属性点重置=20", int(state.player.get("points", -1)) == 20)
    check("基础属性重置", int(state.player.get("attack_base", -1)) == 10)
    check("修行丹计数保留", int(state.player.get("cultivation_pills_used", -1)) == 3)
    check("塔层不动（飞升不回1）", int(state.player.get("tower_floor", -1)) == 7)
    check("回包 duplicate=False", not bool(resp.get("duplicate")))
    sd = (resp.get("saveData") or {}).get("playerData", {})
    check("回包 saveData 等级=1", int(sd.get("level", -1)) == 1)
    check("回包 saveData 境界=散仙", sd.get("realm") == "散仙")

    # ---- 幂等：同 operationId 重放
    resp2 = call(state, {
        "transitionType": "ascend",
        "playerData": ascend_snapshot("散仙"),
        "operationId": "op_ascend_1",
    })
    check("重放请求 duplicate=True", bool(resp2.get("duplicate")))
    check("重放后境界仍=散仙", state.player.get("realm") == "散仙")
    check("重放后等级仍=1", state.player.get("level") == 1)
    check("重放后转生次数仍=0", int(state.player.get("reincarnation", -1)) == 0)

    # ---- 连续第二次飞升（散仙→地仙）：等级已被重置，新快照再来
    resp3 = call(state, {
        "transitionType": "ascend",
        "playerData": ascend_snapshot("地仙"),
        "operationId": "op_ascend_2",
    })
    check("第二次飞升成功", bool(resp3.get("success")))
    check("服务端境界=地仙", state.player.get("realm") == "地仙")
    check("服务端等级=1", state.player.get("level") == 1)
    check("转生次数仍=0", int(state.player.get("reincarnation", -1)) == 0)


# ================================================================ 投胎
def test_reincarnate() -> None:
    print("\n== 投胎：转生次数取客户端快照（不重复+1）/ 塔层回 1 ==")
    cleanup()
    state = State(USER)
    p = state.player
    p.update({
        "realm": "幻灵天神",
        "level": 1200,
        "experience": 0,
        "reincarnation": 5,
        "tower_floor": 9,
        "permanent_points": 40,
    })
    state.flush()

    snap = ascend_snapshot("凡人")
    snap["reincarnation"] = 6            # 客户端 reincarnate() 已 +1
    snap["permanent_points"] = 55        # permanent_delta 之后
    resp = call(state, {
        "transitionType": "reincarnate",
        "playerData": snap,
        "operationId": "op_reinc_1",
    })
    check("投胎请求成功", bool(resp.get("success")), str(resp.get("message", ""))[:40])
    check("服务端境界=凡人", state.player.get("realm") == "凡人")
    check("服务端等级=1", state.player.get("level") == 1)
    check("转生次数=6（bug 时会变 7）", int(state.player.get("reincarnation", -1)) == 6,
          "=%r" % state.player.get("reincarnation"))
    check("塔层回 1", int(state.player.get("tower_floor", -1)) == 1)
    check("永久点数采用快照=55", int(state.player.get("permanent_points", -1)) == 55)
    check("回包 duplicate=False", not bool(resp.get("duplicate")))

    resp2 = call(state, {
        "transitionType": "reincarnate",
        "playerData": snap,
        "operationId": "op_reinc_1",
    })
    check("投胎重放 duplicate=True", bool(resp2.get("duplicate")))
    check("重放后转生次数仍=6", int(state.player.get("reincarnation", -1)) == 6)


# ================================================================ 非法输入
def test_invalid() -> None:
    print("\n== 非法输入 ==")
    cleanup()
    state = State(USER)
    make_player(state)

    resp = call(state, {"transitionType": "flying", "playerData": {}, "operationId": "x"})
    check("未知 transitionType 被拒", not bool(resp.get("success")))
    resp2 = call(state, {"transitionType": "ascend"})
    check("缺 playerData 被拒", not bool(resp2.get("success")))
    bad = ascend_snapshot("散仙")
    bad["realm"] = "仙人"
    resp3 = call(state, {"transitionType": "ascend", "playerData": bad, "operationId": "y"})
    check("未知境界被拒", not bool(resp3.get("success")))
    check("拒绝后服务端状态未变", state.player.get("realm") == "凡人"
          and state.player.get("level") == 1000)


if __name__ == "__main__":
    test_ascend()
    test_reincarnate()
    test_invalid()
    cleanup()
    print("\n结果：OK %d / FAIL %d" % (PASSED, FAILED))
    sys.exit(1 if FAILED else 0)
