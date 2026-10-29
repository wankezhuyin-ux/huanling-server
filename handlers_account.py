#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号 / 存档 / 邮件 / 排行榜 / 离线收益。

这一层对应原来服务端最基础的职责：认证、存档权威、邮件发货、榜单读取。
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from core import Ctx, DATA, RULES, STORE, iso, load_json, now, ok, fail, route, today, DEFAULT_MAP

CLIENT_VERSION = "2.6.0"


# ---------------------------------------------------------------- 账号

NICKNAME_MIN = 2
NICKNAME_MAX = 16


def _valid_nickname(raw) -> tuple[bool, str]:
    """角色名校验（客户端 login_ui 里同一套规则，服务端必须再验一遍）。"""
    name = str(raw or "").strip()
    if len(name) < NICKNAME_MIN or len(name) > NICKNAME_MAX:
        return False, "角色名需要 %d~%d 个字符" % (NICKNAME_MIN, NICKNAME_MAX)
    for ch in name:
        code = ord(ch)
        is_digit = 48 <= code <= 57
        is_alpha = (65 <= code <= 90) or (97 <= code <= 122)
        is_han = 19968 <= code <= 40959
        if not (is_digit or is_alpha or is_han):
            return False, "角色名只能使用汉字、英文和数字"
    return True, ""


def _has_character(state) -> bool:
    """这个账号是否已经创建过角色。

    新号存档是从种子复制的，playerData.name 是种子里的「新人」，
    所以不能用 name 非空判断；只有不等于种子名、或者 meta 里写了昵称才算已创建。
    """
    if str(state.meta.get("nickname") or "").strip():
        return True
    name = str(state.player.get("name") or "").strip()
    return name not in ("", "新人")


@route("POST", "/register")
def register(ctx: Ctx) -> dict:
    username = ctx.s("username").strip()
    password = ctx.s("password")
    # 角色名可以不传：注册只负责账号，角色在登录后的「创建角色」面板里定
    nickname = ctx.s("nickname").strip()
    if nickname:
        valid, msg = _valid_nickname(nickname)
        if not valid:
            return fail(msg, code="NICKNAME_INVALID")
        if STORE.nickname_taken(nickname):
            return fail("角色名已被占用，请换一个", code="NICKNAME_TAKEN")
    created, message, info = STORE.create(username, password, nickname)
    if not created:
        code = "ACCOUNT_EXISTS" if message == "账号已存在" else "REGISTER_FAILED"
        return fail(message, code=code)
    token = STORE.issue_token(username)
    state = STORE.get(username)
    if info["nickname"]:
        state.meta["nickname"] = info["nickname"]
        state.player["name"] = info["nickname"]
    state.bump()
    state.flush()
    return ok(token=token, userId=info["userId"], nickname=info["nickname"],
              needCharacter=not bool(info["nickname"]), wipeId="")


@route("POST", "/login")
def login(ctx: Ctx) -> dict:
    username = ctx.s("username").strip()
    password = ctx.s("password")
    passed, message = STORE.verify(username, password)
    if not passed:
        if message == "账号不存在":
            message = "账号不存在，第一次玩请先点「注册」"
        elif message == "密码错误":
            message = "密码错误，请检查大小写"
        return fail(message, code="LOGIN_FAILED")
    info = STORE.accounts[username]
    token = STORE.issue_token(username)
    state = STORE.get(username)
    # 不能拿空昵称覆盖已有昵称：历史账号的昵称可能只存在存档里
    nick = str(info.get("nickname") or "").strip()
    if nick:
        state.meta["nickname"] = nick
    # 先把「距上次在线过了多久」冻结下来，供登录后立刻发起的
    # /economy/offline-claim 结算离线经验；再刷新 lastSeen。
    state.open_session()
    state.meta["lastLoginAt"] = iso(now())
    state.bump()
    state.flush()
    return ok(token=token, userId=info.get("userId", 0), nickname=nick,
              needCharacter=not _has_character(state), wipeId="")


