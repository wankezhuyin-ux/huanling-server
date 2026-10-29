#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GM 后台：挂在游戏服务端同一个进程上，路径 /gm。

- 页面：GET  /gm              （单页，无外部依赖，手机也能用）
- 接口：POST /gm/api          （所有动作走这一个入口，靠 action 分发）
- 鉴权：独立 GM 密码（与玩家账号无关），密码存 state/gm.json（PBKDF2 加盐）
- 令牌：内存里存 12 小时，服务重启后需要重新登录
- 审计：每个改动都写 state/gm-audit.log
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import traceback

from core import DATA, STATE_DIR, STORE, dump_json, iso, load_json, now, rnd_id, route

GM_FILE = STATE_DIR / "gm.json"
AUDIT_FILE = STATE_DIR / "gm-audit.log"
TOKEN_TTL = 12 * 3600

MAX_LEVEL_PLAYER = 1200
POINTS_PER_LEVEL = {"凡人": 3, "散仙": 3, "地仙": 4, "天仙": 4, "大罗金仙": 5, "幻灵天神": 5}

# 允许被 GM 直接改写的 playerData 字段（白名单，防止手滑写坏存档）
INT_FIELDS = {
    # 注意：level 故意不在白名单里。改等级必须走 level_set，
    # 否则属性点/生命上限不会跟着补，玩家会出现"等级高但很脆"的怪状态。
    "experience", "cultivation", "gold", "yuanbao_currency", "friendship_points",
    "attack_base", "defense_base", "agility_base", "max_hp", "hp", "mp", "max_mp",
    "points", "permanent_points", "pending_auto_points", "reincarnation",
    "tower_floor", "tower_highest", "vip_level", "suit_dungeon_progress",
    "double_exp_until", "magic", "vitality", "talent_hp", "talent_attack",
    "talent_defense", "talent_agility", "lottery_yuanbao_pity_count",
    "noble_points", "noble_level",
}
STR_FIELDS = {"realm", "element", "vip_expire_time_str", "active_title_id", "battle_stance_pet", "battle_stance_player"}
BOOL_FIELDS = {"is_vip", "auto_allocation_enabled", "use_fabao_player_element"}


# ---------------------------------------------------------------- 图鉴与鉴权

def _load_pets() -> dict:
    raw = load_json(DATA / "pets.json", {}) or {}
    return raw.get("pets", {})


PETS = _load_pets()


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


class Admin:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.tokens: dict[str, float] = {}
        self.fails: dict[str, list[float]] = {}
        self.cred = load_json(GM_FILE, {}) or {}
        if not self.cred.get("hash"):
            password = os.environ.get("GM_PASSWORD", "").strip() or "lingyun-gm"
            self.set_password(password)
            print("[GM] 首次启动，已初始化 GM 密码（来源：%s）" % (
                "环境变量 GM_PASSWORD" if os.environ.get("GM_PASSWORD") else "默认值 lingyun-gm"))
        self.ensure_default_password()

    # -- 密码
    def set_password(self, password: str) -> None:
        salt = secrets.token_hex(8)
        self.cred = {"salt": salt, "hash": _hash(password, salt), "updatedAt": iso(now())}
        dump_json(GM_FILE, self.cred)

    def verify(self, password: str, ip: str) -> tuple[bool, str]:
        now_ts = time.time()
        with self.lock:
            recent = [t for t in self.fails.get(ip, []) if now_ts - t < 60]
            self.fails[ip] = recent
            if len(recent) >= 8:
                return False, "尝试太频繁，等一分钟再试"
        if not self.cred.get("hash"):
            return False, "GM 密码未初始化"
        ok = secrets.compare_digest(self.cred["hash"], _hash(str(password), self.cred["salt"]))
        with self.lock:
            if ok:
                self.fails.pop(ip, None)
            else:
                self.fails.setdefault(ip, []).append(now_ts)
        return ok, "" if ok else "密码错误"

    def ensure_default_password(self) -> None:
        """默认密码提醒：仍然用 lingyun-gm 就在日志里喊一声。"""
        if secrets.compare_digest(self.cred.get("hash", ""), _hash("lingyun-gm", self.cred.get("salt", ""))):
            print("[GM] 警告：GM 后台还在用默认密码 lingyun-gm，请立刻登录改掉")

    # -- 令牌
    def issue(self) -> str:
        token = secrets.token_hex(24)
        with self.lock:
            self.tokens[token] = time.time() + TOKEN_TTL
        return token

    def check(self, token: str) -> bool:
        with self.lock:
            exp = self.tokens.get(token)
            if not exp:
                return False
            if exp < time.time():
                self.tokens.pop(token, None)
                return False
            return True

    def drop(self, token: str) -> None:
        with self.lock:
            self.tokens.pop(token, None)


ADMIN = Admin()


def audit(action: str, target: str, detail: dict, ip: str = "") -> None:
    line = json.dumps({"ts": iso(now()), "ip": ip, "action": action, "target": target,
                       "detail": detail}, ensure_ascii=False)
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 工具函数

def _pets_of(state) -> tuple[list, list]:
    return state.pets, state.pet_bag


def _find_pet(state, uid: str) -> dict | None:
    for bucket in (state.pets, state.pet_bag):
        for pet in bucket:
            if str(pet.get("id")) == str(uid):
                return pet
    return None


