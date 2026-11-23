#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性对账：把钱币袋兑换结果校正为「清理前服务端权威快照 × 10 万金币」。

背景（2026-11-07）：批量清理后，在线客户端仍上传了清理前的旧快照，
旧版自动兑换把同一批历史钱袋又发了一遍金币。本脚本以停服前的备份
（accounts-before-coinbag-convert-*.tar.gz）为基准：

    expected_bags = 备份里该账号的钱币袋总数
    应发金币      = expected_bags × 100000
    已发金币      = meta.coinBagAutoConvert.gold（累计，含重复发放）
    校正          = playerData.gold -= (已发 - 应发)

同时清空所有残余钱币袋、给每个账号打上 coinBagPurgeDone 幂等标记，
避免服务恢复后旧快照再次触发兑换。

用法（云主机，服务已停）：
    sudo python3 reconcile_coin_bag_purge.py \
        /opt/lingyun/app/state/accounts /tmp/ly_coinbag_expected.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

NAME = "钱币袋"
UNIT_GOLD = 100000


def bag_count(inv: dict) -> int:
    total = 0
    for key in ("items", "warehouse_items"):
        rows = inv.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and str(row.get("name", "")) == NAME:
                try:
                    total += max(0, int(row.get("count", 0) or 0))
                except (TypeError, ValueError):
                    pass
    return total


def strip_bags(inv: dict) -> None:
    for key in ("items", "warehouse_items"):
        rows = inv.get(key)
        if isinstance(rows, list):
            inv[key] = [row for row in rows
                        if not (isinstance(row, dict) and str(row.get("name", "")) == NAME)]


def main() -> int:
    base = Path(sys.argv[1])
    expected = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    rows = []
    for path in sorted(base.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        save = doc.get("save")
        if not isinstance(save, dict):
            continue
        inv = save.get("inventoryData")
        if not isinstance(inv, dict):
            inv = {}
            save["inventoryData"] = inv
        now_bags = bag_count(inv)
        baseline = max(int(expected.get(path.name, 0) or 0), now_bags)
        should_gold = baseline * UNIT_GOLD
        meta = doc.setdefault("meta", {})
        prev = meta.get("coinBagAutoConvert") if isinstance(meta.get("coinBagAutoConvert"), dict) else {}
        already = int(prev.get("gold", 0) or 0)
        delta = already - should_gold
        player = save.setdefault("playerData", {})
        try:
            gold_before = int(player.get("gold", 0) or 0)
        except (TypeError, ValueError):
            gold_before = 0
        gold_after = max(0, gold_before - delta)
        player["gold"] = gold_after
        strip_bags(inv)
        meta["coinBagAutoConvert"] = {
            "count": baseline,
            "gold": should_gold,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": "reconciled",
        }
        meta["coinBagPurgeDone"] = True
        st = path.stat()
        tmp = path.with_suffix(".json.reconcile.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
        if baseline or delta or now_bags:
            rows.append((path.name, baseline, now_bags, already, should_gold,
                         delta, gold_before, gold_after))
    print("RECONCILED_ACCOUNTS = %d" % len(rows))
    for name, baseline, now_bags, already, should, delta, before, after in rows:
        print("  %-22s 基准%7d 残余%5d 已发%14d 应发%14d 校正%+15d 金币 %d → %d"
              % (name, baseline, now_bags, already, should, -delta, before, after))
    print("TOTAL_BASELINE_BAGS = %d" % sum(r[1] for r in rows))
    print("TOTAL_GOLD_ADJUSTMENT = %+d" % -sum(r[5] for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
