#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""藏宝洞轮次调度（服务端权威）。

原版口径（抓包 build/orig-capture/capture/orig/api_treasure_status.json 实证）：
  - 开放 9:00–24:00（上海时区），**每整点开一轮**；
  - 每轮 = 报名 10 分钟 + 争夺 10 分钟，剩 40 分钟空窗等下轮；
  - 一轮固定 3 个秘箱（chest_no 1..3），每人每轮只能择定 1 个；
  - round_key 形如 treasure_20261028_19（日期 + 整点小时）；
  - round_index_today 从 9:00 的「第 1 轮」起算（10:00=2、13:00=5 …）；
  - 结算后按箱写一条全服战报 treasure_cave_chest_result，开局写
    treasure_cave_round_start（昵称「天机阁」，系统口径）。

我们这边原来 /treasure/status 是写死的桩：round 恒 null、/treasure/join 不记
名次也不进轮次，所以公屏永远不会有藏宝洞的战报。这里补上真正的轮次状态机：

  state/treasure_round.json
    lastRoundId / lastChestId   自增 id
    current                     当前这一轮（含 joins / activity / 内部时间戳）
    last                        上一轮结算快照（给客户端「上轮得主」用）

调度靠一个 20 秒的守护线程 tick()，另在每次 /treasure/status 前也 tick 一次
（服务重启、线程被杀时，玩家一进界面立刻把轮次对齐）。
"""
from __future__ import annotations

import random
import threading
import zlib
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import CN_TZ, RULES, STATE_DIR, STORE, dump_json, load_json, now, rnd_id

ROUND_PATH: Path = STATE_DIR / "treasure_round.json"

OPEN_START_HOUR = 9
OPEN_END_HOUR_EXCLUSIVE = 24
SIGNUP_SECONDS = 600
BATTLE_SECONDS = 600
CHEST_COUNT = 3
ACTIVITY_MAX = 80            # 单轮活动日志上限
HISTORY_MAX = 8              # 已结算轮次留档轮数（战报要能回查，不能一轮就丢）
SETTLE_QUIET_AFTER = 1800    # 战斗结束超过 30 分钟才结算的（服务停过），静默补算不刷屏

SYSTEM_NICKNAME = "天机阁"

# ---- 文案（逐字对齐原版抓包） ----
TEXT_ROUND_START = ("【藏宝洞】传闻仙界灵脉一隅幽窟洞开，雾锁霞明之中古箱三列，"
                    "宝气氤氲四溢，引得八方修士虎视眈眈，暗潮汹涌。（今日第%d轮）")
TEXT_CHEST_RESULT = "【藏宝洞】今日第%d轮，%d号秘箱之争尘埃落定，【%s】技压同列，夺得此箱机缘。"
TEXT_LOG_START = "【烽烟】藏宝洞第「%s」轮已现，三座古箱并列洞前，气机牵引四方修士。"
TEXT_LOG_JOIN = "【入阵】%s选择了某个秘箱入列，戾气与机缘同聚。"
TEXT_LOG_SIGNUP_END = "【闭门】阵前列名已止，各箱之争转入天机幻战，外人只见灵光吞吐。"
TEXT_LOG_BATTLE = "【鏖战】镜像争杀正炽，胜负未分，洞府深处隐有雷鸣。"
TEXT_LOG_WIN = "【落定】%d号秘箱尘埃落定，【%s】技压同列，夺得此箱机缘。"
TEXT_LOG_EMPTY_CHEST = "【空箱】%d号秘箱无人问津，宝光自散，静待下一轮开洞。"
TEXT_LOG_BRACKET = "【签表】%d号秘箱 %d 人入阵，天机幻战单败签表共 %d 场，镜像争杀已毕。"

_LOCK = threading.RLock()
_DB: dict | None = None
_SCHED: threading.Thread | None = None


# ---------------------------------------------------------------- 时间与格式化

def _cfg() -> dict:
    return RULES.m("treasure") or {}


def _signup_seconds() -> int:
    try:
        return max(60, int(_cfg().get("signupMinutes", 10) or 10) * 60)
    except (TypeError, ValueError):
        return SIGNUP_SECONDS


def _battle_seconds() -> int:
    try:
        return max(60, int(_cfg().get("battleMinutes", 10) or 10) * 60)
    except (TypeError, ValueError):
        return BATTLE_SECONDS


def _zulu(dt: datetime) -> str:
    """原版时间戳口径：UTC + 毫秒（2026-10-25T05:00:00.000Z）。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _in_open_window(t: datetime) -> bool:
    return OPEN_START_HOUR <= t.hour < OPEN_END_HOUR_EXCLUSIVE


