#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""战斗：野外 / 天关 / 掉落结算。

原服流程：
  /battle/wild-run   -> 服务端算完一场，返回 fightToken + monsters + battleLog
  /economy/battle-loot -> 客户端拿 fightToken 来结算，服务端发经验/金币/掉落/捕捉
这里照抄这个两段式，奖励口径用真实样本反推：
  玩家经验 = Σ monsters.exp_reward × wildBattle.fieldExpMultiplier(0.5)
  金币     = Σ monsters.gold_reward
  宠物经验 = 与玩家经验同额（样本里两者都是 270000）
"""
from __future__ import annotations

import random
import secrets

import combat
from core import Ctx, RULES, fail, load_json, ok, route
from core import DATA
from core import capture_wild_pet
from core import pet_species_table

MONSTERS = load_json(DATA / "monsters.json")
# 灵兽袋上限，必须和客户端 GameData.MAX_PET_BAG 一致（50）。
PET_BAG_MAX = 50
# 279 张野外地图的真实刷怪表（线上实测 + 客户端静态表交叉验证，见 data/maps.json）
MAPS_DATA = load_json(DATA / "maps.json").get("maps") or {}
# 宠物图鉴：野怪五行以图鉴为准，怪物池里手填的 element 只作兜底
PET_CATALOG = load_json(DATA / "pet-catalog.json") or {}
DEFAULT_MAP_LEVEL = 1500

# 元素克制（简化为同元素互不克制，克 1.2 倍）
ELEMENT_BEATS = {"金": "木", "木": "土", "土": "水", "水": "火", "火": "金"}


def _wild_config() -> dict:
    return RULES.m("gameplay").get("wildBattle", {})


def player_fighter(ctx: Ctx) -> combat.Fighter:
    p = ctx.state.player
    return combat.Fighter(
        name=str(p.get("name", "本地玩家")),
        kind="player",
        side="A",
        index=0,
        hp=float(p.get("final_max_hp", p.get("max_hp", 1000)) or 1000),
        max_hp=float(p.get("final_max_hp", p.get("max_hp", 1000)) or 1000),
        attack=float(p.get("final_attack", p.get("attack_base", 100)) or 100),
        defense=float(p.get("final_defense", p.get("defense_base", 100)) or 100),
        agility=float(p.get("final_agility", p.get("agility_base", 100)) or 100),
        level=int(p.get("level", 1)),
    )


def active_pet(ctx: Ctx) -> dict | None:
    pets = ctx.state.pets
    if not pets:
        return None
    active_id = str(ctx.state.save.get("petsData", {}).get("active_pet_id", ""))
    for pet in pets:
        if str(pet.get("id", "")) == active_id:
            return pet
    return pets[0]


def pet_fighter(pet: dict) -> combat.Fighter:
    return combat.Fighter(
        name=str(pet.get("name", "灵兽")),
        kind="pet",
        side="A",
        index=1,
        hp=float(pet.get("final_max_hp", pet.get("max_hp", 500)) or 500),
        max_hp=float(pet.get("final_max_hp", pet.get("max_hp", 500)) or 500),
        attack=float(pet.get("final_attack", pet.get("attack", 100)) or 100),
        defense=float(pet.get("final_defense", pet.get("defense", 100)) or 100),
        agility=float(pet.get("final_agility", pet.get("agility", 100)) or 100),
        level=int(pet.get("level", 1)),
        pet_id=str(pet.get("pet_id", "")),
        element=str(pet.get("element", "")),
        is_evolution_pet=bool(pet.get("is_evolution_pet", False)),
        is_mutant=bool(pet.get("is_mutant", False)),
    )


# ---------------------------------------------------------------- 战斗加成取值
# 全部对齐 `battle_system.gd` 的取值口径：
#   五行   `FabaoStatBonus.wild_player_element`   → 法宝 player_element 词条，否则玩家自身元素
#   暴击   `_get_player_crit_stats` / `_get_pet_crit_stats`
#   三雕   `_find_equipped_martial_by_effect("triple_shot")` → get_triple_shot_damage_ratio()
#   破甲   `FabaoStatBonus.armor_pierce_bonus_rate()` → armor_pierce_rate 词条 / 100
#   魂魄   `pet_soul_manager.get_bonus_percent` → save.pet_soul.element_points

_MARTIAL_CRIT_RATE_BASE = 10.0      # 1 层 10%，每层 +1.25% → 9 层 20%
_MARTIAL_CRIT_RATE_STEP = 10.0 / 8.0
_MARTIAL_CRIT_DMG_BASE = 10.0       # 1 层 10%，每层 +5% → 9 层 50%
_MARTIAL_CRIT_DMG_STEP = 40.0 / 8.0


def _equipped_affixes(state) -> dict:
    """已装备法宝的词条：{group: {'value'|'element': ...}}。"""
    box = state.save.get("fabao")
    if not isinstance(box, dict):
        return {}
    inst = box.get("equipped")
    if not isinstance(inst, dict):
        return {}
    out: dict = {}
    for aff in inst.get("affixes") or []:
        if isinstance(aff, dict):
            out[str(aff.get("group", ""))] = aff
    return out


def _affix_value(affixes: dict, group: str) -> float:
    aff = affixes.get(group)
    if not isinstance(aff, dict):
        return 0.0
    try:
        return float(aff.get("value", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _equipped_arts(state) -> list[dict]:
    data = state.save.get("martialArtsData")
    if not isinstance(data, dict):
        return []
    equipped = {str(x) for x in (data.get("equipped_ids") or []) if str(x)}
    out: list[dict] = []
    for art in data.get("martial_arts") or []:
        if not isinstance(art, dict):
            continue
        if str(art.get("id", "")) not in equipped:
            continue
        out.append(art)
    return out


def player_battle_element(state) -> str:
    """`FabaoStatBonus.wild_player_element(p.element)`。"""
    if state.player.get("use_fabao_player_element") is False:
        return "普通"
    aff = _equipped_affixes(state).get("player_element")
    if isinstance(aff, dict):
        el = str(aff.get("element", "")).strip()
        if el in combat.FIVE_ELEMENTS:
            return el
    base = str(state.player.get("element", "") or "").strip()
    return base or "普通"


def player_triple_ratio(state) -> float:
    """装备中的「一箭三雕」层数 → 伤害系数（1 层 0.8，每层 +0.05）。"""
    ratio = 0.0
    for art in _equipped_arts(state):
        if str(art.get("effect_type", "")) != "triple_shot":
            continue
        try:
            layer = int(art.get("layer", 1) or 1)
        except (TypeError, ValueError):
            layer = 1
        layer = max(1, min(9, layer))
        ratio = max(ratio, 0.8 + (layer - 1) * 0.05)
    return ratio


def _crit_stats(state, prefix: str) -> dict:
    """`_get_player_crit_stats` / `_get_pet_crit_stats`（不含宠物技能加成）。"""
    rate = combat.BASE_CRIT_RATE
    mult = combat.DEFAULT_CRIT_MULTIPLIER
    for art in _equipped_arts(state):
        effect = str(art.get("effect_type", ""))
        try:
            layer = max(1, min(9, int(art.get("layer", 1) or 1)))
        except (TypeError, ValueError):
            layer = 1
        if effect == "crit_rate" and prefix == "player":
            rate += min(1.0, (_MARTIAL_CRIT_RATE_BASE + (layer - 1) * _MARTIAL_CRIT_RATE_STEP) / 100.0)
        elif effect == "crit_damage":
            mult += (_MARTIAL_CRIT_DMG_BASE + (layer - 1) * _MARTIAL_CRIT_DMG_STEP) / 100.0
    affixes = _equipped_affixes(state)
    rate += _affix_value(affixes, "%s_crit_rate" % prefix) / 100.0
    mult += _affix_value(affixes, "%s_crit_dmg" % prefix) / 100.0
    return {"rate": max(0.0, min(1.0, rate)), "mult": max(1.0, mult)}


def pet_soul_points(state) -> dict:
    """save.pet_soul.element_points（客户端 pet_soul_manager.to_dict 的落盘位置）。"""
    box = state.save.get("pet_soul")
    if not isinstance(box, dict):
        return {}
    points = box.get("element_points")
    return points if isinstance(points, dict) else {}


def pet_skill_keys(pet: dict | None) -> list[str]:
    """宠物技能槽 → skill_key 列表（splash_lv1 / armor_pierce_lv2 …）。"""
    if not isinstance(pet, dict):
        return []
    out: list[str] = []
    for slot in pet.get("skill_slots") or []:
        if isinstance(slot, dict):
            key = str(slot.get("skill_key", "") or "").strip()
            if key:
                out.append(key)
        elif isinstance(slot, str) and slot.strip():
            out.append(slot.strip())
    for key in pet.get("skills") or []:
        text = str(key or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def combat_options(state, pet: dict | None = None) -> dict:
    """一场战斗的加成参数：五行 / 三雕 / 暴击 / 魂魄 / 宠物技能 / 破甲率。"""
    skill_defs = RULES.m("gameplay").get("skillDefinitions") or {}
    affixes = _equipped_affixes(state)
    return {
        "player_element": player_battle_element(state),
        "player_triple_ratio": player_triple_ratio(state),
        "player_crit": _crit_stats(state, "player"),
        "pet_crit": _crit_stats(state, "pet"),
        "pet_soul_points": pet_soul_points(state),
        "pet_skills": combat.resolve_pet_skills(pet_skill_keys(pet), skill_defs),
        "armor_pierce_extra_rate": _affix_value(affixes, "armor_pierce_rate") / 100.0,
    }


def _roll_spawn_count(map_cfg: dict | None) -> int:
    """按线上实测的刷怪数量分布加权抽取；没有样本时退回 wildBattle.spawnMin/Max。"""
    cfg = _wild_config()
    low = int(cfg.get("spawnMin", 1))
    high = max(low, int(cfg.get("spawnMax", 5)))
    dist = (map_cfg or {}).get("spawnDist") or {}
    pairs = [(int(k), int(v)) for k, v in dist.items() if str(k).isdigit() and int(v) > 0]
    if pairs:
        total = sum(w for _n, w in pairs)
        pick = random.uniform(0, total)
        acc = 0.0
        for n, w in sorted(pairs):
            acc += w
            if pick <= acc:
                return max(1, n)
        return max(1, pairs[-1][0])
    return random.randint(low, high)


def spawn_wild_monsters(map_id: str) -> list[combat.Fighter]:
    """按地图真实怪物池刷怪。

    线上口径（实测 279 张图）：
      - 每张图有固定的怪物池，从池里按数量随机抽；
      - 怪物等级 = 该图怪物模板等级（个别图有 ±小幅浮动）；
      - 服务端不认识的 mapId 返回通用「怪物」，等级 = 玩家等级。
    """
    map_cfg = MAPS_DATA.get(map_id)
    if not map_cfg or map_cfg.get("spawnFallback"):
        # 线上对「服务端没有怪物表」的图，直接刷通用怪（实测等级 = 玩家等级 / 图内固定值）
        level = int((map_cfg or {}).get("fallbackLevel") or 1)
        name = str((map_cfg or {}).get("fallbackName") or "怪物")
        return [
            combat.make_monster("monster", name, level, "普通")
            for _ in range(random.randint(1, 4))
        ]
    count = _roll_spawn_count(map_cfg)
    pool = [p for p in (map_cfg.get("pool") or []) if p.get("pet_id")]
    picked: list[combat.Fighter] = []
    if not pool:
        level = int(map_cfg.get("level") or DEFAULT_MAP_LEVEL)
        for _ in range(count):
            picked.append(combat.make_monster("monster", "妖兽", level,
                                              random.choice(list(ELEMENT_BEATS))))
        return picked
    for _ in range(count):
        beast = random.choice(pool)
        lv_min = int(beast.get("levelMin") or beast.get("level") or map_cfg.get("level") or 1)
        lv_max = int(beast.get("levelMax") or beast.get("level") or lv_min)
        level = random.randint(min(lv_min, lv_max), max(lv_min, lv_max))
        pet_id = str(beast.get("pet_id", "monster"))
        # 五行只认宠物图鉴：怪物池是抓包复原的，琳琳/珍珍/盈盈被标成火、金系鸟被标成木，
        # 会让「金打水」这种根本不存在的克制关系出现。图鉴查不到才回落到池里的值。
        catalog_el = str((PET_CATALOG.get(pet_id) or {}).get("element", "") or "").strip()
        if catalog_el == "普通":
            catalog_el = ""
        fighter = combat.make_monster(
            pet_id,
            str(beast.get("name", "妖兽")),
            level,
            catalog_el or str(beast.get("element", "")),
        )
        if beast.get("hp"):
            fighter.hp = float(beast["hp"])
            fighter.max_hp = fighter.hp
        if beast.get("attack"):
            fighter.attack = float(beast["attack"])
        if beast.get("defense"):
            fighter.defense = float(beast["defense"])
        if beast.get("agility"):
            fighter.agility = float(beast["agility"])
        # 线上口径：exp_reward = 90 × 实际等级，gold = 1 × 实际等级
        fighter.exp_reward = level * 90
        fighter.gold_reward = level
        picked.append(fighter)
    return picked


def spawn_tower_monsters(floor: int) -> list[combat.Fighter]:
    cfg = RULES.m("tower").get("monsters", {})
    base = int(cfg.get("levelBase", 100))
    per = int(cfg.get("levelPerFloor", 10))
    level = base + per * max(0, floor - 1)
    count = int(cfg.get("normalMonsterCount", 3))
    if floor % 100 == 0:
        count = int(cfg.get("every100MonsterCount", 1))
    elif floor % 10 == 0:
        count = int(cfg.get("every10MonsterCount", 1))
    if floor >= int(cfg.get("highTierStartFloor", 1300)):
        count = int(cfg.get("highTierNormalCount", 5)) if floor % 10 else int(cfg.get("highTierEvery10Count", 2))
    elements = cfg.get("elements") or ["金", "木", "水", "火", "土"]
    hp_per_level = float(cfg.get("hpPerLevel", 50))
    monsters = []
    for i in range(max(1, count)):
        fighter = combat.make_monster("tower_guard", "天关守卫", level, random.choice(elements))
        fighter.hp = fighter.max_hp = level * hp_per_level
        fighter.exp_reward = int(combat.EXP_PER_LEVEL * level * 0.2)
        fighter.gold_reward = 0
        monster_hp_bonus = 1.0 + (0.5 if floor % 10 == 0 else 0.0)
        fighter.hp = fighter.max_hp = fighter.hp * monster_hp_bonus
        monsters.append(fighter)
    return monsters


_MAP_TIER_ALIASES = {"normal": 0, "easy": 0, "hard": 1, "nightmare": 2, "hell": 3, "special": 4}


def _martial_exp_pct(state) -> float:
    """已装备心法的经验加成（player.gd get_total_exp_multiplier 之外的那一层）。

    客户端 game_manager._apply_field_battle_exp_multipliers 先按
    `martial_arts_manager.get_equipped_effects().exp` 放大一次（百分比），
    再乘 fieldExpMultiplier 和地图难度加成。服务端原来完全没有这一层，
    带「经验」心法的玩家在线上会比原版少掉这部分。
    数值公式照 martial_arts_manager.gd `get_effect_value()`：
    `base_value * (1 + (layer-1) * 0.025)`，layer 夹在 1~9。
    """
    box = state.save.get("martialArtsData")
    if not isinstance(box, dict):
        return 0.0
    arts = box.get("martial_arts") if isinstance(box.get("martial_arts"), list) else []
    equipped_ids = box.get("equipped_ids") if isinstance(box.get("equipped_ids"), list) else []
    ids = {str(x) for x in equipped_ids}
    total = 0.0
    for art in arts:
        if not isinstance(art, dict):
            continue
        if str(art.get("effect_type", "")) != "exp":
            continue
        if ids:
            if str(art.get("id", "")) not in ids:
                continue
        elif not bool(art.get("equipped", False)):
            continue
        try:
            layer = max(1, min(9, int(art.get("layer", 1) or 1)))
            base_value = float(art.get("base_value", 0) or 0)
        except (TypeError, ValueError):
            continue
        total += base_value * (1.0 + (layer - 1) * 0.025)
    return total


def _fabao_exp_pct(state) -> float:
    """法宝经验加成（FabaoStatBonus.exp_multiplier = 1 + exp_pct/100）。

    取已装备法宝实例的 `exp_pct`，没有就用 runtime-config fabao.tiers[tier].expPct。
    没有装备法宝时返回 0（等价于 ×1.0）。
    """
    save = state.save
    box = save.get("fabao") if isinstance(save.get("fabao"), dict) else {}
    inst = box.get("equipped")
    if not isinstance(inst, dict) or not inst:
        return 0.0
    raw = inst.get("exp_pct")
    if raw is not None:
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            pass
    tier = str(inst.get("tier", "normal") or "normal")
    tiers = RULES.m("fabao").get("tiers") if isinstance(RULES.m("fabao"), dict) else {}
    row = (tiers or {}).get(tier) if isinstance(tiers, dict) else None
    if isinstance(row, dict):
        try:
            return float(row.get("expPct", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _map_exp_bonus_pct(map_cfg: dict | None, pending: dict | None = None, difficulty: str = "") -> int:
    """法宝难度地图经验加成（FabaoConfig.map_exp_bonus_pct = 难度档位 × 5%）。

    1~5 档依次 +5%/+10%/+15%/+20%/+25%，普通图 0%。
    难度来源优先级：本次请求 > 开战时的记录 > 地图表里的档位。
    """
    tier = 0
    for raw in (difficulty,
                str((pending or {}).get("difficulty", "") or ""),
                str((map_cfg or {}).get("difficultyTier", "") or "")):
        text = str(raw or "").strip().lower()
        if not text:
            continue
        if text.isdigit():
            tier = max(tier, int(text))
        elif text in _MAP_TIER_ALIASES:
            tier = max(tier, _MAP_TIER_ALIASES[text])
    return max(0, min(5, tier)) * 5


def _store_battle(ctx: Ctx, payload: dict) -> str:
    """暂存一场待结算的战斗，返回 fightToken。

    注意 token 只活在内存里：meta["battles"] 被排除在落盘签名之外（core.State._VOLATILE_META_KEYS），
    这样每次 /battle/wild-run 不会为了记一个「1 秒后就被消费」的 token 而整档重写存档。
    进程重启后未结算的战斗会失效，客户端会提示「战斗记录已失效，请重新发起战斗」。
    """
    token = secrets.token_hex(16)
    pending = ctx.state.meta.setdefault("battles", {})
    # 只保留最近 20 场，避免状态膨胀
    if len(pending) > 20:
        for key in list(pending)[: len(pending) - 20]:
            pending.pop(key, None)
    payload["token"] = token
    pending[token] = payload
    return token


# ---------------------------------------------------------------- 野外

@route("POST", "/battle/wild-run")
def wild_run(ctx: Ctx) -> dict:
    state = ctx.state
    map_id = ctx.s("mapId") or str(state.player.get("current_map", "未知地图"))
    monsters = spawn_wild_monsters(map_id)
    player = player_fighter(ctx)
    pet_data = active_pet(ctx)
    pet = pet_fighter(pet_data) if pet_data else None

    rounds = int(_wild_config().get("battleMaxRounds", 10))
    result = combat.resolve(player, pet, monsters, rounds, **combat_options(state, pet_data))
    token = _store_battle(
        ctx,
        {
            "kind": "wild",
            "mapId": map_id,
            "victory": result["victory"],
            # 「人物阵亡但灵兽独战清完」也算胜利（victory=True），但原版人死了拿不到人物经验，
            # 所以结算时要知道这一场人物到底有没有站到最后。玩家存活只认服务端自己算的。
            "playerAlive": bool(result.get("playerAlive", True)),
            "monsters": [m.public() for m in result["monsters"]],
            "createdAt": ctx.state.meta.get("createdAt", ""),
        },
    )
    return {
        "success": True,
        "fightToken": token,
        "victory": result["victory"],
        "mapId": map_id,
        "monsters": [m.public() for m in result["monsters"]],
        "battleLog": result["battleLog"],
        "playerName": player.name,
        "petName": pet.name if pet else "",
        "mapDifficulty": "normal",
        "jointCount": int(result.get("jointCount", 0) or 0),
        "splashHits": int(result.get("splashHits", 0) or 0),
    }


@route("POST", "/battle/wild-abandon")
def wild_abandon(ctx: Ctx) -> dict:
    token = ctx.s("fightToken")
    battles = ctx.state.meta.setdefault("battles", {})
    battles.pop(token, None)
    return ok()


@route("POST", "/economy/battle-loot")
def battle_loot(ctx: Ctx) -> dict:
    """结算野外战斗：发经验、金币、掉落，并处理捕捉。"""
    state = ctx.state
    token = ctx.s("fightToken")
    pending = state.meta.setdefault("battles", {}).get(token)
    if not pending:
        return fail("战斗记录已失效，请重新发起战斗")
    state.meta["battles"].pop(token, None)
    if not pending.get("victory"):
        return ok(victory=False, goldGained=0, expGained=0, items=[], newGold=state.gold())

    cfg = _wild_config()
    exp_mult = float(cfg.get("fieldExpMultiplier", 0.5))
    monsters = pending.get("monsters", [])
    base_exp = int(sum(int(m.get("exp_reward", 0)) for m in monsters) * exp_mult)
    total_gold = int(sum(int(m.get("gold_reward", 0)) for m in monsters))

    # 线上实测：部分地图服务端不发任何奖励
    #   不认识的 mapId -> code=UNKNOWN_MAP、goldGranted=0、无 playerExpGranted
    #   九曲回廊 / 清幽古洞 系列 -> success=true 但 goldGranted=0、无玩家经验
    map_cfg = MAPS_DATA.get(str(pending.get("mapId", "")))
    if map_cfg is None:
        state.bump()
        return {
            "success": True,
            "goldGranted": 0,
            "newGold": state.gold(),
            "duplicate": False,
            "authorityPhase": "enforce",
            "code": "UNKNOWN_MAP",
        }
    if map_cfg.get("rewardEnabled") is False:
        state.bump()
        out = {
            "success": True,
            "goldGranted": 0,
            "newGold": state.gold(),
            "duplicate": False,
            "authorityPhase": "enforce",
        }
        if map_cfg.get("rewardCode"):
            out["code"] = str(map_cfg["rewardCode"])
        return out

    state.add_gold(total_gold)

    # ---- 经验：复刻 game_manager._apply_field_battle_exp_multipliers + player.add_exp
    # 1) 基础经验 = Σ怪物 exp_reward × wildBattle.fieldExpMultiplier(0.5)
    # 2) 法宝难度地图加成：FabaoConfig.map_exp_bonus_pct = 难度档位 × 5%
    # 3) 总倍率 = VIP倍率(1/1.3/1.6/2.0) + 生效中的双倍经验丹种类数（每种 +100%）
    #    客户端在线模式不自己算经验（直接吃服务端下发的 level/experience），
    #    所以倍率必须在这里乘，否则双倍丹只对离线经验生效、野外经验一点没变。
    map_bonus_pct = _map_exp_bonus_pct(map_cfg, pending, ctx.s("difficulty") or ctx.s("mapDifficulty"))
    # 完整口径（对标 player.gd / game_manager.gd 的乘法顺序）：
    #   base * (1 + 心法exp%) * fieldExpMultiplier * (1 + 地图难度%) * 总倍率 * 法宝exp%
    # 总倍率 = VIP(1/1.3/1.6/2.0) + 生效中的双倍丹种类数
    martial_pct = _martial_exp_pct(state)
    fabao_pct = _fabao_exp_pct(state)
    total_exp = int(round(
        base_exp
        * (1.0 + map_bonus_pct / 100.0)
        * (1.0 + martial_pct / 100.0)
        * state.exp_multiplier()
        * (1.0 + fabao_pct / 100.0)
    ))
    total_exp = max(0, total_exp)

    # 人物阵亡：原版口径「人死了拿不到经验」——灵兽独战把怪清完，算战斗胜利，
    # 金币/掉落/捕捉照常，但人物不涨经验（宠物在场上打，宠物经验照发）。
    # 判定只认开战瞬间服务端自己记的 playerAlive；只有记录缺失（旧 token / 其它入口）
    # 时才用客户端上报的 playerDied 兜底——否则客户端连战标志一旦没重置，
    # 会把正常战斗误判成阵亡、白扣玩家经验。
    if "playerAlive" in pending:
        player_died = pending.get("playerAlive") is False
    else:
        player_died = ctx.body.get("playerDied") is True

    # 服务端权威结算：经验必须在这里真正转成等级，客户端只是把 level/experience
    # 原样接住（game_manager.gd apply_field_battle_loot_player_slice）。
    # 只加经验不判升级 → 玩家永远 1 级、经验条一直满。
    # 野外一场能升几级由追赶机制决定（player.gd get_field_max_level_ups，1~10）。
    state.update_catchup()
    field_ups = state.field_max_level_ups()
    player_exp = 0 if player_died else total_exp
    level_ups = state.grant_player_exp(player_exp, field=True, max_level_ups=field_ups) if player_exp > 0 else 0
    pet_data = active_pet(ctx)
    pet_level_ups = 0
    if pet_data is not None:
        pet_level_ups = state.grant_pet_exp(pet_data, total_exp, field=True, max_level_ups=field_ups)

    # 掉落：按真实掉率表逐只怪 roll
    drops: list[dict] = []
    rates: dict = RULES.drop.get("dropRates", {}) if isinstance(RULES.drop.get("dropRates"), dict) else {}
    for _monster in monsters:
        for name, rate in rates.items():
            if random.random() < float(rate):
                state.add_item(str(name), 1, "equipment" if float(rate) >= 0.01 else "material")
                drops.append({"name": str(name), "count": 1})

    # 捕捉：按原版 pet_manager.capture_from_monster 口径
    #   10% 概率 → 造一只带完整成长资质的宠 → 低于玩家设定阈值投喂图鉴
    #   → 主战栏（10 格）还有位置就进栏 → 满则**放生**（原版不写宠物袋）。
    # 旧实现是「5% 概率静默 append 一只模板宠进 pet_bag」，挂机一晚能把袋子顶到 67/50，
    # 而且模板宠 growth_rates 为空、属性成长全 0 —— 玩家看到的就是「袋里莫名一堆 Lv.1 的宠」。
    captured = None
    capture_skip = ""
    capture_detail: dict = {}
    granted_pets: list = []
    candidate = ctx.body.get("captureCandidate")
    if isinstance(candidate, dict) and candidate.get("petId"):
        pet_id = str(candidate.get("petId"))
        try:
            threshold = float(candidate.get("minGrowthThreshold",
                                            ctx.body.get("minGrowthThreshold", 0.0)) or 0.0)
        except (TypeError, ValueError):
            threshold = 0.0
        result = capture_wild_pet(
            state,
            pet_id,
            # 客户端请求体里没有地图名，兜底用战斗票据记录的 mapId
            map_name=str(ctx.body.get("mapName") or ctx.body.get("currentMap")
                         or pending.get("mapId") or ""),
            min_growth=threshold,
            vip=state.vip_tier() > 0,
        )
        beast = (MONSTERS.get("beasts") or {}).get(pet_id, {})
        pet_row = result.get("pet") if isinstance(result.get("pet"), dict) else {}
        # 名字必须优先取物种中文名：投图鉴 / 放生时 result["pet"] 是空的，
        # 而 monsters.json 的 beasts 只有极少数条目，退回 pet_id 会在战报里打出
        # "linlin 成长值 3.801 低于 7.800，已加入图鉴" 这种英文键。
        species_row = pet_species_table().get(pet_id) or {}
        pet_name = str(
            result.get("name")
            or pet_row.get("name")
            or (species_row.get("name") if isinstance(species_row, dict) else "")
            or beast.get("name")
            or pet_id
        )
        reason = str(result.get("reason", ""))
        growth_now = round(float(result.get("growth", 0) or 0), 3)
        if reason == "" and pet_row:
            granted_pets = [{"id": str(pet_row.get("id", "")), "name": pet_name, "petId": pet_id}]
            captured = {"petId": pet_id, "name": pet_name, "toBag": False}
        elif reason:
            # 客户端 game_manager 按这几条 reason 打战斗日志，文案与提示要对得上
            capture_skip = reason
            if reason.startswith("below_min_growth"):
                capture_detail = {"name": pet_name, "growth": growth_now, "minGrowth": threshold}
            elif reason == "roster_full":
                capture_detail = {"name": pet_name, "growth": growth_now}

    state.bump()
    first_drop = drops[0] if drops else None
    return ok(
        # 线上 battle-loot 的真实响应口径（客户端 game_manager 直接读这些字段）
        victory=True,
        goldGranted=total_gold,
        playerExpGranted=player_exp,
        petExpGranted=total_exp,
        # 客户端据此在战报里补一句「人物阵亡，未获得人物经验」，避免玩家以为漏结算
        playerDied=bool(player_died),
        itemGranted=(first_drop or {}).get("name", ""),
        itemGrantedCount=int((first_drop or {}).get("count", 0)),
        wildDrops=drops,
        captured=bool(captured),
        capturedPet=captured,
        captureSkipReason=capture_skip,
        captureSkipDetail=capture_detail,
        grantedPets=granted_pets,
        authorityPhase="enforce",
        newGold=state.gold(),
        playerLevel=int(state.player.get("level", 1)),
        playerExperience=int(state.player.get("experience", 0)),
        playerLevelUps=int(level_ups),
        petLevel=(int(pet_data.get("level", 1)) if isinstance(pet_data, dict) else 0),
        petExperience=(int(pet_data.get("experience", 0)) if isinstance(pet_data, dict) else 0),
        petLevelUps=int(pet_level_ups),
        saveData=state.save,
        # 兼容旧字段
        goldGained=total_gold,
        expGained=player_exp,
        items=drops,
    )


# ---------------------------------------------------------------- 天关

@route("POST", "/battle/tower-run")
def tower_run(ctx: Ctx) -> dict:
    state = ctx.state
    floor = ctx.i("floor") or int(state.player.get("tower_floor", 1))
    monsters = spawn_tower_monsters(floor)
    player = player_fighter(ctx)
    pet_data = active_pet(ctx)
    pet = pet_fighter(pet_data) if pet_data else None
    rounds = int(RULES.v("tower", "battleMaxRounds", 30) or 30)
    result = combat.resolve(player, pet, monsters, rounds, **combat_options(state, pet_data))
    token = _store_battle(
        ctx,
        {"kind": "tower", "floor": floor, "victory": result["victory"], "monsters": [m.public() for m in result["monsters"]]},
    )
    if result["victory"]:
        highest = int(state.player.get("tower_highest", 0))
        state.player["tower_floor"] = max(int(state.player.get("tower_floor", 1)), floor + 1)
        state.player["tower_highest"] = max(highest, floor)
        state.bump()
    return {
        "success": True,
        "fightToken": token,
        "victory": result["victory"],
        "floor": floor,
        "monsters": [m.public() for m in result["monsters"]],
        "battleLog": result["battleLog"],
        "playerName": player.name,
        "petName": pet.name if pet else "",
        "towerFloor": int(state.player.get("tower_floor", 1)),
        "towerHighest": int(state.player.get("tower_highest", 0)),
    }


# ---------------------------------------------------------------- 结算类奖励

@route("POST", "/economy/tower-clear-reward")
def tower_clear_reward(ctx: Ctx) -> dict:
    state = ctx.state
    floor = ctx.i("floor") or int(state.player.get("tower_floor", 1)) - 1
    gold = int(RULES.v("tower", "clearGoldPerFloor", 100) or 100)
    state.add_gold(gold)
    gifts: list[dict] = []
    every = int(RULES.v("tower", "floorGiftEvery", 10) or 10)
    if floor > 0 and floor % every == 0:
        row = RULES.roll(RULES.v("tower", "floorGiftRewards", []) or [])
        if row:
            state.add_item(str(row.get("name", "天关礼包")), 1, "item")
            gifts.append({"name": str(row.get("name", "")), "count": 1})
    state.bump()
    return ok(goldGained=gold, newGold=state.gold(), gifts=gifts, items=gifts)


@route("POST", "/economy/tower-sweep-reward")
def tower_sweep_reward(ctx: Ctx) -> dict:
    """扫荡奖励：按距今最高层每 10 层发一次百关礼包（走邮件，和线上一致）。"""
    state = ctx.state
    highest = int(state.player.get("tower_highest", 0))
    packs = []
    for floor in range(100, highest + 1, 100):
        packs.append({"name": "%d关礼包" % floor, "count": 1})
    if not packs:
        return ok()
    state.send_mail("天关扫荡 · 百关礼包", "您通过天关扫荡获得以下百关礼包，请查收附件。", packs)
    spent = state.daily("tower_sweep")
    state.daily_set("tower_sweep", spent + 1)
    state.player["tower_sweep_count"] = int(state.player.get("tower_sweep_count", 0)) + 1
    state.bump()
    return ok(items=packs, newGold=state.gold())
