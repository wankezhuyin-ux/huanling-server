# -*- coding: utf-8 -*-
"""帮会模块：落地客户端 gang_network.gd / gang_manager.gd 所需的全部接口。

设计取舍
--------
* 帮会数据是**全局**的（一个帮会跨多个账号），所以单独存 `state/gangs.json`，
  不塞进玩家存档；玩家与帮会的归属关系存在帮会的 members 列表里。
* 帮战 / 领地战是重度玩法（镜像战斗、排期、竞价），本服不开放：
  GET 类接口返回结构合法的空数据（客户端不会报错），POST 类接口明确返回
  「本服未开放」，而不是假装成功，避免玩家以为打了一场。
* 所有写操作都走 `_LOCK`，服务端是多线程的。
"""
from __future__ import annotations

import threading

from core import (STATE_DIR, Ctx, dump_json, fail, iso, load_json, now, ok,
                  route)

GANG_PATH = STATE_DIR / "gangs.json"
_LOCK = threading.RLock()

ROLE_LEADER = 1
ROLE_VICE = 2
ROLE_ELITE = 3
ROLE_MEMBER = 4

# 帮派等级表：升到第 N 级需要的资金/材料/声望，以及该级人数上限
# 声望从 6 级开始才需要（与原版一致）
# 数值按本服经济规模下调过（原版是全服大服，资金需求是百万级）
_LEVEL_TABLE = []
for _lv in range(1, 11):
    _LEVEL_TABLE.append({
        "level": _lv,
        "funds": 0 if _lv == 1 else 10000 * (_lv - 1),
        "materials": 0 if _lv == 1 else 10 * (_lv - 1),
        "reputation": 0 if _lv < 6 else 10 * (_lv - 5),
        "memberCap": 20 + 5 * (_lv - 1),
        "unlock": "帮战" if _lv == 5 else "",
    })

# 帮贡商店：id / 名称 / 价格（帮贡）/ 周限购 / 解锁帮派等级
SHOP_ITEMS = [
    # 道具名必须与客户端 game_data.gd 一致（原版名：双倍经验丹）。
    # 这里原本写「双倍经验符」，客户端道具表里没有这个名字，买了也用不了。
    {"id": "gang_exp_pill", "itemName": "双倍经验丹", "cost": 200, "weeklyLimit": 5,
     "minGangLevel": 1, "itemType": "consumable", "count": 1,
     "desc": "使用后获得 30 分钟双倍经验"},
    {"id": "gang_material", "itemName": "建设材料", "cost": 100, "weeklyLimit": 20,
     "minGangLevel": 1, "itemType": "material", "count": 1,
     "desc": "帮派建设材料，可捐献"},
    {"id": "gang_pet_exp_pill", "itemName": "宠物双倍经验丹", "cost": 300, "weeklyLimit": 5,
     "minGangLevel": 3, "itemType": "consumable", "count": 1,
     "desc": "宠物 30 分钟双倍经验（只加灵兽，人物不受影响）"},
    {"id": "gang_cultivation_pill", "itemName": "凝神丹", "cost": 500, "weeklyLimit": 3,
     "minGangLevel": 5, "itemType": "consumable", "count": 1,
     "desc": "增加200点修行值（每世限用5颗）"},
]

