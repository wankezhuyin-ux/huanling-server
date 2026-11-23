#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨端道具名对账：服务端会「扣」或「发」的名字，客户端 ALL_ITEMS 里必须有。

为什么需要这个脚本
------------------
服务端 `State.take_item()` / `find_item()` 是**按名字精确匹配**的，客户端
`inventory_manager.add_item()` 只认 `game_data.gd ALL_ITEMS` 的键。两边名字一旦
对不上，症状是：

  · 消耗类：玩家点「使用」→「道具数量不足」，一颗都吃不上（双倍经验符 / 蓄灵丹）
  · 发放类：邮件或背包出现「未知物品：xxx」，奖励永久卡住（神兽蛋 / 灵兽经验丹）

这类 bug 不会报错、不会崩，只会在玩家手里静默失效，所以必须靠对账脚本兜住。

检查范围
--------
  1. 消耗规则：`_CONSUME_RULES` / `_CULTIVATION_PILL_RULES` 里的道具名
  2. 硬编码收发：`add_item("X")` / `take_item("X")` / `item_count("X")` / `has_item("X")`
  3. 商店与奖励表：`"itemName"` / `"item_id"` 后面的名字
  4. 反向：客户端白名单（CULTIVATION_PILL_WHITELIST_KEYS / DOUBLE_EXP_WHITELIST_KEYS）
     里的每个 consumeKey，服务端规则表必须有，且**道具名与客户端一致**

用法
----
    python3 tools/check_item_names.py          # 报告
    python3 tools/check_item_names.py --strict # 有问题时退出码 1（打包前用）
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
CLIENT = SERVER.parent.parent / "recovered-godot-project"
GAME_DATA = CLIENT / "scripts" / "game_data.gd"
INVENTORY_GD = CLIENT / "scripts" / "inventory_manager.gd"

# 只把这些名字当作「可能是道具名」来核对，避免把状态字段、地图名等当成道具
ITEMISH = ("丹", "石", "锦囊", "礼包", "书", "蛋", "碎片", "装备", "令牌", "草",
           "材料", "套装", "符", "袋", "盒", "玉", "晶", "魂", "骨", "皮", "羽",
           "花", "果", "浆", "丸", "散", "印", "珠", "铁", "木", "矿", "强化石")

# 已知的「名字对不上但不是回归」的例外，必须写清原因，否则等于把真 bug 藏起来：
KNOWN_EXCEPTIONS = {
    "五行属性宠物蛋": "签到表的族名，handlers_play._SIGNIN7_ITEM_FAMILY 发货前会随机落成"
                       "金/木/水/火/土属性宠物蛋，不会真的把这四个字发进背包",
    "套装转换石": "/equipment/suit-convert 仍是旧桩（客户端传 equipmentIds + 三件材料），"
                  "待按客户端契约重做后一起清掉",
}

CALL_RE = re.compile(r'\b(add_item|take_item|item_count|has_item)\(\s*"([^"]+)"')
RULE_LINE_RE = re.compile(r'^\s*"([a-z0-9_]+)":\s*\(\s*"([^"]+)"')
SHOP_RE = re.compile(r'"itemName":\s*"([^"]+)"')
TABLE_RE = re.compile(r'"(?:item_id|itemName)":\s*"([^"]+)"')


def client_items() -> set[str]:
    names = set(re.findall(r'^\t"([^"]+)":', GAME_DATA.read_text(encoding="utf-8"), re.M))
    snap = SERVER / "data" / "client-item-names.json"
    if snap.exists():
        rows = json.loads(snap.read_text(encoding="utf-8"))
        items = rows.get("items") if isinstance(rows, dict) else rows
        names |= {str(x) for x in (items or [])}
    return names


def server_rules() -> dict[str, tuple[str, str, int]]:
    """{consumeKey: (道具名, 文件, 行号)}"""
    out: dict[str, tuple[str, str, int]] = {}
    py = SERVER / "handlers_growth.py"
    if not py.exists():
        return out
    lines = py.read_text(encoding="utf-8").splitlines()
    in_rules = False
    for i, line in enumerate(lines, 1):
        if line.startswith("_CONSUME_RULES = {") or line.startswith("_CULTIVATION_PILL_RULES = {"):
            in_rules = True
            continue
        if in_rules and line.startswith("}"):
            in_rules = False
            continue
        if in_rules:
            m = RULE_LINE_RE.match(line)
            if m:
                out[m.group(1)] = (m.group(2), py.name, i)
    return out


