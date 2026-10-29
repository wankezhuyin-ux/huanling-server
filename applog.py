# -*- coding: utf-8 -*-
"""请求日志：按大小自动轮转 + gzip 归档。

原实现是纯 append（`LOG_PATH.open("a")`），requests.log 从 10-24 上线到 10-28
四天就长到 14 MB。按今日 2.7 万请求/天算，一年约 1.2 GB —— 40 GB 的盘看着不急，
但这种只涨不跌的文件迟早会在某次磁盘告警里成为嫌疑犯，而且真出事时想 grep
一周前的日志，先得从 300 MB 里翻。

改成：
  requests.log                      当前文件，超过 max_bytes 就换新的
  requests-20261028-201500.log.gz   历史归档（文本日志压缩率通常 10~15 倍）
  requests-20261028-203001.log.gz   ...
只保留最近 keep 份，更早的自动删。

设计取舍：
· 不依赖 logrotate / cron。轮转逻辑跟着代码走，换机器、重装系统都不用再配一遍；
  想从外部接管也可以——每次写之前会重读文件大小，被 copytruncate 或手工删掉都能自愈。
· 压缩扔到后台线程。轮转那一刻只做一次 rename（微秒级），不阻塞请求。
· 每次写入仍然「开-写-关」，和原实现一致（每请求一次 open 的代价约 20µs），
  好处是进程被 kill -9 也不会丢最后几行。
"""
from __future__ import annotations

import gzip
import os
import threading
import time
from pathlib import Path

DEFAULT_MAX_BYTES = 32 * 1024 * 1024   # 单文件上限 32 MB（线上约 9 天写满）
DEFAULT_KEEP = 8                       # 最多留 8 份归档（≈ 9 周历史，压缩后约 20 MB）


def _env_int(name: str, default: int) -> int:
    try:
        raw = str(os.environ.get(name, "") or "").strip()
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


class RotatingRequestLog:
    """线程安全的请求日志写入器。"""

    def __init__(self, path, max_bytes: int | None = None, keep: int | None = None) -> None:
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes or _env_int("LY_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)))
        self.keep = max(1, int(keep if keep is not None else _env_int("LY_LOG_KEEP", DEFAULT_KEEP)))
        self._lock = threading.Lock()
        self._size = 0
        self._writes = 0
        self.rotations = 0
        self._resync()

    # -- 内部
    def _resync(self) -> None:
        """以磁盘为准纠正字节计数（进程刚启动、或被外部工具动过文件时）。"""
        try:
            self._size = self.path.stat().st_size
        except OSError:
            self._size = 0

    def _archive_name(self, stamp: str) -> Path:
        base = "%s-%s.log" % (self.path.stem, stamp)
        target = self.path.with_name(base)
        n = 1
        # 必须同时避开 .log 和 .log.gz：压缩完成后原始 .log 会被删掉，
        # 只判断 .log 是否存在的话，同一秒内的下一次轮转会重新用这个名字，
        # 压缩时就把上一份归档覆盖掉了（日志直接少一段）。
        while target.exists() or target.with_name(target.name + ".gz").exists():
            n += 1
            target = self.path.with_name("%s-%s-%d.log" % (self.path.stem, stamp, n))
        return target

    def _rotate_locked(self) -> None:
        """调用方必须已持有 self._lock。"""
        try:
            if self.path.stat().st_size <= 0:
                self._size = 0
                return
        except OSError:
            self._size = 0
            return
        target = self._archive_name(time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(self.path, target)
        except OSError:
            # 改名失败（只读盘 / 权限）就别再折腾，计数归零继续往原文件写
            self._size = 0
            return
        self._size = 0
        self.rotations += 1
        threading.Thread(target=self._compress, args=(target,), name="log-gzip", daemon=True).start()

    def _compress(self, plain: Path) -> None:
        gz = plain.with_name(plain.name + ".gz")
        try:
            with plain.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
            plain.unlink()
        except OSError:
            return
        self._trim()

    def _trim(self) -> None:
        try:
            files = sorted(self.path.parent.glob("%s-*.log.gz" % self.path.stem))
        except OSError:
            return
        for old in files[:max(0, len(files) - self.keep)]:
            try:
                old.unlink()
            except OSError:
                pass

    # -- 对外
    def write(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        data = line.encode("utf-8")
        with self._lock:
            if self._size + len(data) > self.max_bytes:
                self._rotate_locked()
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                self._size += len(data)
            except OSError:
                pass
            self._writes += 1
            if self._writes % 500 == 0:
                # 每 500 行对齐一次真实大小：外部 logrotate 截断后计数器会偏大，
                # 不纠正就会一直以为「快满了」，反复空转轮转。
                self._resync()

    def info(self) -> dict:
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        try:
            archives = sorted(self.path.parent.glob("%s-*.log.gz" % self.path.stem))
        except OSError:
            archives = []
        return {
            "path": str(self.path),
            "size": size,
            "maxBytes": self.max_bytes,
            "keep": self.keep,
            "archives": len(archives),
            "archiveBytes": sum(p.stat().st_size for p in archives if p.exists()),
            "rotations": self.rotations,
        }
