#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""套装副本扫荡回归测试。

玩家报障：「套装副本怎么没有扫荡功能，原版游戏是挑战过的可以直接扫荡，
每天有扫荡次数」。

原版规则：**挑战通关过的层可以直接扫荡，每天有扫荡次数（VIP 提升上限）**。

原实现的三处问题，这里逐条守住：
  A. `/economy/suit-dungeon-sweep-reward` 直接 `return suit_clear_reward(ctx)` ——
     既不扣次数、也不回 sweepsLeft，客户端 `player.suit_dungeon_sweeps` 永远是满的，
     界面「今日可扫荡: N 次」怎么点都不掉，金币掉落还能无限刷。
  B. `suit_clear_reward` 完全忽略 `level`，`suit_dungeon_progress` 永远停在 0；
     客户端 `is_cleared = level <= suit_dungeon_progress` 恒为假，二十一层全是
     「未通关」，扫荡按钮一次都出不来。
  C. `handlers_play` 里那套 `/suit-dungeon/*` 把 `suit_dungeon_sweeps` 当
     「已用次数」读（客户端语义是「剩余次数」），一进副本就「今日扫荡次数已用完」。

守这些事：
  1. 初始 info：progress=0，sweepsLeft=上限
  2. 没通关过任何层 → 不能扫荡
  3. 通关第 N 层 → progress 推到 N，且回包**不带** sweepsLeft（会被网络层截走）
  4. 扫荡已通关的层 → 扣 1 次、发金币、剩余次数 -1
  5. 扫荡未通关的层 → 拒绝，且不扣次数
  6. 次数用完 → 拒绝
  7. 同 operationId 重放 → 不重复发钱、不重复扣次
  8. 跨天 → 次数回满
  9. VIP 越高次数上限越高（配置 2 / VIP 表 2,3,4,5 取大）
 10. 进度只增不减

    python3 tools/test_suit_dungeon_sweep.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_suitsweep_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_extra as hx  # noqa: E402
import handlers_play as hp  # noqa: E402

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


def new_state(tag: str, progress: int = 0, is_vip: bool = False, vip_level: int = 0):
    st = core.State("u_" + tag)
    st.save.setdefault("playerData", {})
    if progress:
        st.player["suit_dungeon_progress"] = progress
    if is_vip:
        st.player["is_vip"] = True
        st.player["vip_level"] = vip_level
        expire = core.now() + timedelta(days=30)
        st.player["vip_expire_time_str"] = expire.strftime("%Y-%m-%dT%H:%M:%S")
    st.flush(force=True)
    return st


def info(st) -> dict:
    return hp.suit_dungeon_info(core.Ctx(st, {}, {}, None, {}))


def sweep(st, level: int, op_id: str = "") -> dict:
    body = {"level": level}
    if op_id:
        body["operationId"] = op_id
    return hx.suit_sweep_reward(core.Ctx(st, body, {}, None, {}))


def clear(st, level: int, op_id: str = "") -> dict:
    body = {"level": level}
    if op_id:
        body["operationId"] = op_id
    return hx.suit_clear_reward(core.Ctx(st, body, {}, None, {}))


def left(st) -> int:
    return int(info(st).get("sweepsLeft", -1))


def mails(st) -> int:
    return len(st.meta.get("mails") or [])


SWEEP_GOLD = hp._suit_sweep_gold
print("扫荡参数：第1层金币=%d 第3层金币=%d" % (SWEEP_GOLD(1), SWEEP_GOLD(3)))

print("== 1. 初始状态 ==")
st = new_state("a")
i0 = info(st)
check("progress=0", int(i0.get("suitDungeonProgress", -1)) == 0, str(i0))
check("sweepsLeft=上限", int(i0.get("sweepsLeft", -1)) == int(i0.get("maxSweeps", -2)), str(i0))
check("上限 ≥2", int(i0.get("maxSweeps", 0)) >= 2, str(i0))
cap0 = int(i0.get("maxSweeps", 0))

print("== 2. 没通关过任何层不能扫荡 ==")
r = sweep(st, 1, "op_none")
check("返回失败", r.get("success") is False, str(r)[:160])

print("== 3. 通关第 1 层 ==")
gold_before = st.gold()
r = clear(st, 1, "op_clear1")
check("成功", r.get("success") is True, str(r)[:160])
check("回包带 suitDungeonProgress=1", int(r.get("suitDungeonProgress", -1)) == 1, str(r))
check("回包不带 sweepsLeft（否则会被网络层截走）", "sweepsLeft" not in r, str(r)[:200])
check("服务端 progress=1", int(st.player.get("suit_dungeon_progress", 0)) == 1,
      str(st.player.get("suit_dungeon_progress")))
