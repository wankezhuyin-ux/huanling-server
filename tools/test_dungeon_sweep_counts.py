#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""副本扫荡次数 / VIP 加次数 —— 服务端权威回归。

覆盖 2026-11-06 审计发现的三个「假生效」：
  1. 天关扫荡：core.State.VIP_TOWER_SWEEPS = [1,2,2,3] 定义了却从没接线，
     /tower/sweep 与 /economy/tower-sweep-reward 都不限次数；sweepCount 是
     **历史累计**且跨天不归零，客户端 remaining = VIP上限 - 累计值 永远为负，
     玩家扫过一次之后天天弹「今日扫荡次数已用完」。
  2. /economy/tower-sweep-reward 旧实现 `if not packs: return ok()`，最高层
     不到 100 时连次数都不涨；且 operationId 没做幂等，重试会重复发礼包邮件。
  3. 五行副本 five_elements_dungeon_attempts 只累加不写日期，10 次打满后
     remainingAttempts 永远为 0（跨天不回满）。

原版口径（scripts/vip_benefits.gd + settings_hall VIP 特权表）：
  天关扫荡/日 [1,2,2,3] / 套装扫荡/日 [2,3,4,5] / 自创武功/日 [3,4,5,6]
  五行副本 dailyMax=10，VIP 特权表无此项（固定次数属正常）

运行：python3 tools/test_dungeon_sweep_counts.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core
import handlers_account as ha
import handlers_battle as hb
import handlers_extra as he
import handlers_play as hp

_tmp = Path(tempfile.mkdtemp(prefix="ly_sweepcount_"))
(_tmp / "accounts").mkdir()
core.STATE_DIR = _tmp

PASS = 0
FAIL = 0
TOTAL = 0


def check(name, cond, detail=""):
    global PASS, FAIL, TOTAL
    TOTAL += 1
    if cond:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def new_state(name, vip=0, expire_delta=86400):
    st = core.State(name)
    if vip:
        st.player.update({
            "is_vip": True,
            "vip_level": vip,
            "vip_expire_time_str": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                 time.localtime(time.time() + expire_delta)),
        })
    st.player["suit_dungeon_progress"] = 5
    st.player["tower_highest"] = 211
    st.player["tower_floor"] = 212
    return st


def ctx(st, body=None):
    return core.Ctx(st, body or {}, {}, None, {})


def mail_count(st):
    return len(st.meta.get("mails") or [])


print("=== A. VIP 档位 → 天关扫荡上限 ===")
for tier, want in [(0, 1), (1, 2), (2, 2), (3, 3)]:
    check("VIP%d 天关扫荡上限 = %d" % (tier, want),
          hp._tower_sweep_cap(new_state("cap_%d" % tier, tier)) == want,
          "got=%s" % hp._tower_sweep_cap(new_state("capx_%d" % tier, tier)))
st_expired = new_state("cap_expired", 3, -3600)
check("已过期 VIP3 回落为 1 次", hp._tower_sweep_cap(st_expired) == 1,
      str(hp._tower_sweep_cap(st_expired)))

print("\n=== B. /tower/info 回的是「今日已用」而非历史累计 ===")
st = new_state("info")
i0 = hp.tower_info(ctx(st))
check("初始 sweepCount=0 / sweepsLeft=1 / maxSweeps=1",
      i0.get("sweepCount") == 0 and i0.get("sweepsLeft") == 1 and i0.get("maxSweeps") == 1,
      str(i0))
hp.tower_sweep(ctx(st))
i1 = hp.tower_info(ctx(st))
check("扫过一次后 sweepCount=1 / sweepsLeft=0",
      i1.get("sweepCount") == 1 and i1.get("sweepsLeft") == 0, str(i1))
st.player["tower_sweep_date"] = "2000-01-01"
i2 = hp.tower_info(ctx(st))
check("跨天后 sweepCount 归 0 / sweepsLeft 回满 1",
      i2.get("sweepCount") == 0 and i2.get("sweepsLeft") == 1, str(i2))
st_old = new_state("legacy")
st_old.player["tower_sweep_count"] = 87   # 老账号历史累计，无日期键
i3 = hp.tower_info(ctx(st_old))
check("老账号 87 次累计不生效（今日按 0 算）",
      i3.get("sweepCount") == 0 and i3.get("sweepsLeft") == 1, str(i3))

print("\n=== C. /tower/sweep（遗留入口）限次 ===")
st = new_state("ts_plain", 0)
m0 = mail_count(st)
r1 = hp.tower_sweep(ctx(st))
r2 = hp.tower_sweep(ctx(st))
check("普通玩家第 1 次扫荡成功", bool(r1.get("success")), str(r1)[:200])
check("回包带 sweepCount/sweepsLeft/maxSweeps",
      r1.get("sweepCount") == 1 and r1.get("sweepsLeft") == 0 and r1.get("maxSweeps") == 1,
      str(r1)[:200])
