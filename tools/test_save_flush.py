#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""落盘去重（读请求不写盘）的回归测试。

背景：server.py 原来每个已鉴权请求结束都无条件 state.flush()，
等于把整份存档重写一遍（线上实测 6.9ms/次，其中 6.76ms 是 indent=1 的序列化）。
今日 78% 的请求是纯读，这些请求不该产生任何磁盘写入。

这套测试守三条底线：
  1. 内容没变 → 不写（且文件 mtime 不变）
  2. 真改了 → 一定写（bump 路径与「忘记 bump」路径都要覆盖）
  3. lastSeen 这种每请求都刷新的字段 → 不触发整档重写，但 60 秒内至少落一次

    python3 tools/test_save_flush.py
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def mtime(p: Path) -> float:
    return p.stat().st_mtime_ns


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lyflush_"))
    core.STATE_DIR = tmp          # State 每次构造时读这个全局
    seed = {"playerData": {"name": "测试道友", "level": 10},
            "petsData": {"active_pet_id": "p1", "pets": [{"id": "p1", "name": "嘟嘟鸟"}]},
            "items": [{"id": "i1", "name": "金币", "count": 100}],
            "gameSettings": {"current_map": "浮光水城外郊（0,0）"}}
    try:
        st = core.State("flush_test", seed=seed)
        path = st.path
        check("新号首次落盘", path.exists())

        # ---- 1. 纯读：连续 flush 不应再写盘
        st.flush()
        before = mtime(path)
        time.sleep(0.01)
        wrote = [st.flush() for _ in range(5)]
        check("内容没变 → flush 全部跳过", wrote == [False] * 5, str(wrote))
        check("内容没变 → 文件 mtime 不变", mtime(path) == before)

        # ---- 2. bump 后必须落盘
        st.bump()
        check("bump 后 flush 写入", st.flush() is True)
        disk = json.loads(path.read_text(encoding="utf-8"))
        check("revision 已落盘", int(disk["meta"]["revision"]) == int(st.meta["revision"]),
              "%s vs %s" % (disk["meta"].get("revision"), st.meta.get("revision")))
        check("写完立刻再 flush 又是跳过", st.flush() is False)

        # ---- 3. 忘记 bump 也要兜住（签名兜底）
        st.save.setdefault("playerData", {})["level"] = 99
        check("没 bump 但数据变了 → 仍然落盘", st.flush() is True)
        disk = json.loads(path.read_text(encoding="utf-8"))
        check("改动已落盘", disk["save"]["playerData"]["level"] == 99,
              str(disk["save"]["playerData"].get("level")))

        # ---- 4. 纯读接口的典型写法：只调 touch_seen（模拟 /user/info、/ranking）
        st.touch_seen()
        first = st.flush()
        check("touch_seen 首次（60 秒窗口）会落一次", first is True)
        time.sleep(0.01)
        st._seen_flush_ts = time.time()      # 把节流窗口推到「刚写过」
        before = mtime(path)
        for _ in range(10):
            st.touch_seen()
            check_skip = st.flush()
        check("窗口内连续心跳 → 不再写盘", before == mtime(path) and check_skip is False)

        # ---- 5. 落盘内容能原样读回（紧凑格式不影响结构）
        st2 = core.State("flush_test", seed=None)
        check("紧凑 JSON 可被重新读档", st2.save["playerData"]["level"] == 99,
              str(st2.save.get("playerData", {}).get("level")))
        check("读档后 flush 跳过写盘", st2.flush() is False)

        # ---- 6. force：停机时必须真写，把 lastSeen 这类易变字段带下去
        before = mtime(path)
        time.sleep(0.01)
        check("force=True 强制落盘", st2.flush(force=True) is True and mtime(path) != before)

        # ---- 6b. 贵族等级自愈必须自己 bump（线上真报过：GET /noble/info 改了存档没标脏）
        st3 = core.State("noble_test", seed=seed)
        st3.flush()
        rev_before = int(st3.meta["revision"])
        # 累计消耗够 2 档，但等级字段故意写成 0（老档 / 后台改档的典型样子）
        st3.player["noble_points"] = 2500
        st3.player["noble_level"] = 0
        st3.player.pop("noble_name", None)
        lv = st3.sync_noble_level()
        check("sync_noble_level 算出正确等级", lv == 2, str(lv))
        check("sync_noble_level 改了数据就 bump", int(st3.meta["revision"]) == rev_before + 1,
              "%s -> %s" % (rev_before, st3.meta.get("revision")))
        check("sync_noble_level 后 flush 立即写盘（不走签名兜底）", st3.flush() is True)
        disk3 = json.loads(st3.path.read_text(encoding="utf-8"))
        check("等级与称谓已落盘",
              int(disk3["save"]["playerData"]["noble_level"]) == 2
              and disk3["save"]["playerData"]["noble_name"] == st3.NOBLE_NAMES[2])
        check("没变化时不重复 bump", st3.sync_noble_level() == 2
              and int(st3.meta["revision"]) == rev_before + 1)

        # ---- 7. 相关容器（treasure_round / tokens）也走紧凑格式，行为不变
        p2 = tmp / "rounds.json"
        core.dump_json(p2, {"a": [1, 2, {"b": "中文"}]})
        check("dump_json 中文不转义", "中文" in p2.read_text(encoding="utf-8"))
        check("dump_json 无缩进", "\n" not in p2.read_text(encoding="utf-8").strip())
        check("dump_json 结构可读回", json.loads(p2.read_text(encoding="utf-8"))["a"][2]["b"] == "中文")

        # ---- 8. 性能：同样 124KB 的档，跳过的代价应远小于整档重写
        big = copy.deepcopy(st.raw)
        for i in range(400):
            big["save"].setdefault("items", []).append({"id": "x%d" % i, "name": "占位道具", "count": i})
        size_kb = len(json.dumps(big, ensure_ascii=False).encode()) / 1024
        t0 = time.perf_counter()
        for _ in range(20):
            core.dump_json(tmp / "big.json", big)
        write_ms = (time.perf_counter() - t0) / 20 * 1000
        st_big = core.State("big_test", seed=big.get("save"))
        st_big.flush()
        st_big.dirty = False
        t0 = time.perf_counter()
        for _ in range(20):
            st_big.flush()
        skip_ms = (time.perf_counter() - t0) / 20 * 1000
        print("  存档约 %.0fKB：整档重写 %.2fms → 判定跳过 %.2fms（快 %.1f 倍）"
              % (size_kb, write_ms, skip_ms, write_ms / max(skip_ms, 0.001)))
        check("跳过比写完快 2 倍以上", write_ms > skip_ms * 2, "%.2f vs %.2f" % (write_ms, skip_ms))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
