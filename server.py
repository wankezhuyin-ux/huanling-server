#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地游戏服务端入口。

    python3 server.py                # 监听 3000（API）与 3001（聊天）
    python3 server.py --port 3000

客户端 `scripts/game_server_config.gd` 的 API 地址是 `http://<host>:3000/api`，
所以这里会把 `/api` 前缀剥掉再匹配路由，客户端一条代码都不用改。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import handlers_account  # noqa: F401  注册路由
import handlers_arena  # noqa: F401
import handlers_battle  # noqa: F401
import handlers_gang  # noqa: F401
import handlers_growth  # noqa: F401
import handlers_mail  # noqa: F401  覆盖 handlers_account 里的旧邮箱接口
import handlers_fabao  # noqa: F401  法宝：洗练/重置走 409 两阶段确认
import handlers_extra  # noqa: F401
import handlers_play  # noqa: F401
# 必须排在 handlers_play 之后：世界 Boss 战斗结算、养宠赛排行、生死塔房间
# 会覆盖 handlers_play 里的占位实现（room/list 原来固定返回空数组）
import handlers_boss_tower  # noqa: F401
# 必须再排在 handlers_boss_tower 之后：生死塔的赛事编排（报名→轮次→回放→塔主）
# 与仙魔镜像要覆盖前者 handlers_extra 里固定返回空的 round/matchup、floor、match
import handlers_shengsi_event  # noqa: F401
import gm  # noqa: F401  GM 后台（/gm 页面 + /gm/api）
import hotfix
from applog import RotatingRequestLog
from core import ROUTES, STORE, Ctx, dump_json, iso, load_json, match_dynamic, now

ROOT = Path(__file__).resolve().parent
# 请求日志按大小轮转（详见 applog.py 模块注释：只涨不跌的 requests.log 4 天 14 MB）
LOG = RotatingRequestLog(ROOT / "state" / "requests.log")
PUBLIC_PATHS = {"/register", "/login", "/version/check", "/updates/manifest.json",
                # 客户端 chat_network 轮询公告时不带 Authorization，原服该接口也是公开的
                "/announcements",
                # 客户端排行榜请求（network.gd get_ranking / get_ranking_players /
                # get_ranking_pets_*）只发了 Content-Type，没带令牌；榜单本身也是全局数据。
                # 不放行会 401，而 401 会被主界面的"在线检测"判成断网 → 弹「正在自动重连」。
                "/ranking", "/ranking/players",
                "/ranking/pets/evolution", "/ranking/pets/non-evolution",
                # 探活类接口：main_ui._main_ui_periodic_online_check() 每 45 秒打一次，
                # connectivity_fallback_url() 指向 /api/ranking，这些都必须免鉴权。
                "/health", "/version", "/ping",
                # 仙魔战场的只读接口：客户端 network.gd 里 xianmo_get_faction_stats /
                # xianmo_get_ranking / xianmo_get_week_info / xianmo_get_shop_items
                # 只发了 Content-Type，**没带 Authorization**。不放行就一律 401，
                # 而刷新的阵营人数/积分永远拿不到 → 界面一直显示「仙道 0人 0分」。
                # 这四条都不写玩家存档，可以不鉴权。
                "/xianmo/faction/stats", "/xianmo/week/info",
                "/xianmo/ranking", "/xianmo/shop/items",
                # GM 后台自带独立密码校验，不走玩家令牌
                "/gm", "/gm/", "/gm/api"}


