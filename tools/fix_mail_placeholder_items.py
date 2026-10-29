#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把邮箱里「之一」类占位道具换成具体道具（一次性数据修复）。

背景：`/lottery/draw` 早先把奖池配置里的 `item_pool` 行（name 是
「普通技能书之一」「稀有技能书之一」「极限装备之一」「五行属性宠物蛋」）
直接当道具名塞进邮件附件。客户端 `inventory_manager.add_item()` 只认具体
道具名，点「打开」必弹：

    附件添加失败：
    - 普通技能书之一 x1 添加失败：未知物品：普通技能书之一

奖励就永久卡在邮箱里。服务端已经改成抽奖时现场展开，这个脚本负责把**已经
发出去的**那些坏附件换成具体道具（等概率，口径与客户端 probabilityText 一致）。

用法：
    python3 tools/fix_mail_placeholder_items.py                # 预演，只打印
    python3 tools/fix_mail_placeholder_items.py --apply        # 真改（自动备份）
    python3 tools/fix_mail_placeholder_items.py --data /opt/lingyun/data --apply
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

# 占位名 → 可替换的具体道具（与 handlers_growth.LOTTERY_ITEM_POOLS 同口径）
PLACEHOLDER_ITEMS = {
    "普通技能书之一": ["自愈lv1技能书", "迅捷lv1技能书", "坚固lv1技能书",
                       "长寿lv1技能书", "强壮lv1技能书", "凶猛lv1技能书",
                       "追击lv1技能书", "忠心lv1技能书"],
    "稀有技能书之一": ["溅射lv1技能书", "反击lv1技能书", "神佑lv1技能书",
                       "连射lv1技能书", "吸血lv1技能书", "破甲lv1技能书",
                       "格挡lv1技能书", "必杀lv1技能书", "嘲讽lv1技能书"],
    "极限装备之一": ["极限武器", "极限衣服", "极限鞋子", "极限血佩", "极限内带"],
    "五行属性宠物蛋": ["金属性宠物蛋", "木属性宠物蛋", "水属性宠物蛋",
                       "火属性宠物蛋", "土属性宠物蛋"],
}

# 世界 Boss 礼包用的是另一个占位名，包里本来就该给「随机一本 lv1 技能书」
PLACEHOLDER_ITEMS["lv1宠物技能书之一"] = sorted({
    name for books in PLACEHOLDER_ITEMS.values() for name in books if name.endswith("技能书")
})


def _pick(name: str, rng: random.Random) -> str:
    options = PLACEHOLDER_ITEMS.get(name)
    return rng.choice(options) if options else name


def _fix_items(items, rng: random.Random, hits: list, where: str) -> bool:
    """就地替换 items 列表里的占位名，返回是否改动过。"""
    if not isinstance(items, list):
        return False
    changed = False
    for entry in items:
        if isinstance(entry, dict):
            raw = str(entry.get("name", "") or entry.get("item_id", "") or "")
            if raw in PLACEHOLDER_ITEMS:
                new_name = _pick(raw, rng)
                entry["name"] = new_name
                if "item_id" in entry:
                    entry["item_id"] = new_name
                hits.append((where, raw, new_name, int(entry.get("count", 1) or 1)))
                changed = True
    return changed


def fix_mails(mails, rng: random.Random, hits: list, where: str) -> bool:
    if not isinstance(mails, list):
        return False
    changed = False
    for mail in mails:
        if not isinstance(mail, dict):
            continue
        title = str(mail.get("title", "") or "")
        mid = str(mail.get("id", "") or "")
        attach = mail.get("attachments")
        if isinstance(attach, dict):
            if _fix_items(attach.get("items"), rng, hits, "%s#%s %s" % (where, mid, title)):
                changed = True
        elif isinstance(attach, list):
            for entry in attach:
                if isinstance(entry, dict) and _fix_items(entry.get("items"), rng, hits,
                                                          "%s#%s %s" % (where, mid, title)):
                    changed = True
    return changed


def process_file(path: Path, apply: bool, rng: random.Random) -> tuple[int, list]:
    """返回 (改动的账号文件数, 命中明细)。"""
    try:
        raw = io.open(path, encoding="utf-8").read()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        print("  跳过 %s：%s" % (path.name, exc))
        return 0, []
    if not isinstance(data, dict):
        return 0, []

    hits: list = []
    changed = False
    where = path.stem
    meta = data.get("meta")
    if isinstance(meta, dict):
        if fix_mails(meta.get("mails"), rng, hits, where):
            changed = True
        # 极少数老档把邮件放在 meta.mailList 之类的别名下，一并兜住
        for key in ("mailList", "mailsData"):
            if isinstance(meta.get(key), list) and fix_mails(meta.get(key), rng, hits, where):
                changed = True
    # 有些版本把邮箱挂在存档根上
    for key in ("mails", "mailList"):
        if isinstance(data.get(key), list) and fix_mails(data.get(key), rng, hits, where):
            changed = True

    if changed and apply:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = path.parent.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_dir / ("%s.json.bak-mailfix-%s" % (path.stem, stamp)))
        tmp = path.with_suffix(".json.tmp")
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        os.replace(tmp, path)
    return (1 if changed else 0), hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent.parent / "state"))
    ap.add_argument("--apply", action="store_true", help="真写盘（默认只预演）")
    ap.add_argument("--seed", type=int, default=20261028)
    args = ap.parse_args()

    data_dir = Path(args.data)
    accounts_dir = data_dir / "accounts"
    if not accounts_dir.is_dir():
        print("账号目录不存在：%s" % accounts_dir)
        return 2

    rng = random.Random(args.seed)
    files = sorted(accounts_dir.glob("*.json"))
    total_hits = 0
    touched = 0
    for path in files:
        n, hits = process_file(path, args.apply, rng)
        if hits:
            touched += n
            total_hits += len(hits)
            print("%s：" % path.name)
            for where, old, new, count in hits:
                print("   %-28s %s x%d → %s" % (where, old, count, new))
    mode = "已写入" if args.apply else "预演（未写盘）"
    print("\n%s：扫描 %d 个账号，%d 个有占位附件，共 %d 条被替换"
          % (mode, len(files), touched, total_hits))
    if not args.apply and total_hits:
        print("确认无误后加 --apply 再跑一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
