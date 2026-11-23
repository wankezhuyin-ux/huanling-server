#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法宝系统：难度切换、合成、装备、仓库、分解、洗练、重置、词条锁定。

客户端侧 `scripts/fabao/*.gd` 只做渲染，法宝实例（词条/数值）从来不由客户端生成，
全部由服务端产出后下发 —— 所以这里是唯一的权威实现。

协议要点（照 fabao_manager.gd 逐条对齐）：
  * 洗练/重置是两阶段：先 POST 出预览，服务端用 **HTTP 409 + needsConfirm** 回
    `pending`（含 token），客户端弹对比框；再 POST /confirm 带 token 决定是否落盘。
    409 里必须一起带 saveData —— 石头是在第一阶段就扣掉的，客户端要靠它同步扣款。
  * 锁定词条按 (已锁定数 + 1) × 100 元宝收费，解锁不退。
  * 分解产出 50 个同档碎片，合成消耗 100 个。
"""
from __future__ import annotations

import copy
import random

from core import Ctx, RULES, fail, iso, now, ok, route, rnd_id

# ---------------------------------------------------------------- 常量
# 数值以**原服 `/gameplay/runtime-config` 抓包**为准（handoff-2026-10-23 与
# capture-20261023 两份抓包一致）。注意 client 的 fabao_config.gd 里还留着一套
# 更旧的硬编码默认值（combatCap 12/16/20/24/27），进游戏后会被服务端配置覆盖，
# 所以这里的兜底也必须用原服值，否则配置一旦缺失就会悄悄跑偏。
TIER_IDS = ("normal", "hard", "hell", "epic", "legend", "supreme", "chaos", "wushang")
TIER_NAMES = ("普通", "困难", "地狱", "史诗", "传说", "至尊", "混沌", "无上")
SPECIAL_ID = "memorial_2026"
SPECIAL_NAME = "2026纪念"

STAT_MUL = (1, 2, 4, 6, 8, 10, 12, 14)
REINC_NEED = (0, 100, 200, 300, 400, 500, 700, 1000)
NOBLE_NEED = (0, 5, 10, 15, 0, 0, 0, 0)
EXP_PCT = (2.5, 5.0, 7.5, 10.0, 12.5, 15.0, 17.5, 20.0)
COMBAT_CAP = (5, 8, 11, 14, 17, 21, 25, 30)
CRIT_RATE_CAP = (2, 3, 4, 5, 6, 7, 8, 10)

SPECIAL_EXP_PCT = 30.0
SPECIAL_COMBAT_CAP = 40
SPECIAL_CRIT_RATE_CAP = 15
SPECIAL_ARMOR_PIERCE_CAP = 5

AFFIX_GROUPS = (
    "player_atk", "pet_atk", "player_def", "pet_def",
    "player_agi", "pet_agi", "player_hp", "pet_hp",
    "player_crit_rate", "pet_crit_rate", "player_crit_dmg", "pet_crit_dmg",
    "evolve_mutate", "player_element", "armor_pierce_rate",
)
RARE_GROUPS = ("evolve_mutate", "player_element", "armor_pierce_rate")
ELEMENTS = ("金", "木", "水", "火", "土")
CRIT_RATE_GROUPS = ("player_crit_rate", "pet_crit_rate")

SYNTH_FRAGMENT_COUNT = 100
DISMANTLE_FRAGMENT_COUNT = 50
RANDOM_AFFIX_COUNT = 5
COMMON_AFFIX_WEIGHT = 5
RARE_AFFIX_WEIGHT = 1
MAX_WAREHOUSE = 100
AFFIX_LOCK_COST_STEP = 100
REROLL_STONE_NAME = "法宝洗练石"
RESET_STONE_NAME = "法宝重置石"


def _cfg() -> dict:
    payload = RULES.m("fabao")
    return payload if isinstance(payload, dict) else {}


def _tier_row(tier: str) -> dict:
    tiers = _cfg().get("tiers")
    if isinstance(tiers, dict) and isinstance(tiers.get(tier), dict):
        return tiers[tier]  # type: ignore[return-value]
    idx = TIER_IDS.index(tier) if tier in TIER_IDS else 0
    return {
        "mul": STAT_MUL[idx],
        "reinc": REINC_NEED[idx],
        "expPct": EXP_PCT[idx],
        "combatCap": COMBAT_CAP[idx],
        "critRateCap": CRIT_RATE_CAP[idx],
    }


def _is_special(tier: str) -> bool:
    return tier == SPECIAL_ID


def _normalize_map_tier(tier: str) -> str:
    value = (tier or "").strip()
    return value if value in TIER_IDS else "normal"


def _normalize_tier(tier: str) -> str:
    value = (tier or "").strip()
    if value == SPECIAL_ID:
        return SPECIAL_ID
    return value if value in TIER_IDS else "normal"


def _display_name(tier: str) -> str:
    if _is_special(tier):
        return SPECIAL_NAME
    return TIER_NAMES[TIER_IDS.index(_normalize_map_tier(tier))]


def _fragment_name(tier: str) -> str:
    return "%s法宝碎片" % _display_name(tier)


def _fabao_name(tier: str) -> str:
    return "%s法宝" % _display_name(tier)


def _exp_pct(tier: str) -> float:
    if _is_special(tier):
        return SPECIAL_EXP_PCT
    return float(_tier_row(tier).get("expPct", EXP_PCT[0]) or 0.0)


def _combat_cap(tier: str) -> int:
    if _is_special(tier):
        return SPECIAL_COMBAT_CAP
    return max(1, int(_tier_row(tier).get("combatCap", COMBAT_CAP[0]) or COMBAT_CAP[0]))


def _crit_cap(tier: str) -> int:
    if _is_special(tier):
        return SPECIAL_CRIT_RATE_CAP
    return max(1, int(_tier_row(tier).get("critRateCap", CRIT_RATE_CAP[0]) or CRIT_RATE_CAP[0]))


def _reinc_need(tier: str) -> int:
    if _is_special(tier):
        return 0
    return max(0, int(_tier_row(tier).get("reinc", 0) or 0))


def _noble_need(tier: str) -> int:
    if _is_special(tier):
        return 0
    idx = TIER_IDS.index(_normalize_map_tier(tier))
    return NOBLE_NEED[idx]


def _tier_unlocked(tier: str, reincarnation: int, noble: int) -> bool:
    if reincarnation >= _reinc_need(tier):
        return True
    need = _noble_need(tier)
    if need <= 0:
        return False
    return noble >= need


def _unlock_hint(tier: str) -> str:
    name = _display_name(tier)
    need_n = _noble_need(tier)
    # 注意：% 右侧必须是 tuple。写成 list 时 Python 会把整个 list 当成一个值，
    # 于是 "a real number is required, not list" —— 越级切难度会直接 500，
    # 玩家看到的是「操作失败」而不是「投胎满 N 次」。
    if need_n > 0:
        return "投胎满%d次或贵族等级达到%d可解锁%s" % (_reinc_need(tier), need_n, name)
    return "投胎满%d次可解锁%s" % (_reinc_need(tier), name)


def _is_lockable_group(group: str) -> bool:
    return bool(group) and group != "evolve_mutate" and group in AFFIX_GROUPS


def _is_protected_group(tier: str, group: str) -> bool:
    """2026纪念法宝的三条专属词条不参与重置。"""
    return _is_special(tier) and group in ("evolve_mutate", "player_element", "armor_pierce_rate")


# ---------------------------------------------------------------- 存档容器

def _box(state) -> dict:
    """法宝数据落在 saveData.fabao（客户端 game_manager.gd:4824 存的就是这个键）。"""
    save = state.save
    box = save.get("fabao")
    if not isinstance(box, dict):
        box = {}
        save["fabao"] = box
    box.setdefault("current_difficulty", "normal")
    if not isinstance(box.get("equipped"), dict):
        box["equipped"] = None
    for key in ("inventory", "warehouse"):
        if not isinstance(box.get(key), list):
            box[key] = []
    return box


def _locked_groups(inst: dict) -> list[str]:
    """只保留真实存在的、可锁定的词条组（客户端 normalize_locked_groups 同口径）。"""
    present = {str(aff.get("group", "")) for aff in inst.get("affixes", []) if isinstance(aff, dict)}
    raw = inst.get("lockedGroups", inst.get("locked_groups", []))
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            group = str(item).strip()
            if _is_lockable_group(group) and group in present and group not in out:
                out.append(group)
    return out


def _owned(state, fabao_id: str):
    """按 id 找法宝，返回 (实例, 容器键, 容器列表)；找不到返回 (None, "", [])。"""
    box = _box(state)
    fid = (fabao_id or "").strip()
    if not fid:
        return None, "", []
    for key in ("inventory", "warehouse"):
        rows = box.get(key) or []
        for inst in rows:
            if isinstance(inst, dict) and str(inst.get("id", "")) == fid:
                return inst, key, rows
    equipped = box.get("equipped")
    if isinstance(equipped, dict) and str(equipped.get("id", "")) == fid:
        return equipped, "equipped", []
    return None, "", []


# ---------------------------------------------------------------- 词条生成

def _roll_value(tier: str, group: str) -> dict:
    if group == "evolve_mutate":
        return {"group": group, "value": 1}
    if group == "player_element":
        return {"group": group, "element": random.choice(list(ELEMENTS))}
    if group == "armor_pierce_rate":
        return {"group": group, "value": random.randint(1, SPECIAL_ARMOR_PIERCE_CAP)}
    if group in CRIT_RATE_GROUPS:
        return {"group": group, "value": random.randint(1, _crit_cap(tier))}
    return {"group": group, "value": random.randint(1, _combat_cap(tier))}


def _reroll_value(tier: str, affix: dict) -> dict:
    """洗练只换数值，词条种类不变。"""
    group = str(affix.get("group", ""))
    if group == "player_element":
        # 五行只在其它四种里换，避免"洗了个寂寞"
        cur = str(affix.get("element", ""))
        pool = [el for el in ELEMENTS if el != cur] or list(ELEMENTS)
        return {"group": group, "element": random.choice(pool)}
    fresh = _roll_value(tier, group)
    old = int(affix.get("value", 0) or 0)
    # 连续洗出同一个数值很劝退，重摇一次
    if int(fresh.get("value", 0)) == old and group != "evolve_mutate":
        fresh = _roll_value(tier, group)
    return fresh


def _pick_group(exclude: set[str]) -> str:
    pool: list[str] = [g for g in AFFIX_GROUPS if g not in exclude]
    if not pool:
        return ""
    weights = [RARE_AFFIX_WEIGHT if g in RARE_GROUPS else COMMON_AFFIX_WEIGHT for g in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def _new_instance(state, tier: str) -> dict:
    tier = _normalize_tier(tier)
    used: set[str] = set()
    affixes: list[dict] = []
    # 2026纪念法宝固定带三条专属词条，其余位置摇普通词条
    if _is_special(tier):
        for group in RARE_GROUPS:
            affixes.append(_roll_value(tier, group))
            used.add(group)
    target = max(1, int(_cfg().get("randomAffixCount", RANDOM_AFFIX_COUNT) or RANDOM_AFFIX_COUNT))
    while len(affixes) < target:
        group = _pick_group(used)
        if not group:
            break
        used.add(group)
        affixes.append(_roll_value(tier, group))
    return {
        "id": rnd_id("fb"),
        "tier": tier,
        "name": _fabao_name(tier),
        "affixes": affixes,
        "lockedGroups": [],
        "exp_pct": _exp_pct(tier),
        "created_at": iso(now()),
    }


def _affix_summary(inst: dict) -> str:
    parts = []
    for aff in inst.get("affixes", []):
        if not isinstance(aff, dict):
            continue
        group = str(aff.get("group", ""))
        if group == "player_element":
            parts.append("五行%s" % str(aff.get("element", "")))
        elif group == "evolve_mutate":
            parts.append("变异+1%")
        else:
            parts.append("%s+%d%%" % (group, int(aff.get("value", 0) or 0)))
    return "、".join(parts)


# ---------------------------------------------------------------- 响应封装

def _resp(state, **extra) -> dict:
    payload = ok(saveData=state.save, fabaoData=_box(state))
    payload.update(extra)
    return payload


def _confirm_409(state, pending: dict) -> dict:
    """两阶段确认：客户端只在 HTTP 409 + needsConfirm 时才弹对比框。"""
    payload = _resp(state, needsConfirm=True, pending=pending, token=pending.get("token", ""))
    payload["_status"] = 409
    return payload


def _next_token(state, kind: str) -> str:
    return "%s_%s" % (kind, rnd_id(""))


def _pending_of(state) -> dict:
    pending = state.meta.get("fabaoPending")
    return pending if isinstance(pending, dict) and pending else {}


# ================================================================ 只读

@route("GET", "/fabao/overview")
def fabao_overview(ctx: Ctx) -> dict:
    """GM / 自检用：一次拿到当前法宝全部状态和本文档口径的数值表。"""
    state = ctx.state
    box = _box(state)
    player = state.player
    reincarnation = int(player.get("reincarnation", 0) or 0)
    noble = int(player.get("noble_level", 0) or 0)
    tiers = []
    for tier in TIER_IDS:
        tiers.append({
            "id": tier,
            "name": _display_name(tier),
            "mul": STAT_MUL[TIER_IDS.index(tier)],
            "reincNeed": _reinc_need(tier),
            "nobleNeed": _noble_need(tier),
            "expPct": _exp_pct(tier),
            "combatCap": _combat_cap(tier),
            "critRateCap": _crit_cap(tier),
            "unlocked": _tier_unlocked(tier, reincarnation, noble),
        })
    return ok(
        currentDifficulty=_normalize_map_tier(box.get("current_difficulty", "normal")),
        equipped=box.get("equipped"),
        inventory=box.get("inventory", []),
        warehouse=box.get("warehouse", []),
        pending=_pending_of(state),
        reincarnation=reincarnation,
        nobleLevel=noble,
        stoneReroll=state.item_count(REROLL_STONE_NAME),
        stoneReset=state.item_count(RESET_STONE_NAME),
        tiers=tiers,
    )


# ================================================================ 难度

@route("POST", "/fabao/set-difficulty")
def fabao_set_difficulty(ctx: Ctx) -> dict:
    state = ctx.state
    # 这里必须校验原始值：normalize 会把任何乱码都折成 normal，
    # 那样"传个不存在的难度"会静默变成切回普通难度，玩家以为切成功了。
    want = ctx.s("tier").strip()
    if want not in TIER_IDS:
        return fail("无效的地图难度")
    player = state.player
    reincarnation = int(player.get("reincarnation", 0) or 0)
    noble = int(player.get("noble_level", 0) or 0)
    if not _tier_unlocked(want, reincarnation, noble):
        return fail(_unlock_hint(want))
    box = _box(state)
    box["current_difficulty"] = want
    # 客户端 apply_from_game_settings 读的是 gameSettings.current_map_difficulty，
    # 只写 fabao.current_difficulty 的话重登会退回普通难度。
    gs = state.save.get("gameSettings")
    if not isinstance(gs, dict):
        gs = {}
        state.save["gameSettings"] = gs
    gs["current_map_difficulty"] = want
    state.bump()
    return _resp(state, tier=want, current_map_difficulty=want,
                 displayName=_display_name(want), statMul=STAT_MUL[TIER_IDS.index(want)],
                 message="已切换到%s难度" % _display_name(want))


# ================================================================ 合成 / 分解

@route("POST", "/fabao/synthesize")
def fabao_synthesize(ctx: Ctx) -> dict:
    state = ctx.state
    tier = _normalize_tier(ctx.s("tier"))
    need = max(1, int(_cfg().get("synthFragmentCount", SYNTH_FRAGMENT_COUNT) or SYNTH_FRAGMENT_COUNT))
    frag = _fragment_name(tier)
    have = state.item_count(frag)
    if have < need:
        return fail("需要%d个%s才能合成（当前%d个）" % (need, frag, have))
    state.take_item(frag, need)
    inst = _new_instance(state, tier)
    box = _box(state)
    box["inventory"].append(inst)
    state.bump()
    return _resp(state, fabao=inst, instance=inst,
                 message="合成成功：%s（%s）" % (inst["name"], _affix_summary(inst)))


@route("POST", "/fabao/dismantle")
def fabao_dismantle(ctx: Ctx) -> dict:
    state = ctx.state
    inst, where, rows = _owned(state, ctx.s("fabaoId"))
    if inst is None:
        return fail("法宝不存在")
    if where == "equipped":
        return fail("请先卸下法宝再分解")
    tier = _normalize_tier(str(inst.get("tier", "normal")))
    rows.remove(inst)
    frag = _fragment_name(tier)
    state.add_item(frag, DISMANTLE_FRAGMENT_COUNT, "material")
    state.bump()
    return _resp(state, fragments=DISMANTLE_FRAGMENT_COUNT, itemName=frag,
                 message="分解成功，获得%d个%s" % (DISMANTLE_FRAGMENT_COUNT, frag))


@route("POST", "/fabao/discard")
def fabao_discard(ctx: Ctx) -> dict:
    state = ctx.state
    inst, where, rows = _owned(state, ctx.s("fabaoId"))
    if inst is None:
        return fail("法宝不存在")
    if where == "equipped":
        return fail("请先卸下法宝再丢弃")
    rows.remove(inst)
    state.bump()
    return _resp(state, message="已丢弃%s" % str(inst.get("name", "法宝")))


# ================================================================ 装备 / 仓库

@route("POST", "/fabao/equip")
def fabao_equip(ctx: Ctx) -> dict:
    state = ctx.state
    box = _box(state)
    inst, where, rows = _owned(state, ctx.s("fabaoId"))
    if inst is None:
        return fail("法宝不存在")
    if where == "warehouse":
        return fail("请先从仓库取出法宝")
    if where == "equipped":
        return _resp(state, equipped=inst, message="该法宝已在装备中")
    rows.remove(inst)
    old = box.get("equipped")
    if isinstance(old, dict):
        box["inventory"].append(old)
    box["equipped"] = inst
    state.bump()
    return _resp(state, equipped=inst, message="已装备%s" % str(inst.get("name", "法宝")))


@route("POST", "/fabao/unequip")
def fabao_unequip(ctx: Ctx) -> dict:
    state = ctx.state
    box = _box(state)
    inst = box.get("equipped")
    if not isinstance(inst, dict):
        return fail("当前没有装备法宝")
    box["equipped"] = None
    box["inventory"].append(inst)
    state.bump()
    return _resp(state, equipped=None, message="已卸下%s" % str(inst.get("name", "法宝")))


@route("POST", "/fabao/store-warehouse")
def fabao_store_warehouse(ctx: Ctx) -> dict:
    state = ctx.state
    box = _box(state)
    inst, where, rows = _owned(state, ctx.s("fabaoId"))
    if inst is None:
        return fail("法宝不存在")
    if where == "equipped":
        return fail("请先卸下法宝再存仓库")
    if where == "warehouse":
        return fail("该法宝已在仓库")
    if len(box["warehouse"]) >= MAX_WAREHOUSE:
        return fail("仓库已满（上限%d）" % MAX_WAREHOUSE)
    rows.remove(inst)
    box["warehouse"].append(inst)
    state.bump()
    return _resp(state, warehouseCount=len(box["warehouse"]),
                 message="已存入仓库（%d/%d）" % (len(box["warehouse"]), MAX_WAREHOUSE))


@route("POST", "/fabao/retrieve-warehouse")
def fabao_retrieve_warehouse(ctx: Ctx) -> dict:
    state = ctx.state
    box = _box(state)
    inst, where, rows = _owned(state, ctx.s("fabaoId"))
    if inst is None:
        return fail("法宝不存在")
    if where != "warehouse":
        return fail("该法宝不在仓库")
    rows.remove(inst)
    box["inventory"].append(inst)
    state.bump()
    return _resp(state, warehouseCount=len(box["warehouse"]), message="已从仓库取出")


# ================================================================ 洗练 / 重置（两阶段）

@route("POST", "/fabao/reroll")
def fabao_reroll(ctx: Ctx) -> dict:
    state = ctx.state
    # 已经有一份待确认结果时，把预览重新推给客户端，避免石头被重复扣
    existing = _pending_of(state)
    if existing:
        return _confirm_409(state, existing)
    inst, where, _rows = _owned(state, ctx.s("fabaoId"))
    if inst is None:
        return fail("法宝不存在")
    if where != "inventory":
        return fail("只能洗练背包里未穿戴的法宝")
    tier = _normalize_tier(str(inst.get("tier", "normal")))
    locked = _locked_groups(inst)
    affixes = [aff for aff in inst.get("affixes", []) if isinstance(aff, dict)]
    rerollable = [aff for aff in affixes
                  if str(aff.get("group", "")) not in locked
                  and str(aff.get("group", "")) != "evolve_mutate"]
    if not rerollable:
        return fail("请至少保留一条未锁定属性")
    if state.item_count(REROLL_STONE_NAME) < 1:
        return fail("缺少%s" % REROLL_STONE_NAME)

    preview: list[dict] = []
    for aff in affixes:
        group = str(aff.get("group", ""))
        if group in locked or group == "evolve_mutate":
            preview.append(copy.deepcopy(aff))
        else:
            preview.append(_reroll_value(tier, aff))

    state.take_item(REROLL_STONE_NAME, 1)
    token = _next_token(state, "reroll")
    pending = {
        "kind": "reroll",
        "token": token,
        "fabaoId": str(inst.get("id", "")),
        "tier": tier,
        "exp_pct": float(inst.get("exp_pct", _exp_pct(tier)) or _exp_pct(tier)),
        "currentAffixes": copy.deepcopy(affixes),
        "previewAffixes": preview,
        "lockedGroups": locked,
        "stone": REROLL_STONE_NAME,
        "createdAt": iso(now()),
    }
    state.meta["fabaoPending"] = pending
    state.bump()
    return _confirm_409(state, pending)


@route("POST", "/fabao/reroll/confirm")
def fabao_reroll_confirm(ctx: Ctx) -> dict:
    state = ctx.state
    pending = _pending_of(state)
    token = ctx.s("token")
    if not pending or str(pending.get("token", "")) != token:
        return fail("洗练结果已失效，请重新洗练")
    apply = bool(ctx.body.get("apply", False))
    box = _box(state)
    inst = None
    for row in box["inventory"]:
        if isinstance(row, dict) and str(row.get("id", "")) == str(pending.get("fabaoId", "")):
            inst = row
            break
    if inst is None:
        # 法宝被分解/移动了：把预览作废，但石头已经扣过，如实告知
        state.meta.pop("fabaoPending", None)
        state.bump()
        return fail("法宝已不在背包，本次洗练结果作废（%s已消耗）" % REROLL_STONE_NAME)
    if apply:
        preview = pending.get("previewAffixes")
        if isinstance(preview, list) and preview:
            inst["affixes"] = copy.deepcopy(preview)
            inst["lockedGroups"] = _locked_groups(inst)
    state.meta.pop("fabaoPending", None)
    state.bump()
    return _resp(state, applied=apply, fabao=inst,
                 message="已应用本次洗练" if apply else "已放弃本次洗练")


@route("POST", "/fabao/reset")
def fabao_reset(ctx: Ctx) -> dict:
    state = ctx.state
    existing = _pending_of(state)
    if existing:
        return _confirm_409(state, existing)
    inst, where, _rows = _owned(state, ctx.s("fabaoId"))
    if inst is None:
        return fail("法宝不存在")
    if where != "inventory":
        return fail("只能重置背包里未穿戴的法宝")
    tier = _normalize_tier(str(inst.get("tier", "normal")))
    locked = _locked_groups(inst)
    affixes = [aff for aff in inst.get("affixes", []) if isinstance(aff, dict)]
    keep_groups = set(locked)
    for aff in affixes:
        group = str(aff.get("group", ""))
        if _is_protected_group(tier, group):
            keep_groups.add(group)
    resettable = [aff for aff in affixes if str(aff.get("group", "")) not in keep_groups]
    if not resettable:
        return fail("没有可重置的未锁定属性")
    if state.item_count(RESET_STONE_NAME) < 1:
        return fail("缺少%s" % RESET_STONE_NAME)

    used: set[str] = set(keep_groups)
    preview: list[dict] = []
    # 顺序必须和 currentAffixes 一一对应：客户端的重置对比是按 index 逐行比对的
    for aff in affixes:
        group = str(aff.get("group", ""))
        if group in keep_groups:
            preview.append(copy.deepcopy(aff))
            continue
        new_group = _pick_group(used)
        if not new_group:
            preview.append(copy.deepcopy(aff))
            continue
        used.add(new_group)
        preview.append(_roll_value(tier, new_group))

    state.take_item(RESET_STONE_NAME, 1)
    pending = {
        "kind": "reset",
        "token": _next_token(state, "reset"),
        "fabaoId": str(inst.get("id", "")),
        "tier": tier,
        "exp_pct": float(inst.get("exp_pct", _exp_pct(tier)) or _exp_pct(tier)),
        "currentAffixes": copy.deepcopy(affixes),
        "previewAffixes": preview,
        "lockedGroups": locked,
        "stone": RESET_STONE_NAME,
        "createdAt": iso(now()),
    }
    state.meta["fabaoPending"] = pending
    state.bump()
    return _confirm_409(state, pending)


@route("POST", "/fabao/reset/confirm")
def fabao_reset_confirm(ctx: Ctx) -> dict:
    state = ctx.state
    pending = _pending_of(state)
    token = ctx.s("token")
    if not pending or str(pending.get("token", "")) != token:
        return fail("重置结果已失效，请重新重置")
    apply = bool(ctx.body.get("apply", False))
    box = _box(state)
    inst = None
    for row in box["inventory"]:
        if isinstance(row, dict) and str(row.get("id", "")) == str(pending.get("fabaoId", "")):
            inst = row
            break
    if inst is None:
        state.meta.pop("fabaoPending", None)
        state.bump()
        return fail("法宝已不在背包，本次重置结果作废（%s已消耗）" % RESET_STONE_NAME)
    if apply:
        preview = pending.get("previewAffixes")
        if isinstance(preview, list) and preview:
            inst["affixes"] = copy.deepcopy(preview)
            inst["lockedGroups"] = _locked_groups(inst)
    state.meta.pop("fabaoPending", None)
    state.bump()
    return _resp(state, applied=apply, fabao=inst,
                 message="已应用本次重置" if apply else "已放弃本次重置")


# ================================================================ 词条锁定

@route("POST", "/fabao/affix/lock")
def fabao_affix_lock(ctx: Ctx) -> dict:
    state = ctx.state
    group = ctx.s("group").strip()
    inst, where, _rows = _owned(state, ctx.s("fabaoId"))
    if inst is None:
        return fail("法宝不存在")
    if where != "inventory":
        return fail("只能锁定背包里未穿戴的法宝词条")
    if not _is_lockable_group(group):
        return fail("该词条不支持锁定")
    present = {str(aff.get("group", "")) for aff in inst.get("affixes", []) if isinstance(aff, dict)}
    if group not in present:
        return fail("法宝上没有该词条")
    locked = _locked_groups(inst)
    if group in locked:
        return _resp(state, group=group, locked=True, cost=0, message="该词条已锁定")
    cost = (len(locked) + 1) * AFFIX_LOCK_COST_STEP
    if state.yuanbao() < cost:
        return fail("元宝不足（需要%d，当前%d）" % (cost, state.yuanbao()))
    state.add_yuanbao(-cost)
    locked.append(group)
    inst["lockedGroups"] = locked
    state.bump()
    return _resp(state, group=group, locked=True, cost=cost, newYuanbao=state.yuanbao(),
                 message="已锁定该词条，消耗%d元宝" % cost)


@route("POST", "/fabao/affix/unlock")
def fabao_affix_unlock(ctx: Ctx) -> dict:
    state = ctx.state
    group = ctx.s("group").strip()
    inst, where, _rows = _owned(state, ctx.s("fabaoId"))
    if inst is None:
        return fail("法宝不存在")
    locked = _locked_groups(inst)
    if group not in locked:
        return _resp(state, group=group, locked=False, cost=0, message="该词条未锁定")
    locked.remove(group)
    inst["lockedGroups"] = locked
    state.bump()
    return _resp(state, group=group, locked=False, cost=0, message="已解锁该词条")
