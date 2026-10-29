#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把邮箱里「客户端不认识的物品」换成等值的真实道具（一次性数据修复）。

背景：早期奖励池里写了 神兽蛋 / 高级技能书 / 洗髓丹 / 灵兽经验丹 / 坚固lv1技能书，
这几个名字在客户端 game_data.gd ALL_ITEMS 里**都不存在**，玩家点邮件「打开」必弹：

    附件添加失败：
    - 神兽蛋 x1 添加失败：未知物品：神兽蛋

奖励就永久卡在邮箱里（玩家截图实证）。服务端已改成只发客户端认识的名字，
这个脚本负责把**已经发出去的**那些坏附件换掉，并拆掉「法宝碎片*20」这类
把数量写进名字的附件。

只处理明确的坏名单，其它认不出的名字只打印提醒（可能是活动道具，别乱改）。

用法：
    python3 tools/fix_xianmo_mail_items.py                # 预演，只打印
    python3 tools/fix_xianmo_mail_items.py --apply        # 真改（先备份 state/accounts）
"""
from __future__ import annotations

import json
import random
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import STATE_DIR, split_item_qty  # noqa: E402

NAMES = set(json.loads((ROOT / "data" / "client-item-names.json").read_text(encoding="utf-8"))["items"])

EGGS = [e for e in ["金变异宠物蛋", "木变异宠物蛋", "水变异宠物蛋", "火变异宠物蛋",
                    "土变异宠物蛋", "5+宠物蛋"] if e in NAMES]
BOOKS = [b for b in ["格挡lv1技能书", "反击lv1技能书", "自愈lv1技能书", "迅捷lv1技能书",
                     "长寿lv1技能书", "强壮lv1技能书", "追击lv1技能书", "忠心lv1技能书",
                     "嘲讽lv1技能书", "破甲lv1技能书"] if b in NAMES]

# 明确「客户端没有、且我们知道该换成什么」的坏名字
EXPLICIT = {
    "神兽蛋": lambda: random.choice(EGGS),
    "高级技能书": lambda: random.choice(BOOKS),
    "坚固lv1技能书": lambda: "格挡lv1技能书" if "格挡lv1技能书" in NAMES else random.choice(BOOKS),
    "洗髓丹": lambda: "悟道仙丹",
    "灵兽经验丹": lambda: "双倍经验丹",
    "宠物升级丹": lambda: "双倍经验丹",
    "人物升级丹": lambda: "双倍经验丹",
}


def main() -> None:
    apply = "--apply" in sys.argv
    accounts = STATE_DIR / "accounts"
    files = sorted(accounts.glob("*.json"))
    changed_files = 0
    changed_items = 0
    unknown: set[str] = set()
    preview: list[str] = []

    if apply and files:
        backup = STATE_DIR / ("accounts-backup-xianmo-mail-%s" % time.strftime("%Y%m%d-%H%M%S"))
        shutil.copytree(accounts, backup)
        print("备份 ->", backup)

    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mails = ((raw.get("meta") or {}).get("mails") or [])
        touched = False
        for mail in mails:
            if not isinstance(mail, dict) or mail.get("is_claimed"):
                continue
            items = ((mail.get("attachments") or {}).get("items") or [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "") or "")
                note = ""
                # ① 名字里带数量后缀：拆成 name + count
                base, qty = split_item_qty(name)
                if base != name:
                    item["name"] = base
                    item["count"] = max(1, int(item.get("count", 1) or 1)) * qty
                    note = "%s*%d" % (base, qty)
                    name = base
                # ② 明确的坏名字：等值替换
                if name in EXPLICIT:
                    new_name = EXPLICIT[name]()
                    if new_name and new_name != name:
                        item["name"] = new_name
                        if "id" in item:
                            item["id"] = new_name
                        note = (note + " → " if note else "") + new_name
                if note:
                    changed_items += 1
                    touched = True
                    preview.append("%s  [%s] %s x%s  =>  %s" % (
                        path.stem, mail.get("title", ""), name,
                        item.get("count", 1), note))
                elif name and NAMES and name not in NAMES:
                    unknown.add(name)
        if touched:
            changed_files += 1
            if apply:
                path.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")

    for line in preview[:60]:
        print(line)
    if len(preview) > 60:
        print("… 另有 %d 条" % (len(preview) - 60))
    if unknown:
        print("\n以下名字也不在客户端物品表里，但不在已知坏名单，未改动（请人工确认）：")
        for n in sorted(unknown):
            print("  ·", n)
    print("\n%s：%d 个附件 / %d 个账号" % ("已修复" if apply else "待修复",
                                          changed_items, changed_files))
    if not apply and changed_items:
        print("确认后加 --apply 执行")


if __name__ == "__main__":
    main()
