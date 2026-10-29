#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运维自净能力测试：请求日志轮转 + 令牌表清理。

对应两个「只涨不跌」的文件：
  · state/requests.log  纯 append，4 天 14 MB（≈1.2 GB/年）
  · state/tokens.json   登录一次加一条，从不回收，598 条里只有 13 个账号

守的底线：
  1. 轮转不丢日志行（归档 + 当前文件 = 写过的全部行）
  2. 归档有上限，不会把上一轮垃圾挪个地方继续堆
  3. 并发写不炸、不错行
  4. 令牌清理只删「确定用不上」的：过期 / 账号已删 / 同账号超额的旧令牌
  5. 没东西可删时不写盘（别把例行清理变成每半小时一次的整表重写）

    python3 tools/test_ops_housekeeping.py
"""
from __future__ import annotations

import gzip
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
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


def read_archive(p: Path) -> str:
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return fh.read()


def wait_archives(folder: Path, count: int, timeout: float = 8.0) -> list[Path]:
    """等后台压缩线程把 count 份归档写出来（压缩是异步的）。"""
    end = time.time() + timeout
    while time.time() < end:
        files = sorted(folder.glob("*.log.gz"))
        if len(files) >= count:
            return files
        time.sleep(0.02)
    return sorted(folder.glob("*.log.gz"))


def settle(folder: Path, plain_pattern: str, timeout: float = 10.0) -> None:
    """等压缩线程收工：不再有未压缩的归档残留。"""
    end = time.time() + timeout
    while time.time() < end:
        if not list(folder.glob(plain_pattern)):
            return
        time.sleep(0.02)


# ---------------------------------------------------------------- 日志轮转
def test_rotation(tmp: Path) -> None:
    print("\n[日志轮转]")
    log = applog.RotatingRequestLog(tmp / "requests.log", max_bytes=2048, keep=3)
    # 1. 阈值内不轮转
    for i in range(5):
        log.write("短行 %d" % i)
    check("阈值内不轮转", log.rotations == 0 and (tmp / "requests.log").exists())

    # 2. 写爆阈值 → 轮转，当前文件重新开始
    for i in range(200):
        log.write("2026-10-28T20:00:00 GET /ranking 200 2.11.0 ok #%04d" % i)
    archives = wait_archives(tmp, 1)
    check("超阈值触发轮转", log.rotations >= 1, "rotations=%d" % log.rotations)
    check("生成 gzip 归档", len(archives) >= 1, str(archives))
    check("归档后当前文件变小", (tmp / "requests.log").stat().st_size < 2048,
          str((tmp / "requests.log").stat().st_size))
    check("归档内容可解压且是原文本", "#" in read_archive(archives[0]) and "ranking" in read_archive(archives[0]))

    # 3. 不丢行：阈值压到 1KB，一秒内连转十几次，归档不能被同名覆盖
    #    （真实踩过：压缩删掉原始 .log 后，同一秒的下一次轮转又用了同一个名字，
    #      压缩时把上一份归档整个覆盖掉，日志凭空少一大段）
    lossless = applog.RotatingRequestLog(tmp / "lossless.log", max_bytes=1024, keep=99)
    written = ["行 %03d 这是一条用于校验不丢行的日志" % i for i in range(600)]
    for line in written:
        lossless.write(line)
    settle(tmp, "lossless-*.log")          # 等后台压缩全部完成
    text = (tmp / "lossless.log").read_text(encoding="utf-8")
    text += "".join(read_archive(p) for p in sorted(tmp.glob("lossless-*.log.gz")))
    got = [ln for ln in text.splitlines() if ln.strip()]
    check("同一秒内连转 %d 次也不丢行" % lossless.rotations, sorted(got) == sorted(written),
          "写 %d 行，读完只剩 %d 行（轮转 %d 次）" % (len(written), len(got), lossless.rotations))
    check("确实转了多次（否则这条检查没意义）", lossless.rotations >= 5, str(lossless.rotations))

    # 4. keep 上限：再写爆 6 轮，归档数不应超过 keep
    for _ in range(6):
        for i in range(200):
            log.write("轮转压力测试 %s" % i)
        time.sleep(0.05)
    settle(tmp, "requests-*.log")
    files = sorted(tmp.glob("requests-*.log.gz"))
    check("归档份数不超过 keep=3", 1 <= len(files) <= 3, "实际 %d 份" % len(files))

    # 5. 外部截断（logrotate copytruncate / 手工清空）后能自愈
    (tmp / "requests.log").write_text("", encoding="utf-8")
    rot_before = log.rotations
    for i in range(10):
        log.write("截断后继续写 %d" % i)
    body = (tmp / "requests.log").read_text(encoding="utf-8")
    body += "".join(read_archive(p) for p in sorted(tmp.glob("requests-*.log.gz")))
    check("外部截断后继续正常写", all("截断后继续写 %d" % i in body for i in range(10)))
    check("外部截断不会被误判成「快满了」而空转轮转",
          log.rotations - rot_before <= 1, "多转了 %d 次" % (log.rotations - rot_before))

    # 6. info() 给运维看的东西齐全
    info = log.info()
    check("info() 字段齐全",
          {"path", "size", "maxBytes", "keep", "archives", "rotations"} <= set(info),
          str(info))

    # 7. 并发写不丢行（8 线程 × 100 行）
    log2 = applog.RotatingRequestLog(tmp / "concurrent.log", max_bytes=4096, keep=50)
    errors: list[str] = []

    def worker(tid: int) -> None:
        try:
            for i in range(100):
                log2.write("线程%d-%03d" % (tid, i))
        except Exception as exc:      # noqa: BLE001
            errors.append("%s: %s" % (tid, exc))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    settle(tmp, "concurrent-*.log")
    body = (tmp / "concurrent.log").read_text(encoding="utf-8")
    body += "".join(read_archive(p) for p in sorted(tmp.glob("concurrent-*.log.gz")))
    lines = [ln for ln in body.splitlines() if ln.strip()]
    check("并发写无异常", not errors, str(errors))
    check("并发写 800 行不丢不重", len(lines) == 800 and len(set(lines)) == 800, "实际 %d 行" % len(lines))


# ---------------------------------------------------------------- 令牌清理
def build_store(tmp: Path, tokens: dict, accounts: dict) -> core.AccountStore:
    (tmp / "accounts.json").write_text(json.dumps(accounts, ensure_ascii=False), encoding="utf-8")
    (tmp / "tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
    core.STATE_DIR = tmp
    return core.AccountStore()


def test_prune_tokens(tmp: Path) -> None:
    print("\n[令牌清理]")
    now = int(time.time())
    accounts = {
        "alice": {"userId": 10001, "nickname": "爱丽丝", "salt": "x", "hash": "y"},
        "bob": {"userId": 10002, "nickname": "鲍勃", "salt": "x", "hash": "y"},
    }

    def tok(user: str, age_sec: int) -> dict:
        return {"username": user, "userId": 1, "ts": now - age_sec, "lastSeen": now - age_sec}

    tokens = {}
    # alice：6 个新鲜令牌（只该留最新 3 个）
    for i in range(6):
        tokens["a%d" % i] = tok("alice", 600 - i)     # a5 最新，a0 最旧
    # alice：一个签发 40 天前的（过期）
    tokens["a_old"] = tok("alice", 40 * 86400)
    # bob：2 个，都该留
    tokens["b1"] = tok("bob", 100)
    tokens["b2"] = tok("bob", 50)
    # 账号已删（早期测试号）：必须清
    tokens["ghost"] = tok("zz_flush_probe", 60)
    # 结构坏掉的脏数据
    tokens["broken"] = "not-a-dict"

    store = build_store(tmp, tokens, accounts)
    total_before = len(store.tokens)

    removed = store.prune_tokens(max_per_user=3)
    print("  清理前 %d 条 → 删除 %d 条，剩余 %d 条（%s）"
          % (total_before, removed, len(store.tokens), ",".join(sorted(store.tokens))))
    check("删掉了 6 条垃圾（1 过期 + 1 删号 + 1 脏数据 + 3 超额旧令牌）", removed == 6,
          "实际 %d" % removed)
    check("alice 只留最新 3 个", sorted(t for t in store.tokens if t.startswith("a")) == ["a3", "a4", "a5"],
          str(sorted(store.tokens)))
    check("bob 两个都保留", {"b1", "b2"} <= set(store.tokens))
    check("删号账号的令牌被清掉", "ghost" not in store.tokens)
    check("脏数据被清掉", "broken" not in store.tokens)
    check("过期令牌被清掉", "a_old" not in store.tokens)

    disk = json.loads((tmp / "tokens.json").read_text(encoding="utf-8"))
    check("清理结果已落盘", len(disk) == len(store.tokens), "%d vs %d" % (len(disk), len(store.tokens)))

    # 语义检查：留下的能用，删掉的走「未登录」而不是 500
    row, reason = store.resolve_ex("a5")
    check("最新令牌仍可鉴权", row is not None and reason == "ok", reason)
    row, reason = store.resolve_ex("a0")
    check("被清理的旧令牌判 missing", row is None and reason == "missing", reason)

    # 幂等 + 不空写
    path = tmp / "tokens.json"
    before = path.stat().st_mtime_ns
    time.sleep(0.01)
    again = store.prune_tokens(max_per_user=3)
    check("没有可删的 → 返回 0", again == 0, str(again))
    check("没有可删的 → 不写盘", path.stat().st_mtime_ns == before)

    # TTL 边界：29 天留下、31 天清掉
    ttl_dir = tmp / "ttl"
    ttl_dir.mkdir(parents=True, exist_ok=True)
    tokens2 = {"keep": tok("bob", 29 * 86400), "drop": tok("bob", 31 * 86400)}
    store2 = build_store(ttl_dir, tokens2, accounts)
    store2.prune_tokens()
    check("29 天内保留 / 31 天清掉", set(store2.tokens) == {"keep"}, str(sorted(store2.tokens)))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lyhouse_"))
    try:
        log_dir = tmp / "logs"
        log_dir.mkdir()
        tok_dir = tmp / "tokens"
        tok_dir.mkdir()
        test_rotation(log_dir)
        test_prune_tokens(tok_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