def _slot_for(t: datetime) -> dict | None:
    """当前时刻属于哪个整点轮（不在开放窗口返回 None）。"""
    if not _in_open_window(t):
        return None
    start = t.replace(minute=0, second=0, microsecond=0)
    return {
        "key": "treasure_%s_%02d" % (start.strftime("%Y%m%d"), start.hour),
        "start": start,
        "index": start.hour - OPEN_START_HOUR + 1,
    }


def _next_start_after(t: datetime) -> datetime:
    """下一次开局时间（当天下一整点，或次日 9:00）。"""
    if _in_open_window(t):
        nxt = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if nxt.hour < OPEN_END_HOUR_EXCLUSIVE:
            return nxt
        return (t + timedelta(days=1)).replace(hour=OPEN_START_HOUR, minute=0, second=0, microsecond=0)
    if t.hour < OPEN_START_HOUR:
        return t.replace(hour=OPEN_START_HOUR, minute=0, second=0, microsecond=0)
    return (t + timedelta(days=1)).replace(hour=OPEN_START_HOUR, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------- 存储

def _normalize(raw) -> dict:
    db = raw if isinstance(raw, dict) else {}
    out = {
        "lastRoundId": int(db.get("lastRoundId", 2900) or 2900),
        "lastChestId": int(db.get("lastChestId", 8600) or 8600),
        "current": db.get("current") if isinstance(db.get("current"), dict) else None,
        "last": db.get("last") if isinstance(db.get("last"), dict) else None,
        "history": db.get("history") if isinstance(db.get("history"), list) else [],
    }
    return out


def _load() -> dict:
    global _DB
    if _DB is None:
        _DB = _normalize(load_json(ROUND_PATH, {}))
    return _DB


def _save(db: dict) -> None:
    try:
        dump_json(ROUND_PATH, db)
    except OSError:
        pass


def _mark(db: dict) -> None:
    db["_dirty"] = True


def _flush(db: dict) -> None:
    if db.pop("_dirty", False):
        _save(db)


def _log(round_: dict, text: str, t: datetime | None = None) -> None:
    rows = round_.setdefault("activity", [])
    rows.append({"at": _zulu(t or now()), "message": text})
    if len(rows) > ACTIVITY_MAX:
        del rows[:-ACTIVITY_MAX]


# ---------------------------------------------------------------- 轮次状态机

def _new_round(db: dict, slot: dict, t: datetime) -> dict:
    db["lastRoundId"] = int(db.get("lastRoundId", 2900) or 2900) + 1
    rid = db["lastRoundId"]
    start = slot["start"]
    signup_end = start + timedelta(seconds=_signup_seconds())
    battle_end = signup_end + timedelta(seconds=_battle_seconds())
    chests = []
    for no in range(1, CHEST_COUNT + 1):
        db["lastChestId"] = int(db.get("lastChestId", 8600) or 8600) + 1
        chests.append({
            "id": db["lastChestId"],
            "round_id": rid,
            "chest_no": no,
            "status": "signup_open",
            "winner_user_id": None,
            "winner_name": None,
            "reward_mail_id": None,
            "battle_log_ref": None,
            "created_at": _zulu(start),
            "updated_at": _zulu(start),
        })
    rnd = {
        "id": rid,
        "round_key": slot["key"],
        "round_index_today": int(slot["index"]),
        "starts_at": _zulu(start),
        "signup_ends_at": _zulu(signup_end),
        "battle_ends_at": _zulu(battle_end),
        "status": "signup_open",
        "settled": False,
        "winner_computed": 0,
        "created_at": _zulu(start),
        "updated_at": _zulu(t),
        "start_ts": _ts(start),
        "signup_end_ts": _ts(signup_end),
        "battle_end_ts": _ts(battle_end),
        "chests": chests,
        "joins": {},
        "activity": [],
    }
    _log(rnd, TEXT_LOG_START % slot["key"], start)
    db["current"] = rnd
    _mark(db)
    return rnd


def _advance(rnd: dict, t: datetime) -> bool:
    """报名 → 争夺 的阶段推进。返回是否发生变化。"""
    if rnd.get("settled"):
        return False
    if rnd.get("status") != "signup_open":
        return False
    if _ts(t) < float(rnd.get("signup_end_ts") or 0):
        return False
    rnd["status"] = "battle_running"
    rnd["updated_at"] = _zulu(t)
    for chest in rnd.get("chests", []):
        if chest.get("status") == "signup_open":
            chest["status"] = "battle_running"
            chest["updated_at"] = _zulu(t)
    _log(rnd, TEXT_LOG_SIGNUP_END, t)
    _log(rnd, TEXT_LOG_BATTLE, t)
    return True


def _pick_winner(candidates: list[dict]) -> dict:
    """兜底口径（签表跑挂时用）：按报名时锁定的镜像战力加权抽样。"""
    weights = []
    for row in candidates:
        try:
            weights.append(max(1, int(row.get("power", 1) or 1)))
        except (TypeError, ValueError):
            weights.append(1)
    return random.choices(candidates, weights=weights, k=1)[0]


def _grant_chest(account: str, round_index: int, chest_no: int,
                 round_id: int = 0) -> dict | None:
    """把藏宝匣塞进获胜者背包（每个箱子独立 token，开箱时按 token 核销）。"""
    try:
        st = STORE.get(account)
    except Exception:
        return None
    token = "tc_" + rnd_id("")
    st.items.append({
        "id": rnd_id("i"),
        "name": "藏宝匣",
        "count": 1,
        "item_type": "treasure_chest",
        # round_id / chest_no 只用于「这箱子发过没」的核对，开箱仍按 token
        "custom_data": {"treasure_token": token,
                        "treasure_round": int(round_id or 0),
                        "treasure_chest_no": int(chest_no or 0)},
    })
    mail = st.send_mail(
        "藏宝洞夺宝所得",
        "恭喜你在藏宝洞今日第%d轮夺得%d号秘箱，藏宝匣已放入背包，使用即可开奖。"
        % (round_index, chest_no),
    )
    st.bump()
    # 结算跑在调度线程里，没有请求收尾去落盘 —— 这里必须自己 flush，
    # 否则服务一重启，赢家的藏宝匣和邮件就只在内存里蒸发了。
    st.flush()
    return mail


def _has_grant(account: str, round_id: int, chest_no: int) -> bool:
    if not account or not round_id:
        return False
    try:
        st = STORE.get(account)
    except Exception:
        return False
    for item in (st.items or []):
        custom = item.get("custom_data")
        if not isinstance(custom, dict):
            continue
        if (int(custom.get("treasure_round", 0) or 0) == int(round_id)
                and int(custom.get("treasure_chest_no", 0) or 0) == int(chest_no)):
            return True
    return False


def _ensure_grants(rnd: dict) -> bool:
    """结算过但奖励没真正到手（当时没落盘/发奖时崩了）的箱子，补发一次。

    判据不是 reward_mail_id（内存里设过、重启就丢了），而是直接查赢家背包里
    有没有「第 N 轮第 M 号箱」的藏宝匣，缺了才补。
    """
    if not rnd.get("settled"):
        return False
    rid = int(rnd.get("id", 0) or 0)
    index = int(rnd.get("round_index_today", 0) or 0)
    changed = False
    for chest in rnd.get("chests", []):
        if not chest.get("winner_name"):
            continue
        account = str(chest.get("winner_user_id") or "")
        no = int(chest.get("chest_no", 0) or 0)
        if not account or not no:
            continue
        if _has_grant(account, rid, no):
            continue
        try:
            mail = _grant_chest(account, index, no, rid)
            if isinstance(mail, dict):
                chest["reward_mail_id"] = int(mail.get("id", 0) or 0)
            _log(rnd, "【补发】%d号秘箱奖励已补发至【%s】背包。" % (no, chest.get("winner_name")))
            changed = True
        except Exception:
            traceback.print_exc()
    return changed


def _uid(account: str) -> int:
    """账号 → 数字 userId（战报里「甲 vs 乙」靠它区分，0 会被客户端当成轮空）。"""
    try:
        row = (STORE.accounts or {}).get(account) or {}
        uid = int(row.get("userId", 0) or 0)
        if uid > 0:
            return uid
    except Exception:
        pass
    # 账号索引里没有（比如已注销/测试号），给个稳定的正数，别退化成 0
    try:
        return 100000 + (zlib.crc32(str(account).encode("utf-8")) % 800000)
    except Exception:
        return 1


def _settle(db: dict, rnd: dict, t: datetime) -> None:
    from broadcasts import append as broadcast

    quiet = (_ts(t) - float(rnd.get("battle_end_ts") or 0)) > SETTLE_QUIET_AFTER
    index = int(rnd.get("round_index_today", 0) or 0)
    joins = rnd.get("joins") or {}
    by_chest: dict[int, list[dict]] = {}
    for account, info in joins.items():
        if not isinstance(info, dict):
            continue
        try:
            no = int(info.get("chest_no", 0) or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= no <= CHEST_COUNT:
            mirror = info.get("mirror") if isinstance(info.get("mirror"), dict) else {}
            by_chest.setdefault(no, []).append({
                "account": account,
                "user_id": _uid(account),
                "name": str(info.get("name", "") or account),
                "power": info.get("power", 1),
                "mirror": mirror,
                # 报名时定格的四维（人物 + 出战宠），争夺就按这份打
                "combat": (mirror.get("combat") if isinstance(mirror.get("combat"), dict) else {}),
            })

    import treasure_battle

    winners = []
    for chest in rnd.get("chests", []):
        no = int(chest.get("chest_no", 0) or 0)
        pool = by_chest.get(no) or []
        chest["status"] = "finished"
        chest["updated_at"] = _zulu(t)
        if not pool:
            _log(rnd, TEXT_LOG_EMPTY_CHEST % no, t)
            continue
        # 单败签表：报名顺序两两配对，奇数人末位轮空；每场都是镜像对镜像，
        # 战报逐场存进 chest["matches"]，客户端按 chestId 拉回去看回放。
        try:
            result = treasure_battle.run_bracket(pool, seed=int(rnd.get("id", 0) or 0) * 100 + no)
        except Exception:
            traceback.print_exc()
            result = {"winner": _pick_winner(pool), "matches": []}
        win = result.get("winner") or _pick_winner(pool)
        chest["matches"] = result.get("matches") or []
        chest["battle_log_ref"] = "chest_%d" % int(chest.get("id", 0) or 0)
        if len(pool) > 1:
            _log(rnd, TEXT_LOG_BRACKET % (no, len(pool), len(chest["matches"])), t)
        chest["winner_name"] = win["name"]
        chest["winner_user_id"] = win["account"]
        try:
            mail = _grant_chest(win["account"], index, no, int(rnd.get("id", 0) or 0))
            if isinstance(mail, dict):
                chest["reward_mail_id"] = int(mail.get("id", 0) or 0)
        except Exception:
            traceback.print_exc()
        _log(rnd, TEXT_LOG_WIN % (no, win["name"]), t)
        winners.append({"chest_no": no, "winner_name": win["name"],
                        "winner_user_id": win["account"]})
        if not quiet:
            try:
                broadcast(
                    "treasure_cave_chest_result",
                    SYSTEM_NICKNAME,
                    TEXT_CHEST_RESULT % (index, no, win["name"]),
                    {"chestNo": no, "roundId": int(rnd.get("id", 0) or 0),
                     "winnerName": win["name"], "roundIndexToday": index},
                )
            except Exception:
                traceback.print_exc()

    rnd["settled"] = True
    rnd["status"] = "finished"
    rnd["winner_computed"] = 1
    rnd["settled_at"] = _zulu(t)
    rnd["updated_at"] = _zulu(t)
    db["last"] = {
        "round_key": rnd.get("round_key"),
        "round_index_today": index,
        "settled_at": _zulu(t),
        "winners": winners,
    }
    _archive(db, rnd)
    _mark(db)


def _archive(db: dict, rnd: dict) -> None:
    """把结算完的轮次留档（去掉 joins/activity，只留箱子与逐场战报）。

    current 整点一换就没了，战报得能回查，所以单独留最近 HISTORY_MAX 轮。
    """
    slim = {
        "id": int(rnd.get("id", 0) or 0),
        "round_key": rnd.get("round_key"),
        "round_index_today": int(rnd.get("round_index_today", 0) or 0),
        "settled_at": rnd.get("settled_at"),
        "chests": [],
    }
    for chest in rnd.get("chests", []):
        slim["chests"].append({
            "id": int(chest.get("id", 0) or 0),
            "round_id": int(chest.get("round_id", 0) or 0),
            "chest_no": int(chest.get("chest_no", 0) or 0),
            "status": chest.get("status"),
            "winner_user_id": chest.get("winner_user_id"),
            "winner_name": chest.get("winner_name"),
            "reward_mail_id": chest.get("reward_mail_id"),
            "battle_log_ref": chest.get("battle_log_ref"),
            "matches": chest.get("matches") or [],
        })
    hist = db.get("history")
    if not isinstance(hist, list):
        hist = []
    rid = slim["id"]
    hist = [h for h in hist if int((h or {}).get("id", 0) or 0) != rid]   # 幂等
    hist.append(slim)
    if len(hist) > HISTORY_MAX:
        del hist[:-HISTORY_MAX]
    db["history"] = hist


def tick(now_dt: datetime | None = None) -> dict:
    """推进轮次状态机（幂等，随便调）。"""
    with _LOCK:
        db = _load()
        t = now_dt or now()
        cur = db.get("current") if isinstance(db.get("current"), dict) else None

        # ① 该结算的结算（战斗已过点）
        if cur and not cur.get("settled") and _ts(t) >= float(cur.get("battle_end_ts") or 0):
            _settle(db, cur, t)

        # ② 该开新轮的开新轮
        slot = _slot_for(t)
        cur = db.get("current") if isinstance(db.get("current"), dict) else None
        if slot and (cur is None or cur.get("round_key") != slot["key"]):
            if cur is not None and not cur.get("settled"):
                _settle(db, cur, t)
            cur = _new_round(db, slot, t)
            # 开局战报（原版每轮都推；服务刚起来补轮时不补推旧局）
            if _ts(t) - float(cur.get("start_ts") or 0) <= 900:
                try:
                    from broadcasts import append as broadcast
                    broadcast(
                        "treasure_cave_round_start",
                        SYSTEM_NICKNAME,
                        TEXT_ROUND_START % int(cur.get("round_index_today", 0) or 0),
                        {"roundId": int(cur.get("id", 0) or 0),
                         "roundKey": cur.get("round_key"),
                         "roundIndexToday": int(cur.get("round_index_today", 0) or 0)},
                    )
                except Exception:
                    traceback.print_exc()

        # ③ 阶段推进
        if cur and _advance(cur, t):
            _mark(db)

        # ④ 结算过但奖励没落地的，补发（服务重启后自愈）
        if cur and _ensure_grants(cur):
            _mark(db)

        _flush(db)
        return db


# ---------------------------------------------------------------- 对外数据

def _public_round(rnd: dict) -> dict:
    keys = ("id", "round_key", "round_index_today", "starts_at", "signup_ends_at",
            "battle_ends_at", "status", "winner_computed", "created_at", "updated_at")
    return {k: rnd.get(k) for k in keys}


def _participants_of(rnd: dict, chest_no: int) -> int:
    joins = rnd.get("joins") or {}
    n = 0
    for info in joins.values():
        if isinstance(info, dict) and int(info.get("chest_no", 0) or 0) == chest_no:
            n += 1
    return n


def _join_info(state, dt: datetime) -> dict:
    box = state.meta.setdefault("treasure", {})
    day = dt.strftime("%Y-%m-%d")
    record = box.get("joinRounds")
    if not isinstance(record, dict) or record.get("date") != day:
        record = {"date": day, "keys": []}
        box["joinRounds"] = record
    keys = record.get("keys")
    if not isinstance(keys, list):
        keys = []
        record["keys"] = keys
    return record


def _daily_limit(state=None) -> int:
    """每日入阵次数：按 VIP 档位取 runtime-config.treasure.vipDailyJoinLimits。

    配置是 [2, 4, 5, 8]（非VIP / 青铜 / 白银 / 黄金），对应客户端
    treasure_cave_ui.gd 帮助里「非VIP2次、青铜4次、白银5次、黄金8次」。
    以前这里固定取 limits[0]，等于所有人的上限都是 2 次，VIP 那三档从来没生效过。
    """
    limits = _cfg().get("vipDailyJoinLimits") or [2]
    if not isinstance(limits, (list, tuple)) or not limits:
        limits = [2]
    tier = 0
    if state is not None:
        try:
            tier = int(state.vip_tier())
        except Exception:
            tier = 0
    idx = max(0, min(tier, len(limits) - 1))
    try:
        return max(1, int(limits[idx] or 2))
    except (TypeError, ValueError):
        return 2


def payload(state) -> dict:
    """GET /treasure/status 的完整响应体。"""
    db = tick()
    t = now()
    open_now = _in_open_window(t)
    cur = db.get("current") if isinstance(db.get("current"), dict) else None
    record = _join_info(state, t)
    used = len(record.get("keys") or [])
    limit = _daily_limit(state)
    next_start = _next_start_after(t)
    seconds_to_next = max(0, int((next_start - t).total_seconds()))
    base = {
        "success": True,
        "openNow": open_now,
        "todayJoinCount": used,
        "dailyJoinLimit": limit,
        "secondsToNextRoundStart": seconds_to_next,
    }

    # 关洞时段对齐原版：round: null、chests: []（客户端自己走 9:00 倒计时）
    if not open_now or cur is None:
        base.update({"round": None, "chests": [], "joinedChestId": 0, "joinedChestNo": 0,
                     "lastRoundChestWinners": [], "activityLog": []})
        return base

    _advance(cur, t)
    mine = (cur.get("joins") or {}).get(state.username)
    joined_no = int(mine.get("chest_no", 0) or 0) if isinstance(mine, dict) else 0
    joined_id = 0
    if cur.get("status") == "finished":
        remain = seconds_to_next
    else:
        remain = max(0, int(float(cur.get("battle_end_ts") or 0) - _ts(t)))
        if cur.get("status") == "signup_open":
            remain = max(0, int(float(cur.get("signup_end_ts") or 0) - _ts(t)))
    chests = []
    for chest in cur.get("chests", []):
        no = int(chest.get("chest_no", 0) or 0)
        if no == joined_no:
            joined_id = int(chest.get("id", 0) or 0)
        row = {
            "id": int(chest.get("id", 0) or 0),
            "round_id": int(chest.get("round_id", 0) or 0),
            "chest_no": no,
            "status": chest.get("status", "pending"),
            "winner_user_id": chest.get("winner_user_id"),
            "winner_name": chest.get("winner_name"),
            "reward_mail_id": chest.get("reward_mail_id"),
            "battle_log_ref": chest.get("battle_log_ref"),
            "created_at": chest.get("created_at"),
            "updated_at": chest.get("updated_at"),
            "participants": _participants_of(cur, no),
            "remain_sec": remain if chest.get("status") != "finished" else seconds_to_next,
        }
        chests.append(row)

    last = db.get("last") if isinstance(db.get("last"), dict) else {}
    base.update({
        "round": _public_round(cur),
        "chests": chests,
        "joinedChestId": joined_id,
        "joinedChestNo": joined_no,
        # 本轮已锁定的镜像（报名那一刻的出战宠），没报名就是空
        "myMirror": (mine.get("mirror") if isinstance(mine, dict) else {}) or {},
        "lastRoundChestWinners": list(last.get("winners") or []),
        "activityLog": list(cur.get("activity") or [])[-40:],
    })
    return base


def _find_chest(rnd: dict, chest_id: int) -> dict | None:
    for chest in rnd.get("chests", []):
        if int(chest.get("id", 0) or 0) == int(chest_id or 0):
            return chest
    return None


def join(state, chest_id: int) -> tuple[bool, dict]:
    """POST /treasure/join：一轮一人一箱，日内总次数受 VIP 限制。"""
    db = tick()
    t = now()
    if not _in_open_window(t):
        return False, {"error": "藏宝洞未开启（每日 9:00–24:00）"}
    cur = db.get("current") if isinstance(db.get("current"), dict) else None
    if cur is None:
        return False, {"error": "本轮尚未开洞，请稍候"}
    if cur.get("status") != "signup_open":
        return False, {"error": "本轮报名已截止，请等下一轮"}

    chest = _find_chest(cur, chest_id)
    if chest is None:
        # 客户端可能把 chest_no 当 id 传上来（老包）
        for row in cur.get("chests", []):
            if int(row.get("chest_no", 0) or 0) == int(chest_id or 0):
                chest = row
                break
    if chest is None:
        return False, {"error": "秘箱不存在，请刷新"}

    no = int(chest.get("chest_no", 0) or 0)
    joins = cur.setdefault("joins", {})
    mine = joins.get(state.username)
    if isinstance(mine, dict):
        if int(mine.get("chest_no", 0) or 0) == no:
            return True, _join_ok(state, cur, chest, duplicate=True)
        return False, {"error": "本轮已择定%d号秘箱" % int(mine.get("chest_no", 0) or 0)}

    record = _join_info(state, t)
    keys = record.setdefault("keys", [])
    limit = _daily_limit(state)
    if len(keys) >= limit:
        return False, {"error": "今日参团次数已用完（%d/%d）" % (len(keys), limit)}

    import treasure_mirror

    mirror = treasure_mirror.resolve_for_join(state)
    joins[state.username] = {
        "chest_no": no,
        "name": state.nickname(),
        "at": _zulu(t),
        # 报名时把镜像钉在本轮：之后换宠/换角色都不改这份（对齐原版规则）
        "mirror": mirror,
        "power": int(mirror.get("power", 1) or 1),
    }
    if cur.get("round_key") not in keys:
        keys.append(cur.get("round_key"))
    _log(cur, TEXT_LOG_JOIN % state.nickname(), t)
    cur["updated_at"] = _zulu(t)
    chest["updated_at"] = _zulu(t)
    _mark(db)
    _flush(db)
    state.bump()
    return True, _join_ok(state, cur, chest)


def _join_ok(state, cur: dict, chest: dict, duplicate: bool = False) -> dict:
    record = _join_info(state, now())
    used = len(record.get("keys") or [])
    limit = _daily_limit(state)
    no = int(chest.get("chest_no", 0) or 0)
    return {
        "success": True,
        "duplicate": bool(duplicate),
        "chestId": int(chest.get("id", 0) or 0),
        "chestNo": no,
        "joinedChestId": int(chest.get("id", 0) or 0),
        "joinedChestNo": no,
        "roundId": int(cur.get("id", 0) or 0),
        "roundKey": cur.get("round_key"),
        "todayJoinCount": used,
        "dailyLimit": limit,
        "dailyJoinLimit": limit,
    }


def battle_log(chest_id: int, page: int = 1, page_size: int = 10) -> dict:
    """GET /treasure/battle-log：某个秘箱的逐场战报（先查本轮，再查留档）。

    返回体对齐客户端 _show_battle_log_list_dialog()：
    matches[] 每场含 bracket_round / match_no / 甲乙昵称 / winner_nickname /
    battle_log（数组，客户端也可吃 JSON 字符串）。
    """
    db = tick()
    targets: list[dict] = []
    cur = db.get("current")
    if isinstance(cur, dict):
        targets.append(cur)
    for row in (db.get("history") or []):
        if isinstance(row, dict):
            targets.append(row)

    chest = None
    home = None
    for rnd in targets:
        chest = _find_chest(rnd, chest_id)
        if chest is None:
            # 老包可能把 chest_no 当 id 传
            for row in rnd.get("chests", []):
                if int(row.get("chest_no", 0) or 0) == int(chest_id or 0) and int(chest_id or 0) <= CHEST_COUNT:
                    chest = row
                    break
        if chest is not None:
            home = rnd
            break

    if chest is None:
        return {"success": True, "chestId": int(chest_id or 0), "matches": [],
                "total": 0, "page": 1, "pageSize": max(1, int(page_size or 10))}

    matches = [m for m in (chest.get("matches") or []) if isinstance(m, dict)]
    total = len(matches)
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, min(50, int(page_size or 10)))
    except (TypeError, ValueError):
        page_size = 10
    start = (page - 1) * page_size
    page_rows = []
    for m in matches[start:start + page_size]:
        row = dict(m)
        # 客户端读的是 battle_log_json（数组或 JSON 字符串都认），留档里存的是
        # battle_log —— 这里补个别名，免得同一份日志在 state 里存两遍。
        row["battle_log_json"] = m.get("battle_log") or []
        page_rows.append(row)
    return {
        "success": True,
        "chestId": int(chest.get("id", 0) or 0),
        "chestNo": int(chest.get("chest_no", 0) or 0),
        "roundId": int((home or {}).get("id", 0) or 0),
        "roundKey": (home or {}).get("round_key"),
        "roundIndexToday": int((home or {}).get("round_index_today", 0) or 0),
        "winnerName": chest.get("winner_name"),
        "matchCount": total,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "matches": page_rows,
    }


def participants(state, chest_id: int) -> dict:
    db = tick()
    cur = db.get("current") if isinstance(db.get("current"), dict) else None
    rows: list[dict] = []
    if cur is not None:
        chest = _find_chest(cur, chest_id)
        no = int(chest.get("chest_no", 0) or 0) if chest else int(chest_id or 0)
        for info in (cur.get("joins") or {}).values():
            if isinstance(info, dict) and int(info.get("chest_no", 0) or 0) == no:
                rows.append({"nickname": info.get("name", ""), "seed_score": 0})
    return {"success": True, "participants": rows}


# ---------------------------------------------------------------- 调度线程

def start_scheduler(interval: float = 20.0) -> None:
    global _SCHED
    with _LOCK:
        if _SCHED is not None and _SCHED.is_alive():
            return

        def loop() -> None:
            while True:
                try:
                    tick()
                except Exception:
                    traceback.print_exc()
                time.sleep(max(5.0, float(interval)))

        _SCHED = threading.Thread(target=loop, name="treasure-round", daemon=True)
        _SCHED.start()
