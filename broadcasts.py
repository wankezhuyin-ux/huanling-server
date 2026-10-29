#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全服广播（江湖传闻 / 仙魔界战报）的真实存储。

背景：
  旧实现 GET /xianmo/broadcasts 直接把 `data/xianmo-broadcasts.json` ——
  也就是**原游戏线上抓包**的 15 条记录 —— 原样吐给客户端，而且完全忽略
  lastId / limit。客户端每次进主城都会拉一次历史（limit=15），于是那些
  「恭喜【搬砖的】得道飞升成仙」「【立羽】投胎转世」的原游戏老消息
  每次都被当成新公告弹一遍。

现在的口径（对齐原游戏）：
  - 公屏 = 玩家事件流 + 玩家聊天，没有预置内容，也不做「每日活动播报」。
  - 写入只走 POST /xianmo/broadcast（客户端 broadcast_event → send_server_broadcast），
    服务端按 token 取存档里的名字，客户端上报的名字只做兜底。
  - id 单调递增且**跨重启不回退**：初值取当前毫秒时间戳，保证远大于旧抓包
    里的 91xxxx 段，客户端存档里遗留的水位（last_broadcast_id）不会把新广播全过滤掉。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from core import STATE_DIR, dump_json, iso, load_json, now

BROADCAST_PATH: Path = STATE_DIR / "broadcasts.json"
BROADCAST_MAX_ROWS = 500          # 最多留 500 条，超了丢最旧的
BROADCAST_MAX_CHARS = 300         # 单条字数上限（飞升/投胎带整段诗词，给宽一点）
# 旧抓包数据（id 91xxxx）一律视为脏数据，读取时直接丢
BROADCAST_MIN_VALID_ID = 10 ** 12  # 1e12 ≈ 2001 年的毫秒时间戳

_LOCK = threading.RLock()
_DB: dict | None = None


def _normalize(raw) -> dict:
    rows = raw.get("rows") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        rows = []
    clean: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            rid = int(r.get("id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if rid < BROADCAST_MIN_VALID_ID:
            continue          # 原游戏抓包遗留，丢掉
        clean.append(r)
    clean.sort(key=lambda r: int(r.get("id", 0) or 0))
    if len(clean) > BROADCAST_MAX_ROWS:
        clean = clean[-BROADCAST_MAX_ROWS:]
    last_id = int(clean[-1].get("id", 0) or 0) if clean else 0
    seed = max(last_id + 1, int(time.time() * 1000))
    return {"lastId": seed, "rows": clean}


def _load() -> dict:
    global _DB
    if _DB is None:
        raw = load_json(BROADCAST_PATH, {})
        _DB = _normalize(raw)
    return _DB


def _save(db: dict) -> None:
    try:
        dump_json(BROADCAST_PATH, db)
    except OSError:
        pass


def _next_id(db: dict) -> int:
    rid = max(int(db.get("lastId", 0) or 0), int(time.time() * 1000))
    db["lastId"] = rid + 1
    return rid


def append(broadcast_type: str, nickname: str, content: str,
           extra: dict | None = None) -> dict | None:
    """写一条全服广播，返回落库后的行（含服务端授予的 id）。"""
    text = str(content or "").strip()[:BROADCAST_MAX_CHARS]
    if not text:
        return None
    nick = str(nickname or "").strip()[:16] or "系统"
    with _LOCK:
        db = _load()
        rid = _next_id(db)
        row = {
            "id": rid,
            "broadcast_type": str(broadcast_type or "chat")[:32],
            "nickname": nick,
            "content": text,
            "extra_data": extra if isinstance(extra, dict) else {},
            "created_at": iso(now()),
        }
        db["rows"].append(row)
        if len(db["rows"]) > BROADCAST_MAX_ROWS:
            db["rows"] = db["rows"][-BROADCAST_MAX_ROWS:]
        _save(db)
    return row


def latest(limit: int = 20) -> list[dict]:
    """最新的 limit 条，按 id 升序（客户端拿到后自己排最旧→最新）。"""
    cap = max(1, min(int(limit or 20), 100))
    with _LOCK:
        rows = list(_load()["rows"])
    return [dict(r) for r in rows[-cap:]]


def since(last_id: int, limit: int = 50) -> list[dict]:
    """lastId 之后的新广播，按 id 升序。"""
    try:
        floor = int(last_id or 0)
    except (TypeError, ValueError):
        floor = 0
    cap = max(1, min(int(limit or 50), 100))
    with _LOCK:
        rows = [r for r in _load()["rows"] if int(r.get("id", 0) or 0) > floor]
    rows.sort(key=lambda r: int(r.get("id", 0) or 0))
    return [dict(r) for r in rows[:cap]]


def purge_legacy() -> int:
    """清掉所有 id < 1e12 的历史脏数据（原游戏抓包），返回删掉多少条。"""
    with _LOCK:
        db = _load()
        before = len(db["rows"])
        if db["rows"]:
            db["rows"] = [r for r in db["rows"]
                          if int(r.get("id", 0) or 0) >= BROADCAST_MIN_VALID_ID]
            if len(db["rows"]) != before:
                db["lastId"] = max(int(db.get("lastId", 0) or 0),
                                   int(time.time() * 1000))
                _save(db)
        return before - len(db["rows"])