@route("POST", "/player/name-available")
def player_name_available(ctx: Ctx) -> dict:
    """创建角色时的重名预检（输入框边打边查）。"""
    nickname = ctx.s("nickname").strip()
    valid, msg = _valid_nickname(nickname)
    if not valid:
        return fail(msg, code="NICKNAME_INVALID", available=False)
    if STORE.nickname_taken(nickname, exclude_username=ctx.state.username):
        return fail("该角色名已被占用", code="NICKNAME_TAKEN", available=False)
    return ok(available=True, nickname=nickname)


PLAYER_CHARACTER_MAX_ID = 6


@route("POST", "/player/create-character")
def player_create_character(ctx: Ctx) -> dict:
    """首次进入游戏时创建角色：定名字 + 选形象，免费。

    形象以后想换，仍然走 /player/switch-character（消耗 5 亿金币）。
    """
    state = ctx.state
    nickname = ctx.s("nickname").strip()
    valid, msg = _valid_nickname(nickname)
    if not valid:
        return fail(msg, code="NICKNAME_INVALID")
    if _has_character(state):
        return fail("角色已创建，无需重复创建", code="CHARACTER_EXISTS")
    try:
        cid = int(ctx.body.get("characterId", 0) or 0)
    except (TypeError, ValueError):
        return fail("角色参数不合法")
    if cid < 0 or cid > PLAYER_CHARACTER_MAX_ID:
        return fail("角色参数不合法")
    if STORE.nickname_taken(nickname, exclude_username=state.username):
        return fail("角色名已被占用，请换一个", code="NICKNAME_TAKEN")
    STORE.set_nickname(state.username, nickname)
    state.meta["nickname"] = nickname
    state.player["name"] = nickname
    state.player["player_character_id"] = cid
    state.bump()
    state.flush()
    # 和别的经济接口一样回权威存档切片（客户端 apply_cloud_save 只认这几个键）
    try:
        from handlers_growth import _authority_save
        payload_save = _authority_save(state)
    except Exception:
        payload_save = state.save
    return ok(nickname=nickname, characterId=cid, playerCharacterId=cid, cost=0,
              free=True, newGold=state.gold(), saveData=payload_save,
              saveRevision=state.revision)


@route("GET", "/version/check")
def version_check(ctx: Ctx) -> dict:
    version = str(ctx.q.get("version", CLIENT_VERSION))
    # 私人服不做版本升级：把客户端上报的版本原样当成最新版，避免弹「强制更新」
    return ok(
        # 原版字段名（客户端 main.gd _check_version 读这几个）
        latestVersion=version or CLIENT_VERSION,
        currentVersion=version,
        needUpdate=False,
        forceUpdate=False,
        updateUrl="",
        message="",
        releaseNotes="",
        # 本项目客户端早期字段，保留兼容
        downloadUrl="",
        notice="",
    )


# ---------------------------------------------------------------- 存档

@route("GET", "/save/download")
def save_download(ctx: Ctx) -> dict:
    state = ctx.state
    return {
        "success": True,
        "saveRevision": state.revision,
        "saveData": state.save,
    }


_REALM_ORDER = ("凡人", "散仙", "地仙", "天仙", "大罗金仙", "幻灵天神")

# 属性点是玩家资产：升级丹按等级补点、飞升重置后重发 20 点，都只由服务端发放。
# 客户端整包上传里经常还是「发点之前」的旧快照，必须整体兜住，
# 否则下一次自动存档就把几百点清零（玩家看到的就是「升级丹吃了没属性点」）。
_ATTR_ASSET_KEYS = (
    "points", "pending_auto_points", "auto_allocation_enabled", "auto_allocation_plan",
    "permanent_points", "vitality", "magic", "attack_base", "defense_base", "agility_base",
    "max_hp", "attr_reset_guard_level",
)


def _attr_invested_total(pd: dict) -> int:
    """已投入的属性点总量，与客户端 player.gd `_attr_point_total_accounting()` 同口径。

    只是「总量」，不含怎么分配 —— 玩家自己手动加点（points → 五维）不会改变这个值，
    所以用它可以安全地区分「正常加点」和「旧快照回滚」。
    """
    try:
        level = max(1, int(pd.get("level", 1) or 1))
    except (TypeError, ValueError):
        level = 1
    total = 0
    for key in ("points", "pending_auto_points"):
        try:
            total += max(0, int(pd.get(key, 0) or 0))
        except (TypeError, ValueError):
            pass
    for key in ("vitality", "magic", "attack_base", "defense_base", "agility_base"):
        try:
            total += max(0, int(pd.get(key, 0) or 0) - 10)
        except (TypeError, ValueError):
            pass
    floor_hp = 100 + 5 * max(0, level - 1)
    try:
        extra_hp = max(0, int(pd.get("max_hp", floor_hp) or floor_hp) - floor_hp)
    except (TypeError, ValueError):
        extra_hp = 0
    total += extra_hp // 10
    return total