check("金币到账（+%d）" % int(r.get("goldGained", 0)),
      st.gold() == gold_before + int(r.get("goldGained", 0)), str(st.gold()))

print("== 4. 扫荡已通关的第 1 层 ==")
gold_before = st.gold()
mail_before = mails(st)
r = sweep(st, 1, "op_sweep1")
check("成功", r.get("success") is True, str(r)[:160])
check("金币 = 基础值 %d" % SWEEP_GOLD(1), st.gold() == gold_before + SWEEP_GOLD(1), str(st.gold()))
check("回 sweepsLeft=cap-1", int(r.get("sweepsLeft", -1)) == cap0 - 1, str(r.get("sweepsLeft")))
check("服务端剩余次数 = cap-1", left(st) == cap0 - 1, str(left(st)))
check("回包 level 正确", int(r.get("level", 0)) == 1, str(r.get("level")))
check("有掉落就走邮件", (not r.get("dropItems")) or mails(st) == mail_before + 1,
      "drop=%s mails=%d" % (r.get("dropItems"), mails(st)))

print("== 5. 扫荡未通关的第 2 层 ==")
before = left(st)
gold_before = st.gold()
r = sweep(st, 2, "op_sweep2")
check("返回失败", r.get("success") is False, str(r)[:160])
check("没扣次数", left(st) == before, str(left(st)))
check("没发钱", st.gold() == gold_before, str(st.gold()))

print("== 6. 次数用完 ==")
while left(st) > 0:
    rr = sweep(st, 1, "op_drain_%d" % left(st))
    if rr.get("success") is not True:
        break
check("剩余次数归零", left(st) == 0, str(left(st)))
r = sweep(st, 1, "op_over")
check("用完后拒绝", r.get("success") is False, str(r)[:160])
check("提示是次数用完", "次数" in str(r.get("error", "")), str(r.get("error")))

print("== 7. operationId 幂等 ==")
st = new_state("idem", progress=3)
gold_before = st.gold()
r1 = sweep(st, 1, "op_same")
g1 = st.gold()
r2 = sweep(st, 1, "op_same")
check("第一次成功", r1.get("success") is True, str(r1)[:120])
check("第二次回 duplicate", bool(r2.get("duplicate")), str(r2)[:160])
check("第二次不再发钱", st.gold() == g1, "%d -> %d" % (g1, st.gold()))
check("第二次不再扣次", int(r2.get("sweepsLeft", -1)) == int(r1.get("sweepsLeft", -2)),
      "%s vs %s" % (r2.get("sweepsLeft"), r1.get("sweepsLeft")))

print("== 8. 跨天回满 ==")
st = new_state("day", progress=2)
sweep(st, 1, "op_d1")
check("用掉一次", left(st) == cap0 - 1, str(left(st)))
st.player["suit_dungeon_sweeps_date"] = "2000-01-01"
check("跨天后回满", left(st) == cap0, str(left(st)))

print("== 9. VIP 提升上限 ==")
st_plain = new_state("v0")
st_vip = new_state("v3", is_vip=True, vip_level=3)
cap_plain = int(info(st_plain).get("maxSweeps", 0))
cap_vip = int(info(st_vip).get("maxSweeps", 0))
check("VIP3 上限 > 平民上限", cap_vip > cap_plain, "%d vs %d" % (cap_vip, cap_plain))
check("VIP3 上限 = 5（vip_benefits.suit_sweeps_max）", cap_vip == 5, str(cap_vip))

print("== 10. 进度只增不减 ==")
st = new_state("prog", progress=5)
r = clear(st, 2, "op_back")
check("回包进度仍是 5", int(r.get("suitDungeonProgress", -1)) == 5, str(r)[:160])
check("服务端进度仍是 5", int(st.player.get("suit_dungeon_progress", 0)) == 5,
      str(st.player.get("suit_dungeon_progress")))

print("== 11. 层数越界 ==")
st = new_state("bounds", progress=1)
r = sweep(st, 0, "op_zero")
check("第 0 层拒绝", r.get("success") is False, str(r)[:120])
r = sweep(st, 99, "op_99")
check("第 99 层拒绝", r.get("success") is False, str(r)[:120])

print("== 12. 老接口 /suit-dungeon/sweep 同一套语义 ==")
st = new_state("legacy", progress=1)
before = left(st)
r = hp.suit_dungeon_sweep(core.Ctx(st, {"level": 1}, {}, None, {}))
check("成功", r.get("success") is True, str(r)[:160])
check("扣了 1 次", left(st) == before - 1, "%d -> %d" % (before, left(st)))
check("回 sweepsLeft", int(r.get("sweepsLeft", -1)) == before - 1, str(r.get("sweepsLeft")))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
