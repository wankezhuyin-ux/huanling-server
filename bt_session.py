#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兄弟天关会话状态机（本地单机模拟）。

设计目标：把原来"永远第 1 波、finished 写死"的静态桩，改成真正按配置
`brotherTower.waveCount` 逐波推进的会话；并集中处理每日限流与按 giftDesc
权重表的真实掉落，供 handlers_play.py / handlers_extra.py 共用。

会话结构（存在 state.meta["brotherTower"]["session"]）：
  {
    "sessionId": int, "battleSeed": int, "status": in_battle|cleared|failed|forfeit,
    "difficulty": str, "currentWave": int, "waveCount": int,
    "members": [...], "driverUserId": int, "role": "driver",
    "startedAt": iso, "events": [...], "nextSeq": int,
    "lastClearedWave": int, "result": {...}  # cleared 时写入结算快照
  }

注意：客户端是完整的多人实时协议（driver 上报事件、viewer 拉取回放）。
本地只有一队，因此这里由服务端在 wave-cleared 时自动生成该波的事件流，
viewer/driver 都能从 GET battle-events 拉到，语义与线上一致。
"""
from __future__ import annotations

import random
import re
import threading

from core import RULES, STATE_DIR, dump_json, iso, load_json, now

# 房间是跨账号共享的（A 建房，B 才能申请/看到），和生死塔一样单独落盘，
# 不能塞进各自的玩家存档。会话/session、joined、pinned、令牌消费记录仍是玩家私有。
_ROOM_LOCK = threading.RLock()
_ROOM_PATH = STATE_DIR / "brother-tower-rooms.json"
_ROOMS: dict | None = None


def _rooms() -> dict:
    global _ROOMS
    with _ROOM_LOCK:
        if _ROOMS is None:
            raw = load_json(_ROOM_PATH, {"seq": 1000, "rooms": {}})
            if not isinstance(raw, dict):
                raw = {"seq": 1000, "rooms": {}}
            raw.setdefault("seq", 1000)
            if not isinstance(raw.get("rooms"), dict):
                raw["rooms"] = {}
            _ROOMS = raw
        return _ROOMS


def _flush_rooms() -> None:
    with _ROOM_LOCK:
        dump_json(_ROOM_PATH, _rooms())


def box(state) -> dict:
    return state.meta.setdefault("brotherTower", {})


def cfg() -> dict:
    return RULES.m("brother_tower")


def wave_count() -> int:
    return max(1, int(RULES.v("brother_tower", "waveCount", 10) or 10))


def monsters_per_wave() -> int:
    return max(1, int(RULES.v("brother_tower", "monstersPerWave", 8) or 8))


def difficulty_cfg(difficulty: str) -> dict:
    for row in cfg().get("difficulties", []) or []:
        if isinstance(row, dict) and str(row.get("id", "")) == difficulty:
            return row
    return {}


# ---------------------------------------------------------------- 会话

def new_session(state, difficulty: str = "easy", snapshot: dict | None = None) -> dict:
    """开战：生成带 battleSeed/members 的会话，写入 meta 并返回。"""
    b = box(state)
    total = wave_count()
    members = []
    snap = snapshot if isinstance(snapshot, dict) else b.get("pinned") or {}
    # 单机：把自己作为唯一成员，带上 pin-snapshot 的角色信息供客户端构建出战单位
    member = {
        "userId": int(snap.get("userId", 0) or 0),
        "name": str(snap.get("playerName", "") or state.nickname()),
        "level": int(snap.get("playerLevel", 0) or 0),
        "realm": str(snap.get("playerRealm", "") or ""),
        "reincarnation": int(snap.get("playerReincarnation", 0) or 0),
        "petName": str(snap.get("petName", "") or ""),
        "petLevel": int(snap.get("petLevel", 0) or 0),
        "petId": str(snap.get("petId", "") or ""),
        "role": "driver",
    }
    members.append(member)
    sid = int(now().timestamp() * 1000) % 1_000_000_000
    sess = {
        "sessionId": sid,
        "battleSeed": random.randint(1, 2_000_000_000),
        "status": "in_battle",
        "difficulty": difficulty or "easy",
        "currentWave": 1,
        "waveCount": total,
        "monstersPerWave": monsters_per_wave(),
        "members": members,
        "driverUserId": member["userId"],
        "role": "driver",
        "startedAt": iso(now()),
        "events": [],
        "nextSeq": 1,
        "lastClearedWave": 0,
        "result": {},
        # 首波事件立即生成，viewer 一进来就能拉到 wave_start
        "_pendingWave": 1,
    }
    _emit_wave_start(sess, 1)
    b["session"] = sess
    return sess


def current(state) -> dict:
    return box(state).get("session") or {}


def _emit_wave_start(sess: dict, wave: int) -> None:
    seq = int(sess.get("nextSeq", 1))
    sess.setdefault("events", []).append({
        "seq": seq, "eventType": "wave_start",
        "payload": {"wave": wave, "monsters": int(sess.get("monstersPerWave", 8))},
    })
    sess["nextSeq"] = seq + 1


def _emit_wave_end(sess: dict, wave: int, total: int) -> None:
    seq = int(sess.get("nextSeq", 1))
    events = sess.setdefault("events", [])
    events.append({
        "seq": seq, "eventType": "battle_end",
        "payload": {"wave": wave, "victory": True},
    })
    sess["nextSeq"] = seq + 1
    if wave >= total:
        events.append({
            "seq": sess["nextSeq"], "eventType": "all_cleared",
            "payload": {"wave": wave, "totalWaves": total},
        })
        sess["nextSeq"] += 1


def clear_wave(state, wave: int | None = None) -> dict:
    """清完一波：推进 currentWave，最后一波把 status 置 cleared。"""
    sess = current(state)
    if not sess or sess.get("status") != "in_battle":
        return sess
    total = int(sess.get("waveCount", wave_count()))
    w = int(wave or sess.get("currentWave", 1))
    w = max(1, min(w, total))
    sess["lastClearedWave"] = w
    _emit_wave_end(sess, w, total)
    if w >= total:
        sess["status"] = "cleared"
        sess["currentWave"] = total
    else:
        nxt = w + 1
        sess["currentWave"] = nxt
        _emit_wave_start(sess, nxt)
    box(state)["session"] = sess
    return sess


def events_since(state, since_seq: int) -> tuple[list, int, bool]:
    """按 sinceSeq 增量返回事件；finished 仅当会话已 cleared/failed/forfeit。"""
    sess = current(state)
    if not sess:
        return [], 0, False
    evs = [e for e in sess.get("events", []) if int(e.get("seq", 0)) > since_seq]
    finished = sess.get("status") in ("cleared", "failed", "forfeit")
    return evs, int(sess.get("nextSeq", 1)), finished


def session_view(state) -> dict:
    """给 GET /session 的视图，剔除内部字段。"""
    sess = current(state)
    if not sess:
        return {}
    return {k: v for k, v in sess.items() if not k.startswith("_")}


def settle_view(state) -> dict:
    return dict(current(state).get("result", {}))


def mark_result(state, result: dict) -> None:
    sess = current(state)
    if sess:
        sess["result"] = dict(result)
        box(state)["session"] = sess


def invalidate(state) -> None:
    box(state)["session"] = {}


# ---------------------------------------------------------------- 房间 / 申请 / 令牌
# 说明：本地是单机模拟，没有真正的第二个玩家；但客户端按完整多人协议发请求，
# 所以这里维护一套"语义完整"的房间状态机：队长建房、成员申请（review）/直接入队
# （auto）、队长审批、ready、离开/解散、令牌门槛。多人同步暂不做（README 设计一致）。

def token_name() -> str:
    return str(cfg().get("tokenItemName", "兄弟天关令牌") or "兄弟天关令牌")


def max_members() -> int:
    return max(2, int(cfg().get("maxMembers", 2) or 2))


def member_of(ctx, snapshot: dict | None = None) -> dict:
    """构造当前玩家在房间里的成员条目（userId 与客户端 network.user_id 对齐）。"""
    state = ctx.state
    uid = int(ctx.user.get("userId", 0) or 0)
    p = state.player
    snap = snapshot if isinstance(snapshot, dict) else {}
    return {
        "userId": uid,
        "name": str(snap.get("playerName") or state.nickname()),
        "nickname": str(snap.get("playerName") or state.nickname()),
        "level": int(snap.get("playerLevel", 0) or p.get("level", 1) or 0),
        "reincarnation": int(snap.get("playerReincarnation", 0) or p.get("reincarnation", 0) or 0),
        "isLeader": False,
        "isReady": False,
        "petName": str(snap.get("petName", "") or ""),
        "petId": str(snap.get("petId", "") or ""),
    }


def all_rooms() -> list[dict]:
    """全局房间列表（跨账号）。"""
    with _ROOM_LOCK:
        return list(_rooms().get("rooms", {}).values())


def my_room(state, uid: int = 0) -> dict | None:
    """按 joined 指针或成员 userId 找当前玩家所在房间（全局）。"""
    joined = int(box(state).get("joined", 0) or 0)
    rooms = all_rooms()
    if joined:
        for r in rooms:
            if int(r.get("roomId", 0) or r.get("id", 0)) == joined:
                return r
    if uid:
        for r in rooms:
            for m in r.get("members", []) or []:
                if int(m.get("userId", 0)) == uid:
                    return r
    return None


def find_room(state, room_id: int) -> dict | None:
    with _ROOM_LOCK:
        return _rooms().get("rooms", {}).get(str(int(room_id or 0)))


def my_room_id(state, uid: int = 0) -> int:
    """我所在房间 id：优先按 userId 在全局房间查（跨账号审批后 joined 指针可能未同步）。"""
    room = my_room(state, uid)
    if room:
        return int(room.get("roomId", 0) or room.get("id", 0) or 0)
    return int(box(state).get("joined", 0) or 0)


def room_view(room: dict | None, uid: int = 0) -> dict:
    """给客户端的房间视图：补齐 roomId/memberCount/maxMembers/difficultyLabel。"""
    if not isinstance(room, dict):
        return {}
    v = dict(room)
    v.setdefault("roomId", int(room.get("id", 0) or 0))
    members = [dict(m) for m in (room.get("members", []) or [])]
    v["members"] = members
    v["memberCount"] = len(members)
    v["maxMembers"] = int(room.get("maxMembers", max_members()))
    v["difficultyLabel"] = str(room.get("difficultyLabel") or _difficulty_label(room.get("difficulty", "easy")))
    v["isLeader"] = any(int(m.get("userId", 0)) == uid and bool(m.get("isLeader")) for m in members)
    return v


def _difficulty_label(difficulty: str) -> str:
    d = difficulty_cfg(str(difficulty or "easy"))
    return str(d.get("label", {"easy": "简单", "normal": "普通", "hard": "困难",
                               "hell": "地狱", "epic": "史诗"}.get(difficulty, difficulty)))


def _next_room_id() -> int:
    with _ROOM_LOCK:
        data = _rooms()
        seq = int(data.get("seq", 1000) or 1000) + 1
        data["seq"] = seq
        return int(seq)


def create_room(ctx, difficulty: str, join_mode: str, snapshot: dict | None = None) -> dict:
    """建房：校验已在队，写入全局房间并把 joined 指向它。令牌在开战时才扣。"""
    state = ctx.state
    uid = int(ctx.user.get("userId", 0) or 0)
    if my_room(state, uid) or box(state).get("joined"):
        return {"error": "已在队伍中"}
    diff = str(difficulty or "easy")
    mode = "review" if str(join_mode).lower() == "review" else "auto"
    me = member_of(ctx, snapshot)
    me["isLeader"] = True
    with _ROOM_LOCK:
        rid = _next_room_id()
        room = {
            "roomId": rid, "id": rid,
            "difficulty": diff, "difficultyLabel": _difficulty_label(diff),
            "joinMode": mode,
            "leaderId": uid, "leaderNickname": state.nickname(), "owner": state.nickname(),
            "maxMembers": max_members(),
            "status": "waiting", "createdAt": iso(now()), "updatedAt": iso(now()),
            "members": [me],
            "applications": [],
        }
        _rooms().setdefault("rooms", {})[str(rid)] = room
        _flush_rooms()
    box(state)["joined"] = rid
    state.bump()
    return {"room": room_view(room, uid), "roomId": rid}


def join_result(ctx, room_id: int, snapshot: dict | None = None) -> dict:
    """入队：auto 直接入；review 记为申请。返回 applied/joined 供客户端分支。"""
    state = ctx.state
    b = box(state)
    uid = int(ctx.user.get("userId", 0) or 0)
    with _ROOM_LOCK:
        room = find_room(state, room_id)
        if not room:
            return {"error": "房间不存在或已解散"}
        if my_room(state, uid) or b.get("joined"):
            return {"error": "你已在队伍中"}
        members = room.setdefault("members", [])
        if any(int(m.get("userId", 0)) == uid for m in members):
            return {"error": "你已在队伍中"}
        if len(members) >= int(room.get("maxMembers", max_members())):
            return {"error": "房间已满"}
        mode = str(room.get("joinMode", "auto")).lower()
        me = member_of(ctx, snapshot)
        room["updatedAt"] = iso(now())
        if mode == "review":
            apps = room.setdefault("applications", [])
            if not any(int(a.get("userId", 0)) == uid for a in apps):
                apps.append(me)
            _flush_rooms()
            return {"applied": True, "joined": False, "roomId": int(room.get("roomId", 0)),
                    "room": room_view(room, uid)}
        # auto：直接入队
        members.append(me)
        _flush_rooms()
    b["joined"] = int(room.get("roomId", 0))
    state.bump()
    return {"applied": False, "joined": True, "roomId": int(room.get("roomId", 0)),
            "room": room_view(room, uid)}


def decide_result(ctx, room_id: int, target_uid: int, approve: bool) -> dict:
    """队长审批申请：approved 时把申请者加入成员，并从其 pending 列表移除。"""
    state = ctx.state
    uid = int(ctx.user.get("userId", 0) or 0)
    with _ROOM_LOCK:
        room = find_room(state, room_id)
        if not room:
            return {"error": "房间不存在或已解散"}
        if int(room.get("leaderId", 0)) != uid:
            return {"error": "只有队长可以处理申请"}
        apps = room.setdefault("applications", [])
        target = next((a for a in apps if int(a.get("userId", 0)) == int(target_uid)), None)
        if target is None:
            return {"approved": False, "roomId": room_id, "error": "申请不存在"}
        apps[:] = [a for a in apps if int(a.get("userId", 0)) != int(target_uid)]
        if approve:
            if len(room.get("members", [])) < int(room.get("maxMembers", max_members())):
                target["isReady"] = False
                room.setdefault("members", []).append(target)
            else:
                _flush_rooms()
                return {"approved": False, "roomId": room_id, "error": "房间已满"}
        room["updatedAt"] = iso(now())
        _flush_rooms()
    state.bump()
    return {"approved": bool(approve), "roomId": room_id, "userId": int(target_uid),
            "room": room_view(room, uid)}


def set_ready(ctx, ready: bool = True) -> dict:
    """标记自己在房间内 ready。"""
    state = ctx.state
    uid = int(ctx.user.get("userId", 0) or 0)
    with _ROOM_LOCK:
        room = my_room(state, uid)
        if not room:
            return {"error": "你不在队伍中"}
        for m in room.get("members", []) or []:
            if int(m.get("userId", 0)) == uid:
                m["isReady"] = bool(ready)
                m["is_ready"] = bool(ready)
        room["updatedAt"] = iso(now())
        _flush_rooms()
        view = room_view(room, uid)
    state.bump()
    return {"ready": bool(ready), "roomId": int(view.get("roomId", 0)),
            "userId": uid, "room": view}


def leave_result(ctx) -> dict:
    """离队：从 members 移除自己；空房则解散。"""
    state = ctx.state
    b = box(state)
    uid = int(ctx.user.get("userId", 0) or 0)
    with _ROOM_LOCK:
        room = my_room(state, uid)
        if not room:
            b["joined"] = None
            return {"message": "你不在队伍中"}
        room["members"] = [m for m in room.get("members", []) or []
                           if int(m.get("userId", 0)) != uid]
        room["applications"] = [a for a in room.get("applications", []) or []
                                if int(a.get("userId", 0)) != uid]
        rid = int(room.get("roomId", 0) or room.get("id", 0))
        if not room["members"]:
            _rooms().get("rooms", {}).pop(str(rid), None)
            _flush_rooms()
            b["joined"] = None
            state.bump()
            return {"disbanded": True, "message": "队伍已解散"}
        if int(room.get("leaderId", 0)) == uid:
            new_leader = room["members"][0]
            new_leader["isLeader"] = True
            room["leaderId"] = int(new_leader.get("userId", 0))
            room["leaderNickname"] = str(new_leader.get("name", ""))
            room["owner"] = str(new_leader.get("name", ""))
        room["updatedAt"] = iso(now())
        _flush_rooms()
        view = room_view(room, uid)
    b["joined"] = None
    state.bump()
    return {"roomId": rid, "message": "已离开队伍", "room": view}


def disband_result(ctx) -> dict:
    """解散：仅队长可解散自己所在房间。"""
    state = ctx.state
    uid = int(ctx.user.get("userId", 0) or 0)
    with _ROOM_LOCK:
        room = my_room(state, uid)
        if not room:
            return {"error": "你不在队伍中"}
        if int(room.get("leaderId", 0)) != uid:
            return {"error": "只有队长可以解散队伍"}
        rid = int(room.get("roomId", 0) or room.get("id", 0))
        _rooms().get("rooms", {}).pop(str(rid), None)
        _flush_rooms()
    box(state)["joined"] = None
    state.bump()
    return {"disbanded": True, "roomId": rid, "message": "队伍已解散"}


def pending_application_room_ids(state, uid: int = 0) -> list[int]:
    """我（按 userId）已申请、待审批的房间 id 列表（全局）。"""
    ids: list[int] = []
    for r in all_rooms():
        for a in r.get("applications", []) or []:
            if uid and int(a.get("userId", 0)) == uid:
                ids.append(int(r.get("roomId", 0) or r.get("id", 0)))
                break
    return ids


def has_token(state) -> bool:
    return state.item_count(token_name()) >= 1


def consume_token(state) -> bool:
    """开战消耗 1 个令牌（队长）。不足返回 False。"""
    return state.take_item(token_name(), 1)


# ---------------------------------------------------------------- 限流与掉落

def clear_limits(state=None) -> tuple[int, int]:
    # dailyClearMax/dailyRewardMax 在顶层
    clear = int(cfg().get("dailyClearMax", 3) or 3)
    # 原版：基础通关次数 + 贵族每级 +N（brother_tower_ui.gd:419-424）
    # deploy/server 既有此语义；local-server 的 State 无 noble_level 时自动跳过
    noble = getattr(state, "noble_level", None) if state is not None else None
    if callable(noble):
        try:
            clear += int(noble()) * int(cfg().get("nobleClearBonusPerLevel", 1) or 0)
        except Exception:
            pass
    reward = int(cfg().get("dailyRewardMax", 1) or 1)
    return clear, reward


def gift_count(difficulty: str) -> int:
    d = difficulty_cfg(difficulty)
    return max(1, int(d.get("giftCount", 1) or 1))


def friendship_gain(difficulty: str) -> int:
    d = difficulty_cfg(difficulty)
    return max(0, int(d.get("friendshipGain", 4) or 4))


# giftDesc 形如 "60.54%1亿金币 / 5%5亿金币 / 5%一箭三雕 / ..."，按 % 抽权重。
_RATE_RE = re.compile(r"^\s*([\d.]+)\s*%\s*(.+?)\s*$")


def parse_gift_table() -> list[tuple[float, str]]:
    desc = str(cfg().get("giftDesc", "") or "")
    rows: list[tuple[float, str]] = []
    for part in desc.split("/"):
        m = _RATE_RE.match(part)
        if not m:
            continue
        try:
            rate = float(m.group(1))
        except ValueError:
            continue
        name = m.group(2).strip()
        if rate > 0 and name:
            rows.append((rate, name))
    return rows


def roll_gifts(difficulty: str) -> list[dict]:
    """按 giftDesc 权重表 roll 出 giftCount 件真实奖励。

    金币类直接给数值（add_gold），其余作为道具/装备发放。返回结构供 complete 使用。
    """
    table = parse_gift_table()
    picks: list[dict] = []
    if not table:
        return picks
    total_w = sum(r for r, _ in table)
    for _ in range(gift_count(difficulty)):
        x = random.random() * total_w
        acc = 0.0
        chosen = table[-1][1]
        for rate, name in table:
            acc += rate
            if x <= acc:
                chosen = name
                break
        m = re.match(r"^([\d.]+)亿金币$", chosen)
        if m:
            gold = int(float(m.group(1)) * 100_000_000)
            picks.append({"kind": "gold", "name": chosen, "count": 1, "gold": gold})
        else:
            picks.append({"kind": "item", "name": chosen, "count": 1})
    return picks