def make_pet(pid: str, level: int, elite: bool) -> dict:
    """按客户端 pet.gd 的公式造一只合法宠物。"""
    info = PETS[pid]
    ratio = {"attack": int(info["ratio"][0]), "defense": int(info["ratio"][1]), "agility": int(info["ratio"][2])}
    mult = 1.35 if elite else 1.0            # 客户端精英倍率 1.3~1.5 随机，这里取中值
    combat = float(info["growth"]) * mult
    gr = {k: round(combat * ratio[k] / 100.0, 3) for k in ratio}
    gr["hp"] = round(float(info["hp_growth"]) * mult, 3)
    total_growth = round(gr["attack"] + gr["defense"] + gr["agility"], 3)

    cap = int(info.get("max_level", 1500))
    lv = max(1, min(int(level), cap))
    diff = lv - 1
    base = {"attack": info["attack"], "defense": info["defense"], "agility": info["agility"], "hp": info["hp"]}
    atk = round(base["attack"] + gr["attack"] * diff, 2)
    dfn = round(base["defense"] + gr["defense"] * diff, 2)
    agi = round(base["agility"] + gr["agility"] * diff, 2)
    # 生命成长按 hp_growth（原版口径），不是 total_growth
    max_hp = round(base["hp"] + gr["hp"] * diff, 2)

    skills = [{"slot": i + 1, "level": 0, "skill_id": "", "skill_key": "", "skill_name": ""} for i in range(3)]
    return {
        "id": rnd_id("gm"),
        "pet_id": pid,
        "name": info["name"],
        "type": info["type"],
        "element": info["element"],
        "level": lv,
        "max_level": cap,
        "form": info["form"],
        "is_elite": bool(elite),
        "is_mutant": bool(info.get("is_mutant", False)),
        "mutation_count": 0,
        "generation": 0,
        "experience": 0,
        "base_stats": dict(base),
        "stat_ratio": ratio,
        "growth_rates": gr,
        "total_growth": total_growth,
        "lineage_seed_growth": total_growth,
        "attack": atk,
        "defense": dfn,
        "agility": agi,
        "hp": max_hp,
        "max_hp": max_hp,
        "final_attack": atk,
        "final_defense": dfn,
        "final_agility": agi,
        "final_max_hp": max_hp,
        "capture_map": "",
        "locked": False,
        "is_evolution_pet": bool(info.get("is_evolution_pet", False)),
        "evolution_origin": info.get("evolution_origin", ""),
        "evolution_stage": int(info.get("evolution_stage", 0)),
        "reincarnation_count": 0,
        "pet_raise_race_used": False,
        "medal_counts": {},
        "pending_medal_sync": False,
        "pending_medal_sync_reason": "",
        "pending_skill_learn_sync": False,
        "pending_skill_learn_prev_level": 0,
        "last_max_level_skill_learn_cap": 0,
        "last_max_level_medal_cap": 0,
        "skill_slots": skills,
    }


def pet_to_level(pet: dict, level: int) -> dict:
    """把已存在的宠物拉到指定等级（按自身成长率重算）。"""
    base = pet.get("base_stats") or {}
    gr = pet.get("growth_rates") or {}
    cap = int(pet.get("max_level", 1500))
    lv = max(1, min(int(level), cap))
    diff = lv - 1
    total_growth = float(gr.get("attack", 0)) + float(gr.get("defense", 0)) + float(gr.get("agility", 0))
    pet["level"] = lv
    pet["attack"] = round(float(base.get("attack", 0)) + float(gr.get("attack", 0)) * diff, 2)
    pet["defense"] = round(float(base.get("defense", 0)) + float(gr.get("defense", 0)) * diff, 2)
    pet["agility"] = round(float(base.get("agility", 0)) + float(gr.get("agility", 0)) * diff, 2)
    pet["max_hp"] = round(float(base.get("hp", 0)) + float((pet.get("growth_rates") or {}).get("hp", 0) or 0) * diff, 2)
    pet["hp"] = pet["max_hp"]
    pet["final_attack"] = pet["attack"]
    pet["final_defense"] = pet["defense"]
    pet["final_agility"] = pet["agility"]
    pet["final_max_hp"] = pet["max_hp"]
    pet["total_growth"] = round(total_growth, 3)
    pet["experience"] = 0
    return pet


def set_player_level(state, target: int) -> dict:
    """改等级，并把客户端升级时会做的副作用补上（属性点、每级 +5 生命上限）。"""
    player = state.player
    cur = int(player.get("level", 1))
    target = max(1, min(int(target), MAX_LEVEL_PLAYER))
    delta = target - cur
    realm = str(player.get("realm", "凡人"))
    per_level = POINTS_PER_LEVEL.get(realm, 3)
    gained_points = 0
    if delta > 0:
        gained_points = delta * per_level
        if bool(player.get("auto_allocation_enabled", True)):
            player["pending_auto_points"] = int(player.get("pending_auto_points", 0)) + gained_points
        else:
            player["points"] = int(player.get("points", 0)) + gained_points
        player["max_hp"] = int(player.get("max_hp", 0)) + 5 * delta
        player["hp"] = int(player["max_hp"])
    player["level"] = target
    player["experience"] = 0
    return {"from": cur, "to": target, "pointsAdded": gained_points}


def player_brief(username: str) -> dict:
    info = STORE.accounts.get(username, {})
    state = STORE.get(username)
    player = state.player
    online = any(str(t.get("username")) == username and
                 int(time.time()) - int(t.get("ts", 0)) <= STORE.TOKEN_TTL
                 for t in STORE.tokens.values())
    return {
        "username": username,
        "nickname": info.get("nickname") or player.get("name") or username,
        "userId": info.get("userId", 0),
        "level": int(player.get("level", 1)),
        "realm": str(player.get("realm", "")),
        "gold": int(player.get("gold", 0)),
        "yuanbao": int(player.get("yuanbao_currency", 0)),
        "online": online,
        "createdAt": info.get("createdAt", ""),
        "lastLoginAt": info.get("lastLoginAt", ""),
    }


def player_full(username: str) -> dict:
    state = STORE.get(username)
    player = state.player
    info = dict(STORE.accounts.get(username, {}))
    info.pop("hash", None)
    info.pop("salt", None)
    pets = [{k: p.get(k) for k in ("id", "name", "pet_id", "level", "max_level", "is_elite",
                                   "element", "attack", "defense", "agility", "max_hp", "locked")}
            for p in state.pets]
    bag = [{k: p.get(k) for k in ("id", "name", "pet_id", "level", "max_level", "is_elite",
                                  "element", "attack", "defense", "agility", "max_hp", "locked")}
           for p in state.pet_bag]
    items = [{"name": i.get("name"), "count": i.get("count"), "item_type": i.get("item_type")}
             for i in state.items]
    return {
        "account": info,
        "player": {k: v for k, v in player.items() if not isinstance(v, (dict, list))},
        "pets": pets,
        "petBag": bag,
        "items": items,
        "mailCount": len(state.meta.get("mails", []) or []),
        "petBagSlots": 60,
    }


# ---------------------------------------------------------------- 接口