CONFIG = {
    # 私人服：建帮不收费、不卡转生，方便朋友自己开帮
    "createYuanbaoCost": 0,
    "createMinReincarnation": 0,
    "createLeaderContribution": 2000,
    "memberCap": 30,
    "reputationMax": 150,
    "maintenanceFeePerLevelSq": 500,
    "leaveCooldownHours": 24,
    "levels": _LEVEL_TABLE,
    "donations": {
        # 原版数值是给大服设计的（金币 1000 万一次），本服金币量级只有千位，
        # 这里按本服经济重新定价：一次捐献约等于一场野外战斗的收益。
        "gold": {"costGold": 1000, "gainMaterials": 10, "gainFunds": 1000, "gainContribution": 20},
        "yuanbao": {"costYuanbao": 30, "gainMaterials": 30, "gainFunds": 3000, "gainContribution": 60},
        "material": {"costMaterials": 1, "gainFunds": 800, "gainContribution": 10},
    },
    # 帮战 / 领地战参数照客户端默认值给一份，界面规则说明才不会显示 0
    "kickHallCost": 50000,
    "kickPrepMinutes": 30,
    "kickBattleHours": 2,
    "kickWeeklyStartLimit": 2,
    "kickWeeklyDefendLimit": 3,
    "kickProtectHours": 24,
    "kickRepairMaxPerSession": 3,
    "kickRepairContribution": 500,
    "kickAttackContributionCost": 200,
    "kickAttackContributionLimit": 3,
    "kickYuanbaoAttackCost": 100,
    "kickYuanbaoAttackLimit": 3,
    "kickYuanbaoDefenseCost": 100,
    "kickYuanbaoDefenseLimit": 3,
    "kickDailyAttackLimit": 3,
    "kickAttackCooldownSeconds": 120,
    "kickRepairCooldownSeconds": 60,
    "kickStartBlockEndHour": 8,
    "kickWinMaterialStealPct": 0.1,
    "kickWinFundStealPct": 0.1,
    "siegeBaseBid": 100000,
    "siegeMinBidIncrement": 10000,
    "weeklySiegeAttempts": 3,
    "siegeTerritoryProtectDays": 7,
    "siegeFreeAttackLimit": 3,
    "siegeAttackContributionCost": 200,
    "siegeAttackContributionLimit": 3,
    "siegeYuanbaoAttackCost": 100,
    "siegeYuanbaoAttackLimit": 3,
    "siegeRepairContribution": 200,
    "siegeRepairMaxPerRound": 3,
    "siegeYuanbaoDefenseCost": 100,
    "siegeYuanbaoDefenseLimit": 3,
    "maxTerritoriesPerGang": 3,
    "territoryMemberDailyByTier": {},
    "siegeBuffs": [],
    "siegeSchedule": {"battleWeekday": 0, "rounds": [], "ignoreWeekdayForTest": True},
}


# ---------------------------------------------------------------- 存储

