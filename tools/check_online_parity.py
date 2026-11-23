#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上 / 本地代码一致性检查：一条命令看穿「改了但没上线」。

背景：部署清单是手工维护的，漏一个文件就等于本地怎么修线上都不生效
（handlers_mail.py 漏了 9 天，gm.py / treasure_round.py 同样漏过）。
本工具不依赖部署清单，直接把两边所有 .py + data/*.json 的 md5 摆在一起。

用法：
  python3 tools/check_online_parity.py                # 人类可读
  python3 tools/check_online_parity.py --json        # 机器可读
  USER_HOST=user@host APP=/opt/lingyun/app python3 tools/check_online_parity.py

退出码：0 = 完全一致或差异全部在 FILES 里（下次部署会覆盖）
        1 = 存在「不在 FILES 里」的差异（需要人工决定要不要同步）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
USER_HOST = os.environ.get("USER_HOST") or os.environ.get("LINGYUN_SSH") or ""
APP = os.environ.get("APP", "/opt/lingyun/app")
MANIFEST = SERVER.parent / "update_server_all.sh"


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def local_map() -> dict[str, str]:
    out = {}
    for p in sorted(SERVER.glob("*.py")) + sorted((SERVER / "data").glob("*.json")):
        rel = p.relative_to(SERVER).as_posix()
        out[rel] = md5(p)
    return out


def remote_map() -> dict[str, str]:
    if not USER_HOST:
        raise RuntimeError("未设置 USER_HOST（或 LINGYUN_SSH），格式 user@host")
    cmd = ("cd %s && md5sum *.py data/*.json 2>/dev/null" % APP)
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", USER_HOST, cmd],
                       capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError("ssh 失败: " + (r.stderr.strip() or r.stdout.strip())[:200])
    out = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        h, f = line.split(None, 1)
        out[f.strip()] = h
    return out


def files_in_manifest() -> set[str]:
    txt = MANIFEST.read_text(encoding="utf-8")
    m = re.search(r"^FILES=\((.*?)^\)", txt, re.S | re.M)
    if not m:
        return set()
    return {l.split("#")[0].strip() for l in m.group(1).splitlines()
            if l.split("#")[0].strip().endswith((".py", ".json"))}


def main() -> int:
    as_json = "--json" in sys.argv
    try:
        loc, rem = local_map(), remote_map()
    except Exception as e:
        print("ERROR", e, file=sys.stderr)
        return 2

    managed = files_in_manifest()
    common = set(loc) & set(rem)
    diff = sorted(f for f in common if loc[f] != rem[f])
    only_local = sorted(set(loc) - set(rem))
    only_remote = sorted(set(rem) - set(loc))
    unmanaged = [f for f in diff if f not in managed]

    payload = {
        "local_files": len(loc), "remote_files": len(rem),
        "diff": [{"file": f, "managed": f in managed} for f in diff],
        "only_local": only_local, "only_remote": only_remote,
        "diff_not_in_FILES": unmanaged,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print("=== 线上/本地一致性 | 本地 %d 文件, 线上 %d 文件 ===" %
              (len(loc), len(rem)))
        print("--- 内容不同 %d 个 ---" % len(diff))
        for f in diff:
            print("  %-8s %s" % ("[会部署]" if f in managed else "[需人工]", f))
        if only_local:
            print("--- 仅本地有 %d 个（线上缺） ---" % len(only_local))
            for f in only_local:
                print("  ", f)
        if only_remote:
            print("--- 仅线上有 %d 个 ---" % len(only_remote))
            for f in only_remote:
                print("  ", f)
        print("\n判定：%s" % ("⚠️ 有 %d 个差异不在 FILES 里，下次部署也不会同步" % len(unmanaged)
                              if unmanaged else "✅ 所有差异都会在下次部署时被覆盖"))

    return 1 if unmanaged else 0


if __name__ == "__main__":
    sys.exit(main())