def normalize(path: str) -> str:
    path = path or "/"
    if path.startswith("/api/api/"):
        return path[4:]
    if path.startswith("/api/"):
        return path[4:]
    if path == "/api":
        return "/"
    return path


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalGameServer/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础
    def handle(self) -> None:
        """吞掉「客户端已断开」类异常。

        手机切网/退后台时，keep-alive 连接会被 RST 掉，socketserver 随即把一整段
        ConnectionResetError 堆栈打进 server.log（每天几十条，纯噪音，还会把真正的
        报错埋掉）。这类异常对业务没有任何意义，直接不管。
        """
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        return

    def _send(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Client-Version")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _log(self, method: str, path: str, status: int, extra: str = "") -> None:
        line = "%s %s %s %s %s %s" % (iso(now()), method, path, status, self.headers.get("X-Client-Version", "-"), extra)
        try:
            LOG.write(line)
        except Exception:       # 日志永远不该把请求搞挂
            pass

    # -- 分发
    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = normalize(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                parsed_body = json.loads(raw.decode("utf-8"))
                body = parsed_body if isinstance(parsed_body, dict) else {"_payload": parsed_body}
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {}

        if path in ("/health", "/ping"):
            # 探活：客户端的在线检测会打这里，必须免鉴权且永远 200
            self._send({"success": True, "status": "ok", "serverTime": iso(now()),
                        "online": STORE.online_count()})
            self._log(method, path, 200, "health")
            return

        if path == "/version":
            # 私人服不做强制更新：把客户端上报的版本原样回敬，避免弹「必须更新」
            ver = str(query.get("version") or "2.5.9")
            self._send({"success": True, "version": ver, "latestVersion": ver,
                        "currentVersion": ver, "forceUpdate": False, "downloadUrl": ""})
            self._log(method, path, 200, "version")
            return

        if path in ("/gm", "/gm/", "/gm/index.html"):
            self._send_html(gm.GM_HTML)
            self._log(method, path, 200)
            return

        if path == "/updates/manifest.json":
            # 热更清单：patch_version 走热更，version 保持不高于客户端（否则弹「必须更新」）
            self._send(hotfix.build_manifest(self.headers))
            self._log(method, path, 200, "manifest")
            return

        if path.startswith("/updates/"):
            # 补丁本体（PCK），流式下发，支持 Range
            if hotfix.serve_file(self, path[len("/updates/"):]):
                self._log(method, path, 200, "patch")
                return
            self._send({"success": False, "message": "补丁不存在", "error": "patch-not-found"}, 404)
            self._log(method, path, 404, "patch-missing")
            return

        auth = self.headers.get("Authorization", "")
        token = auth.split("Bearer ")[-1].strip() if "Bearer " in auth else ""
        user, auth_reason = STORE.resolve_ex(token)
        if user is None and path not in PUBLIC_PATHS:
            if auth_reason == "idle":
                # 退后台/锁屏太久：客户端认 "Account logged in elsewhere" 这个 error，
                # 会弹「账号已下线 → 登录已失效，请重新登录」并回登录页。
                self._send({
                    "success": False,
                    "message": "长时间未操作，已自动下线，请重新登录",
                    "error": "Account logged in elsewhere",
                    "code": "SESSION_IDLE_TIMEOUT",
                }, 401)
                self._log(method, path, 401, "idle-timeout")
                return
            self._send({"success": False, "message": "未登录", "error": "unauthorized"}, 401)
            self._log(method, path, 401, "no-token")
            return
        if user is not None:
            # 已鉴权请求 = 玩家在线，刷新会话空闲计时
            STORE.touch_token(token)

        handler = ROUTES.get((method, path))
        if handler is None:
            # 同一路径可能只注册了另一种方法，优先用已注册的方法兜底
            for (reg_method, reg_path), fn in ROUTES.items():
                if reg_path == path:
                    handler = fn
                    break
        if handler is None:
            # 带路径参数的路由（/mail/detail/123 之类）
            handler, path_params = match_dynamic(method, path)
            if handler is not None:
                query.update(path_params)
        if handler is None:
            self._send({"success": False, "message": "本地服务端未实现该接口：%s" % path, "unimplemented": True}, 404)
            self._log(method, path, 404, "unimplemented")
            return

        # 每个账号一份独立存档；公开接口（登录/注册/版本检查）不落到任何玩家档
        state = STORE.get(user["username"]) if user else None
        ctx = Ctx(state, body, query, user, dict(self.headers))
        try:
            if state is None:
                result = handler(ctx)
            else:
                with state.lock:
                    # 记下当前接口，落盘兜底告警里能直接看出是哪条路由改了数据没 bump
                    state._req_path = path
                    result = handler(ctx)
                    # 已鉴权请求 = 玩家在线，刷新最后在线时间；离线结算本身除外，
                    # 否则它会把自己的离线时长算成 0。
                    if path != "/economy/offline-claim":
                        state.touch_seen()
                    # flush 内部会先比对内容：纯读请求（占今日 78%）不会产生写盘
                    state.flush()
        except Exception as exc:  # 任何 handler 异常都转成失败响应，别让客户端卡死
            traceback.print_exc()
            result = {"success": False, "message": "本地服务端内部错误：%s" % exc, "error": str(exc)}
            self._log(method, path, 500, str(exc)[:80])
            self._send(result, 500)
            return
        # 客户端 network.gd 只在 HTTP 状态码非 200 时才读 error/message 显示给玩家；
        # 登录/注册若用 200 + success=false 返回，会被并成「登录响应格式错误」，
        # 真实原因（账号不存在 / 密码错误 / 账号已存在）全被吞掉。
        # 这两个接口单独走 400，文案才能到玩家眼前。
        status = 200
        if isinstance(result, dict) and "_status" in result:
            # handler 自己指定状态码（法宝洗练/重置的 409 + needsConfirm 两阶段协议）
            try:
                status = int(result.pop("_status"))
            except (TypeError, ValueError):
                status = 200
        elif isinstance(result, dict) and result.get("success") is False and path in ("/login", "/register"):
            status = 400
        self._send(result, status)
        # 有些接口按客户端要求直接回 JSON 数组（/announcements、/ranking 的榜单…），
        # 这里原来无脑 result.get() → AttributeError，异常发生在写响应之后，
        # 请求看起来「成功」但服务端日志刷满堆栈，排查时容易误判成业务出错。
        if isinstance(result, dict):
            note = "ok" if result.get("success") else str(result.get("message", "fail"))[:60]
        elif isinstance(result, list):
            note = "ok (list %d)" % len(result)
        else:
            note = "ok (%s)" % type(result).__name__
        self._log(method, path, status, note)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send({"success": True})


# ---------------------------------------------------------------- 聊天端口（3001）
# 老实现是个桩：不管收到什么，都回 {"success": true, "messages": []}。
# 于是玩家点「发送」看起来完全没反应 —— 服务端一条都不存，5 秒一次的轮询自然
# 永远拉不到自己的消息，而 HTTP 200 又让客户端以为发成功了（连报错都不弹）。
# 这里按 scripts/chat_network.gd 的协议补成真的多频道消息队列：
#
#   POST /api/messages   Authorization: Bearer <token>
#        {player_name, message, event_type, is_vip, title, vip_level, channel, gangId}
#   GET  /api/messages?lastId=&limit=&channel=&gangId=
#        返回**顶层 JSON 数组**（客户端 `if not data is Array: return`，
#        包一层 {success:true,messages:[]} 会被整条丢弃）
CHAT_STATE_PATH = ROOT / "state" / "chat.json"
CHAT_LOCK = threading.Lock()
CHAT_MAX_ROWS = 800          # 最多留 800 条，超了丢最旧的
CHAT_MAX_CHARS = 160         # 单条字数上限
CHAT_MIN_GAP = 1.5           # 同一账号连发最短间隔（秒），防连点刷屏


def _chat_load() -> dict:
    raw = load_json(CHAT_STATE_PATH, {}) or {}
    rows = raw.get("messages")
    if not isinstance(rows, list):
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    try:
        next_id = int(raw.get("nextId") or 0)
    except (TypeError, ValueError):
        next_id = 0
    if next_id <= 0:
        next_id = 1 + max([int(r.get("id", 0) or 0) for r in rows] or [0])
    return {"nextId": next_id, "messages": rows}


def _chat_save(db: dict) -> None:
    try:
        dump_json(CHAT_STATE_PATH, db)
    except OSError:
        pass


CHAT_DB = _chat_load()


class ChatHandler(BaseHTTPRequestHandler):
    """3001 聊天端口：世界/帮派频道的消息读写。"""

    protocol_version = "HTTP/1.1"
    server_version = "LocalChatServer/2.0"

    def handle(self) -> None:
        # 同 Handler：客户端断开不该在 server.log 里留堆栈
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Client-Version")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> str:
        return (urlparse(self.path).path or "/").rstrip("/") or "/"

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _auth(self) -> tuple[str, str]:
        """返回 (用户名, token)。未登录返回空用户名。"""
        auth = self.headers.get("Authorization", "") or ""
        token = auth.split("Bearer ")[-1].strip() if "Bearer " in auth else ""
        row, _reason = STORE.resolve_ex(token)
        return (str(row.get("username", "")), token) if row else ("", token)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json({"success": True})

    def do_GET(self) -> None:  # noqa: N802
        path = self._route()
        query = self._query()
        if path in ("/api/online", "/online"):
            self._send_json({"success": True, "online": STORE.online_count()})
            return
        if path not in ("/api/messages", "/messages"):
            self._send_json({"success": True, "messages": [], "online": STORE.online_count()})
            return

        def _int(key: str, default: int) -> int:
            try:
                return int(query.get(key) or default)
            except (TypeError, ValueError):
                return default

        last_id = _int("lastId", 0)
        limit = max(1, min(_int("limit", 50), 100))
        channel = (query.get("channel") or "world").strip() or "world"
        gang_id = _int("gangId", 0)
        with CHAT_LOCK:
            rows = [
                dict(r) for r in CHAT_DB["messages"]
                if int(r.get("id", 0) or 0) > last_id
                and str(r.get("channel_type", "world")) == channel
                and (channel != "gang" or int(r.get("gang_id", 0) or 0) == gang_id)
            ]
        rows.sort(key=lambda r: int(r.get("id", 0) or 0))
        self._send_json(rows[:limit])

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        path = self._route()
        if path not in ("/api/messages", "/messages"):
            self._send_json({"success": True})
            return
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        username, token = self._auth()
        if not username:
            # 客户端认 401/403 → 「登录状态失效，请重新登录」，别静默丢弃
            self._send_json({"success": False, "message": "未登录", "error": "unauthorized"}, 401)
            return
        STORE.touch_token(token)

        text = str(payload.get("message", "") or "").strip()
        if not text:
            self._send_json({"success": False, "message": "消息不能为空", "error": "empty"}, 400)
            return
        text = text[:CHAT_MAX_CHARS]
        channel = str(payload.get("channel", "world") or "world").strip()
        if channel not in ("world", "gang"):
            channel = "world"
        try:
            gang_id = int(payload.get("gangId", 0) or 0)
        except (TypeError, ValueError):
            gang_id = 0
        if channel == "gang" and gang_id <= 0:
            self._send_json({"success": False, "message": "帮派频道缺少帮派", "error": "no-gang"}, 400)
            return
        try:
            vip_level = max(0, min(int(payload.get("vip_level", 0) or 0), 9))
        except (TypeError, ValueError):
            vip_level = 0

        # 名字以服务端存档为准，客户端上报的只作兜底 —— 否则谁都能顶着别人的名字说话
        name = ""
        try:
            st = STORE.get(username)
            # State.nickname 是个普通方法（没挂 @property），取存档字段更直接
            name = str(st.meta.get("nickname") or "").strip()
            if not name:
                name = str((st.player or {}).get("name") or "").strip()
        except Exception:
            name = ""
        if not name:
            name = str(payload.get("player_name", "") or "").strip()[:16] or "修仙者"

        now_ts = int(time.time())
        with CHAT_LOCK:
            for row in reversed(CHAT_DB["messages"][-40:]):
                if row.get("username") == username and now_ts - int(row.get("ts", 0) or 0) < CHAT_MIN_GAP:
                    self._send_json({"success": False, "message": "说话太快了，缓一缓", "error": "too-fast"}, 429)
                    return
            mid = int(CHAT_DB["nextId"])
            CHAT_DB["nextId"] = mid + 1
            row = {
                "id": mid,
                "username": username,
                "player_name": name,
                "sender": name,
                "message": text,
                "event_type": str(payload.get("event_type", "chat") or "chat"),
                "is_vip": bool(payload.get("is_vip", False)),
                "vip_level": vip_level,
                "title": str(payload.get("title", "") or "")[:32],
                "channel_type": channel,
                "gang_id": gang_id,
                "ts": now_ts,
                "time": iso(now()),
            }
            CHAT_DB["messages"].append(row)
            if len(CHAT_DB["messages"]) > CHAT_MAX_ROWS:
                CHAT_DB["messages"] = CHAT_DB["messages"][-CHAT_MAX_ROWS:]
            _chat_save(CHAT_DB)
        self._send_json({"success": True, "id": mid, "message": row})


def coverage_report() -> dict:
    contract_path = ROOT / "contract.json"
    if not contract_path.exists():
        return {}
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    endpoints = contract.get("endpoints", {})
    implemented, missing = [], []
    for path, info in endpoints.items():
        methods = info.get("methods") or ["GET"]
        if any((m, path) in ROUTES for m in methods):
            implemented.append(path)
        else:
            missing.append(path)
    report = {
        "generated": iso(now()),
        "total": len(endpoints),
        "implemented": len(implemented),
        "missing": len(missing),
        "coverage": round(len(implemented) / max(1, len(endpoints)) * 100, 1),
        "missing_paths": sorted(missing),
    }
    dump_json(ROOT / "state" / "coverage.json", report)
    return report


def start_housekeeping(interval: float = 1800.0) -> None:
    """后台例行清理：过期/多余的登录令牌（启动时先跑一次，之后每半小时）。"""

    def once() -> None:
        try:
            removed = STORE.prune_tokens()
            if removed:
                # flush=True：stdout 重定向到文件时是块缓冲，不刷的话这行要等 4KB
                # 攒满才出现在 server.log 里（重启后想确认清理跑没跑会看不到）
                print("[housekeeping] 清理令牌 %d 条，剩余 %d 条" % (removed, len(STORE.tokens)), flush=True)
        except Exception:
            traceback.print_exc()

    def loop() -> None:
        while True:
            time.sleep(max(60.0, float(interval)))
            once()

    once()
    threading.Thread(target=loop, name="housekeeping", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--chat-port", type=int, default=3001)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    # stdout 重定向到文件时默认是块缓冲（攒满 4~8KB 才落盘），于是重启后
    # server.log 里常常一片空白、启动横幅要等半天才出现，排障时看不到东西。
    # 改成行缓冲：print 一行就进文件。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass

    report = coverage_report()
    if report:
        print("接口覆盖：%d/%d（%.1f%%），未实现 %d 个" % (
            report["implemented"], report["total"], report["coverage"], report["missing"]))

    api = ThreadingHTTPServer((args.host, args.port), Handler)
    chat = ThreadingHTTPServer((args.host, args.chat_port), ChatHandler)
    threading.Thread(target=chat.serve_forever, daemon=True).start()

    # 藏宝洞整点轮次调度（开轮/结算/公屏战报）
    try:
        import treasure_round
        treasure_round.start_scheduler()
    except Exception:
        traceback.print_exc()

    # 例行清理：令牌表只增不减，不收拾的话会一直长（详见 AccountStore.prune_tokens）
    try:
        start_housekeeping()
    except Exception:
        traceback.print_exc()

    print("本地服务端已启动：http://%s:%d/api （聊天 %d）" % (args.host, args.port, args.chat_port))
    print("存档：%s" % (ROOT / "state" / "state.json"))
    try:
        api.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        # 旧代码这里写的是 STATE.flush()，STATE 早就拆成每账号一份了，Ctrl+C 会直接 NameError
        for st in STORE.all_states():
            try:
                # 停机时强制落一次：flush 平时会按「内容没变」跳过，
                # 但 lastSeen 这类易变字段只有在 force 时才会连带写下去
                st.flush(force=True)
            except OSError:
                pass


if __name__ == "__main__":
    main()
