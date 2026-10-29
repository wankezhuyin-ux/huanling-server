#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务端侧：把前 3 张图那 5 只怪的显示名换成新名。

服务端的物种表、地图刷怪表、新档种子、图鉴、进化链、前端配置各自存了一份名字，
漏一处就会出现「服务端邮件发的是旧名、客户端显示新名」。一次全扫。

「独行」只改带引号的形式 —— 注释里有「单独行动」，裸替换会把它改坏。
用法：python3 tools/rename_pets.py [--check]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OLD_TO_NEW = {
    "琳琳": "赤翅蛾",
    "愣头青": "苔背兽",
    "御林兔": "沙耳兔",
    "小叮咚": "滴水蛙",
}
QUOTED_ONLY = {
    '"独行"': '"铁嘴鸦"',
}

SKIP_DIRS = {".git", "__pycache__", "state", "backup", "logs"}


def main() -> int:
    check = "--check" in sys.argv
    total = 0
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix not in (".py", ".json", ".md", ".txt") or not path.is_file():
            continue
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        hits = 0
        for old, new in OLD_TO_NEW.items():
            n = text.count(old)
            if n:
                hits += n
                text = text.replace(old, new)
        for old, new in QUOTED_ONLY.items():
            n = text.count(old)
            if n:
                hits += n
                text = text.replace(old, new)
        if hits:
            total += hits
            print(f"{'待改' if check else '已改'} {hits:>3} 处：{rel}")
            if not check:
                path.write_text(text, encoding="utf-8")
    print(f"合计 {total} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
