#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生死塔赛事编排：覆盖 handlers_extra 里那几个固定返回空的轮次接口。

原版是「每日 20:30 报名、20:40 开赛、每层 8 队单败淘汰」的多人赛事：
服务端定时生成配对、跑战斗、写回放，客户端只负责播放（`start_shengsi_tower_replay`）。

私人服很难在同一时段凑满 8 支队伍，所以这里带一个**镜像补位**：
报名队伍不足 8 支时，用报名者的镜像（四维 0.8~1.2 抖动）补满 8 支，
单独一人报名也能完整打满 3 轮，赢了就是塔主、拿该层礼包。
真实队伍互相优先配对，镜像只填空位。

赛事状态落在 `state/shengsi-events.json`（按日期一个 event）。
轮次推进是**惰性**的：任何一次请求进来，先按当前时间把该发生的轮次补齐，
所以不需要后台定时器，也不会因为没人看而卡住。
"""
from __future__ import annotations

import copy
import random
import threading
from datetime import timedelta

from core import (Ctx, RULES, STATE_DIR, STORE, dump_json, fail, iso, load_json,
                  now, ok, route, today)
import combat
import treasure_mirror

_LOCK = threading.RLock()
_PATH = STATE_DIR / "shengsi-events.json"
_DB: dict | None = None

_AI_NAMES = ["守塔人·甲", "守塔人·乙", "守塔人·丙", "守塔人·丁",
             "守塔人·戊", "守塔人·己", "守塔人·庚", "守塔人·辛"]
_DEFAULT_CAPACITY = 8
_DEFAULT_ROUNDS = 3


# ---------------------------------------------------------------- 存储

def _db() -> dict:
    global _DB
    with _LOCK:
        if _DB is None:
            raw = load_json(_PATH, {"events": {}})
            if not isinstance(raw, dict) or not isinstance(raw.get("events"), dict):
                raw = {"events": {}}
            _DB = raw
        return _DB


def _flush() -> None:
    with _LOCK:
        dump_json(_PATH, _db())


def _cfg() -> dict:
    cfg = RULES.m("shengsi_tower")
    return cfg if isinstance(cfg, dict) else {}


def _timeline(cfg: dict, t=None):
    """今天的报名开始 / 开赛 / 轮次间隔 / 轮数 / 结束（均为 datetime）。"""
    t = t or now()
    ss = t.replace(hour=int(cfg.get("signupStartHour", 20) or 20),
                   minute=int(cfg.get("signupStartMinute", 30) or 0),
                   second=0, microsecond=0)
    st = t.replace(hour=int(cfg.get("startHour", 20) or 20),
                   minute=int(cfg.get("startMinute", 40) or 0),
                   second=0, microsecond=0)
    gap = max(30, int(cfg.get("roundGapSeconds", 60) or 60))
    prep = max(5, int(cfg.get("prepSeconds", 20) or 20))
    rounds = _DEFAULT_ROUNDS
    fin = st + timedelta(seconds=gap * rounds)
    return ss, st, gap, prep, rounds, fin


def _phase(cfg: dict) -> dict:
    ss, st, gap, prep, rounds, fin = _timeline(cfg)
    t = now()
    if t < ss:
        phase, in_win, past = "signup", False, False
    elif t < st:
        phase, in_win, past = "signup", True, False
    else:
        in_win, past = False, True
        phase = "finished" if t >= fin else "battle"
    return {
        "phase": phase,
        "inSignupWindow": in_win,
        "pastStart": past,
        "signupStartAt": int(ss.timestamp()),
        "signupEndAt": int(st.timestamp()),
        "startAt": int(st.timestamp()),
        "finishAt": int(fin.timestamp()),
        "serverNow": int(t.timestamp()),
        "eventKey": t.strftime("%Y%m%d"),
        "roundGapSeconds": gap,
        "prepSeconds": prep,
        "totalRounds": rounds,
    }


# ---------------------------------------------------------------- 赛事 / 楼层

def _event(create: bool = True) -> dict | None:
    key = now().strftime("%Y%m%d")
    db = _db()
    ev = db["events"].get(key)
    if ev is None and create:
        ev = {"key": key, "date": today(), "floors": {}, "finished": False,
              "seq": 0, "createdAt": iso(now())}
        db["events"][key] = ev
        # 只留最近 5 天，免得文件无限长
        if len(db["events"]) > 5:
            for old in sorted(db["events"])[:-5]:
                db["events"].pop(old, None)
        _flush()
    return ev


def _floor_box(ev: dict, floor: int) -> dict:
    key = str(int(floor))
    fd = ev["floors"].get(key)
    if not isinstance(fd, dict):
        fd = {"floor": int(floor), "entries": [], "matches": {},
              "champion": None, "status": "waiting"}
        ev["floors"][key] = fd
    return fd


def _entry(fd: dict, entry_id: int) -> dict | None:
    for e in fd.get("entries", []):
        if int(e.get("entryId", 0)) == int(entry_id):
            return e
    return None


def _my_entry(fd: dict, uid: int) -> dict | None:
    for e in fd.get("entries", []):
        if uid in [int(x) for x in (e.get("memberIds") or [])]:
            return e
    return None


def _entry_brief(e: dict | None, with_snap: bool = True) -> dict:
    if not isinstance(e, dict):
        return {}
    return {
        "entryId": int(e.get("entryId", 0)),
        "floor": int(e.get("floor", 0)),
        "leaderId": int(e.get("leaderId", 0)),
        "leaderNickname": str(e.get("leaderNickname", "")),
        "memberNickname": str(e.get("memberNickname", "")),
        "members": e.get("members") or [],
        "isAi": bool(e.get("ai", False)),
        "eliminated": bool(e.get("eliminated", False)),
        "leader": (e.get("leaderSnap") or {}) if with_snap else {},
        "member": (e.get("memberSnap") or {}) if with_snap else {},
    }


def _entry_team_row(e: dict, uid: int) -> dict:
    return {
        "entryId": int(e.get("entryId", 0)),
        "floor": int(e.get("floor", 0)),
        "leaderId": int(e.get("leaderId", 0)),
        "leaderNickname": str(e.get("leaderNickname", "")),
        "memberNickname": str(e.get("memberNickname", "")),
        "members": e.get("members") or [],
        "isMine": uid in [int(x) for x in (e.get("memberIds") or [])],
        "eliminated": bool(e.get("eliminated", False)),
        "isPractice": False,
        "isAi": bool(e.get("ai", False)),
    }


# ---------------------------------------------------------------- 快照 / 战斗

def _num(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(d)


def _scale_snap(snap: dict, k: float, name: str = "") -> dict:
    out = copy.deepcopy(snap or {})
    for key in ("player", "pet"):
        row = out.get(key)
        if not isinstance(row, dict):
            continue
        for field in ("final_max_hp", "max_hp", "hp", "final_attack", "attack",
                      "final_defense", "defense", "final_agility", "agility",
                      "final_max_mp", "max_mp", "mp"):
            if field in row:
                row[field] = max(1, int(_num(row.get(field), 1) * k))
        if name:
            row["name"] = name
            row["pet_name"] = name
    return out


def _fighter(snap_row: dict, side: str, index: int, kind: str):
    """把镜像快照里的 player/pet 行转成战斗单位。"""
    if not isinstance(snap_row, dict) or not snap_row:
        return None
    hp = _num(snap_row.get("final_max_hp", snap_row.get("max_hp", 500)), 500)
    return combat.Fighter(
        name=str(snap_row.get("name") or snap_row.get("pet_name") or "对手"),
        kind=kind,
        side=side,
        index=index,
        hp=hp,
        max_hp=hp,
        attack=_num(snap_row.get("final_attack", snap_row.get("attack", 100)), 100),
        defense=_num(snap_row.get("final_defense", snap_row.get("defense", 100)), 100),
        agility=_num(snap_row.get("final_agility", snap_row.get("agility", 100)), 100),
        level=int(_num(snap_row.get("level"), 1) or 1),
        pet_id=str(snap_row.get("pet_id", "")),
        element=str(snap_row.get("element", "")),
        is_evolution_pet=bool(snap_row.get("is_evolution_pet", False)),
        is_mutant=bool(snap_row.get("is_mutant", False)),
    )


def _unit_info(side: str, role: str, kind: str, f) -> dict:
    return {
        "type": "unit_info",
        "id": "%s_%s_%s" % (side, role, kind),
        "name": f.name,
        "attack": int(f.attack),
        "defense": int(f.defense),
        "agility": int(f.agility),
        "hp": int(f.max_hp),
        "maxHp": int(f.max_hp),
    }


def _simulate(a: dict, b: dict) -> tuple[int, list, dict]:
    """跑一场 1v1（各带一只宠），返回 (胜者 entryId, 战斗日志, 双方单位快照)。"""
    sa = a.get("leaderSnap") or {}
    sb = b.get("leaderSnap") or {}
    pa = _fighter(sa.get("player") or {}, "A", 0, "player")
    peta = _fighter(sa.get("pet") or {}, "A", 1, "pet")
    pb = _fighter(sb.get("player") or {}, "B", 0, "player")
    petb = _fighter(sb.get("pet") or {}, "B", 1, "pet")
    if pa is None or pb is None:
        # 快照缺失时按战力判，别让比赛开天窗
        wa = _num((sa.get("player") or {}).get("final_attack"), 1)
        wb = _num((sb.get("player") or {}).get("final_attack"), 1)
        return (int(a["entryId"]) if wa >= wb else int(b["entryId"])), [], {}
    foes = [f for f in (pb, petb) if f is not None]
    try:
        res = combat.resolve(pa, peta, foes, max_rounds=20,
                             player_element=str((sa.get("player") or {}).get("element", "")))
    except Exception:
        res = {"victory": pa.attack >= pb.attack, "battleLog": []}
    log = list(res.get("battleLog") or [])
    # 我方/对方人物死亡在 combat 里都是 pet_dead，这里统一成 defeat 让客户端自己分
    for row in log:
        if not isinstance(row, dict):
            continue
        if row.get("type") == "pet_dead":
            row["type"] = "defeat"
            row["targetName"] = str(row.get("name") or row.get("target") or "")
        if row.get("type") == "enemy_dead":
            row["targetName"] = str(row.get("name") or row.get("target") or "")
    head = [{"type": "battle_start", "text": "生死塔对决开始"}]
    if pa is not None:
        head.append(_unit_info("A", "leader", "player", pa))
    if peta is not None:
        head.append(_unit_info("A", "leader", "pet", peta))
    if pb is not None:
        head.append(_unit_info("B", "leader", "player", pb))
    if petb is not None:
        head.append(_unit_info("B", "leader", "pet", petb))
    win = int(a["entryId"]) if bool(res.get("victory", False)) else int(b["entryId"])
    return win, head + log, {}


# ---------------------------------------------------------------- 轮次推进

def _rng(ev: dict, floor: int, rnd: int) -> random.Random:
    return random.Random("%s-%s-%s" % (ev.get("key", ""), floor, rnd))


def _fill_mirrors(ev: dict, fd: dict, capacity: int) -> bool:
    """报名不足 8 支时用镜像补满；返回是否有新增。"""
    entries = fd.get("entries") or []
    real = [e for e in entries if not e.get("ai")]
    if not real:
        return False
    changed = False
    idx = len([e for e in entries if e.get("ai")])
    cfg = _cfg()
    while len(entries) < capacity and idx < len(_AI_NAMES):
        base = real[idx % len(real)]
        # 守塔镜像统一压到玩家 0.78~0.96：私人服往往只有一支真实队伍，
        # 补位的镜像要是和玩家五五开，一个人打三轮几乎必输（0.5^3），
        # 这就成了"每天来输一次"。留一点悬念但不难打。
        k = 0.78 + (idx * 0.03) % 0.19
        name = _AI_NAMES[idx]
        snap = _scale_snap(base.get("leaderSnap") or {}, k, name)
        ev["seq"] = int(ev.get("seq", 0)) + 1
        entries.append({
            "entryId": int(ev["seq"]),
            "floor": int(fd.get("floor", 1)),
            "roomId": 0,
            "leaderId": 0,
            "leaderNickname": name,
            "memberNickname": "",
            "memberIds": [],
            "members": [{"nickname": name, "level": int(_num((snap.get("player") or {}).get("level"), 1) or 1)}],
            "username": "",
            "leaderSnap": snap,
            "memberSnap": {},
            "ai": True,
            "eliminated": False,
        })
        idx += 1
        changed = True
    fd["entries"] = entries
    return changed


def _make_round(ev: dict, fd: dict, rnd: int) -> list:
    alive = [e for e in fd.get("entries", []) if not e.get("eliminated")]
    if len(alive) < 2:
        return []
    rng = _rng(ev, int(fd.get("floor", 1)), rnd)
    rng.shuffle(alive)
    matches = []
    floor = int(fd.get("floor", 1))
    for i in range(0, len(alive) - 1, 2):
        a, b = alive[i], alive[i + 1]
        win, log, _ = _simulate(a, b)
        loser = b if win == int(a["entryId"]) else a
        loser["eliminated"] = True
        ev["seq"] = int(ev.get("seq", 0)) + 1
        matches.append({
            "matchId": int(ev["seq"]),
            "floor": floor,
            "round": rnd,
            "aEntryId": int(a["entryId"]),
            "bEntryId": int(b["entryId"]),
            "winnerEntryId": int(win),
            "battleLog": log,
            "createdAt": iso(now()),
        })
    return matches


def _advance_floor(ev: dict, fd: dict, cfg: dict, t) -> bool:
    ss, st, gap, prep, rounds, fin = _timeline(cfg, t)
    if t < st:
        return False
    changed = False
    if fd.get("status") != "finished" and fd.get("status") != "running":
        fd["status"] = "running"
        changed = True
    if not (fd.get("entries") or []):
        return changed
    if _fill_mirrors(ev, fd, _DEFAULT_CAPACITY):
        changed = True
    for rnd in range(1, rounds + 1):
        round_start = st + timedelta(seconds=(rnd - 1) * gap)
        if t < round_start:
            break
        key = str(rnd)
        if key not in (fd.get("matches") or {}):
            fd.setdefault("matches", {})[key] = _make_round(ev, fd, rnd)
            changed = True
    alive = [e for e in fd.get("entries", []) if not e.get("eliminated")]
    # 等最后一轮的时间走完再戴冠，别让玩家在备战阶段就看到结果
    if len(alive) == 1 and not fd.get("champion") and t >= fin:
        fd["champion"] = {
            "entryId": int(alive[0]["entryId"]),
            "leaderNickname": str(alive[0].get("leaderNickname", "")),
            "memberNickname": str(alive[0].get("memberNickname", "")),
            "members": alive[0].get("members") or [],
            "settledAt": iso(now()),
        }
        fd["status"] = "finished"
        _pay_champion(fd, cfg)
        changed = True
    return changed


def _pay_champion(fd: dict, cfg: dict) -> None:
    floor = int(fd.get("floor", 1))
    gift = ""
    for row in (cfg.get("floors") or []):
        if int(row.get("floor", 0)) == floor:
            gift = str(row.get("championGiftItem", "") or "")
            break
    gift = gift or "塔%d礼包" % floor
    ch = fd.get("champion") or {}
    for e in fd.get("entries", []):
        if int(e.get("entryId", 0)) != int(ch.get("entryId", 0)):
            continue
        for member in (e.get("memberIds") or []):
            username = str((e.get("memberUsernames") or {}).get(str(member), "") or "")
            if not username:
                continue
            try:
                state = STORE.get(username)
                state.send_mail(
                    "生死塔 · 第%d层 塔主奖励" % floor,
                    "恭喜你成为第%d层塔主，奖励已附上。" % floor,
                    [{"name": gift, "count": 1}],
                )
                state.bump()
                state.flush()
            except Exception:
                pass


def _advance(cfg: dict) -> dict:
    ev = _event()
    with _LOCK:
        t = now()
        changed = False
        if ev is not None:
            for fd in (ev.get("floors") or {}).values():
                if isinstance(fd, dict) and _advance_floor(ev, fd, cfg, t):
                    changed = True
            if not ev.get("finished"):
                done = all(str(fd.get("status")) == "finished"
                           for fd in (ev.get("floors") or {}).values() if isinstance(fd, dict))
                if done and (ev.get("floors") or {}):
                    ev["finished"] = True
                    changed = True
        if changed:
            _flush()
    return ev or {}


# ---------------------------------------------------------------- 报名

def _room_of(ctx: Ctx):
    import handlers_boss_tower as bt
    return bt._find_my_room(ctx), bt


def _register(ctx: Ctx, floor: int) -> tuple[dict | None, str]:
    """把当前房间写成一条赛事 entry（同一天重复报名直接复用）。"""
    cfg = _cfg()
    win = _phase(cfg)
    if win["phase"] != "signup":
        return None, "报名已截止"
    if not win["inSignupWindow"]:
        return None, "当前不在报名时间（每日 %02d:%02d 开始报名）" % (
            int(cfg.get("signupStartHour", 20)), int(cfg.get("signupStartMinute", 30)))
    state = ctx.state
    uid = int(ctx.user.get("userId", 0) or 0)
    if uid <= 0:
        return None, "未登录"
    floor = int(floor or 0)
    if floor <= 0:
        return None, "请先选择楼层"
    room, _bt = _room_of(ctx)
    if not isinstance(room, dict):
        return None, "请先创建或加入队伍再报名"
    ev = _advance(cfg)
    fd = _floor_box(ev, floor)
    old = _my_entry(fd, uid)
    if isinstance(old, dict):
        return old, ""
    # 同一个房间只允许一条 entry：队友再点一次报名不该多出一支队
    room_id_now = int(room.get("roomId", 0) or 0)
    if room_id_now > 0:
        for row in fd.get("entries") or []:
            if int(row.get("roomId", 0) or 0) == room_id_now:
                return row, ""
    snap = treasure_mirror.snapshot(state)
    members = room.get("members") or []
    leader = next((m for m in members if bool(m.get("isLeader"))), members[0] if members else {})
    mate = next((m for m in members if m is not leader), None)
    leader_id = int(leader.get("userId", 0) or 0)
    ev["seq"] = int(ev.get("seq", 0)) + 1
    entry = {
        "entryId": int(ev["seq"]),
        "floor": floor,
        "roomId": int(room.get("roomId", 0) or 0),
        "leaderId": leader_id,
        "leaderNickname": str(leader.get("nickname", "") or state.nickname()),
        "memberNickname": str((mate or {}).get("nickname", "") or ""),
        "memberIds": [int(m.get("userId", 0) or 0) for m in members if int(m.get("userId", 0) or 0) > 0],
        "memberUsernames": {str(int(m.get("userId", 0) or 0)): str(m.get("username", "") or "")
                            for m in members},
        "members": [{"nickname": str(m.get("nickname", "")),
                     "level": int(m.get("level", 1) or 1),
                     "reincarnation": int(m.get("reincarnation", 0) or 0)}
                    for m in members],
        "username": state.username,
        "leaderSnap": snap,
        "memberSnap": {},
        "ai": False,
        "eliminated": False,
    }
    fd.setdefault("entries", []).append(entry)
    fd["status"] = fd.get("status") if fd.get("status") == "finished" else "registered"
    room["status"] = "registered"
    room["registeredAt"] = iso(now())
    _flush()
    box = state.meta.setdefault("shengsi", {})
    box["registeredDate"] = today()
    box["myRoomId"] = int(room.get("roomId", 0) or 0)
    state.bump()
    return entry, ""


# ---------------------------------------------------------------- 视图

def _match_view(ev: dict, fd: dict, m: dict, cfg: dict) -> dict:
    ss, st, gap, prep, rounds, fin = _timeline(cfg)
    rnd = int(m.get("round", 1))
    start_at = st + timedelta(seconds=(rnd - 1) * gap)
    in_prep = now() < start_at + timedelta(seconds=prep)
    status = "prep" if in_prep else "done"
    a = _entry_brief(_entry(fd, int(m.get("aEntryId", 0))))
    b = _entry_brief(_entry(fd, int(m.get("bEntryId", 0))))
    out = {
        "matchId": int(m.get("matchId", 0)),
        "floor": int(m.get("floor", 0)),
        "round": rnd,
        "status": status,
        "a": a,
        "b": b,
        "aEntryId": int(m.get("aEntryId", 0)),
        "bEntryId": int(m.get("bEntryId", 0)),
        "winnerEntryId": int(m.get("winnerEntryId", 0)),
        "hasBattleLog": True,
        "readyTotal": 4,
        "readyCount": 0,
        "prepEndAt": int((start_at + timedelta(seconds=prep)).timestamp()),
        "battleLog": [],
    }
    if not in_prep:
        out["battleLog"] = m.get("battleLog") or []
    return out


def _current_match(ev: dict, fd: dict, cfg: dict) -> dict | None:
    ss, st, gap, prep, rounds, fin = _timeline(cfg)
    t = now()
    if t < st:
        return None
    rnd = int((t - st).total_seconds() // gap) + 1
    rnd = max(1, min(rounds, rnd))
    rows = (fd.get("matches") or {}).get(str(rnd)) or []
    return rows[0] if rows else None


def _my_match(ev: dict, fd: dict, entry_id: int, cfg: dict) -> dict | None:
    """当前这一轮里「我」参与的那场；打完了就返回最后一场（用于看回放）。"""
    ss, st, gap, prep, rounds, fin = _timeline(cfg)
    t = now()
    if t < st:
        return None
    rnd = int((t - st).total_seconds() // gap) + 1
    rnd = max(1, min(rounds, rnd))
    found = None
    for r in range(1, rnd + 1):
        for m in (fd.get("matches") or {}).get(str(r)) or []:
            if entry_id in (int(m.get("aEntryId", 0)), int(m.get("bEntryId", 0))):
                found = m
    return found


def _floor_status(fd: dict, cfg: dict) -> str:
    win = _phase(cfg)
    if fd.get("champion"):
        return "finished"
    if win["phase"] == "battle":
        return "running"
    if win["phase"] == "finished":
        return "ended"
    return "registered" if (fd.get("entries") or []) else "waiting"


# ---------------------------------------------------------------- 路由

@route("GET", "/shengsi-tower/window")
def shengsi_window(ctx: Ctx) -> dict:
    cfg = _cfg()
    win = _phase(cfg)
    state = ctx.state
    box = state.meta.setdefault("shengsi", {})
    ev = _advance(cfg)
    uid = int(ctx.user.get("userId", 0) or 0)
    my_floor = int((box.get("signup") or {}).get("floor", 0) or box.get("floor", 0) or 0)
    entry = None
    if uid > 0:
        for fd in (ev.get("floors") or {}).values():
            hit = _my_entry(fd, uid)
            if hit is not None:
                entry = _entry_brief(hit, with_snap=False)
                my_floor = int(hit.get("floor", my_floor))
                break
    payload = dict(win)
    payload.update(
        featureEnabled=bool(cfg.get("featureEnabled", True)),
        signupRangeText=str(cfg.get("signupRangeText", "") or ""),
        myReincarnation=int(state.player.get("reincarnation", 0) or 0),
        myFloor=my_floor,
        mySignup=box.get("signup"),
        myEntry=entry,
    )
    return ok(**payload)


@route("GET", "/shengsi-tower/overview")
def shengsi_overview(ctx: Ctx) -> dict:
    import handlers_boss_tower as bt
    cfg = _cfg()
    win = _phase(cfg)
    ev = _advance(cfg)
    state = ctx.state
    uid = int(ctx.user.get("userId", 0) or 0)
    floors = []
    my_entry = None
    for row in (cfg.get("floors") or []):
        fno = int(row.get("floor", 0) or 0)
        fd = (ev.get("floors") or {}).get(str(fno)) or {}
        item = dict(row)
        item["registeredCount"] = len([e for e in (fd.get("entries") or []) if not e.get("ai")])
        item["status"] = _floor_status(fd, cfg) if fd else ("waiting" if win["phase"] == "signup" else "ended")
        item["capacity"] = int(row.get("capacity", _DEFAULT_CAPACITY) or _DEFAULT_CAPACITY)
        item["champion"] = (fd.get("champion") or {})
        item["teams"] = [_entry_team_row(e, uid) for e in (fd.get("entries") or []) if not e.get("ai")]
        floors.append(item)
        if my_entry is None and uid > 0 and fd:
            hit = _my_entry(fd, uid)
            if hit is not None:
                my_entry = _entry_brief(hit, with_snap=False)
    room = bt._find_my_room(ctx)
    view = bt._room_view(room, uid) if isinstance(room, dict) else {}
    payload = dict(win)
    payload.update(
        featureEnabled=bool(cfg.get("featureEnabled", True)),
        config=cfg,
        floors=floors,
        mySignup=state.meta.get("shengsi", {}).get("signup"),
        myRoom=view,
        myEntry=my_entry,
        pinnedSnapshot=(state.meta.get("shengsi", {}) or {}).get("pinnedSnapshot", {}),
        myReincarnation=int(state.player.get("reincarnation", 0) or 0),
    )
    return ok(**payload)


@route("GET", "/shengsi-tower/floor")
def shengsi_floor(ctx: Ctx) -> dict:
    import handlers_boss_tower as bt
    cfg = _cfg()
    floor = max(1, ctx.qi("floor", 1))
    ev = _advance(cfg)
    fd = (ev.get("floors") or {}).get(str(floor)) or {}
    uid = int(ctx.user.get("userId", 0) or 0)
    cfg_row = next((r for r in (cfg.get("floors") or []) if int(r.get("floor", 0)) == floor), {})
    rooms = []
    with bt._ROOM_LOCK:
        for room in bt._rooms()["rooms"].values():
            if int(room.get("floor", 0)) == floor and len(room.get("members") or []) > 0:
                rooms.append(bt._room_view(room, uid))
    return ok(
        floor=floor,
        floorConfig=cfg_row,
        config=cfg_row,
        status=_floor_status(fd, cfg) if fd else "waiting",
        teams=[_entry_team_row(e, uid) for e in (fd.get("entries") or []) if not e.get("ai")],
        allTeams=[_entry_team_row(e, uid) for e in (fd.get("entries") or [])],
        registeredCount=len([e for e in (fd.get("entries") or []) if not e.get("ai")]),
        capacity=int(cfg_row.get("capacity", _DEFAULT_CAPACITY) or _DEFAULT_CAPACITY),
        champion=(fd.get("champion") or {}),
        openRooms=rooms,
        eventKey=ev.get("key", ""),
        eventFinished=bool(ev.get("finished")),
    )


@route("POST", "/shengsi-tower/round/matchup")
def shengsi_round_matchup(ctx: Ctx) -> dict:
    cfg = _cfg()
    win = _phase(cfg)
    if win["phase"] == "signup":
        return ok(matchup=[], round=0, phase="signup", myEntryId=0, match=None,
                  **{k: win[k] for k in ("eventKey", "serverNow", "pastStart", "signupStartAt", "signupEndAt")})
    ev = _advance(cfg)
    uid = int(ctx.user.get("userId", 0) or 0)
    my_entry = None
    my_fd = None
    for fd in (ev.get("floors") or {}).values():
        hit = _my_entry(fd, uid) if uid > 0 else None
        if hit is not None:
            my_entry, my_fd = hit, fd
            break
    floor = int((my_entry or {}).get("floor", 0) or ctx.i("floor", 0) or 0)
    if my_fd is None and floor > 0:
        my_fd = (ev.get("floors") or {}).get(str(floor))
    match = None
    entry_id = int((my_entry or {}).get("entryId", 0) or 0)
    if isinstance(my_fd, dict) and entry_id > 0:
        m = _my_match(ev, my_fd, entry_id, cfg)
        if m:
            match = _match_view(ev, my_fd, m, cfg)
    elif isinstance(my_fd, dict):
        m = _current_match(ev, my_fd, cfg)
        if m:
            match = _match_view(ev, my_fd, m, cfg)
    round_no = int((match or {}).get("round", 0) or 0)
    return ok(matchup=[match] if match else [], match=match, round=round_no,
              myEntryId=entry_id, myEntry=_entry_brief(my_entry),
              eventKey=ev.get("key", ""), phase=win["phase"], serverNow=win["serverNow"])


@route("GET", "/shengsi-tower/round/matchup")
def shengsi_round_matchup_get(ctx: Ctx) -> dict:
    return shengsi_round_matchup(ctx)


@route("GET", "/shengsi-tower/match")
def shengsi_match_detail(ctx: Ctx) -> dict:
    cfg = _cfg()
    ev = _advance(cfg)
    mid = ctx.qi("matchId", 0)
    uid = int(ctx.user.get("userId", 0) or 0)
    my_entry_id = 0
    for fd in (ev.get("floors") or {}).values():
        hit = _my_entry(fd, uid) if uid > 0 else None
        if hit is not None:
            my_entry_id = int(hit.get("entryId", 0))
        for rows in (fd.get("matches") or {}).values():
            for m in rows or []:
                if int(m.get("matchId", 0)) != int(mid):
                    continue
                view = _match_view(ev, fd, m, cfg)
                view["battleLog"] = m.get("battleLog") or []
                view["status"] = "done"
                return ok(battle=view, match=view, myEntryId=my_entry_id)
    return fail("战报不存在")


@route("POST", "/shengsi-tower/round/ready")
def shengsi_round_ready(ctx: Ctx) -> dict:
    state = ctx.state
    box = state.meta.setdefault("shengsi", {})
    rnd = ctx.i("round", 0)
    box["readyRound"] = rnd
    state.bump()
    return ok(ready=True, round=rnd)


@route("POST", "/shengsi-tower/round/stance")
def shengsi_round_stance(ctx: Ctx) -> dict:
    stance = ctx.s("stance") or "attack"
    state = ctx.state
    state.meta.setdefault("shengsi", {})["stance"] = stance
    if stance in ("attack", "defense"):
        state.player["battle_stance_player"] = stance
    state.bump()
    return ok(stance=stance)


@route("GET", "/shengsi-tower/floor/battles")
def shengsi_floor_battles(ctx: Ctx) -> dict:
    cfg = _cfg()
    ev = _advance(cfg)
    floor = max(1, ctx.qi("floor", 1))
    fd = (ev.get("floors") or {}).get(str(floor)) or {}
    uid = int(ctx.user.get("userId", 0) or 0)
    my_entry = _my_entry(fd, uid) if uid > 0 else None
    rows = []
    for rnd_rows in (fd.get("matches") or {}).values():
        for m in rnd_rows or []:
            rows.append({
                "matchId": int(m.get("matchId", 0)),
                "floor": int(m.get("floor", floor)),
                "round": int(m.get("round", 0)),
                "aEntryId": int(m.get("aEntryId", 0)),
                "bEntryId": int(m.get("bEntryId", 0)),
                "winnerEntryId": int(m.get("winnerEntryId", 0)),
                "a": _entry_brief(_entry(fd, int(m.get("aEntryId", 0))), with_snap=False),
                "b": _entry_brief(_entry(fd, int(m.get("bEntryId", 0))), with_snap=False),
                "hasBattleLog": True,
                "createdAt": m.get("createdAt", ""),
            })
    return ok(floor=floor, battles=rows, eventKey=ev.get("key", ""),
              finished=bool(ev.get("finished")),
              myEntryId=int((my_entry or {}).get("entryId", 0) or 0))


@route("GET", "/shengsi-tower/feed")
def shengsi_feed(ctx: Ctx) -> dict:
    cfg = _cfg()
    ev = _advance(cfg)
    lines = []
    for fno, fd in (ev.get("floors") or {}).items():
        if not isinstance(fd, dict):
            continue
        for rnd_rows in (fd.get("matches") or {}).values():
            for m in rnd_rows or []:
                a = _entry(fd, int(m.get("aEntryId", 0))) or {}
                b = _entry(fd, int(m.get("bEntryId", 0))) or {}
                win = _entry(fd, int(m.get("winnerEntryId", 0))) or {}
                lines.append({
                    "floor": int(fno),
                    "message": "第%d轮：%s 击败 %s，%s 晋级" % (
                        int(m.get("round", 0)),
                        str(win.get("leaderNickname", "?")),
                        str((b if win.get("entryId") == a.get("entryId") else a).get("leaderNickname", "?")),
                        str(win.get("leaderNickname", "?")),
                    ),
                    "createdAt": str(m.get("createdAt", "")),
                })
        ch = fd.get("champion") or {}
        if ch:
            lines.append({
                "floor": int(fno),
                "message": "%s 成为第%s层塔主" % (str(ch.get("leaderNickname", "?")), fno),
                "createdAt": str(ch.get("settledAt", "")),
            })
    return ok(lines=lines, eventKey=ev.get("key", ""))


@route("POST", "/shengsi-tower/feed")
def shengsi_feed_post(ctx: Ctx) -> dict:
    return shengsi_feed(ctx)


@route("POST", "/shengsi-tower/register")
def shengsi_register(ctx: Ctx) -> dict:
    import handlers_boss_tower as bt
    cfg = _cfg()
    state = ctx.state
    box = state.meta.setdefault("shengsi", {})
    uid = int(ctx.user.get("userId", 0) or 0)
    # 已经报过名就直接回原来那条 entry，重复点按钮不该报错
    ev_now = _event(create=False) or {}
    for fd in (ev_now.get("floors") or {}).values():
        hit = _my_entry(fd, uid) if uid > 0 else None
        if hit is not None:
            room = bt._find_my_room(ctx)
            view = bt._room_view(room, uid) if isinstance(room, dict) else {}
            return ok(room=view, myEntry=_entry_brief(hit),
                      myEntryId=int(hit.get("entryId", 0)), duplicate=True)
    cap = max(1, int(cfg.get("dailyLimitPerUser", 1) or 1))
    if box.get("registeredDate") == today() and int(box.get("registerCount", 0)) >= cap:
        return fail("今日报名次数已用完")
    floor = ctx.i("floor", 0) or int((bt._find_my_room(ctx) or {}).get("floor", 0) or 0)
    entry, err = _register(ctx, floor)
    if err:
        return fail(err)
    room = bt._find_my_room(ctx)
    view = bt._room_view(room, int(ctx.user.get("userId", 0) or 0)) if isinstance(room, dict) else {}
    box["registerCount"] = int(box.get("registerCount", 0)) + 1
    box["floor"] = int(floor)
    # 参与奖：报名即发，一天一份。塔主礼包在结算时另外发。
    gift = str(cfg.get("participateGiftItem", "") or "")
    if gift and bool(cfg.get("participateGiftEnabled", False)) and box.get("participateDate") != today():
        from core import client_has_item
        if client_has_item(gift):
            state.send_mail("生死塔参与礼包", "报名成功，参与奖励已附上。",
                            [{"name": gift, "count": 1}])
            box["participateDate"] = today()
    state.bump()
    return ok(room=view, myEntry=_entry_brief(entry), myEntryId=int(entry.get("entryId", 0)))


@route("POST", "/shengsi-tower/unregister")
def shengsi_unregister(ctx: Ctx) -> dict:
    import handlers_boss_tower as bt
    cfg = _cfg()
    win = _phase(cfg)
    if win["pastStart"]:
        return fail("已开赛，不能取消报名")
    state = ctx.state
    uid = int(ctx.user.get("userId", 0) or 0)
    ev = _event(create=False) or {}
    for fd in (ev.get("floors") or {}).values():
        hit = _my_entry(fd, uid) if uid > 0 else None
        if hit is not None:
            fd["entries"] = [e for e in fd.get("entries", []) if e is not hit]
    _flush()
    room = bt._find_my_room(ctx)
    if isinstance(room, dict):
        room["members"] = [m for m in room.get("members", []) if int(m.get("userId", 0)) != uid]
        if not room["members"]:
            bt._rooms()["rooms"].pop(str(room.get("roomId")), None)
        bt._flush_rooms()
    box = state.meta.setdefault("shengsi", {})
    box["registeredDate"] = ""
    box["myRoomId"] = 0
    state.bump()
    return ok(unregistered=True, message="已取消报名")


# ---------------------------------------------------------------- 仙魔镜像（第 4 项）

@route("GET", "/xianmo/mirror")
def xianmo_mirror(ctx: Ctx) -> dict:
    """仙魔战场的「镜湖照影」：给玩家看一个与自己同档的对手。

    老实现固定返回 mirror=None，客户端 `xianmo_battle_processor` 拿不到就用
    本地随机造一个 —— 两边算出来的对手当然不一样。这里按当天固定 seed 生成，
    同一天反复刷新看到的是同一个人，和 `/xianmo/adventure` 的镜像战斗口径一致。
    """
    cfg = RULES.m("xianmo")
    state = ctx.state
    box = state.meta.setdefault("xianmo", {})
    day = today()
    mirror = box.get("mirror")
    if not isinstance(mirror, dict) or str(mirror.get("date", "")) != day:
        rng = random.Random("%s-%s-mirror" % (state.username, day))
        p = state.player
        lv = int(p.get("level", 1) or 1)
        scale = rng.uniform(0.9, 1.12)
        faction = str(box.get("faction", "xian") or "xian")
        names = ["镜湖剑客", "无相道人", "血河魔君", "青罗仙姬", "断岳散人", "幽篁魔尊"]
        mirror = {
            "date": day,
            "id": rng.randint(100000, 999999),
            "nickname": rng.choice(names),
            "level": lv,
            "hp": max(1, int(_num(p.get("final_max_hp", p.get("max_hp", 100)), 100) * scale)),
            "attack": max(1, int(_num(p.get("final_attack", p.get("attack_base", 10)), 10) * scale)),
            "defense": max(1, int(_num(p.get("final_defense", p.get("defense_base", 10)), 10) * scale)),
            "agility": max(1, int(_num(p.get("final_agility", p.get("agility_base", 10)), 10) * scale)),
            "element": rng.choice(["金", "木", "水", "火", "土"]),
            "faction": "mo" if faction == "xian" else "xian",
            "pet": None,
            "updatedAt": iso(now()),
        }
        box["mirror"] = mirror
        state.bump()
    out = dict(mirror)
    out.pop("date", None)
    return ok(mirror=out, hasMirror=True, featureEnabled=bool(cfg.get("featureEnabled", True)))