check("第 2 次被拒且提示次数用完",
      not r2.get("success") and "次数已用完" in str(r2.get("message", "")), str(r2)[:200])
check("被拒时不重复发百关礼包邮件", mail_count(st) == m0 + 1,
      "mails %d -> %d" % (m0, mail_count(st)))

st3 = new_state("ts_vip3", 3)
oks = [bool(hp.tower_sweep(ctx(st3)).get("success")) for _ in range(3)]
check("VIP3 前 3 次全部成功", all(oks), str(oks))
check("VIP3 第 4 次被拒", not hp.tower_sweep(ctx(st3)).get("success"))

print("\n=== D. /economy/tower-sweep-reward（客户端真实入口） ===")
st = new_state("econ_plain", 0)
st.player["tower_highest"] = 50   # 不到 100 层：旧实现在这里直接 return ok()，不计数
m0 = mail_count(st)
r1 = hb.tower_sweep_reward(ctx(st, {"operationId": "econ_op1"}))
check("低层无百关礼包时也成功并扣次数",
      bool(r1.get("success")) and r1.get("sweepCount") == 1, str(r1)[:220])
check("低层不发邮件但回包字段完整",
      mail_count(st) == m0 and r1.get("maxSweeps") == 1 and r1.get("sweepsLeft") == 0,
      "mails %d->%d %s" % (m0, mail_count(st), str(r1)[:160]))
r2 = hb.tower_sweep_reward(ctx(st, {"operationId": "econ_op2"}))
check("第 2 次被拒且不扣次数",
      not r2.get("success") and hp._tower_sweep_used(st) == 1, str(r2)[:200])

# operationId 幂等
dup = hb.tower_sweep_reward(ctx(st, {"operationId": "econ_op1"}))
check("同 operationId 重放返回 duplicate=True", bool(dup.get("duplicate")), str(dup)[:200])
check("重放不重复扣次数", hp._tower_sweep_used(st) == 1, str(hp._tower_sweep_used(st)))
check("重放不重复发邮件", mail_count(st) == m0, "mails=%d" % mail_count(st))

st = new_state("econ_high", 3)
st.player["tower_highest"] = 250
m0 = mail_count(st)
r = hb.tower_sweep_reward(ctx(st, {"operationId": "econ_high1"}))
check("≥100 层仍发百关礼包邮件", mail_count(st) == m0 + 1 and len(r.get("items") or []) == 2,
      "mails %d->%d items=%s" % (m0, mail_count(st), r.get("items")))
check("VIP3 第一次后 sweepsLeft=2", r.get("sweepsLeft") == 2, str(r)[:200])

print("\n=== E. 两条天关扫荡接口共用一份计数 ===")
st = new_state("mixed", 3)
a = hp.tower_sweep(ctx(st))
b = hb.tower_sweep_reward(ctx(st, {"operationId": "mix_2"}))
c = hb.tower_sweep_reward(ctx(st, {"operationId": "mix_3"}))
d = hp.tower_sweep(ctx(st))
check("混打 3 次后刚好用完",
      bool(a.get("success")) and bool(b.get("success")) and bool(c.get("success"))
      and not d.get("success")
      and hp._tower_sweep_used(st) == 3,
      "used=%s" % hp._tower_sweep_used(st))

print("\n=== F. 五行副本每日次数跨天归零 ===")
st = new_state("fe", 0)
s0 = hp.fe_status(ctx(st))
check("初始 remainingAttempts=10", s0.get("remainingAttempts") == 10, str(s0))
for i in range(10):
    hp.fe_start(ctx(st))
s1 = hp.fe_status(ctx(st))
check("打满 10 次后 remainingAttempts=0", s1.get("remainingAttempts") == 0, str(s1))
check("第 11 次被拒", not hp.fe_start(ctx(st)).get("success"))
st.player["five_elements_dungeon_attempts_date"] = "2000-01-01"
s2 = hp.fe_status(ctx(st))
check("跨天后回满 10 次", s2.get("remainingAttempts") == 10, str(s2))
check("跨天后又能正常开始", bool(hp.fe_start(ctx(st)).get("success")))
check("日期键写入存档", st.player.get("five_elements_dungeon_attempts_date") == core.today(),
      str(st.player.get("five_elements_dungeon_attempts_date")))
check("VIP3 五行次数仍固定 10（原版无此权益）",
      hp.fe_status(ctx(new_state("fe_vip3", 3))).get("maxAttempts") == 10)

print("\n=== G. 套装副本对照（别改坏已有 VIP 权益） ===")
for tier, want in [(0, 2), (1, 3), (2, 4), (3, 5)]:
    got = hp._suit_sweep_cap(new_state("suit_%d" % tier, tier))
    check("VIP%d 套装扫荡上限 = %d" % (tier, want), got == want, "got=%s" % got)