def _merge_authoritative_progress(old: dict, incoming: dict) -> None:
    """防止客户端旧存档把服务端刚发的奖励冲掉（原地改 `incoming`）。

    客户端 `save_game` 是把整份存档原样传上来的。只要它本地还没应用服务端回包
    （网络抖动、apply_cloud_save 被跳过、或者回包里少了 inventoryData），
    这次上传就会把服务端已经写好的数据覆盖回旧值 ——
    玩家看到的现象就是「双倍丹吃了没效果」「开出来的宠物/装备下一分钟就没了」。

    这里只兜住**客户端没有权威来源**的几类字段，方向一律是「服务端 → 客户端」：
      - playerData.level / experience：等级经验只涨不跌（回档由 GM 处理，不走上传）；
        **但飞升 / 投胎会合法地把等级归 1**，此时必须让路（见下面 realm_transitioned）
      - playerData 属性点资产（points / pending / 五维 / max_hp）：只由服务端发，
        总量只增不减，旧快照不能把它们冲回加之前的水平
      - double_exp_channels / double_exp_until：按时段取 max，时效不会倒退
      - petsData.pets / pet_bag：按 id 取并集，服务端刚发的宠物不会被抹掉
      - inventoryData.pet_equipment_items：按 id 取并集（仙魔宝箱开出来的宠装）
    释放宠物 / 分解装备都走服务端接口（/pet/release-batch、/pet-equipment/dismantle-batch），
    服务端删掉之后这里也不会再把它们塞回去。
    """
    old_p = old.get("playerData") if isinstance(old.get("playerData"), dict) else {}
    new_p = incoming.get("playerData") if isinstance(incoming.get("playerData"), dict) else {}
    if old_p and new_p:
        # 境界转换（飞升 / 投胎）会让等级合法地归 1，且把五维/属性点重置。
        # 这里的「等级只增不减」必须给它让路，否则玩家点完飞升，
        # 客户端紧接着的整包上传会把 level=1 顶回 1200：
        #   · 界面显示「飞升成功但等级没变」；
        #   · 等级仍满级 → 门槛继续通过 → 12 秒内能连点三次飞升（散仙→地仙→天仙）。
        old_realm = str(old_p.get("realm", "") or "")
        new_realm = str(new_p.get("realm", "") or "")
        try:
            old_lv = int(old_p.get("level", 1) or 1)
            new_lv = int(new_p.get("level", 1) or 1)
        except (TypeError, ValueError):
            old_lv = new_lv = 1
        try:
            old_rei = int(old_p.get("reincarnation", 0) or 0)
            new_rei = int(new_p.get("reincarnation", 0) or 0)
        except (TypeError, ValueError):
            old_rei = new_rei = 0
        old_idx = _REALM_ORDER.index(old_realm) if old_realm in _REALM_ORDER else -1
        new_idx = _REALM_ORDER.index(new_realm) if new_realm in _REALM_ORDER else -1
        # 合法的境界转换只有两种：向前飞升、幻灵天神投胎回凡人。
        # 反方向（上传包里境界比服务端低）一律视为旧快照，不认。
        ascend = old_idx >= 0 and new_idx > old_idx
        reincarnated = (old_idx == len(_REALM_ORDER) - 1 and new_idx == 0 and new_rei > old_rei)
        realm_transitioned = ascend or reincarnated
        realm_rolled_back = (old_idx >= 0 and new_idx >= 0 and new_idx < old_idx
                             and not realm_transitioned)
        if realm_transitioned:
            # 飞升 / 投胎的快照就是权威：新境界 + 等级 1 + 经验 0，原样落库
            pass
        elif realm_rolled_back:
            # 上传的是转换前的旧快照：境界 / 等级 / 经验都保留服务端的
            new_p["realm"] = old_realm
            new_p["level"] = old_lv
            new_p["experience"] = int(old_p.get("experience", 0) or 0)
        elif old_lv > new_lv:
            new_p["level"] = old_lv
            new_p["experience"] = int(old_p.get("experience", 0) or 0)
        elif old_lv == new_lv:
            old_exp = int(old_p.get("experience", 0) or 0)
            new_exp = int(new_p.get("experience", 0) or 0)
            if old_exp > new_exp:
                new_p["experience"] = old_exp
        # 属性点资产兜底：服务端刚发（升级丹按等级补点 / 自动分配落点）的
        # 总量一定 >= 客户端旧快照。旧快照上传时整块回填，避免「点数消失」。
        if realm_transitioned:
            # 飞升 / 投胎的快照自带重置后的属性，信任它
            pass
        elif realm_rolled_back:
            # 上传的是转换前的旧快照：属性也得整块退回服务端的「新境界重置态」，
            # 否则会变成「散仙 1 级却带着飞升前 1200 级的五维」。
            for key in _ATTR_ASSET_KEYS:
                if key in old_p:
                    new_p[key] = old_p[key]
            try:
                new_p["hp"] = max(0, min(int(new_p.get("hp", 0) or 0),
                                         int(new_p.get("max_hp", 0) or 0)))
            except (TypeError, ValueError):
                pass
        else:
            try:
                old_attr_total = _attr_invested_total(old_p)
                new_attr_total = _attr_invested_total(new_p)
            except Exception:
                old_attr_total = new_attr_total = 0
            if old_attr_total > new_attr_total:
                for key in _ATTR_ASSET_KEYS:
                    if key in old_p:
                        new_p[key] = old_p[key]
                try:
                    new_p["hp"] = max(0, min(int(new_p.get("hp", 0) or 0),
                                             int(new_p.get("max_hp", 0) or 0)))
                except (TypeError, ValueError):
                    pass
        channels: dict = {}
        for src in (old_p.get("double_exp_channels"), new_p.get("double_exp_channels")):
            if isinstance(src, dict):
                for key, value in src.items():
                    try:
                        prev = int(channels.get(str(key), 0))
                        channels[str(key)] = max(prev, int(value or 0))
                    except (TypeError, ValueError):
                        continue
        if channels:
            new_p["double_exp_channels"] = channels
            try:
                prev_until = int(new_p.get("double_exp_until", 0) or 0)
            except (TypeError, ValueError):
                prev_until = 0
            new_p["double_exp_until"] = max(prev_until, max(channels.values()))
        # VIP 是服务端权威（GM/活动发放）。客户端本地根本没有开通入口，
        # 上传包里永远是 is_vip=false / vip_level=0，不兜住的话刚开的 VIP
        # 下一次自动存档就被冲掉 —— 表现是「VIP 开了但经验倍率没变」。
        if bool(old_p.get("is_vip", False)) and not bool(new_p.get("is_vip", False)):
            new_p["is_vip"] = True
            new_p["vip_level"] = int(old_p.get("vip_level", 0) or 0)
            new_p["vip_expire_time_str"] = str(old_p.get("vip_expire_time_str", "") or "")
        # 贵族是服务端权威。客户端 playerData 里**根本没有** noble_* 这三个键
        # （等级来自 /noble/info 的 nobleLevel，客户端另外存了一份缓存），
        # 不兜住的话每次自动存档上传都会把贵族冲回 0 级、累计额度清零 ——
        # 表现就是"贵族没生效"。
        for _noble_key in ("noble_points", "noble_level", "noble_name"):
            if _noble_key in old_p:
                new_p[_noble_key] = old_p[_noble_key]
        # 元宝也只由服务端发放/扣减（商城、抽奖、VIP 领取、GM 邮件、副本买次数），
        # 上传包里的旧元宝会把刚扣掉的退回去。
        if "yuanbao_currency" in old_p:
            try:
                new_p["yuanbao_currency"] = int(old_p.get("yuanbao_currency", 0) or 0)
            except (TypeError, ValueError):
                pass

    # 图鉴进度只增不减。
    # 客户端是整包上传，旧快照（切后台前、上传失败留下的）会把刚投进去的 +1 冲掉，
    # 表现就是「图鉴数加了又退回去」。和等级/经验一样，这里取 max。
    old_pc = old.get("petCollectionData") if isinstance(old.get("petCollectionData"), dict) else {}
    if old_pc:
        new_pc = incoming.get("petCollectionData")
        if not isinstance(new_pc, dict):
            new_pc = {}
            incoming["petCollectionData"] = new_pc
        for _pc_key in ("progress", "mutation_progress"):
            src = old_pc.get(_pc_key)
            if not isinstance(src, dict):
                continue
            dst = new_pc.get(_pc_key)
            if not isinstance(dst, dict):
                dst = {}
                new_pc[_pc_key] = dst
            for _sp, _v in src.items():
                try:
                    _old_v = int(_v or 0)
                    _new_v = int(dst.get(_sp, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if _old_v > _new_v:
                    dst[_sp] = _old_v
    old_pets = old.get("petsData") if isinstance(old.get("petsData"), dict) else {}
    new_pets = incoming.get("petsData") if isinstance(incoming.get("petsData"), dict) else {}
    if old_pets and new_pets:
        for key in ("pets", "pet_bag"):
            old_list = old_pets.get(key) if isinstance(old_pets.get(key), list) else []
            new_list = new_pets.get(key) if isinstance(new_pets.get(key), list) else []
            if not old_list:
                continue
            seen = {str(row.get("id", "")) for row in new_list if isinstance(row, dict)}
            for row in old_list:
                if isinstance(row, dict) and str(row.get("id", "")) not in seen:
                    new_list.append(row)
            new_pets[key] = new_list
        if not str(new_pets.get("active_pet_id", "") or "").strip():
            new_pets["active_pet_id"] = str(old_pets.get("active_pet_id", "") or "")

    old_inv = old.get("inventoryData") if isinstance(old.get("inventoryData"), dict) else {}
    new_inv = incoming.get("inventoryData") if isinstance(incoming.get("inventoryData"), dict) else {}
    if old_inv and new_inv:
        key = "pet_equipment_items"
        old_list = old_inv.get(key) if isinstance(old_inv.get(key), list) else []
        new_list = new_inv.get(key) if isinstance(new_inv.get(key), list) else []
        if old_list:
            seen = {str(row.get("id", "")) for row in new_list if isinstance(row, dict)}
            for row in old_list:
                if isinstance(row, dict) and str(row.get("id", "")) not in seen:
                    new_list.append(row)
            new_inv[key] = new_list

        # 藏宝洞的藏宝箱（`item_type=treasure_chest` + `custom_data.treasure_token`）
        # 是**服务端结算线程**直接写进背包的（treasure_round._grant_chest），
        # 只能通过 /treasure/chest/open 核销。发奖那一刻玩家往往不在线，
        # 客户端本地还没有这口箱子，下一次整包上传就会把它冲掉：
        # 邮件里写着「藏宝箱已放入背包」，背包里却什么都没有（2026-10-28 报障）。
        # 所以这里按 token 兜住：只要服务端那份还在、客户端没带上，就补回去。
        # 玩家真开了箱，服务端会先把箱子从 old 里删掉，这段就不会再塞回来。
        old_items = old_inv.get("items")
        if isinstance(old_items, list) and old_items:
            new_items = new_inv.get("items")
            if not isinstance(new_items, list):
                new_items = []
                new_inv["items"] = new_items
            seen_ids = {str(row.get("id", "")) for row in new_items if isinstance(row, dict)}
            for row in old_items:
                if not isinstance(row, dict):
                    continue
                custom = row.get("custom_data")
                if not isinstance(custom, dict) or not custom.get("treasure_token"):
                    continue
                if str(row.get("id", "")) in seen_ids:
                    continue
                new_items.append(row)


def _has_payload(value) -> bool:
    """判断一个字段值里是否真的装了东西（dict/list 递归判空）。"""
    if value is None:
        return False
    if isinstance(value, dict):
        return any(_has_payload(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _path_get(root, parts):
    """按点号路径取值，取不到返回 (False, None)。"""
    cur = root
    for key in parts:
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _path_set(root: dict, parts, value) -> None:
    parent = root
    for key in parts[:-1]:
        nxt = parent.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            parent[key] = nxt
        parent = nxt
    parent[parts[-1]] = value


def _apply_restricted_authority_fields(old: dict, incoming: dict) -> list:
    """按客户端声明的 `restricted_authority_fields` 保住服务端权威字段。

    客户端 compact 上传（`game_manager.upload_cloud_save`）为了省流量，
    把整块 equipmentData / suitData / petEquipmentData 传成 `{}`，
    同时在 `gameSettings.restricted_authority_fields` 里列出这些字段名，
    意思是「这次我没带真数据，别用它覆盖你的」。

    服务端以前完全没实现这个约定，直接 `state.raw["save"] = incoming`
    整体替换 —— 于是每上传一次紧凑存档，服务端的人物装备、套装、宠物装备
    就被清空一次。玩家看到的现象是：

      * 礼包开出来的装备在背包里，点「装备」提示「装备不存在」；
      * 宠物装备页点穿戴提示「灵兽不存在」/「宠装不存在」；
      * 已穿戴的装备过一会儿全掉光。

    规则：服务端旧值**有内容**时才覆盖客户端上传值；旧值为空说明服务端
    自己也丢了（历史遗漏），这时保留客户端上传的内容，让它能自愈。
    """
    gs = incoming.get("gameSettings")
    if not isinstance(gs, dict):
        return []
    fields = gs.get("restricted_authority_fields")
    if not isinstance(fields, list) or not fields:
        return []
    protected: list = []
    for raw in fields:
        path = str(raw or "").strip()
        if not path:
            continue
        parts = [p for p in path.split(".") if p]
        if not parts:
            continue
        found, old_value = _path_get(old, parts)
        if found and _has_payload(old_value):
            _path_set(incoming, parts, old_value)
            protected.append(path)
    return protected


@route("POST", "/save/upload")
def save_upload(ctx: Ctx) -> dict:
    """客户端把整份 saveData 传上来，服务端以最后一次上传为准。"""
    state = ctx.state
    incoming = ctx.body.get("saveData") if isinstance(ctx.body.get("saveData"), dict) else ctx.body
    if not isinstance(incoming, dict) or not incoming:
        return fail("存档内容为空")
    _merge_authoritative_progress(state.save or {}, incoming)
    # 紧凑存档里被声明为「受限」的字段不能用空占位覆盖服务端权威数据，
    # 否则装备/套装/宠装每上传一次就被清空一次。
    protected_fields = _apply_restricted_authority_fields(state.save or {}, incoming)
    if protected_fields:
        state.meta["lastRestrictedFields"] = protected_fields
    # 并集会把「服务端刚发 / 客户端本地已有」的宠都保留下来，但也会把
    # 已经消耗掉的宠（放生 / 图鉴投入 / 幻化副宠）从旧快照里带回来。
    # 按 meta.consumedPetIds 把这类宠剔掉，见 State.mark_pet_consumed。
    try:
        state.drop_consumed_pets(incoming)
    except Exception:
        pass
    # 客户端上传的 gameSettings.current_map 偶尔是空串（刚进游戏、还没选图时），
    # 直接落库会让玩家下次登录变成"无地图"状态，这里用服务端已有值兜住。
    new_gs = incoming.get("gameSettings")
    old_gs = state.save.get("gameSettings") if isinstance(state.save.get("gameSettings"), dict) else {}
    srv_map = str(old_gs.get("current_map", "") or "").strip()
    if isinstance(new_gs, dict):
        up_map = str(new_gs.get("current_map", "") or "").strip()
        if srv_map:
            # current_map 的唯一权威来源是 /player/set-current-map。
            # 换图时客户端会同时发一个 /save/upload（包里还是旧地图）和一个
            # set-current-map，若让上传覆盖，旧地图会晚到把新地图冲掉 ——
            # 表现就是「开了自动换图会乱切地图」。
            new_gs["current_map"] = srv_map
        elif not up_map:
            new_gs["current_map"] = DEFAULT_MAP
        # previous_map 同理：由 set-current-map 维护，上传包里经常是空的
        if srv_map and not str(new_gs.get("previous_map", "") or "").strip():
            new_gs["previous_map"] = str(old_gs.get("previous_map", "") or "")
    # 保留服务端权威字段（次数、抽奖保底等以服务端为准，避免客户端改写上限）
    keep = {
        "saveRevision": state.revision + 1,
        "updatedAt": iso(now()),
    }
    incoming.update(keep)
    state.raw["save"] = incoming
    state.bump()
    state.flush()
    return ok(saveRevision=state.revision, revision=state.revision)


@route("GET", "/user/info")
def user_info(ctx: Ctx) -> dict:
    state = ctx.state
    # catchup_* 是按「与全服最高级的差距」现算的，轮询时要保证是最新值
    state.update_catchup()
    player = state.player
    pets = state.pets
    return {
        "success": True,
        "duplicate": False,
        "newGold": state.gold(),
        "goldGranted": 0,
        "authorityPhase": "enforce",
        "playerExpGranted": 0,
        "playerLevel": int(player.get("level", 1)),
        "playerExperience": int(player.get("experience", 0)),
        "petExpGranted": 0,
        "petLevel": int(pets[0].get("level", 1)) if pets else 1,
        "petExperience": int(pets[0].get("experience", 0)) if pets else 0,
        "captured": False,
        "captureSkipReason": "local",
        "saveData": {"playerData": player, "petsData": state.save.get("petsData", {})},
        # 客户端 network.gd 用 data.has("user") 判断 VIP 信息是否到达，
        # 缺这个字段会导致 vip_info_loaded 永不触发、登录后卡在「欢迎回来」面板。
        # 值必须来自存档：以前这里硬编码 vip_level=0，导致服务端已经开了 VIP、
        # 经验倍率也按 VIP 算了，但客户端界面永远是「未开通」，两边对不上。
        "user": {
            "vip_level": state.vip_tier(),
            "vip_daily_yuanbao_granted": int(player.get("vip_daily_yuanbao_granted", 0) or 0),
            "vip_daily_mutation_egg": str(player.get("vip_daily_mutation_egg", "") or ""),
            "vip_expire_time": str(player.get("vip_expire_time_str", "") or ""),
        },
        # 原版 /user/info 还带这几项（客户端 network.gd 会读 catchup_* 播「投胎追赶」、
        # 读 gangBuffs 叠帮派加成、读 serverUnixTime 校时）。以前缺了，追赶提示永远不出现。
        "serverUnixTime": int(state.now_ts()) if hasattr(state, "now_ts") else int(__import__("time").time()),
        # 建号当天返回 0 会让客户端「新人经验加成」提示不出现（经验其实有算），
        # 这里走 state.newbie_exp_until()：缺字段时按 createdAt+10 天现算并落库。
        "newbie_exp_until": int(state.newbie_exp_until()),
        "catchup_enabled": bool(player.get("catchup_enabled", False)),
        "catchup_gap": int(player.get("catchup_gap", 0) or 0),
        "catchup_day": str(player.get("catchup_day", "") or ""),
        "catchup_field_max_level_ups": int(player.get("catchup_field_max_level_ups", 1) or 1),
        "catchup_first_reincarnation": int(player.get("catchup_first_reincarnation", 0) or 0),
        "gangBuffs": state.meta.get("gangBuffs") or {
            "attackBonusPercent": 0, "defenseBonusPercent": 0, "hpBonusPercent": 0,
            "expBonusPercent": 0, "agilityBonusPercent": 0, "goldDropBonusPercent": 0,
            "gangLevel": 0,
        },
    }


# ---------------------------------------------------------------- 排行榜

@route("GET", "/ranking")
def ranking(ctx: Ctx) -> dict:
    # 榜单是全局的，不依赖当前玩家存档；客户端这几个请求也不带 Authorization
    # （network.gd get_ranking() 只发了 Content-Type），所以必须能无令牌访问。
    return {"success": True, "rankings": STORE.rankings("all")}


@route("GET", "/ranking/players")
def ranking_players(ctx: Ctx) -> dict:
    return {"success": True, "rankings": STORE.rankings("players")}


@route("GET", "/ranking/pets/evolution")
def ranking_pets_evolution(ctx: Ctx) -> dict:
    return {"success": True, "rankings": STORE.rankings("pets_evolution")}


@route("GET", "/ranking/pets/non-evolution")
def ranking_pets_non_evolution(ctx: Ctx) -> dict:
    return {"success": True, "rankings": STORE.rankings("pets_non_evolution")}


# ---------------------------------------------------------------- 邮件 / 公告

@route("GET", "/mail/list")
def mail_list(ctx: Ctx) -> dict:
    return {"success": True, "mails": ctx.state.meta.get("mails", [])}


@route("POST", "/mail/claim")
def mail_claim(ctx: Ctx) -> dict:
    """附件领取：把邮件里的 items 落进背包。"""
    mail_id = ctx.i("mailId") or ctx.i("id")
    state = ctx.state
    for mail in state.meta.get("mails", []):
        if int(mail.get("id", 0)) != mail_id:
            continue
        if mail.get("is_claimed"):
            return fail("附件已领取")
        for entry in (mail.get("attachments") or {}).get("items", []):
            state.add_item(str(entry.get("name", "")), int(entry.get("count", 1)), "item")
        mail["is_claimed"] = True
        mail["is_read"] = True
        state.bump()
        return ok(newGold=state.gold())
    return fail("邮件不存在")


@route("POST", "/mail/read")
def mail_read(ctx: Ctx) -> dict:
    mail_id = ctx.i("mailId") or ctx.i("id")
    for mail in ctx.state.meta.get("mails", []):
        if int(mail.get("id", 0)) == mail_id:
            mail["is_read"] = True
            ctx.state.bump()
            return ok()
    return fail("邮件不存在")


@route("POST", "/mail/delete")
def mail_delete(ctx: Ctx) -> dict:
    mail_id = ctx.i("mailId") or ctx.i("id")
    mails = ctx.state.meta.get("mails", [])
    keep = [m for m in mails if int(m.get("id", 0)) != mail_id]
    if len(keep) == len(mails):
        return fail("邮件不存在")
    ctx.state.meta["mails"] = keep
    ctx.state.bump()
    return ok()


@route("GET", "/announcements")
def announcements(ctx: Ctx) -> dict:
    """系统公告（会以红字 📢 行插进「江湖传闻」）。

    原游戏这个接口线上实测**恒为空数组**（多份抓包全是 []）：公屏只跑玩家事件，
    版本更新 / 维护补偿 / 活动奖励都走邮件，不做每日定时播报。
    这里也不再垫那条「欢迎来到朋友服」——客户端每次进主城都会拉一次公告，
    垫底那条就成了每次重复弹的噪音。要发公告就往 data/announcements.json 里写，
    格式 [{"id":1,"title":"...","content":"...","created_at":"..."}]。
    """
    seeded = load_json(DATA / "announcements.json", [])
    if isinstance(seeded, list):
        return seeded
    return []


# ---------------------------------------------------------------- 离线收益

@route("GET", "/offline/info")
def offline_info(ctx: Ctx) -> dict:
    """离线时长查询（客户端 network.get_offline_info，暂未在 UI 里调用）。

    真正的离线结算在 `/economy/offline-claim`，这里只回时长与 VIP 上限，
    方便以后做「离线收益预览」。
    """
    state = ctx.state
    player = state.player
    try:
        vip = int(player.get("vip_level", 0) or 0)
    except (TypeError, ValueError):
        vip = 0
    vip = max(0, min(3, vip))
    hours_table = RULES.v("offline", "maxHoursByVip", [8, 12, 16, 24]) or [8, 12, 16, 24]
    try:
        max_hours = float(hours_table[min(vip, len(hours_table) - 1)])
    except (TypeError, ValueError, IndexError):
        max_hours = 8.0
    seconds = state.peek_offline_seconds()
    return {
        "success": True,
        "offlineSeconds": seconds,
        "effectiveSeconds": min(seconds, int(max_hours * 3600)),
        "maxOfflineHours": max_hours,
        "isFirstLogin": not bool(state.meta.get("lastSeen")),
        "isVip": vip > 0,
        "vipTier": vip,
    }


@route("POST", "/offline/heartbeat")
def offline_heartbeat(ctx: Ctx) -> dict:
    ctx.state.touch_seen()
    return ok()
