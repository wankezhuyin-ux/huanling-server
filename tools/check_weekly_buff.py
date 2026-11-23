#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周常加成体检：今天有没有「野外战斗经验 +100%」。

用法（在 server 目录下）：
    python3 tools/check_weekly_buff.py              # 只看本地配置 + 今天判定
    python3 tools/check_weekly_buff.py --dump WEEK  # 空 WEEK 时打印一整周的表，
                                                   # 例如 --dump 7 表示模拟"假如今天是周日"

判定口径与 core.State.weekly_field_exp_bonus() 完全一致：
    weeklyBuff.enabled = true
    且当天那条 day.enabled = true
    且 label 含「野外」、value >= 100
    weekday 用 isoweekday，周一是 1、周日是 7（与 runtime-config 的 days[].weekday 对应）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402

NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def day_of(wd: int) -> dict:
    cfg = core.RULES.m("weekly_buff")
    for day in cfg.get("days") or []:
        if int(day.get("weekday", 0) or 0) != wd:
            continue
        if not bool(day.get("enabled", True)):
            continue
        return day
    return {}


def field_exp_bonus(day: dict) -> float:
    if not day:
        return 0.0
    label = str(day.get("label", "") or "")
    try:
        value = float(day.get("value", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if "野外" in label and value >= 100:
        return value / 100.0
    return 0.0


def main() -> int:
    cfg = core.RULES.m("weekly_buff")
    now = core.now()
    wd = now.isoweekday()

    print("北京时间：%s（%s）" % (now.strftime("%Y-%m-%d %H:%M:%S"), NAMES[wd - 1]))
    print("weeklyBuff.enabled = %s" % cfg.get("enabled", True))

    if "--dump" in sys.argv:
        idx = sys.argv.index("--dump")
        wd = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else wd

    day = day_of(wd) if bool(cfg.get("enabled", True)) else {}
    bonus = field_exp_bonus(day)

    print()
    if day:
        print("今天周常：%s（value=%s，dailyCap=%s）"
              % (day.get("label"), day.get("value"), day.get("dailyCap")))
    else:
        print("今天周常：无")

    print("野外经验加成：%s" % ("+%d%%" % int(bonus * 100) if bonus > 0 else "无"))

    # 全周对照
    print()
    print("%-6s %-58s %s" % ("星期", "周常内容", "野外经验"))
    for d in range(1, 8):
        row = day_of(d) if bool(cfg.get("enabled", True)) else {}
        b = field_exp_bonus(row)
        mark = "→ " if d == now.isoweekday() else "  "
        print("%s%-4s %-58s %s"
              % (mark, NAMES[d - 1], str(row.get("label", "—"))[:58],
                 ("+%d%%" % int(b * 100)) if b > 0 else "—"))

    print()
    if bonus > 0:
        print("结论：今天【野外战斗经验 +100%】生效（周末常驻，全服所有玩家，不限次数）。")
        print("      注意：只在服务端权威结算里乘（野外战斗、仙魔战场经验事件），")
        print("      天关只给金币和礼包、没有经验，所以和这条无关。")
    else:
        print("结论：今天没有野外经验加成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
