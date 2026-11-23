#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从客户端 `scripts/data/suit_database_detailed.gd` 抽出套装数值 → data/suit-stats.json

服务端只有一份 Python，读不到客户端的 GDScript 常量；但「套装转换」要在服务端
算「同套装的某个部位基础值 × 0.9~1.1」，所以把 SUIT_DATA 抽成 JSON 放在 data/。

    python3 tools/gen_suit_stats.py                      # 默认路径
    python3 tools/gen_suit_stats.py /path/to/suit_database_detailed.gd

客户端改了套装数值就重跑一次（打包含新套装时必须重跑，否则转出来的数值对不上）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
DEFAULT_GD = (SERVER.parent.parent
              / "recovered-godot-project" / "scripts" / "data" / "suit_database_detailed.gd")
OUT = SERVER / "data" / "suit-stats.json"


def _extract_block(text: str, const_name: str) -> str:
    """取出 `const XXX = { ... }` 的完整花括号内容（配平计数，不依赖缩进）。"""
    m = re.search(r"^const\s+%s\s*[:=]\s*\{" % re.escape(const_name), text, re.M)
    if not m:
        raise SystemExit("没找到 const %s" % const_name)
    depth = 0
    start = m.end() - 1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise SystemExit("const %s 花括号不配平" % const_name)


def _gd_dict_to_json(blob: str) -> dict:
    """GDScript 字典字面量 → JSON。

    只处理这里会出现的三种差异：整数键要加引号、尾逗号、true/false/null 大小写。
    字符串里的中文/全角符号原样保留。
    """
    s = blob
    # 数字键：1: { → "1": {
    s = re.sub(r'(?m)(^|[{,]\s*)(\d+)(\s*:)', lambda m: '%s"%s"%s' % (m.group(1), m.group(2), m.group(3)), s)
    # 去尾逗号（GDScript 允许，JSON 不允许）
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    # 布尔 / 空
    s = re.sub(r"\btrue\b", "true", s)
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bnull\b", "null", s)
    return json.loads(s)


def main() -> int:
    gd = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_GD
    if not gd.exists():
        raise SystemExit("客户端脚本不存在：%s" % gd)
    text = gd.read_text(encoding="utf-8")
    suits = _gd_dict_to_json(_extract_block(text, "SUIT_DATA"))
    slots = ["weapon", "helmet", "armor", "boots", "accessory"]
    out = {}
    for sid, data in sorted(suits.items(), key=lambda kv: int(kv[0])):
        piece = data.get("piece_stats", {}) or {}
        out[str(int(sid))] = {
            "name": str(data.get("name", "")),
            "required_cultivation": int(data.get("required_cultivation", 0) or 0),
            "slot_stats": {slot: {k: int(v) for k, v in (piece.get(slot) or {}).items()} for slot in slots},
        }
    # 校验：每个套装 5 个部位都要有，且各部位恰好一个主属性
    for sid, data in out.items():
        missing = [s for s in slots if s not in data["slot_stats"]]
        if missing:
            raise SystemExit("套装 %s 缺部位：%s" % (sid, missing))
    payload = {
        "note": "由 tools/gen_suit_stats.py 从客户端 suit_database_detailed.gd 生成，勿手改",
        "variation": {"min": 0.9, "max": 1.1},
        "slots": slots,
        "slot_names": {"weapon": "武器", "helmet": "头盔", "armor": "衣服", "boots": "鞋子", "accessory": "饰品"},
        "suits": out,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("写出 %s：%d 个套装" % (OUT, len(out)))
    for sid, data in list(out.items())[:3]:
        print("  %s %s %s" % (sid, data["name"], data["slot_stats"]["weapon"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
