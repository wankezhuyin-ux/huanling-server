#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VIP 档位判定 + 各档权益上限的回归测试。

VIP 只有「青铜/白银/黄金」三档（vip_level 1/2/3），权益数值以客户端
scripts/vip_benefits.gd 为准，服务端在 core.State 里照抄了一份。这里守两件事：

  1. `vip_tier()`：未开通、没过期、过期、档位越界都要算对（过期必须掉回 0，
     否则白送黄金权益）。
  2. 各处上限真的按档位取：「藏宝洞入阵次数」[2,4,5,8]、
     「套装副本扫荡」[2,3,4,5]（曾是写死 2 次，黄金 VIP 第 3 次就被拒）。
     顺带对照客户端表，防止两边数值漂移。

    python3 tools/test_vip_perks.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lyvipperks_"))

import handlers_play as hp  # noqa: E402  （import 即注册路由）
import treasure_round as tr  # noqa: E402

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


def iso(delta_sec: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + delta_sec))


def state_with_vip(level: int, expire: str) -> core.State:
    st = core.State("vipprobe_%d_%s" % (level, expire[:4] or "none"))
    st.player.update({"is_vip": bool(level), "vip_level": level, "vip_expire_time_str": expire})
    return st


def main() -> int:
    print("① 档位判定")
    check("未开通 → 0", state_with_vip(0, "").vip_tier() == 0)
    check("青铜 → 1", state_with_vip(1, iso(86400)).vip_tier() == 1)
    check("白银 → 2", state_with_vip(2, iso(86400)).vip_tier() == 2)
    check("黄金 → 3", state_with_vip(3, iso(86400)).vip_tier() == 3)
    check("已过期 → 0", state_with_vip(3, iso(-3600)).vip_tier() == 0,
          str(state_with_vip(3, iso(-3600)).vip_tier()))
    check("档位越界按 3 封顶", state_with_vip(9, iso(86400)).vip_tier() == 3)
    check("空到期时间 → 0", state_with_vip(3, "").vip_tier() == 0)
    check("脏到期时间 → 0", state_with_vip(3, "not-a-date").vip_tier() == 0)

    print("② 藏宝洞每日入阵次数（runtime-config.vipDailyJoinLimits = [2,4,5,8]）")
    for tier, want in ((0, 2), (1, 4), (2, 5), (3, 8)):
        got = tr._daily_limit(state_with_vip(tier, iso(86400) if tier else ""))
        check("VIP%d → %d 次" % (tier, want), got == want, "实际 %d" % got)

    print("③ 套装副本每日扫荡上限（vip_benefits.suit_sweeps_max = [2,3,4,5]）")
    for tier, want in ((0, 2), (1, 3), (2, 4), (3, 5)):
        got = hp._suit_sweep_cap(state_with_vip(tier, iso(86400) if tier else ""))
        check("VIP%d → %d 次" % (tier, want), got == want, "实际 %d" % got)

    print("④ 与客户端 vip_benefits.gd 的数值对照")
    gd = ROOT.parent.parent / "recovered-godot-project" / "scripts" / "vip_benefits.gd"
    if not gd.exists():
        print("       （跳过：本机没有客户端源码，服务端机器上不校验）")
    else:
        txt = gd.read_text(encoding="utf-8", errors="replace")

        def table(fn: str) -> list[float] | None:
            import re
            m = re.search(r"static func %s\b[^\n]*\n(?:.*\n){0,4}?\s*return \[([^\]]+)\]" % fn, txt)
            if not m:
                return None
            return [float(x.strip()) for x in m.group(1).split(",") if x.strip()]

        pairs = [
            ("exp_multiplier", "倍率 1.0/1.3/1.6/2.0"),
            ("max_offline_hours", "离线小时"),
            ("xianmo_max_action_points", "仙魔行动力"),
            ("martial_daily_limit", "武功次数"),
            ("tower_sweep_limit", "天关扫荡"),
            ("suit_sweeps_max", "套装扫荡"),
        ]
        server = {
            "exp_multiplier": core.State.VIP_EXP_MULT,
            "max_offline_hours": core.State.VIP_OFFLINE_HOURS,
            "xianmo_max_action_points": core.State.VIP_XIANMO_POINTS,
            "martial_daily_limit": core.State.VIP_MARTIAL_DAILY,
            "tower_sweep_limit": core.State.VIP_TOWER_SWEEPS,
            "suit_sweeps_max": core.State.VIP_SUIT_SWEEPS,
        }
        for fn, label in pairs:
            client_vals = table(fn)
            if client_vals is None:
                print("       （客户端没有可解析的 %s，跳过）" % fn)
                continue
            mine = [float(x) for x in server[fn]]
            check("%s 与客户端一致 %s" % (label, client_vals), mine == client_vals, "服务端 %s" % mine)

    print("\n结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
