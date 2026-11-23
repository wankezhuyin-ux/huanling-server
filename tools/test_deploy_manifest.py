#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署清单守卫：把「本地修了、线上不生效」变成测试失败，而不是靠人记。

2026-11-06 复盘：handlers_mail.py 修好后线上一直跑 10-28 初始版，因为它
从没进 update_server_all.sh 的 FILES。同类盲区还有 30 个文件（被 server.py
import、或被 load_json(DATA/...) 读取，却既不在 FILES 也没人跟踪）。

本测试自动推导「必须被跟踪的文件全集」，要求每个文件至少落在：
  · FILES    —— 每次部署自动覆盖（改动会生效）
  · TRACKED  —— 显式登记「我知道它，但不自动覆盖」（线上可能手改过）
两者都没有 = 新增模块/数据文件后会被静默漏掉 → FAIL。

运行：python3 tools/test_deploy_manifest.py
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
DEPLOY = SERVER.parent
MANIFEST = DEPLOY / "update_server_all.sh"

FAILS: list[str] = []
TOTAL = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    TOTAL[0] += 1
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def parse_array(text: str, name: str) -> set[str]:
    m = re.search(r"^%s=\((.*?)^\)" % re.escape(name), text, re.S | re.M)
    if not m:
        return set()
    out = set()
    for line in m.group(1).splitlines():
        line = line.split("#")[0].strip()
        if line and (line.endswith(".py") or line.endswith(".json")):
            out.add(line)
    return out


def derive_needed() -> tuple[set[str], set[str]]:
    """返回 (py 模块, data 文件)。"""
    py: set[str] = {"core.py", "server.py"}
    data: set[str] = set()
    sources = sorted(SERVER.glob("*.py"))
    for p in sources:
        if p.name.startswith("test_"):
            continue
        src = p.read_text(encoding="utf-8")
        for m in re.findall(r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_]*)", src, re.M):
            if (SERVER / (m + ".py")).exists():
                py.add(m + ".py")
        # load_json(DATA / "x.json") 与任意 DATA / "x.json"
        for f in re.findall(r"DATA\s*/\s*[\'\"]([^\'\"]+\.json)[\'\"]", src):
            if (SERVER / "data" / f).exists():
                data.add("data/" + f)
    return py, data


def main() -> int:
    txt = MANIFEST.read_text(encoding="utf-8")
    files = parse_array(txt, "FILES")
    tracked = parse_array(txt, "TRACKED")
    py_needed, data_needed = derive_needed()
    needed = py_needed | data_needed

    print("=== 部署清单守卫 ===")
    print("  推导：%d 个 py 模块 + %d 个 data 文件 = %d 个必须跟踪" %
          (len(py_needed), len(data_needed), len(needed)))
    print("  FILES=%d  TRACKED=%d" % (len(files), len(tracked)))

    check("FILES 非空", len(files) >= 10, str(len(files)))
    untracked = sorted(needed - files - tracked)
    check("所有被引用文件都在 FILES 或 TRACKED 里", not untracked,
          "未跟踪: " + ", ".join(untracked[:20]))
    for f in untracked[:20]:
        print("       UNTRACKED", f)

    # FILES 里的每个文件必须存在且可解析
    bad = []
    for f in sorted(files):
        p = SERVER / f
        if not p.exists():
            bad.append(f + " (缺失)")
            continue
        try:
            if f.endswith(".json"):
                json.loads(p.read_text(encoding="utf-8"))
            else:
                ast.parse(p.read_text(encoding="utf-8"))
        except Exception as e:
            bad.append("%s (%s)" % (f, type(e).__name__))
    check("FILES 里每个文件存在且语法/JSON 可解析", not bad, "; ".join(bad[:5]))

    # 清单里的文件不能凭空出现（本地没有 = 部署会失败）
    ghosts = sorted(f for f in (files | tracked) if not (SERVER / f).exists())
    check("清单没有指向本地不存在的路径", not ghosts, ", ".join(ghosts[:8]))

    # 部署脚本必须带「部署后 md5 复核」，否则传上去没生效也看不出来
    check("部署脚本含部署后 md5 复核", "PARITY_CHECK" in txt and "md5sum" in txt)
    check("TRACKED 已登记不自动覆盖的文件", len(tracked) >= 20, str(len(tracked)))

    # 线上/本地一致性检查工具必须在
    check("存在线上一致性检查工具", (SERVER / "tools" / "check_online_parity.py").exists())

    print("\n通过 %d，失败 %d" % (TOTAL[0] - len(FAILS), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