def client_whitelists() -> dict[str, tuple[str, str]]:
    """{consumeKey: (客户端道具名, 常量名)}，取自 inventory_manager.gd 的 `"名字": "key"` 表。"""
    out: dict[str, tuple[str, str]] = {}
    if not INVENTORY_GD.exists():
        return out
    text = INVENTORY_GD.read_text(encoding="utf-8")
    for const in ("CULTIVATION_PILL_WHITELIST_KEYS", "DOUBLE_EXP_WHITELIST_KEYS",
                  "PET_DOUBLE_EXP_WHITELIST_KEYS"):
        m = re.search(r"const %s\s*:?\s*=\s*\{(.*?)\n\}" % const, text, re.S)
        if not m:
            continue
        for row in re.finditer(r'"([^"]+)"\s*:\s*"([^"]+)"', m.group(1)):
            out[row.group(2)] = (row.group(1), const)
    return out


def scan() -> list[tuple[str, int, str, str]]:
    """返回 [(文件, 行号, 道具名, 来源说明)]。"""
    hits: list[tuple[str, int, str, str]] = []
    for py in sorted(SERVER.glob("*.py")):
        if py.name.startswith("test_") or py.name.startswith("fix_"):
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            for m in CALL_RE.finditer(line):
                hits.append((py.name, i, m.group(2), m.group(1)))
            if '"itemName"' in line:
                for m in SHOP_RE.finditer(line):
                    hits.append((py.name, i, m.group(1), "商店 itemName"))
            if '"item_id"' in line or '"itemName"' in line:
                for m in TABLE_RE.finditer(line):
                    hits.append((py.name, i, m.group(1), "奖励表"))
    for key, (name, fname, line) in server_rules().items():
        hits.append((fname, line, name, "消耗规则"))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有缺失时退出码 1")
    args = ap.parse_args()

    names = client_items()
    if not names:
        print("❌ 读不到客户端道具表：%s" % GAME_DATA)
        return 2
    print("客户端 ALL_ITEMS：%d 项" % len(names))

    problems = 0

    # ---- 1/2/3：服务端用到的名字，客户端是否认识
    seen: set[str] = set()
    missing: list[tuple[str, int, str, str]] = []
    skipped: list[str] = []
    for fname, line, name, src in scan():
        if name in names or not any(k in name for k in ITEMISH):
            continue
        if name in KNOWN_EXCEPTIONS:
            if name not in skipped:
                skipped.append(name)
            continue
        if name not in seen:
            seen.add(name)
        missing.append((fname, line, name, src))

    if skipped:
        print("\n已知例外（不算回归）：")
        for name in skipped:
            print("  %-14s %s" % (name, KNOWN_EXCEPTIONS[name]))

    if missing:
        problems += len(missing)
        print("\n❌ 客户端不认识的道具名：")
        for fname, line, name, src in missing:
            near = difflib.get_close_matches(name, names, n=3, cutoff=0.3)
            print("  %-22s:%-5d %-4s %-14s  近似：%s"
                  % (fname, line, src, name, "、".join(near) or "无"))
    else:
        print("\n✓ 服务端引用到的道具名，客户端全部认识")

    # ---- 4：客户端消耗白名单 → 服务端规则表，key 与名字都要对上
    rules = server_rules()
    wl = client_whitelists()
    if wl:
        bad: list[str] = []
        for key, (item_name, const) in sorted(wl.items()):
            if key not in rules:
                bad.append("%s 的 %s（%s）在服务端无消耗规则" % (const, key, item_name))
                continue
            srv_name = rules[key][0]
            if srv_name != item_name:
                bad.append("%s 的 %s：客户端=%s，服务端=%s（%s:%d）"
                           % (const, key, item_name, srv_name, rules[key][1], rules[key][2]))
        if bad:
            problems += len(bad)
            print("\n❌ 消耗白名单与服务端规则不一致：")
            for row in bad:
                print("  " + row)
        else:
            print("✓ 客户端 %d 个消耗白名单 key，服务端规则与道具名全部对齐" % len(wl))
    else:
        print("⚠ 没读到客户端消耗白名单（inventory_manager.gd），跳过第 4 项")

    return 1 if (args.strict and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