st = new_state("suit_consume", 0)
he.suit_sweep_reward(ctx(st, {"level": 3, "operationId": "s1"}))
he.suit_sweep_reward(ctx(st, {"level": 3, "operationId": "s2"}))
check("普通玩家套装第 3 次被拒",
      not he.suit_sweep_reward(ctx(st, {"level": 3, "operationId": "s3"})).get("success"))

print("\n=== H. /save/upload 不能返还次数 ===")
st = new_state("upload", 0)
hb.tower_sweep_reward(ctx(st, {"operationId": "up1"}))
hp.fe_start(ctx(st))
old = {"playerData": dict(st.player)}
incoming = {"playerData": {
    "tower_sweep_count": 0,
    "tower_sweep_date": "",
    "five_elements_dungeon_attempts": 0,
    "five_elements_dungeon_attempts_date": "",
}}
ha._merge_authoritative_progress(old, incoming)
p = incoming["playerData"]
check("旧快照不能把天关次数冲回 0", p.get("tower_sweep_count") == 1, str(p))
check("旧快照不能抹掉天关日期键", p.get("tower_sweep_date") == core.today(), str(p))
check("旧快照不能把五行次数冲回 0", p.get("five_elements_dungeon_attempts") == 1, str(p))
check("旧快照不能抹掉五行日期键",
      p.get("five_elements_dungeon_attempts_date") == core.today(), str(p))

print("\n=== H2. VIP 三字段双向权威（防伪造 / 防冲掉） ===")
st = new_state("vip_fake", 0)
old = {"playerData": dict(st.player)}
incoming = {"playerData": {
    "is_vip": True, "vip_level": 3,
    "vip_expire_time_str": time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(time.time() + 86400)),
}}
ha._merge_authoritative_progress(old, incoming)
p = incoming["playerData"]
check("客户端上传 VIP3 被服务端存档覆盖", not p.get("is_vip") and int(p.get("vip_level", 0)) == 0, str(p))
st.player.update(p)
check("伪造 VIP 后 vip_tier 仍为 0", st.vip_tier() == 0, str(st.vip_tier()))
st2 = new_state("vip_real", 3)
old2 = {"playerData": dict(st2.player)}
inc2 = {"playerData": {"is_vip": False, "vip_level": 0, "vip_expire_time_str": ""}}
ha._merge_authoritative_progress(old2, inc2)
p2 = inc2["playerData"]
check("GM 开通的 VIP 不会被旧快照冲掉",
      bool(p2.get("is_vip")) and int(p2.get("vip_level", 0)) == 3, str(p2))
# 元宝购买次数仍是客户端本地计数，服务端没有权威写入点，不能被上面的保护清掉
st3 = new_state("vip_yb", 0)
old3 = {"playerData": dict(st3.player)}
inc3 = {"playerData": {"fe_yuanbao_attempts": 1, "fe_yuanbao_attempts_date": core.today()}}
ha._merge_authoritative_progress(old3, inc3)
check("元宝挑战次数保留客户端上传值（服务端尚未权威结算）",
      inc3["playerData"].get("fe_yuanbao_attempts") == 1, str(inc3["playerData"])[:180])

print("\n=== I. 客户端字段静态对齐 ===")
CLIENT = ROOT.parent.parent / "recovered-godot-project"
vip = (CLIENT / "scripts" / "vip_benefits.gd").read_text(encoding="utf-8")
net = (CLIENT / "scripts" / "network.gd").read_text(encoding="utf-8")
ui = (CLIENT / "ui" / "city" / "tower_ui.gd").read_text(encoding="utf-8")
check("客户端 tower_sweep_limit = [1,2,2,3]",
      "return [1, 2, 2, 3][t]" in vip, "not found")
check("客户端 suit_sweeps_max = [2,3,4,5]",
      "return [2, 3, 4, 5][t]" in vip, "not found")
check("客户端 /tower/info 解析 sweepCount", "data.get(\"sweepCount\", 0)" in net)
check("客户端一键扫荡读服务端 sweepCount",
      "d.get(\"sweepCount\", game_manager.player.tower_sweep_count)" in ui)
check("服务端不再 unlimited 放行（VIP 表有生产引用）",
      "VIP_TOWER_SWEEPS" in (ROOT / "handlers_play.py").read_text(encoding="utf-8"))
check("存档上传保护含新字段",
      "five_elements_dungeon_attempts_date" in (ROOT / "handlers_account.py").read_text(encoding="utf-8"))

print("\n通过 %d / 失败 %d（合计 %d 项）" % (PASS, FAIL, TOTAL))
raise SystemExit(0 if FAIL == 0 else 1)
