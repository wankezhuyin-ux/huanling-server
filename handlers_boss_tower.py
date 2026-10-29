#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""世界 Boss（真实战斗结算）、养宠赛排行、生死塔大厅（组队/报名/快照）。

世界 Boss 的战斗是**客户端本地跑**的：`/world-boss/battle-start` 只回 Boss 属性和
battleToken（replay=false），客户端按真实战斗系统打满 10 回合，再把伤害
POST 回 `/world-boss/battle-report` 结算。所以这里要做的是：
  * 开战时占用当日次数（原来的 /challenge 是同一个次数池）
  * 上报时按"回合数 × 单回合最大可能输出"给伤害封顶，防止直接上报 21 亿
  * 结算后写榜、发参与礼包

生死塔的房间是**跨账号**的，不能塞进玩家存档，这里单独落在
`state/shengsi-rooms.json`，用一把锁保护并发写。
"""
from __future__ import annotations

import threading

from core import (Ctx, RULES, STATE_DIR, dump_json, fail, in_window, iso, load_json,
                  now, ok, route, rnd_id, today)

_ROOM_LOCK = threading.RLock()
_ROOM_PATH = STATE_DIR / "shengsi-rooms.json"
_ROOMS: dict | None = None


# ================================================================ 公共

def _active_pet(state) -> dict:
    pets = state.pets
    for pet in pets:
        if isinstance(pet, dict) and bool(pet.get("is_active", pet.get("active", False))):
            return pet
    return pets[0] if pets and isinstance(pets[0], dict) else {}


def _wb_box(state) -> dict:
    """世界 Boss 的当日伤害池，跨天清零。"""
    box = state.meta.setdefault("worldBoss", {})
    if box.get("date") != today():
        box["date"] = today()
        box["damage"] = 0
    return box


def _wb_window(cfg: dict) -> bool:
    return in_window(int(cfg.get("challengeStartHour", 9)), int(cfg.get("challengeStartMinute", 30)),
                     int(cfg.get("challengeEndHour", 22)), int(cfg.get("challengeEndMinute", 30)))


def _wb_rank(state) -> list[dict]:
    """全服当日伤害榜（真实账号，不含补位假数据）。"""
    from core import STORE  # 延迟导入，避免 core 内部的循环引用
    rows = []
    for st in STORE.all_states():
        if st is state:
            box = _wb_box(state)
        else:
            box = st.meta.setdefault("worldBoss", {})
            if box.get("date") != today():
                continue
        damage = int(box.get("damage", 0) or 0)
        if damage <= 0:
            continue
        pet = _active_pet(st)
        rows.append({
            "playerName": st.nickname(),
            "characterId": int(st.player.get("userId", 0) or 0),
            "totalDamage": damage,
            "petName": str(pet.get("name", "")),
            "userId": int(st.player.get("userId", 0) or 0),
        })
    rows.sort(key=lambda r: (-r["totalDamage"], r["playerName"]))
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return rows


# ================================================================ 世界 Boss

@route("GET", "/world-boss/ranking")
def world_boss_ranking(ctx: Ctx) -> dict:
    cfg = RULES.m("world_boss")
    limit = max(1, min(int(ctx.qi("limit", 100) or 100), int(cfg.get("rankingDisplayLimit", 100) or 100)))
    rows = _wb_rank(ctx.state)[:limit]
    mine = 0
    me = ctx.state.nickname()
    for row in rows:
        if row["playerName"] == me:
            mine = int(row["rank"])
            break
    return ok(ranking=rows, myRank=mine, eventDate=today(),
              total=len(rows), limit=limit)


@route("POST", "/world-boss/battle-start")
def world_boss_battle_start(ctx: Ctx) -> dict:
    state = ctx.state
    cfg = RULES.m("world_boss")
    if not bool(cfg.get("featureEnabled", True)):
        return fail("世界 Boss 暂未开放")
    if not _wb_window(cfg):
        return fail("当前不在世界 Boss 开放时间（每日 09:30-22:30）")
    cap = int(cfg.get("dailyMaxAttempts", 3))
    used = state.daily("world_boss")
    if used >= cap:
        return fail("今日挑战次数已用完（%d/%d）" % (used, cap))
    pet = _active_pet(state)
    if not pet:
        return fail("请先设置出战宠物")
    state.daily_inc("world_boss")
    token = rnd_id("wb")
    state.meta["worldBossPending"] = {"token": token, "date": today(), "at": iso(now())}
    state.bump()
    hp = int(cfg.get("bossHp", 1000000000))
    boss = {
        "hp": hp,
        "max_hp": hp,
        "attack": int(cfg.get("bossAttack", 10000)),
        "defense": int(cfg.get("bossDefense", 30000)),
        "agility": int(cfg.get("bossAgility", 10000)),
    }
    return ok(
        battleToken=token,
        bossPetId="shuizhizhangkong",
        battleMaxRounds=int(cfg.get("battleMaxRounds", 10)),
        # replay=false → 客户端按真实战斗系统本地打，结束后再上报伤害
        replay=False,
        damageDealt=0,
        boss=boss,
        remainingAttempts=max(0, cap - used - 1),
        petSnapshot=pet,
    )


@route("POST", "/world-boss/battle-report")
def world_boss_battle_report(ctx: Ctx) -> dict:
    state = ctx.state
    cfg = RULES.m("world_boss")
    pending = state.meta.get("worldBossPending")
    token = ctx.s("battleToken")
    if not isinstance(pending, dict) or not pending or str(pending.get("token", "")) != token:
        return fail("战斗已失效，请重新开战")
    if str(pending.get("date", "")) != today():
        state.meta.pop("worldBossPending", None)
        return fail("战斗已跨天失效")
    state.meta.pop("worldBossPending", None)

    rounds = max(1, int(cfg.get("battleMaxRounds", 10)))
    pet = _active_pet(state)
    raw = max(0, ctx.i("damage", 0))
    # 伤害封顶：10 回合里单回合输出不可能超过宠物攻击的几十倍，
    # 不封顶的话客户端直接上报一个天文数字就把榜占了。
    per_round_cap = max(1.0, float(pet.get("final_attack", pet.get("attack", 100)) or 100)) * 20.0
    cap_damage = int(rounds * per_round_cap)
    damage = min(raw, cap_damage)

    box = _wb_box(state)
    box["damage"] = int(box.get("damage", 0) or 0) + damage

    rows = _wb_rank(state)
    my_rank = 0
    me = state.nickname()
    for row in rows:
        if row["playerName"] == me:
            my_rank = int(row["rank"])
            break

    gift = str(cfg.get("giftItemName", "世界首领锦囊"))
    state.send_mail("世界 Boss 参与礼包",
                    "世界 Boss 挑战结算：本次造成 %d 点伤害，请查收参与礼包。" % damage,
                    [{"name": gift, "count": 1}])
    state.bump()
    return ok(
        damage=damage,
        damageRaw=raw,
        damageClamped=damage != raw,
        myTotalDamage=int(box.get("damage", 0)),
        myRank=my_rank,
        remainingAttempts=max(0, int(cfg.get("dailyMaxAttempts", 3)) - state.daily("world_boss")),
        gift=gift,
    )


# ================================================================ 养宠赛排行

@route("GET", "/pet-raise-race/ranking")
def pet_raise_race_ranking(ctx: Ctx) -> dict:
    from core import STORE
    cfg = RULES.m("pet_raise_race")
    limit = max(1, min(int(ctx.qi("limit", 100) or 100), int(cfg.get("rankingDisplayLimit", 100) or 100)))
    # 当日目标成长取配置区间中点（区间是对称的，中点即最优解）
    target = (float(cfg.get("targetGrowthMin", 20)) + float(cfg.get("targetGrowthMax", 45))) / 2.0
    rows = []
    for st in STORE.all_states():
        box = st.meta.get("petRaiseRace")
        if not isinstance(box, dict):
            continue
        sub = box.get("submit")
        if not isinstance(sub, dict) or not sub:
            continue
        growth = float(sub.get("petGrowth", 0) or 0)
        rows.append({
            "playerName": st.nickname(),
            "petName": str(sub.get("petName", "灵宠")),
            "petGrowth": growth,
            "diff": round(abs(growth - target), 3),
            "submittedAt": str(sub.get("at", "")),
        })
    rows.sort(key=lambda r: (r["diff"], r["submittedAt"]))
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return ok(ranking=rows[:limit], targetGrowth=round(target, 3), eventDate=today(),
              total=len(rows), limit=limit)


# ================================================================ 生死塔房间（跨账号）

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


def _member_of(ctx: Ctx) -> dict:
    user = ctx.user or {}
    state = ctx.state
    player = state.player
    return {
        "userId": int(user.get("userId", 0) or 0),
        "nickname": str(user.get("nickname") or state.nickname()),
        "isLeader": False,
        "level": int(player.get("level", 1) or 1),
        "reincarnation": int(player.get("reincarnation", 0) or 0),
        "username": str(user.get("username", "")),
    }


def _find_my_room(ctx: Ctx):
    uid = int(ctx.user.get("userId", 0) or 0)
    if uid <= 0:
        return None
    for room in _rooms()["rooms"].values():
        for member in room.get("members", []):
            if int(member.get("userId", 0)) == uid:
                return room
    return None


def _max_members() -> int:
    return max(2, int(RULES.v("shengsi_tower", "teamMaxMembers", 2) or 2))


def _room_view(room: dict, uid: int = 0) -> dict:
    view = dict(room)
    view["members"] = [dict(m) for m in room.get("members", [])]
    view["memberCount"] = len(view["members"])
    view["maxMembers"] = int(room.get("maxMembers", _max_members()))
    view["isLeader"] = any(int(m.get("userId", 0)) == uid and bool(m.get("isLeader"))
                           for m in view["members"]) if uid else False
    return view


def _floor_of(ctx: Ctx, snapshot: dict | None = None) -> int:
    if snapshot and isinstance(snapshot, dict):
        for key in ("floor", "floorId", "activeFloor"):
            try:
                val = int(snapshot.get(key, 0) or 0)
            except (TypeError, ValueError):
                val = 0
            if val > 0:
                return val
    return max(0, ctx.i("floor", 0))


@route("GET", "/shengsi-tower/room/list")
def shengsi_room_list(ctx: Ctx) -> dict:
    """覆盖 handlers_play 里返回空列表的旧实现：房间现在是真存在的。"""
    floor = max(0, ctx.qi("floor", 0))
    uid = int(ctx.user.get("userId", 0) or 0)
    rows = []
    with _ROOM_LOCK:
        for room in _rooms()["rooms"].values():
            if floor > 0 and int(room.get("floor", 0)) != floor:
                continue
            view = _room_view(room, uid)
            if view["memberCount"] <= 0:
                continue
            if str(room.get("status", "")) == "disbanded":
                continue
            rows.append(view)
    rows.sort(key=lambda r: (-r["memberCount"], int(r["roomId"])))
    return ok(rooms=rows, floor=floor)


@route("POST", "/shengsi-tower/room/create")
def shengsi_room_create(ctx: Ctx) -> dict:
    snapshot = ctx.body.get("snapshot") if isinstance(ctx.body.get("snapshot"), dict) else {}
    floor = _floor_of(ctx, snapshot)
    if floor <= 0:
        return fail("请先选择楼层")
    me = _member_of(ctx)
    if me["userId"] <= 0:
        return fail("未登录")
    with _ROOM_LOCK:
        existing = _find_my_room(ctx)
        if existing is not None:
            return ok(room=_room_view(existing, me["userId"]), alreadyInRoom=True)
        data = _rooms()
        data["seq"] = int(data.get("seq", 1000)) + 1
        room_id = data["seq"]
        me["isLeader"] = True
        room = {
            "roomId": room_id,
            "floor": floor,
            "leaderId": me["userId"],
            "leaderNickname": me["nickname"],
            "maxMembers": _max_members(),
            "status": "open",
            "createdAt": iso(now()),
            "members": [me],
        }
        data["rooms"][str(room_id)] = room
        _flush_rooms()
    return ok(room=_room_view(room, me["userId"]), roomId=room_id)


@route("POST", "/shengsi-tower/room/join")
def shengsi_room_join(ctx: Ctx) -> dict:
    room_id = ctx.i("roomId", 0)
    me = _member_of(ctx)
    if me["userId"] <= 0:
        return fail("未登录")
    if room_id <= 0:
        return fail("房间号无效")
    with _ROOM_LOCK:
        room = _rooms()["rooms"].get(str(room_id))
        if not isinstance(room, dict):
            return fail("房间不存在或已解散")
        for member in room.get("members", []):
            if int(member.get("userId", 0)) == me["userId"]:
                return ok(room=_room_view(room, me["userId"]), alreadyInRoom=True)
        if len(room.get("members", [])) >= int(room.get("maxMembers", _max_members())):
            return fail("房间已满")
        mine = _find_my_room(ctx)
        if mine is not None:
            return fail("请先退出当前队伍")
        room["members"].append(me)
        room["updatedAt"] = iso(now())
        _flush_rooms()
    return ok(room=_room_view(room, me["userId"]), roomId=room_id)


@route("POST", "/shengsi-tower/room/leave")
def shengsi_room_leave(ctx: Ctx) -> dict:
    uid = int(ctx.user.get("userId", 0) or 0)
    with _ROOM_LOCK:
        room = _find_my_room(ctx)
        if room is None:
            return ok(room={}, message="你不在任何队伍中")
        room["members"] = [m for m in room.get("members", []) if int(m.get("userId", 0)) != uid]
        if not room["members"]:
            _rooms()["rooms"].pop(str(room.get("roomId")), None)
            _flush_rooms()
            return ok(room={}, disbanded=True, message="队伍已解散")
        if int(room.get("leaderId", 0)) == uid:
            # 队长退出就把队长交给剩下的第一个人，否则房间没人能操作
            new_leader = room["members"][0]
            new_leader["isLeader"] = True
            room["leaderId"] = int(new_leader.get("userId", 0))
            room["leaderNickname"] = str(new_leader.get("nickname", ""))
        _flush_rooms()
    return ok(room=_room_view(room, uid), message="已离开队伍")


@route("POST", "/shengsi-tower/room/disband")
def shengsi_room_disband(ctx: Ctx) -> dict:
    uid = int(ctx.user.get("userId", 0) or 0)
    with _ROOM_LOCK:
        room = _find_my_room(ctx)
        if room is None:
            return ok(message="你不在任何队伍中")
        if int(room.get("leaderId", 0)) != uid:
            return fail("只有队长可以解散队伍")
        _rooms()["rooms"].pop(str(room.get("roomId")), None)
        _flush_rooms()
    return ok(disbanded=True, message="队伍已解散")


# ---------------------------------------------------------------- 报名 / 快照

@route("POST", "/shengsi-tower/register")
def shengsi_register(ctx: Ctx) -> dict:
    state = ctx.state
    cfg = RULES.m("shengsi_tower")
    if not bool(cfg.get("featureEnabled", True)):
        return fail("生死塔暂未开放")
    box = state.meta.setdefault("shengsi", {})
    daily_cap = max(1, int(cfg.get("dailyLimitPerUser", 1) or 1))
    if box.get("registeredDate") == today() and int(box.get("registerCount", 0)) >= daily_cap:
        return fail("今日报名次数已用完")
    room = _find_my_room(ctx)
    if room is None:
        return fail("请先创建或加入队伍再报名")
    if len(room.get("members", [])) <= 0:
        return fail("队伍为空")
    with _ROOM_LOCK:
        room["status"] = "registered"
        room["registeredAt"] = iso(now())
        _flush_rooms()
        view = _room_view(room, int(ctx.user.get("userId", 0) or 0))
    box["registeredDate"] = today()
    box["registerCount"] = int(box.get("registerCount", 0)) + 1
    box["myRoomId"] = int(room.get("roomId", 0))
    state.bump()
    return ok(room=view, myEntry={
        "floor": int(room.get("floor", 0)),
        "roomId": int(room.get("roomId", 0)),
        "members": view["members"],
        "status": "registered",
    })


@route("POST", "/shengsi-tower/unregister")
def shengsi_unregister(ctx: Ctx) -> dict:
    state = ctx.state
    uid = int(ctx.user.get("userId", 0) or 0)
    box = state.meta.setdefault("shengsi", {})
    with _ROOM_LOCK:
        room = _find_my_room(ctx)
        if isinstance(room, dict) and str(room.get("status", "")) == "registered":
            if int(room.get("leaderId", 0)) == uid:
                _rooms()["rooms"].pop(str(room.get("roomId")), None)
            else:
                room["members"] = [m for m in room.get("members", [])
                                   if int(m.get("userId", 0)) != uid]
                _flush_rooms()
        elif isinstance(room, dict):
            room["members"] = [m for m in room.get("members", []) if int(m.get("userId", 0)) != uid]
            if not room["members"]:
                _rooms()["rooms"].pop(str(room.get("roomId")), None)
            _flush_rooms()
    box["myRoomId"] = 0
    box["registeredDate"] = ""
    state.bump()
    return ok(unregistered=True, message="已取消报名")


@route("POST", "/shengsi-tower/pin-snapshot")
def shengsi_pin_snapshot(ctx: Ctx) -> dict:
    state = ctx.state
    snapshot = ctx.body.get("localSnapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        return fail("快照内容为空")
    box = state.meta.setdefault("shengsi", {})
    box["pinnedSnapshot"] = snapshot
    box["pinnedAt"] = iso(now())
    box["pinnedOperation"] = ctx.s("operationId")
    state.bump()
    return ok(pinnedSnapshot=snapshot, pinnedAt=box["pinnedAt"])


@route("GET", "/shengsi-tower/overview")
def shengsi_overview(ctx: Ctx) -> dict:
    """覆盖旧实现：补上 myRoom / pinnedSnapshot，大厅才能显示自己的队伍。"""
    cfg = RULES.m("shengsi_tower")
    state = ctx.state
    box = state.meta.setdefault("shengsi", {})
    uid = int(ctx.user.get("userId", 0) or 0)
    with _ROOM_LOCK:
        room = _find_my_room(ctx)
        view = _room_view(room, uid) if isinstance(room, dict) else {}
    return {
        "success": True,
        "featureEnabled": bool(cfg.get("featureEnabled", True)),
        "config": cfg,
        "floors": cfg.get("floors", []),
        "window": {
            "signupRangeText": cfg.get("signupRangeText", ""),
            "signupOpen": in_window(int(cfg.get("signupStartHour", 20)),
                                    int(cfg.get("signupStartMinute", 30)), 23, 59),
            "startHour": int(cfg.get("startHour", 20)),
        },
        "mySignup": box.get("signup"),
        "myRoom": view,
        "pinnedSnapshot": box.get("pinnedSnapshot", {}),
    }