def _load() -> dict:
    raw = load_json(GANG_PATH, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("nextId", 1)
    raw.setdefault("nextAppId", 1)
    if not isinstance(raw.get("gangs"), dict):
        raw["gangs"] = {}
    return raw


def _save(data: dict) -> None:
    dump_json(GANG_PATH, data)


def _week_key() -> str:
    d = now()
    return "%d-W%02d" % (d.isocalendar()[0], d.isocalendar()[1])


def _find_membership(data: dict, username: str):
    for gang in data["gangs"].values():
        for member in gang.get("members", []):
            if str(member.get("username")) == username:
                return gang, member
    return None, None


def _member_payload(member: dict) -> dict:
    return {
        "userId": int(member.get("userId", 0) or 0),
        "username": str(member.get("username", "")),
        "nickname": str(member.get("nickname", "")),
        "role": int(member.get("role", ROLE_MEMBER)),
        "contribution": int(member.get("contribution", 0)),
        "level": int(member.get("level", 1)),
        "realm": str(member.get("realm", "")),
        "online": bool(member.get("online", False)),
        "joinedAt": str(member.get("joinedAt", "")),
        "lastActiveAt": str(member.get("lastActiveAt", "")),
    }


def _gang_payload(gang: dict) -> dict:
    members = gang.get("members", [])
    level = int(gang.get("level", 1))
    row = _LEVEL_TABLE[min(max(level, 1), 10) - 1]
    return {
        "id": int(gang.get("id", 0)),
        "name": str(gang.get("name", "")),
        "emblemId": int(gang.get("emblemId", 1)),
        "notice": str(gang.get("notice", "")),
        "level": level,
        "funds": int(gang.get("funds", 0)),
        "materials": int(gang.get("materials", 0)),
        "reputation": int(gang.get("reputation", 0)),
        "reputationMax": int(CONFIG["reputationMax"]),
        "memberCount": len(members),
        "memberCap": int(row.get("memberCap", CONFIG["memberCap"])),
        "leaderName": str(gang.get("leaderName", "")),
        "leaderUsername": str(gang.get("leaderUsername", "")),
        "createdAt": str(gang.get("createdAt", "")),
        "autoJoin": bool((gang.get("settings") or {}).get("autoJoin", False)),
        "maintenanceFee": int(CONFIG["maintenanceFeePerLevelSq"]) * level * level,
    }


def _used_emblems(data: dict) -> list:
    return sorted({int(g.get("emblemId", 0)) for g in data["gangs"].values()
                   if int(g.get("emblemId", 0)) > 0})


def _cfg() -> dict:
    return CONFIG


def _require_gang(ctx: Ctx):
    """返回 (data, gang, member)；未加入帮会时 gang/member 为 None。"""
    data = _load()
    gang, member = _find_membership(data, str(ctx.user.get("username", "")))
    return data, gang, member


def _not_in_gang():
    return fail("你还没有加入帮会")


# ---------------------------------------------------------------- 读接口

@route("GET", "/gang/overview")
def gang_overview(ctx: Ctx) -> dict:
    data, gang, member = _require_gang(ctx)
    base = {
        "config": _cfg(),
        "usedEmblemIds": _used_emblems(data),
        "serverTime": iso(now()),
        "recommended": [],
    }
    if not gang:
        base.update({
            "hasGang": False,
            "gang": {},
            "membership": {"role": ROLE_MEMBER, "contribution": 0},
            "members": [],
            "applications": [],
        })
        return ok(**base)

    members = gang.get("members", [])
    applications = gang.get("applications", [])
    is_officer = int(member.get("role", ROLE_MEMBER)) <= ROLE_VICE
    base.update({
        "hasGang": True,
        "gang": _gang_payload(gang),
        "membership": {
            "role": int(member.get("role", ROLE_MEMBER)),
            "contribution": int(member.get("contribution", 0)),
            "joinedAt": str(member.get("joinedAt", "")),
            "username": str(member.get("username", "")),
            "nickname": str(member.get("nickname", "")),
        },
        "members": [_member_payload(m) for m in members],
        "applications": [_app_payload(a) for a in applications] if is_officer else [],
        "buffs": {},
        "gangLevelBuffs": {},
        "territoryBuffs": {},
        "ownedTerritoryBuffBreakdown": [],
    })
    return ok(**base)


def _app_payload(app: dict) -> dict:
    return {
        "id": int(app.get("id", 0)),
        "userId": int(app.get("userId", 0)),
        "username": str(app.get("username", "")),
        "nickname": str(app.get("nickname", "")),
        "level": int(app.get("level", 1)),
        "realm": str(app.get("realm", "")),
        "appliedAt": str(app.get("appliedAt", "")),
    }


@route("GET", "/gang/list")
def gang_list(ctx: Ctx) -> dict:
    data = _load()
    limit = max(1, min(ctx.qi("limit", 20), 100))
    rows = []
    for gang in sorted(data["gangs"].values(), key=lambda g: (-int(g.get("level", 1)),
                                                             -int(g.get("reputation", 0)))):
        payload = _gang_payload(gang)
        payload["applicationRequired"] = not bool(payload.get("autoJoin"))
        rows.append(payload)
    return ok(gangs=rows[:limit], usedEmblemIds=_used_emblems(data))


@route("GET", "/gang/members")
def gang_members(ctx: Ctx) -> dict:
    data, gang, member = _require_gang(ctx)
    if not gang:
        return ok(hasGang=False, members=[], applications=[], viewerRole=ROLE_MEMBER)
    is_officer = int(member.get("role", ROLE_MEMBER)) <= ROLE_VICE
    return ok(
        hasGang=True,
        members=[_member_payload(m) for m in gang.get("members", [])],
        applications=[_app_payload(a) for a in gang.get("applications", [])] if is_officer else [],
        viewerRole=int(member.get("role", ROLE_MEMBER)),
    )


@route("GET", "/gang/shop")
def gang_shop(ctx: Ctx) -> dict:
    data, gang, member = _require_gang(ctx)
    level = int(gang.get("level", 0)) if gang else 0
    purchases = {}
    if member is not None:
        purchases = dict(_shop_purchases(gang, member))
    shop = []
    for item in SHOP_ITEMS:
        row = dict(item)
        row["remaining"] = max(0, int(item["weeklyLimit"]) - int(purchases.get(item["id"], 0)))
        row["locked"] = level < int(item["minGangLevel"])
        shop.append(row)
    return ok(shop=shop, purchases=purchases, items=shop, gangLevel=level)


def _shop_purchases(gang: dict, member: dict) -> dict:
    if str(member.get("shopWeek", "")) != _week_key():
        member["shopWeek"] = _week_key()
        member["shopPurchases"] = {}
    return member.setdefault("shopPurchases", {})


@route("GET", "/gang/buffs")
def gang_buffs(ctx: Ctx) -> dict:
    data, gang, member = _require_gang(ctx)
    # 原版 /gang/buffs 恒返回完整数值结构（没帮派就全是 0），不是空对象。
    # 帮派等级加成实测（lv1）：经验 +2.5%，攻/防/血/敏 各 +0.5%；按等级线性外推。
    level = int((gang or {}).get("level", 0) or 0)
    gang_level_buffs = {
        "gangLevel": level,
        "expBonusPercent": round(2.5 * level, 2),
        "attackBonusPercent": round(0.5 * level, 2),
        "defenseBonusPercent": round(0.5 * level, 2),
        "hpBonusPercent": round(0.5 * level, 2),
        "agilityBonusPercent": round(0.5 * level, 2),
    }
    territory = {"attackBonusPercent": 0, "defenseBonusPercent": 0,
                 "hpBonusPercent": 0, "expBonusPercent": 0,
                 "agilityBonusPercent": 0, "goldDropBonusPercent": 0}
    merged = dict(territory)
    for key, value in gang_level_buffs.items():
        if key == "gangLevel":
            merged["gangLevel"] = value
        else:
            merged[key] = round(float(merged.get(key, 0)) + float(value), 2)
    return ok(
        gangLevelBuffs=gang_level_buffs,
        territoryBuffs=territory,
        territoryBuffSkyCityActive=False,
        ownedTerritoryBuffBreakdown=[],
        buffs=merged,
        hasGang=bool(gang),
    )


@route("GET", "/gang/territories")
def gang_territories(ctx: Ctx) -> dict:
    # 领地战未开放，没有占领数据
    return ok(territories=[])


@route("GET", "/gang/ranking")
def gang_ranking(ctx: Ctx) -> dict:
    data = _load()
    rows = []
    for gang in data["gangs"].values():
        member_count = len(gang.get("members", []))
        rows.append({
            "gangId": int(gang.get("id", 0)),
            "id": int(gang.get("id", 0)),
            "name": str(gang.get("name", "")),
            "emblemId": int(gang.get("emblemId", 1)),
            "level": int(gang.get("level", 1)),
            "reputation": int(gang.get("reputation", 0)),
            "memberCount": member_count,
            # 客户端 ranking_hall_panel.gd 读的是下划线命名，只给 memberCount
            # 会让帮派榜的「成员」永远是 0（2026-11-01 修）。
            "member_count": member_count,
            "territory_count": len(gang.get("territories") or []),
            "funds": int(gang.get("funds", 0)),
            "leaderName": str(gang.get("leaderName", "")),
        })
    rows.sort(key=lambda r: (-r["reputation"], -r["level"], -r["memberCount"]))
    for idx, row in enumerate(rows, 1):
        row["rank"] = idx
    return ok(ranking=rows)


@route("GET", "/gang/titles")
def gang_titles(ctx: Ctx) -> dict:
    # 帮派称号依赖帮战声望，本服未开放，返回空列表（客户端会显示“暂无称号”）
    return ok(titles=[])


@route("GET", "/gang/kick/active")
def gang_kick_active(ctx: Ctx) -> dict:
    return ok(asAttacker=[], hasMirror=False, kickActivityLogs=[], kickStartBlockedHour=int(CONFIG["kickStartBlockEndHour"]))


@route("GET", "/gang/kick/defending")
def gang_kick_defending(ctx: Ctx) -> dict:
    return ok(asDefender=[], kickActivityLogs=[])


@route("GET", "/gang/kick/history")
def gang_kick_history(ctx: Ctx) -> dict:
    return ok(records=[])


@route("GET", "/gang/siege/status")
def gang_siege_status(ctx: Ctx) -> dict:
    return ok(activeSessions=[], participationLogs=[], hasMirror=False, config=_cfg(),
              ownedTerritories=[])


@route("GET", "/gang/siege/history")
def gang_siege_history(ctx: Ctx) -> dict:
    return ok(records=[])


@route("GET", "/gang/siege/guards")
def gang_siege_guards(ctx: Ctx) -> dict:
    return ok(territoryId=str(ctx.q.get("territoryId", "")), guards=[])


@route("GET", "/gang/war/reports")
def gang_war_reports(ctx: Ctx) -> dict:
    return ok(reports=[])


@route("GET", "/gang/mirror")
def gang_mirror_get(ctx: Ctx) -> dict:
    return ok(hasMirror=False, mirror={})


@route("GET", "/gang/territory/claim-status")
def gang_territory_claim_status(ctx: Ctx) -> dict:
    return ok(claims=[], claimedToday=False)


# ---------------------------------------------------------------- 写接口

@route("POST", "/gang/create")
def gang_create(ctx: Ctx) -> dict:
    username = str(ctx.user.get("username", ""))
    name = ctx.s("name").strip()
    if not (2 <= len(name) <= 8):
        return fail("帮派名称需 2~8 个字符")
    try:
        emblem_id = max(1, min(32, ctx.i("emblemId", 1) or 1))
    except (TypeError, ValueError):
        emblem_id = 1
    notice = ctx.s("notice").strip()[:200]

    with _LOCK:
        data = _load()
        existing, _member = _find_membership(data, username)
        if existing is not None:
            return fail("你已经加入了帮会「%s」" % existing.get("name", ""))
        for gang in data["gangs"].values():
            if str(gang.get("name", "")).strip() == name:
                return fail("帮派名称已被使用")

        player = ctx.state.player
        cost = int(CONFIG["createYuanbaoCost"])
        if cost > 0:
            if int(player.get("yuanbao_currency", 0) or 0) < cost:
                return fail("创建帮派需要 %d 元宝" % cost)
            # 走 State.add_yuanbao，才能把消耗记进贵族累计额度
            ctx.state.add_yuanbao(-cost)

        gid = int(data.get("nextId", 1))
        data["nextId"] = gid + 1
        user_id = int(ctx.user.get("userId", 0) or 0)
        nickname = str(player.get("name") or ctx.user.get("nickname") or username)
        leader_contrib = int(CONFIG["createLeaderContribution"])
        data["gangs"][str(gid)] = {
            "id": gid,
            "name": name,
            "emblemId": emblem_id,
            "notice": notice,
            "level": 1,
            "funds": 0,
            "materials": 0,
            "reputation": 0,
            "createdAt": iso(now()),
            "leaderUsername": username,
            "leaderName": nickname,
            "settings": {"notice": notice, "autoJoin": False},
            "members": [{
                "username": username,
                "userId": user_id,
                "nickname": nickname,
                "role": ROLE_LEADER,
                "contribution": leader_contrib,
                "level": int(player.get("level", 1) or 1),
                "realm": str(player.get("realm", "") or ""),
                "joinedAt": iso(now()),
                "lastActiveAt": iso(now()),
            }],
            "applications": [],
        }
        _save(data)

    ctx.state.bump()
    return ok(gangId=gid, contributionGained=leader_contrib, duplicate=False)


@route("POST", "/gang/apply")
def gang_apply(ctx: Ctx) -> dict:
    username = str(ctx.user.get("username", ""))
    gid = str(ctx.i("gangId", 0))
    with _LOCK:
        data = _load()
        gang = data["gangs"].get(gid)
        if gang is None:
            return fail("帮派不存在")
        current, _m = _find_membership(data, username)
        if current is not None:
            return fail("你已经在帮会「%s」中" % current.get("name", ""))
        members = gang.get("members", [])
        cap = int(_LEVEL_TABLE[min(max(int(gang.get("level", 1)), 1), 10) - 1]["memberCap"])
        if len(members) >= cap:
            return fail("该帮派人数已满")

        player = ctx.state.player
        user_id = int(ctx.user.get("userId", 0) or 0)
        nickname = str(player.get("name") or ctx.user.get("nickname") or username)
        entry = {
            "username": username,
            "userId": user_id,
            "nickname": nickname,
            "role": ROLE_MEMBER,
            "contribution": 0,
            "level": int(player.get("level", 1) or 1),
            "realm": str(player.get("realm", "") or ""),
            "joinedAt": iso(now()),
            "lastActiveAt": iso(now()),
        }
        auto_join = bool((gang.get("settings") or {}).get("autoJoin", False))
        if auto_join:
            members.append(entry)
            gang["members"] = members
            _save(data)
            ctx.state.bump()
            return ok(joined=True, pending=False, gangId=int(gid))

        apps = gang.setdefault("applications", [])
        if not any(str(a.get("username")) == username for a in apps):
            app_id = int(data.get("nextAppId", 1))
            data["nextAppId"] = app_id + 1
            apps.append({
                "id": app_id,
                "username": username,
                "userId": user_id,
                "nickname": nickname,
                "level": entry["level"],
                "realm": entry["realm"],
                "appliedAt": iso(now()),
            })
        _save(data)
    return ok(joined=False, pending=True, gangId=int(gid))


@route("POST", "/gang/approve")
def gang_approve(ctx: Ctx) -> dict:
    username = str(ctx.user.get("username", ""))
    app_id = ctx.i("applicationId", 0)
    approve = bool(ctx.body.get("approve", False))
    with _LOCK:
        data, gang, member = _require_gang(ctx)
        if not gang:
            return _not_in_gang()
        if int(member.get("role", ROLE_MEMBER)) > ROLE_VICE:
            return fail("只有帮主/副帮主可以审批")

        target = None
        for app in gang.get("applications", []):
            if int(app.get("id", 0)) == int(app_id):
                target = app
                break
        if target is None:
            return fail("申请不存在或已被处理")

        gang["applications"] = [a for a in gang.get("applications", [])
                                if int(a.get("id", 0)) != int(app_id)]
        approved = False
        if approve:
            cap = int(_LEVEL_TABLE[min(max(int(gang.get("level", 1)), 1), 10) - 1]["memberCap"])
            if len(gang.get("members", [])) >= cap:
                _save(data)
                return fail("帮派人数已满")
            if _find_membership(data, str(target.get("username")))[0] is None:
                gang.setdefault("members", []).append({
                    "username": str(target.get("username", "")),
                    "userId": int(target.get("userId", 0)),
                    "nickname": str(target.get("nickname", "")),
                    "role": ROLE_MEMBER,
                    "contribution": 0,
                    "level": int(target.get("level", 1)),
                    "realm": str(target.get("realm", "")),
                    "joinedAt": iso(now()),
                    "lastActiveAt": iso(now()),
                })
                approved = True
        _save(data)
    return ok(approved=approved)


@route("POST", "/gang/leave")
def gang_leave(ctx: Ctx) -> dict:
    username = str(ctx.user.get("username", ""))
    with _LOCK:
        data, gang, member = _require_gang(ctx)
        if not gang:
            return _not_in_gang()
        members = [m for m in gang.get("members", []) if str(m.get("username")) != username]
        gang["members"] = members
        if not members:
            data["gangs"].pop(str(gang.get("id")), None)
        elif int(member.get("role", ROLE_MEMBER)) == ROLE_LEADER:
            # 帮主退帮自动禅让给最早加入的成员
            heir = min(members, key=lambda m: str(m.get("joinedAt", "")))
            heir["role"] = ROLE_LEADER
            gang["leaderUsername"] = str(heir.get("username", ""))
            gang["leaderName"] = str(heir.get("nickname", ""))
        _save(data)
    ctx.state.bump()
    return ok(left=True)


@route("POST", "/gang/donate")
def gang_donate(ctx: Ctx) -> dict:
    dtype = ctx.s("type", "gold").strip() or "gold"
    username = str(ctx.user.get("username", ""))
    with _LOCK:
        data, gang, member = _require_gang(ctx)
        if not gang:
            return _not_in_gang()
        cfg = dict(CONFIG["donations"].get(dtype) or {})
        if not cfg:
            return fail("未知的捐献类型")

        player = ctx.state.player
        materials_gained = 0
        if dtype == "gold":
            cost = int(cfg.get("costGold", 0))
            if int(player.get("gold", 0) or 0) < cost:
                return fail("金币不足，需要 %d" % cost)
            player["gold"] = int(player.get("gold", 0)) - cost
        elif dtype == "yuanbao":
            cost = int(cfg.get("costYuanbao", 0))
            if int(player.get("yuanbao_currency", 0) or 0) < cost:
                return fail("元宝不足，需要 %d" % cost)
            ctx.state.add_yuanbao(-cost)
        elif dtype == "material":
            if not ctx.state.take_item("建设材料", 1):
                return fail("背包里没有【建设材料】")
            materials_gained = 0

        funds_gained = int(cfg.get("gainFunds", 0))
        materials_gained += int(cfg.get("gainMaterials", 0))
        contribution_gained = int(cfg.get("gainContribution", 0))
        gang["funds"] = int(gang.get("funds", 0)) + funds_gained
        gang["materials"] = int(gang.get("materials", 0)) + materials_gained
        member["contribution"] = int(member.get("contribution", 0)) + contribution_gained
        member["lastActiveAt"] = iso(now())
        _save(data)

    ctx.state.bump()
    return ok(contributionGained=contribution_gained, fundsGained=funds_gained,
              materialsGained=materials_gained)


@route("POST", "/gang/upgrade")
def gang_upgrade(ctx: Ctx) -> dict:
    with _LOCK:
        data, gang, member = _require_gang(ctx)
        if not gang:
            return _not_in_gang()
        if int(member.get("role", ROLE_MEMBER)) > ROLE_VICE:
            return fail("只有帮主/副帮主可以升级帮派")
        level = int(gang.get("level", 1))
        if level >= 10:
            return fail("帮派已达最高等级")
        nxt = _LEVEL_TABLE[level]  # 下一级
        need_funds = int(nxt.get("funds", 0))
        need_mats = int(nxt.get("materials", 0))
        need_rep = int(nxt.get("reputation", 0))
        if int(gang.get("funds", 0)) < need_funds:
            return fail("帮派资金不足（需要 %d）" % need_funds)
        if int(gang.get("materials", 0)) < need_mats:
            return fail("帮派建设材料不足（需要 %d）" % need_mats)
        if need_rep > 0 and int(gang.get("reputation", 0)) < need_rep:
            return fail("帮派声望不足（需要 %d）" % need_rep)
        gang["funds"] = int(gang.get("funds", 0)) - need_funds
        gang["materials"] = int(gang.get("materials", 0)) - need_mats
        gang["level"] = level + 1
        _save(data)
    ctx.state.bump()
    return ok(level=int(gang.get("level", 1)))


@route("POST", "/gang/kick")
def gang_kick_member(ctx: Ctx) -> dict:
    target_uid = ctx.i("targetUserId", 0)
    with _LOCK:
        data, gang, member = _require_gang(ctx)
        if not gang:
            return _not_in_gang()
        if int(member.get("role", ROLE_MEMBER)) > ROLE_VICE:
            return fail("权限不足")
        if int(member.get("userId", 0)) == target_uid:
            return fail("不能踢出自己")
        before = len(gang.get("members", []))
        gang["members"] = [m for m in gang.get("members", [])
                           if int(m.get("userId", 0)) != target_uid]
        if len(gang["members"]) == before:
            return fail("成员不存在")
        _save(data)
    return ok(kicked=True)


@route("POST", "/gang/set-role")
def gang_set_role(ctx: Ctx) -> dict:
    target_uid = ctx.i("targetUserId", 0)
    role = max(ROLE_VICE, min(ROLE_MEMBER, ctx.i("role", ROLE_MEMBER)))
    with _LOCK:
        data, gang, member = _require_gang(ctx)
        if not gang:
            return _not_in_gang()
        if int(member.get("role", ROLE_MEMBER)) != ROLE_LEADER:
            return fail("只有帮主可以调整职位")
        hit = None
        for m in gang.get("members", []):
            if int(m.get("userId", 0)) == target_uid:
                hit = m
                break
        if hit is None:
            return fail("成员不存在")
        if int(hit.get("role", ROLE_MEMBER)) == ROLE_LEADER:
            return fail("不能修改帮主职位")
        hit["role"] = int(role)
        _save(data)
    return ok(role=int(role))


@route("POST", "/gang/transfer")
def gang_transfer(ctx: Ctx) -> dict:
    target_uid = ctx.i("targetUserId", 0)
    with _LOCK:
        data, gang, member = _require_gang(ctx)
        if not gang:
            return _not_in_gang()
        if int(member.get("role", ROLE_MEMBER)) != ROLE_LEADER:
            return fail("只有帮主可以禅让")
        hit = None
        for m in gang.get("members", []):
            if int(m.get("userId", 0)) == target_uid:
                hit = m
                break
        if hit is None:
            return fail("成员不存在")
        member["role"] = ROLE_VICE
        hit["role"] = ROLE_LEADER
        gang["leaderUsername"] = str(hit.get("username", ""))
        gang["leaderName"] = str(hit.get("nickname", ""))
        _save(data)
    return ok(transferred=True)


@route("POST", "/gang/settings")
def gang_settings(ctx: Ctx) -> dict:
    notice = ctx.s("notice").strip()[:200]
    auto_join = bool(ctx.body.get("autoJoin", False))
    with _LOCK:
        data, gang, member = _require_gang(ctx)
        if not gang:
            return _not_in_gang()
        if int(member.get("role", ROLE_MEMBER)) > ROLE_VICE:
            return fail("权限不足")
        gang["notice"] = notice
        settings = gang.setdefault("settings", {})
        settings["notice"] = notice
        settings["autoJoin"] = auto_join
        _save(data)
    return ok(notice=notice, autoJoin=auto_join)


@route("POST", "/gang/shop/buy")
def gang_shop_buy(ctx: Ctx) -> dict:
    shop_id = ctx.s("shopItemId").strip()
    item = next((i for i in SHOP_ITEMS if i["id"] == shop_id), None)
    if item is None:
        return fail("商品不存在")
    with _LOCK:
        data, gang, member = _require_gang(ctx)
        if not gang:
            return _not_in_gang()
        level = int(gang.get("level", 1))
        if level < int(item["minGangLevel"]):
            return fail("帮派等级不足，需要 Lv.%d" % int(item["minGangLevel"]))
        purchases = _shop_purchases(gang, member)
        bought = int(purchases.get(shop_id, 0))
        if bought >= int(item["weeklyLimit"]):
            return fail("本周兑换次数已用完")
        cost = int(item["cost"])
        if int(member.get("contribution", 0)) < cost:
            return fail("帮贡不足，需要 %d" % cost)
        member["contribution"] = int(member.get("contribution", 0)) - cost
        purchases[shop_id] = bought + 1
        member["lastActiveAt"] = iso(now())
        _save(data)

    ctx.state.add_item(item["itemName"], int(item.get("count", 1)), str(item.get("itemType", "consumable")))
    ctx.state.bump()
    return ok(itemName=item["itemName"], count=int(item.get("count", 1)),
              remaining=int(item["weeklyLimit"]) - (bought + 1),
              contribution=int(member.get("contribution", 0)))


@route("POST", "/gang/titles/activate")
def gang_title_activate(ctx: Ctx) -> dict:
    return fail("暂无可用帮派称号")


@route("POST", "/gang/mirror/update")
def gang_mirror_update(ctx: Ctx) -> dict:
    # 帮战未开放，但镜像接口保持可用，避免客户端一直提示未更新
    return ok(hasMirror=True, mirror={})


@route("POST", "/gang/territory/claim-daily-all")
def gang_territory_claim_all(ctx: Ctx) -> dict:
    return ok(totalGold=0, totalMaterials=0, granted=[])


# ---- 帮战 / 领地战：明确不开放 -------------------------------------------

_NOT_OPEN = "本服未开放帮会战"


def _not_open(msg: str = _NOT_OPEN):
    return fail(msg)


@route("POST", "/gang/kick/start")
def gang_kick_start(ctx: Ctx) -> dict:
    return _not_open()


@route("POST", "/gang/kick/attack")
def gang_kick_attack(ctx: Ctx) -> dict:
    return _not_open()


@route("POST", "/gang/kick/repair")
def gang_kick_repair(ctx: Ctx) -> dict:
    return _not_open()


@route("POST", "/gang/kick/buy-attack")
def gang_kick_buy_attack(ctx: Ctx) -> dict:
    return _not_open()


@route("POST", "/gang/kick/buy-defense")
def gang_kick_buy_defense(ctx: Ctx) -> dict:
    return _not_open()


@route("POST", "/gang/kick/buy-attack-contribution")
def gang_kick_buy_attack_contrib(ctx: Ctx) -> dict:
    return _not_open()


@route("POST", "/gang/siege/apply")
def gang_siege_apply(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/bid")
def gang_siege_bid(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/forfeit")
def gang_siege_forfeit(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/attack")
def gang_siege_attack(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/repair")
def gang_siege_repair(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/buy-attack")
def gang_siege_buy_attack(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/buy-defense")
def gang_siege_buy_defense(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/buy-attack-contribution")
def gang_siege_buy_attack_contrib(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/guards")
def gang_siege_guards_set(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/guards/clear")
def gang_siege_guards_clear(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/siege/guards/join")
def gang_siege_guards_join(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")


@route("POST", "/gang/territory/abandon")
def gang_territory_abandon(ctx: Ctx) -> dict:
    return _not_open("本服未开放领地争夺战")