def _hdr(ctx, name: str) -> str:
    """取请求头。ctx.headers 是普通 dict，键名大小写不敏感要靠这里兜住
    （不同客户端会把 X-GM-Token 发成 X-gm-token）。"""
    target = name.lower()
    for key, value in (ctx.headers or {}).items():
        if str(key).lower() == target:
            return str(value or "")
    return ""


def _require(ctx) -> dict | None:
    token = (_hdr(ctx, "X-GM-Token") or str(ctx.body.get("token", ""))).strip()
    if not ADMIN.check(token):
        return {"success": False, "message": "GM 未登录或登录已过期", "auth": False}
    return None


def _guard(username: str) -> tuple[bool, str]:
    if not username:
        return False, "缺少账号"
    if username not in STORE.accounts:
        return False, "账号不存在：%s" % username
    return True, ""


@route("POST", "/gm/api")
def gm_api(ctx) -> dict:
    action = str(ctx.body.get("action", "")).strip()
    ip = (_hdr(ctx, "X-Real-IP") or _hdr(ctx, "X-Forwarded-For") or "local").strip()

    if action == "login":
        passed, msg = ADMIN.verify(str(ctx.body.get("password", "")), ip)
        if not passed:
            audit("login_failed", "", {}, ip)
            return {"success": False, "message": msg}
        audit("login", "", {}, ip)
        return {"success": True, "token": ADMIN.issue(), "defaultPassword": _is_default_password()}

    denied = _require(ctx)
    if denied:
        return denied

    if action == "logout":
        ADMIN.drop((_hdr(ctx, "X-GM-Token") or str(ctx.body.get("token", ""))).strip())
        return {"success": True}

    if action == "change_password":
        old = str(ctx.body.get("old", ""))
        new = str(ctx.body.get("new", ""))
        if len(new) < 6:
            return {"success": False, "message": "新密码至少 6 位"}
        passed, msg = ADMIN.verify(old, ip)
        if not passed:
            return {"success": False, "message": "原密码不对"}
        ADMIN.set_password(new)
        ADMIN.tokens.clear()
        audit("change_password", "", {}, ip)
        return {"success": True, "message": "密码已更新，请重新登录"}

    if action == "catalog":
        return {"success": True, "pets": [{"id": k, "name": v["name"], "element": v["element"],
                                           "maxLevel": v["max_level"], "type": v["type"]}
                                          for k, v in sorted(PETS.items(), key=lambda kv: kv[1]["name"])],
                "itemPresets": _item_presets()}

    # -- 下面是针对某个玩家动作
    username = str(ctx.body.get("username", "")).strip()
    if action == "players":
        rows = [player_brief(name) for name in STORE.accounts.keys()]
        rows.sort(key=lambda r: (-int(r["level"]), r["username"]))
        return {"success": True, "players": rows}

    if action == "create_account":
        okc, msg, info = STORE.create(str(ctx.body.get("username", "")).strip(),
                                     str(ctx.body.get("password", "")),
                                     str(ctx.body.get("nickname", "")).strip())
        if not okc:
            return {"success": False, "message": msg}
        audit("create_account", str(ctx.body.get("username", "")), {"userId": info.get("userId")}, ip)
        return {"success": True, "message": "已创建账号 %s（Lv.1）" % ctx.body.get("username", ""),
                "info": info}

    if action == "audit":
        lines = []
        if AUDIT_FILE.exists():
            with AUDIT_FILE.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()[-120:]
        return {"success": True, "lines": [json.loads(x) for x in lines if x.strip()]}

    okg, gmsg = _guard(username)
    if not okg:
        return {"success": False, "message": gmsg}

    state = STORE.get(username)

    if action == "detail":
        return {"success": True, "data": player_full(username)}

    if action == "grant":
        detail = {}
        with state.lock:
            for key, field in (("gold", "gold"), ("yuanbao", "yuanbao_currency"), ("friendship", "friendship_points")):
                amount = int(ctx.body.get(key, 0) or 0)
                if amount:
                    player = state.player
                    player[field] = max(0, int(player.get(field, 0)) + amount)
                    detail[key] = {"amount": amount, "now": int(player[field])}
            state.bump()
            state.flush()
        audit("grant", username, detail, ip)
        return {"success": True, "message": "已发放", "detail": detail, "data": player_full(username)}

    if action == "set_fields":
        fields = ctx.body.get("fields") or {}
        applied, rejected = {}, []
        with state.lock:
            for key, value in fields.items():
                if key in INT_FIELDS:
                    try:
                        state.player[key] = int(value)
                        applied[key] = int(value)
                    except (TypeError, ValueError):
                        rejected.append(key)
                elif key in STR_FIELDS:
                    state.player[key] = str(value)
                    applied[key] = str(value)
                elif key in BOOL_FIELDS:
                    state.player[key] = bool(value)
                    applied[key] = bool(value)
                else:
                    rejected.append(key)
            if applied:
                # final_* 是战斗真正读的字段，改完 base 必须重算，
                # 否则 GM 改完存档后伤害仍按旧值算（表现为"改了没生效"）。
                state.sync_final_stats()
                state.bump()
                state.flush()
        audit("set_fields", username, {"applied": applied, "rejected": rejected}, ip)
        return {"success": True, "message": "已修改 %d 项" % len(applied), "applied": applied,
                "rejected": rejected, "data": player_full(username)}

    if action == "level_set":
        with state.lock:
            result = set_player_level(state, int(ctx.body.get("level", 1) or 1))
            state.bump()
            state.flush()
        audit("level_set", username, result, ip)
        return {"success": True, "message": "等级 %d → %d，补属性点 %d" % (
            result["from"], result["to"], result["pointsAdded"]), "detail": result,
            "data": player_full(username)}

    if action == "add_pet":
        pid = str(ctx.body.get("pet_id", "")).strip()
        if pid not in PETS:
            return {"success": False, "message": "图鉴里没有这个宠物：%s" % pid}
        level = int(ctx.body.get("level", 1) or 1)
        elite = bool(ctx.body.get("elite", False))
        to_team = bool(ctx.body.get("toTeam", False))
        pet = make_pet(pid, level, elite)
        with state.lock:
            team = state.pets
            bag = state.pet_bag
            if to_team and len(team) < 10:
                team.append(pet)
                where = "出战"
            else:
                if len(bag) >= 60:
                    return {"success": False, "message": "宠物背包已满（60），先清理或勾选「放入出战」"}
                bag.append(pet)
                where = "背包"
            state.bump()
            state.flush()
        audit("add_pet", username, {"pet_id": pid, "name": pet["name"], "level": pet["level"],
                                   "elite": elite, "where": where}, ip)
        return {"success": True, "message": "已发放 %s Lv.%d（%s）" % (pet["name"], pet["level"], where),
                "data": player_full(username)}

    if action == "pet_level":
        uid = str(ctx.body.get("pet_id", ""))
        level = int(ctx.body.get("level", 1) or 1)
        pet = _find_pet(state, uid)
        if not pet:
            return {"success": False, "message": "找不到这只宠物"}
        with state.lock:
            pet_to_level(pet, level)
            state.bump()
            state.flush()
        audit("pet_level", username, {"name": pet.get("name"), "level": pet["level"]}, ip)
        return {"success": True, "message": "%s 已到 Lv.%d" % (pet.get("name"), pet["level"]),
                "data": player_full(username)}

    if action == "pet_delete":
        uid = str(ctx.body.get("pet_id", ""))
        pet = _find_pet(state, uid)
        if not pet:
            return {"success": False, "message": "找不到这只宠物"}
        with state.lock:
            for bucket in (state.pets, state.pet_bag):
                if pet in bucket:
                    bucket.remove(pet)
                    break
            state.bump()
            state.flush()
        audit("pet_delete", username, {"name": pet.get("name")}, ip)
        return {"success": True, "message": "已删除 %s" % pet.get("name"), "data": player_full(username)}

    if action == "add_item":
        name = str(ctx.body.get("name", "")).strip()
        count = int(ctx.body.get("count", 1) or 1)
        item_type = str(ctx.body.get("itemType", "material") or "material")
        if not name or count <= 0:
            return {"success": False, "message": "道具名和数量要填"}
        with state.lock:
            item = state.add_item(name, count, item_type)
            state.bump()
            state.flush()
        audit("add_item", username, {"name": name, "count": count}, ip)
        return {"success": True, "message": "已发放 %s × %d（现有 %d）" % (
            name, count, int(item.get("count", 0))), "data": player_full(username)}

    if action == "mail":
        title = str(ctx.body.get("title", "")).strip() or "系统邮件"
        content = str(ctx.body.get("content", "")).strip() or "GM 发放"
        items = []
        for row in (ctx.body.get("items") or []):
            n = str(row.get("name", "")).strip()
            c = int(row.get("count", 1) or 1)
            if n and c > 0:
                items.append({"name": n, "count": c})
        attach = {}
        for key in ("gold", "yuanbao", "exp"):
            try:
                amount = int(ctx.body.get(key, 0) or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount > 0:
                attach[key] = amount
        if items:
            attach["items"] = items
        with state.lock:
            mail = state.send_mail(title, content, attachments=attach)
            state.bump()
            state.flush()
        audit("mail", username, {"title": title, "items": items,
                                 "gold": attach.get("gold", 0),
                                 "yuanbao": attach.get("yuanbao", 0)}, ip)
        parts = []
        if attach.get("gold"):
            parts.append("金币 %d" % attach["gold"])
        if attach.get("yuanbao"):
            parts.append("元宝 %d" % attach["yuanbao"])
        if attach.get("exp"):
            parts.append("经验 %d" % attach["exp"])
        if items:
            parts.append("道具 %d 种" % len(items))
        return {"success": True,
                "message": "邮件已发送（附件：%s）" % ("、".join(parts) or "无"),
                "mailId": mail.get("id"), "data": player_full(username)}

    if action == "reset_password":
        new = str(ctx.body.get("password", ""))
        if len(new) < 4:
            return {"success": False, "message": "密码至少 4 位"}
        with STORE.lock:
            info = STORE.accounts.get(username)
            if not info:
                return {"success": False, "message": "账号不存在"}
            salt = secrets.token_hex(8)
            info["salt"] = salt
            info["hash"] = STORE.hash_password(new, salt)
            # 顺带把旧令牌清掉，强制用新密码重新登录
            for token in [t for t, row in STORE.tokens.items() if str(row.get("username")) == username]:
                STORE.tokens.pop(token, None)
            STORE.flush_accounts()
            STORE.flush_tokens()
        audit("reset_password", username, {}, ip)
        return {"success": True, "message": "密码已重置，旧登录已踢下线"}

    if action == "kick":
        removed = 0
        with STORE.lock:
            for token in [t for t, row in STORE.tokens.items() if str(row.get("username")) == username]:
                STORE.tokens.pop(token, None)
                removed += 1
            STORE.flush_tokens()
        audit("kick", username, {"tokens": removed}, ip)
        return {"success": True, "message": "已踢下线（%d 个令牌）" % removed}

    if action == "delete_account":
        if str(ctx.body.get("confirm", "")) != username:
            return {"success": False, "message": "确认字符串不匹配，已取消"}
        with STORE.lock:
            STORE.accounts.pop(username, None)
            for token in [t for t, row in STORE.tokens.items() if str(row.get("username")) == username]:
                STORE.tokens.pop(token, None)
            STORE._cache.pop(username, None)
            path = STORE.dir / (username.replace("/", "_") + ".json")
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
            STORE.flush_accounts()
            STORE.flush_tokens()
        audit("delete_account", username, {}, ip)
        return {"success": True, "message": "账号已删除"}

    if action == "backup":
        return {"success": True, "name": "%s.json" % username,
                "json": json.dumps(state.raw, ensure_ascii=False, indent=1)}

    if action == "starter_pack":
        with state.lock:
            state.add_gold(100000)
            state.add_yuanbao(10000)
            lvl = set_player_level(state, max(int(state.player.get("level", 1)), 30))
            pet = make_pet("tongbao", 30, False) if "tongbao" in PETS else None
            if pet and len(state.pet_bag) < 60:
                state.pet_bag.append(pet)
            state.bump()
            state.flush()
        audit("starter_pack", username, lvl, ip)
        return {"success": True, "message": "新手锦囊已发放：10万金币 + 1万元宝 + 等级30 + 通宝",
                "data": player_full(username)}

    return {"success": False, "message": "未知操作：%s" % action}


def _is_default_password() -> bool:
    cred = ADMIN.cred
    return secrets.compare_digest(cred.get("hash", ""), _hash("lingyun-gm", cred.get("salt", "")))


GM_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>灵兽传说 · GM 后台</title>
<style>
:root{
  --bg:#0f1419; --panel:#161d26; --panel2:#1d2632; --line:#2a3542;
  --txt:#e6edf3; --dim:#8b98a5; --acc:#4a9eff; --ok:#3fb950; --warn:#d29922; --err:#f85149;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
button{font:inherit;cursor:pointer;border:0;border-radius:8px;padding:9px 14px;background:var(--acc);color:#fff;transition:.15s}
button:hover{filter:brightness(1.1)}
button:disabled{opacity:.5;cursor:not-allowed}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--txt)}
button.danger{background:#7d2222}
button.sm{padding:5px 10px;font-size:12px;border-radius:6px}
input,select,textarea{font:inherit;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 11px;width:100%;outline:none}
input:focus,select:focus,textarea:focus{border-color:var(--acc)}
textarea{resize:vertical;min-height:64px}
label{display:block;color:var(--dim);font-size:12px;margin:0 0 5px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end}
.row>div{flex:1;min-width:110px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.card h3{margin:0 0 12px;font-size:14px;color:var(--acc);font-weight:600;display:flex;justify-content:space-between;align-items:center}
.muted{color:var(--dim);font-size:12px}
.login{max-width:340px;margin:14vh auto;padding:24px;background:var(--panel);border:1px solid var(--line);border-radius:14px}
.login h1{font-size:17px;margin:0 0 18px;text-align:center}
#app{display:none}
header{display:flex;gap:10px;align-items:center;padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:9}
header b{font-size:15px}
header .sp{flex:1}
.wrap{display:flex;gap:12px;padding:12px;align-items:flex-start}
aside{width:290px;flex:0 0 290px;position:sticky;top:56px}
main{flex:1;min-width:0}
#list{max-height:calc(100vh - 170px);overflow:auto;margin-top:8px}
.p{display:flex;gap:8px;align-items:center;padding:9px 10px;border-radius:9px;cursor:pointer;border:1px solid transparent}
.p:hover{background:var(--panel2)}
.p.on{background:#1b2b3f;border-color:var(--acc)}
.p .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.p .lv{font-size:11px;color:var(--warn)}
.dot{width:7px;height:7px;border-radius:50%;background:#3a4552;flex:0 0 7px}
.dot.on{background:var(--ok);box-shadow:0 0 6px var(--ok)}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px}
.kv div{background:var(--panel2);border-radius:8px;padding:8px 10px}
.kv .k{font-size:11px;color:var(--dim)}
.kv .v{font-size:15px;font-weight:600;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 4px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--dim);font-weight:500}
.tblwrap{max-height:240px;overflow:auto}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(80px);background:#233042;border:1px solid var(--line);padding:10px 18px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none;max-width:80vw;z-index:99}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast.err{border-color:var(--err);color:#ffb4ae}
#toast.ok{border-color:var(--ok)}
.banner{background:#3d2f10;border:1px solid var(--warn);color:#f0d08a;padding:9px 12px;border-radius:9px;margin-bottom:12px;font-size:12px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.tabs button{background:var(--panel2);color:var(--dim);border:1px solid var(--line)}
.tabs button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
@media(max-width:760px){
  .wrap{flex-direction:column;padding:8px}
  aside{width:100%;flex:none;position:static}
  #list{max-height:200px}
  .kv{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>

<div class="login" id="loginBox">
  <h1>灵兽传说 · GM 后台</h1>
  <label>GM 密码</label>
  <input id="pw" type="password" placeholder="输入 GM 密码" onkeydown="if(event.key==='Enter')login()">
  <div style="height:12px"></div>
  <button style="width:100%" onclick="login()">登录</button>
  <div class="muted" id="loginMsg" style="margin-top:10px;text-align:center"></div>
</div>

<div id="app">
  <header>
    <b>GM 后台</b>
    <span class="muted" id="stat"></span>
    <span class="sp"></span>
    <button class="ghost sm" onclick="showPw()">改密码</button>
    <button class="ghost sm" onclick="doLogout()">退出</button>
  </header>
  <div class="wrap">
    <aside>
      <div class="card" style="margin:0">
        <div class="row">
          <div><label>搜索玩家</label><input id="q" placeholder="昵称或账号" oninput="renderList()"></div>
          <button class="ghost sm" onclick="loadPlayers()" style="margin-bottom:2px">刷新</button>
        </div>
        <div id="list"></div>
        <div style="height:8px"></div>
        <button class="ghost sm" style="width:100%" onclick="toggleBox('createBox')">+ 手动建号</button>
        <div id="createBox" style="display:none;margin-top:10px">
          <label>账号</label><input id="cUser" placeholder="英文/数字">
          <div style="height:6px"></div>
          <label>密码</label><input id="cPass" placeholder="至少4位">
          <div style="height:6px"></div>
          <label>昵称</label><input id="cNick" placeholder="游戏内显示名">
          <div style="height:8px"></div>
          <button class="sm" style="width:100%" onclick="createAccount()">创建</button>
        </div>
      </div>
    </aside>

    <main>
      <div class="card" id="empty">左边选一个玩家，或者先手动建号。</div>
      <div id="panel" style="display:none">
        <div id="pwBanner"></div>

        <div class="card">
          <h3><span id="whose">—</span><button class="ghost sm" onclick="loadDetail()">刷新</button></h3>
          <div class="kv" id="kv"></div>
        </div>

        <div class="card">
          <h3>发放货币（累加）</h3>
          <div class="row">
            <div><label>金币</label><input id="gGold" type="number" value="100000"></div>
            <div><label>元宝</label><input id="gYb" type="number" value="10000"></div>
            <div><label>友情点</label><input id="gFp" type="number" value="0"></div>
            <button onclick="grant()">发放</button>
          </div>
          <div class="row" style="margin-top:8px">
            <button class="ghost sm" onclick="quick('gold',10000)">+1万金</button>
            <button class="ghost sm" onclick="quick('gold',1000000)">+100万金</button>
            <button class="ghost sm" onclick="quick('gold',100000000)">+1亿金</button>
            <button class="ghost sm" onclick="quick('yuanbao',1000)">+1000元宝</button>
            <button class="ghost sm" onclick="quick('yuanbao',10000)">+1万元宝</button>
            <button class="ghost sm" onclick="quick('yuanbao',100000)">+10万元宝</button>
          </div>
        </div>

        <div class="card">
          <h3>等级</h3>
          <div class="row">
            <div><label>目标等级（1-1200，会自动补属性点和生命上限）</label><input id="lvTarget" type="number" value="100"></div>
            <button onclick="levelSet()">设置等级</button>
          </div>
          <div class="muted" style="margin-top:8px">升等级只改等级、可分配点数和生命上限；攻防敏是玩家自己加点的结果，需要的话在下面「属性微调」里补。</div>
        </div>

        <div class="card">
          <h3>属性微调</h3>
          <div class="row">
            <div><label>攻击基础(attack_base)</label><input id="aAtk" type="number"></div>
            <div><label>防御基础(defense_base)</label><input id="aDef" type="number"></div>
            <div><label>敏捷基础(agility_base)</label><input id="aAgi" type="number"></div>
            <div><label>生命上限(max_hp)</label><input id="aHp" type="number"></div>
            <div><label>可分配点数(points)</label><input id="aPts" type="number"></div>
          </div>
          <div class="row" style="margin-top:8px">
            <div><label>境界</label><input id="aRealm" placeholder="凡人/散仙/地仙/天仙/大罗金仙/幻灵天神"></div>
            <div><label>VIP等级</label><input id="aVip" type="number"></div>
            <div><label>封神塔最高层</label><input id="aTower" type="number"></div>
            <button onclick="saveAttrs()">保存</button>
          </div>
        </div>

        <div class="card">
          <h3>宠物 <span class="muted" id="petCount"></span></h3>
          <div class="row">
            <div style="flex:2"><label>选择宠物（可输入名字搜索）</label><input id="petSearch" list="petList" placeholder="例如：通宝"><datalist id="petList"></datalist></div>
            <div><label>等级</label><input id="petLv" type="number" value="100"></div>
            <div style="flex:0 0 auto"><label>&nbsp;</label><label style="display:flex;gap:6px;align-items:center;color:var(--txt)"><input type="checkbox" id="petElite" style="width:auto">精英</label></div>
            <div style="flex:0 0 auto"><label>&nbsp;</label><label style="display:flex;gap:6px;align-items:center;color:var(--txt)"><input type="checkbox" id="petTeam" style="width:auto">出战</label></div>
            <button onclick="addPet()">发放</button>
          </div>
          <div class="tblwrap" style="margin-top:10px"><table id="petTbl"></table></div>
        </div>

        <div class="card">
          <h3>道具</h3>
          <div class="row">
            <div style="flex:2"><label>道具名</label><input id="itemName" list="itemList" placeholder="雪莲"><datalist id="itemList"></datalist></div>
            <div><label>数量</label><input id="itemCount" type="number" value="10"></div>
            <div><label>类型</label><select id="itemType"><option value="material">material 材料</option><option value="consumable">consumable 消耗品</option><option value="equipment">equipment 装备</option></select></div>
            <button onclick="addItem()">发放</button>
          </div>
          <div class="tblwrap" style="margin-top:10px"><table id="itemTbl"></table></div>
        </div>

        <div class="card">
          <h3>发邮件（对方上线后在邮件里领）</h3>
          <div class="row">
            <div><label>标题</label><input id="mailTitle" value="GM 补给"></div>
            <div><label>金币</label><input id="mailGold" type="number" value="0"></div>
            <div><label>元宝</label><input id="mailYuanbao" type="number" value="0"></div>
            <div><label>经验</label><input id="mailExp" type="number" value="0"></div>
          </div>
          <div style="height:8px"></div>
          <label>正文</label><textarea id="mailBody">服主发的补给，记得领。</textarea>
          <div style="height:8px"></div>
          <label>附件道具（每行一个：道具名,数量）</label>
          <textarea id="mailItems" placeholder="雪莲,10
墨草,99"></textarea>
          <div style="height:8px"></div>
          <button onclick="sendMail()">发送邮件</button>
        </div>

        <div class="card">
          <h3>账号操作</h3>
          <div class="row">
            <div><label>重置密码</label><input id="newPw" placeholder="新密码，至少4位"></div>
            <button class="ghost" onclick="resetPw()">重置</button>
            <button class="ghost" onclick="kick()">踢下线</button>
            <button class="ghost" onclick="backup()">导出存档</button>
            <button class="danger" onclick="delAccount()">删除账号</button>
          </div>
        </div>

        <div class="card">
          <h3>操作记录 <button class="ghost sm" onclick="loadAudit()">加载</button></h3>
          <div class="tblwrap"><table id="auditTbl"></table></div>
        </div>
      </div>
    </main>
  </div>
</div>

<div id="toast"></div>

<script>
var T = localStorage.getItem('gmToken') || '';
var PLAYERS = [], CUR = '', DETAIL = null, CATALOG = null;

function toast(msg, kind){
  var el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + (kind||'');
  clearTimeout(el._t);
  el._t = setTimeout(function(){ el.className=''; }, 2600);
}

function api(action, extra){
  var body = Object.assign({action: action}, extra||{});
  return fetch('/gm/api', {
    method:'POST',
    headers:{'Content-Type':'application/json','X-GM-Token':T},
    body: JSON.stringify(body)
  }).then(function(r){ return r.json(); }).then(function(d){
    if (d && d.auth === false){ showLogin(); throw new Error(d.message||'未登录'); }
    return d;
  });
}

function login(){
  var pw = document.getElementById('pw').value;
  if(!pw){ document.getElementById('loginMsg').textContent='请输入密码'; return; }
  fetch('/gm/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'login',password:pw})})
  .then(function(r){return r.json()}).then(function(d){
    if(!d.success){ document.getElementById('loginMsg').textContent = d.message||'登录失败'; return; }
    T = d.token; localStorage.setItem('gmToken', T);
    showApp();
    if(d.defaultPassword){ document.getElementById('pwBanner').innerHTML =
      '<div class="banner">还在用默认密码 <b>lingyun-gm</b>，任何人知道地址都能进。点右上角「改密码」换掉。</div>'; }
  }).catch(function(e){ document.getElementById('loginMsg').textContent='网络错误'; });
}

function showLogin(){
  T=''; localStorage.removeItem('gmToken');
  document.getElementById('app').style.display='none';
  document.getElementById('loginBox').style.display='block';
}

function showApp(){
  document.getElementById('loginBox').style.display='none';
  document.getElementById('app').style.display='block';
  if(!CATALOG) loadCatalog();
  loadPlayers();
}

function doLogout(){
  api('logout',{}).finally(function(){ showLogin(); });
}

function toggleBox(id){
  var el = document.getElementById(id);
  el.style.display = el.style.display==='none' ? 'block' : 'none';
}

function loadCatalog(){
  api('catalog',{}).then(function(d){
    if(!d.success) return;
    CATALOG = d;
    var dl = document.getElementById('petList');
    dl.innerHTML = d.pets.map(function(p){ return '<option value="'+esc(p.name)+'">'+esc(p.id)+' · '+esc(p.element)+'</option>'; }).join('');
    var il = document.getElementById('itemList');
    il.innerHTML = (d.itemPresets||[]).map(function(n){ return '<option value="'+esc(n)+'">'; }).join('');
  });
}

function loadPlayers(){
  api('players',{}).then(function(d){
    if(!d.success){ toast(d.message,'err'); return; }
    PLAYERS = d.players;
    var on = PLAYERS.filter(function(p){return p.online}).length;
    document.getElementById('stat').textContent = PLAYERS.length + ' 个账号 · ' + on + ' 在线';
    renderList();
  });
}

function renderList(){
  var q = (document.getElementById('q').value||'').trim().toLowerCase();
  var box = document.getElementById('list');
  var rows = PLAYERS.filter(function(p){
    if(!q) return true;
    return (p.username+' '+p.nickname).toLowerCase().indexOf(q) >= 0;
  });
  if(!rows.length){ box.innerHTML = '<div class="muted" style="padding:10px">没有匹配的玩家</div>'; return; }
  box.innerHTML = rows.map(function(p){
    return '<div class="p '+(p.username===CUR?'on':'')+'" onclick="select(\''+esc(p.username)+'\')">'+
      '<span class="dot '+(p.online?'on':'')+'"></span>'+
      '<span class="n">'+esc(p.nickname)+'<span class="muted"> · '+esc(p.username)+'</span></span>'+
      '<span class="lv">Lv.'+p.level+'</span></div>';
  }).join('');
}

function select(name){ CUR = name; renderList(); document.getElementById('empty').style.display='none';
  document.getElementById('panel').style.display='block'; loadDetail(); }

function loadDetail(){
  if(!CUR) return;
  api('detail',{username:CUR}).then(function(d){
    if(!d.success){ toast(d.message,'err'); return; }
    DETAIL = d.data; renderDetail();
  });
}

function renderDetail(){
  var d = DETAIL, p = d.player, a = d.account;
  document.getElementById('whose').textContent = '玩家：' + (p.name||a.nickname||CUR) + '（' + CUR + '）';
  var rows = [
    ['账号', CUR], ['昵称', p.name||''], ['等级', p.level], ['境界', p.realm||''],
    ['金币', fmt(p.gold)], ['元宝', fmt(p.yuanbao_currency)], ['友情点', fmt(p.friendship_points)],
    ['战力(生命)', fmt(p.max_hp)], ['攻/防/敏', (p.attack_base||0)+'/'+(p.defense_base||0)+'/'+(p.agility_base||0)],
    ['可分配点', (p.points||0)+' / 自动待分配 '+(p.pending_auto_points||0)],
    ['封神塔', (p.tower_floor||0)+' 层'], ['投胎', p.reincarnation||0],
    ['VIP', (p.is_vip? '是':'否')+' Lv.'+(p.vip_level||0)], ['宠物', d.pets.length+' 出战 / '+d.petBag.length+' 背包'],
    ['邮件', d.mailCount], ['注册', (a.createdAt||'').slice(0,10)], ['最近登录', (a.lastLoginAt||'').slice(0,16).replace('T',' ')]
  ];
  document.getElementById('kv').innerHTML = rows.map(function(r){
    return '<div><div class="k">'+r[0]+'</div><div class="v">'+esc(String(r[1]))+'</div></div>';
  }).join('');

  document.getElementById('aAtk').value = p.attack_base||0;
  document.getElementById('aDef').value = p.defense_base||0;
  document.getElementById('aAgi').value = p.agility_base||0;
  document.getElementById('aHp').value  = p.max_hp||0;
  document.getElementById('aPts').value = p.points||0;
  document.getElementById('aRealm').value = p.realm||'';
  document.getElementById('aVip').value = p.vip_level||0;
  document.getElementById('aTower').value = p.tower_highest||0;

  document.getElementById('petCount').textContent = '（出战 '+d.pets.length+' / 背包 '+d.petBag.length+'）';
  var all = d.pets.map(function(x){ x._t='出战'; return x; })
    .concat(d.petBag.map(function(x){ x._t='背包'; return x; }));
  document.getElementById('petTbl').innerHTML =
    '<tr><th>名称</th><th>Lv</th><th>上限</th><th>攻</th><th>防</th><th>敏</th><th>血</th><th>位置</th><th></th></tr>' +
    all.map(function(x){
      return '<tr><td>'+esc(x.name)+(x.is_elite?' <span style="color:#d29922">精</span>':'')+'</td>'+
        '<td>'+x.level+'</td><td>'+x.max_level+'</td><td>'+Math.round(x.attack)+'</td>'+
        '<td>'+Math.round(x.defense)+'</td><td>'+Math.round(x.agility)+'</td><td>'+Math.round(x.max_hp)+'</td>'+
        '<td>'+x._t+'</td>'+
        '<td><button class="ghost sm" onclick="petLevel(\''+esc(x.id)+'\')">满级</button> '+
        '<button class="danger sm" onclick="petDel(\''+esc(x.id)+'\')">删</button></td></tr>';
    }).join('');

  document.getElementById('itemTbl').innerHTML = '<tr><th>道具</th><th>数量</th><th>类型</th></tr>' +
    d.items.map(function(i){ return '<tr><td>'+esc(i.name)+'</td><td>'+i.count+'</td><td class="muted">'+esc(i.item_type||'')+'</td></tr>'; }).join('');
}

function after(res, okMsg){
  if(!res.success){ toast(res.message||'失败','err'); return; }
  toast(res.message||okMsg||'完成','ok');
  if(res.data){ DETAIL = res.data; renderDetail(); }
  loadPlayers();
}

function grant(){
  after(api('grant',{username:CUR,
    gold: intval('gGold'), yuanbao: intval('gYb'), friendship: intval('gFp')}));
}
function quick(field, amount){
  var body = {username:CUR};
  body[field] = amount;
  after(api('grant', body));
}
function levelSet(){
  var lv = intval('lvTarget');
  if(lv<1||lv>1200){ toast('等级范围 1-1200','err'); return; }
  after(api('level_set',{username:CUR, level:lv}));
}
function saveAttrs(){
  after(api('set_fields',{username:CUR, fields:{
    attack_base: intval('aAtk'), defense_base: intval('aDef'), agility_base: intval('aAgi'),
    max_hp: intval('aHp'), hp: intval('aHp'), points: intval('aPts'),
    realm: document.getElementById('aRealm').value.trim(),
    vip_level: intval('aVip'), tower_highest: intval('aTower')
  }}));
}
function addPet(){
  var key = document.getElementById('petSearch').value.trim();
  if(!key){ toast('先选宠物','err'); return; }
  var pid = key, byName = {};
  CATALOG.pets.forEach(function(p){ if(p.name===key) pid = p.id; });
  var found = CATALOG.pets.some(function(p){ return p.id===pid; });
  if(!found){ toast('图鉴里没有「'+key+'」','err'); return; }
  after(api('add_pet',{username:CUR, pet_id:pid, level:intval('petLv'),
    elite: document.getElementById('petElite').checked, toTeam: document.getElementById('petTeam').checked}));
}
function petLevel(uid){
  after(api('pet_level',{username:CUR, pet_id:uid, level:1500}));
}
function petDel(uid){
  if(!confirm('确定删掉这只宠物？')) return;
  after(api('pet_delete',{username:CUR, pet_id:uid}));
}
function addItem(){
  var n = document.getElementById('itemName').value.trim();
  if(!n){ toast('先填道具名','err'); return; }
  after(api('add_item',{username:CUR, name:n, count:intval('itemCount'),
    itemType: document.getElementById('itemType').value}));
}
function sendMail(){
  var items = document.getElementById('mailItems').value.split('\n').map(function(line){
    var parts = line.split(/[,，]/);
    if(parts.length < 2) return null;
    return {name: parts[0].trim(), count: parseInt(parts[1],10)||0};
  }).filter(function(x){ return x && x.name && x.count>0; });
  after(api('mail',{username:CUR, title:document.getElementById('mailTitle').value,
    content:document.getElementById('mailBody').value, items:items,
    gold: intval('mailGold'), yuanbao: intval('mailYuanbao'), exp: intval('mailExp')}));
}
function resetPw(){
  var pw = document.getElementById('newPw').value.trim();
  if(pw.length<4){ toast('密码至少4位','err'); return; }
  if(!confirm('把 '+CUR+' 的密码重置为「'+pw+'」？')) return;
  after(api('reset_password',{username:CUR, password:pw}));
}
function kick(){ after(api('kick',{username:CUR})); }
function backup(){
  api('backup',{username:CUR}).then(function(d){
    if(!d.success){ toast(d.message,'err'); return; }
    var blob = new Blob([d.json], {type:'application/json'});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = d.name; a.click();
    toast('已导出存档','ok');
  });
}
function delAccount(){
  var s = prompt('删除 '+CUR+' 的账号和存档，不可恢复。\n确认请输入账号名：');
  if(s === null) return;
  after(api('delete_account',{username:CUR, confirm:s.trim()}));
  if(s.trim()===CUR){ CUR=''; document.getElementById('panel').style.display='none';
    document.getElementById('empty').style.display='block'; }
}
function createAccount(){
  var body = {username: document.getElementById('cUser').value.trim(),
              password: document.getElementById('cPass').value,
              nickname: document.getElementById('cNick').value.trim()};
  if(!body.username || !body.password){ toast('账号和密码要填','err'); return; }
  api('create_account', body).then(function(d){
    if(!d.success){ toast(d.message,'err'); return; }
    toast(d.message,'ok'); toggleBox('createBox'); loadPlayers();
  });
}
function loadAudit(){
  api('audit',{}).then(function(d){
    if(!d.success) return;
    var rows = (d.lines||[]).slice().reverse();
    document.getElementById('auditTbl').innerHTML = '<tr><th>时间</th><th>操作</th><th>对象</th><th>内容</th></tr>' +
      rows.map(function(r){
        return '<tr><td class="muted">'+(r.ts||'').slice(5,16).replace('T',' ')+'</td><td>'+esc(r.action)+'</td>'+
          '<td>'+esc(r.target||'')+'</td><td class="muted">'+esc(JSON.stringify(r.detail||{}).slice(0,90))+'</td></tr>';
      }).join('');
  });
}
function showPw(){
  var oldPw = prompt('当前 GM 密码：'); if(oldPw===null) return;
  var newPw = prompt('新密码（至少6位）：'); if(!newPw) return;
  api('change_password',{old:oldPw,new:newPw}).then(function(d){
    if(!d.success){ toast(d.message,'err'); return; }
    toast('已改密码，请重新登录','ok'); showLogin();
  });
}

function intval(id){ var v = parseInt(document.getElementById(id).value,10); return isNaN(v)?0:v; }
function fmt(n){ n = Number(n||0); return n.toLocaleString('en-US'); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

if(T){ showApp(); } else { document.getElementById('loginBox').style.display='block'; }
</script>
</body>
</html>
"""


def _item_presets() -> list[str]:
    names = set()
    for path, key in ((DATA / "shop-catalog.json", "items"),):
        raw = load_json(path, {}) or {}
        for row in raw.get(key, []) or []:
            name = str(row.get("itemName") or "").strip()
            if name:
                names.add(name)
    seed = load_json(DATA / "save_seed.json", {}) or {}
    for item in (seed.get("inventoryData", {}) or {}).get("items", []) or []:
        name = str(item.get("name") or "").strip()
        if name:
            names.add(name)
    names.update(["雪莲", "墨草", "洗髓丹", "还魂丹", "双倍经验卡", "封印套装", "解封石"])
    return sorted(names)
