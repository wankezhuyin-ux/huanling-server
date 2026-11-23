#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性运维脚本：把线上历史钱币袋按 10 万金币/个 等值兑换并清空。

规则必须与 handlers_account._convert_coin_bags_in_save 完全一致
（阈值 999、items + warehouse_items、只加 playerData.gold）。

使用方法（在云主机上，服务用户权限）：
    sudo systemctl stop lingyun
    sudo python3 purge_coin_bags_once.py /opt/lingyun/app/state/accounts
    sudo systemctl start lingyun

**运行前务必先备份 accounts 目录**（2026-11-07 已备份到
/home/ubuntu/backups/accounts-before-coinbag-convert-*.tar.gz）。
服务运行时不要直接改存档：内存里的 State 会把文件覆盖回去。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

NAME = "钱币袋"
UNIT_GOLD = 100000
THRESHOLD = 999


def convert(save: dict) -> tuple[int, int]:
    inv = save.get("inventoryData")
    if not isinstance(inv, dict):
        return 0, 0
    keys = [k for k in ("items", "warehouse_items") if isinstance(inv.get(k), list)]
    total = 0
    for key in keys:
        for row in inv[key]:
            if isinstance(row, dict) and str(row.get("name", "")) == NAME:
                try:
                    total += max(0, int(row.get("count", 0) or 0))
                except (TypeError, ValueError):
                    pass
    if total <= THRESHOLD:
        return 0, 0
    for key in keys:
        inv[key] = [row for row in inv[key]
                    if not (isinstance(row, dict) and str(row.get("name", "")) == NAME)]
    gold = total * UNIT_GOLD
    player = save.get("playerData")
    if isinstance(player, dict):
        try:
            cur = int(player.get("gold", 0) or 0)
        except (TypeError, ValueError):
            cur = 0
        player["gold"] = max(0, cur) + gold
    return total, gold


def main() -> int:
    base = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/lingyun/app/state/accounts")
    rows: list[tuple[str, int, int]] = []
    for path in sorted(base.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print("  SKIP %s (%s)" % (path.name, exc))
            continue
        save = doc.get("save")
        if not isinstance(save, dict):
            continue
        bags, gold = convert(save)
        if not bags:
            continue
        meta = doc.setdefault("meta", {})
        prev = meta.get("coinBagAutoConvert") if isinstance(meta.get("coinBagAutoConvert"), dict) else {}
        meta["coinBagAutoConvert"] = {
            "count": int(prev.get("count", 0) or 0) + bags,
            "gold": int(prev.get("gold", 0) or 0) + gold,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": "one-off-purge",
        }
        st = path.stat()
        tmp = path.with_suffix(".json.purge.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
        rows.append((path.name, bags, gold))
    print("PURGED_ACCOUNTS = %d" % len(rows))
    for name, bags, gold in rows:
        print("  %-24s 钱袋 %7d  →  金币 %d" % (name, bags, gold))
    print("TOTAL_BAGS = %d" % sum(r[1] for r in rows))
    print("TOTAL_GOLD = %d" % sum(r[2] for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
