#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""战斗结算：野外 / 天关 / 竞技 / 兄弟天关 / 仙魔通用。

原服是服务端算好后把 `battleLog` 逐事件发给客户端播放，客户端只做表现。
这里按抓到的 125 场真实战斗的字段结构复刻事件流（14 种事件已全部对齐），
伤害、命中、连击、**合击**、暴击、**五行克制** 都在服务端算，客户端拿到的就是权威结果。

伤害链逐条对齐 `battle_system.gd` / `game_data.gd`：

  基础伤害   = max(1, 攻击 - 目标防御/2) × (0.8 + rand()*0.4)
  五行克制   = `GameData.calculate_damage_with_element`
               克制   → ×1.5（变异/进化宠走 ×1.3）
               被克制 → ×0.5（变异/进化宠走 ×0.7）
               「天」克五行、五行被「天」克同此口径（`is_sky_vs_five_element`）
  魂魄加成   = `_get_pet_soul_bonus_percent`
               只有**互克**（克制或被克制）才吃加成；加成取
               `points >= 10 × p²` 的最大 p（1%~100%，上限 bonusCapPercent）
  暴击       = `_apply_crit_to_damage`
               基础 5%（BASE_CRIT_RATE）+ 武功/法宝加成，倍率 1.5（DEFAULT_CRIT_MULTIPLIER）
  合击       = `_check_joint_attack_condition`
               人宠都活着、都不防御、有活怪，且**人宠敏捷都低于所有活怪**才触发
               人物先手一击 → 宠物补一击（含破甲、魂魄）
  一击三伤   = 心法 `triple_shot`（`get_triple_shot_damage_ratio`，1 层 0.8 → 9 层 1.2）
               合击的人物段打最多 3 个目标（用户说的「伤害溢出去打 2~3 只怪」）
  溅射       = 宠物技能 `splash`（`_preroll_pet_splash_plan`）
               `target_count - 1` 个额外目标，伤害再 × `damage_ratio`
  破甲       = 宠物技能 `armor_pierce`（`_apply_pet_armor_pierce_defense`）
               本次命中忽视目标一半防御
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

# 怪物属性随等级的系数，来自真实样本反推：
# Lv1500 → hp 4112 / atk 2448 / def 979 / agi 1024，且 exp=90*level、gold=1*level
MONSTER_HP_PER_LEVEL = 2.741
MONSTER_ATK_PER_LEVEL = 1.632
MONSTER_DEF_PER_LEVEL = 0.653
MONSTER_AGI_PER_LEVEL = 0.683
EXP_PER_LEVEL = 90
GOLD_PER_LEVEL = 1

# ---------------------------------------------------------------- 五行 / 暴击口径
FIVE_ELEMENTS = ("金", "木", "水", "火", "土")
ELEMENT_SKY = "天"
ELEMENT_COUNTER = {"金": "木", "木": "土", "土": "水", "水": "火", "火": "金"}
PLAIN_ELEMENTS = ("", "普通")

# MartialArtsManager 常量
BASE_CRIT_RATE = 0.05
DEFAULT_CRIT_MULTIPLIER = 1.5

DEFAULT_CRIT = {"rate": BASE_CRIT_RATE, "mult": DEFAULT_CRIT_MULTIPLIER}

# `get_pet_soul_lookup_element` / `_soul_bonus_percent_from_points`
SOUL_FIRST_LEVEL_COST = 10
SOUL_BONUS_CAP = 100


@dataclass
class Fighter:
    name: str
    kind: str            # player / pet / monster
    side: str            # A 我方 / B 敌方
    index: int
    hp: float
    max_hp: float
    attack: float
    defense: float
    agility: float
    alive: bool = field(default=True)
    pet_id: str = ""
    level: int = 1
    exp_reward: int = 0
    gold_reward: int = 0
    element: str = ""
    is_evolution_pet: bool = False
    is_mutant: bool = False
    # 未叠加装备/变异倍率的基础敏捷，只用于 combo_trigger.petBaseAgility（原版同口径）
    base_agility: float = 0.0

    def take(self, damage: float) -> None:
        self.hp = max(0.0, self.hp - damage)
        if self.hp <= 0:
            self.alive = False

    def public(self) -> dict:
        return {
            "pet_id": self.pet_id,
            "monster_id": self.pet_id,
            "id": self.pet_id,
            "level": self.level,
            "name": self.name,
            "monster_name": self.name,
            "element": self.element,
            "attack": int(self.attack),
            "defense": int(self.defense),
            "agility": int(self.agility),
            "hp": int(self.max_hp),
            "max_hp": int(self.max_hp),
            "exp_reward": int(self.exp_reward),
            "gold_reward": int(self.gold_reward),
        }


def make_monster(pet_id: str, name: str, level: int, element: str = "") -> Fighter:
    return Fighter(
        name=name,
        kind="monster",
        side="B",
        index=0,
        hp=level * MONSTER_HP_PER_LEVEL,
        max_hp=level * MONSTER_HP_PER_LEVEL,
        attack=level * MONSTER_ATK_PER_LEVEL,
        defense=level * MONSTER_DEF_PER_LEVEL,
        agility=level * MONSTER_AGI_PER_LEVEL,
        pet_id=pet_id,
        level=level,
        exp_reward=int(EXP_PER_LEVEL * level),
        gold_reward=int(GOLD_PER_LEVEL * level),
        element=element,
    )


# ---------------------------------------------------------------- 五行克制

def is_sky_vs_five_element(attacker_element: str, defender_element: str) -> bool:
    """`GameData.is_sky_vs_five_element`：天克五行 / 五行被天克。"""
    atk = str(attacker_element or "").strip()
    deff = str(defender_element or "").strip()
    if atk == ELEMENT_SKY and deff in FIVE_ELEMENTS:
        return True
    if deff == ELEMENT_SKY and atk in FIVE_ELEMENTS:
        return True
    return False


def element_damage(base: int, attacker_element: str, defender_element: str,
                   evolution_rate: bool = False) -> tuple[int, str]:
    """`GameData.calculate_damage_with_element` 的 Python 版。

    返回 (伤害, 提示文案)。文案和原版完全一致，客户端回放直接显示
    「金克木，伤害提升50%！」这类提示。
    """
    atk = str(attacker_element or "").strip()
    deff = str(defender_element or "").strip()
    if atk in PLAIN_ELEMENTS or deff in PLAIN_ELEMENTS:
        return int(base), ""
    if atk == ELEMENT_SKY and deff == ELEMENT_SKY:
        return int(base), ""

    advantage = False
    disadvantage = False
    if atk == ELEMENT_SKY and deff in FIVE_ELEMENTS:
        advantage = True
    elif deff == ELEMENT_SKY and atk in FIVE_ELEMENTS:
        disadvantage = True
    elif ELEMENT_COUNTER.get(atk) == deff:
        advantage = True
    elif ELEMENT_COUNTER.get(deff) == atk:
        disadvantage = True

    if advantage:
        if evolution_rate:
            return int(base * 1.3), "%s克%s，伤害提升30%%！" % (atk, deff)
        return int(base * 1.5), "%s克%s，伤害提升50%%！" % (atk, deff)
    if disadvantage:
        if evolution_rate:
            return int(base * 0.7), "%s被%s克，伤害降低30%%！" % (atk, deff)
        return int(base * 0.5), "%s被%s克，伤害降低50%%！" % (atk, deff)
    return int(base), ""


def soul_applies(attacker_element: str, defender_element: str) -> bool:
    """`_is_element_counter_or_countered`：只有**互克**才给魂魄加成。

    注意「天」只在「天 vs 五行 / 五行 vs 天」时算互克，天对天不给。
    """
    atk = str(attacker_element or "").strip()
    deff = str(defender_element or "").strip()
    if atk in PLAIN_ELEMENTS or deff in PLAIN_ELEMENTS:
        return False
    if atk == ELEMENT_SKY and deff == ELEMENT_SKY:
        return False
    if is_sky_vs_five_element(atk, deff):
        return True
    return ELEMENT_COUNTER.get(atk) == deff or ELEMENT_COUNTER.get(deff) == atk


def soul_lookup_element(attacker_element: str, defender_element: str) -> str:
    """`GameData.get_pet_soul_lookup_element`：攻击方是天时，按「克对方的那一行」。"""
    atk = str(attacker_element or "").strip()
    deff = str(defender_element or "").strip()
    if atk != ELEMENT_SKY:
        return atk
    for key, value in ELEMENT_COUNTER.items():
        if value == deff:
            return key
    return ""


def soul_bonus_percent(points, element: str, first_level_cost: int = SOUL_FIRST_LEVEL_COST,
                       cap: int = SOUL_BONUS_CAP) -> int:
    """`pet_soul_manager.get_bonus_percent`：points >= firstCost × p² 的最大 p。"""
    if not isinstance(points, dict):
        return 0
    try:
        pts = max(0, int(points.get(str(element or "").strip(), 0) or 0))
    except (TypeError, ValueError):
        pts = 0
    if pts <= 0:
        return 0
    bonus = 0
    for p in range(1, max(0, int(cap)) + 1):
        if pts >= max(1, int(first_level_cost)) * p * p:
            bonus = p
        else:
            break
    return bonus


# ---------------------------------------------------------------- 暴击

def apply_crit(damage: int, crit_stats: dict | None) -> tuple[int, bool]:
    stats = crit_stats if isinstance(crit_stats, dict) else DEFAULT_CRIT
    try:
        rate = float(stats.get("rate", BASE_CRIT_RATE))
    except (TypeError, ValueError):
        rate = BASE_CRIT_RATE
    try:
        mult = float(stats.get("mult", DEFAULT_CRIT_MULTIPLIER))
    except (TypeError, ValueError):
        mult = DEFAULT_CRIT_MULTIPLIER
    rate = max(0.0, min(1.0, rate))
    if rate > 0.0 and random.random() < rate:
        return max(1, int(float(damage) * mult)), True
    return int(damage), False


def _soul_suffix(percent: int) -> str:
    if percent <= 0:
        return ""
    return "（魂魄+%d%%）" % percent


# ---------------------------------------------------------------- 单次命中

def _hit(attacker: Fighter, target: Fighter, *,
         crit_stats: dict | None = None,
         attacker_element: str = "",
         evolution_rate: bool = False,
         damage_mult: float = 1.0,
         defense_override: float | None = None,
         soul_points: dict | None = None) -> dict:
    """一次命中的完整伤害链：基础 → 五行 → 魂魄 → 暴击 → 额外倍率。"""
    defense = float(target.defense if defense_override is None else defense_override)
    base = max(1.0, float(attacker.attack) - defense / 2.0)
    base = int(base * (0.8 + random.random() * 0.4))
    damage, element_msg = element_damage(base, attacker_element, target.element, evolution_rate)
    soul_pct = 0
    if soul_points and soul_applies(attacker_element, target.element):
        soul_pct = soul_bonus_percent(soul_points, soul_lookup_element(attacker_element, target.element))
        if soul_pct > 0:
            damage = max(1, int(damage * (1.0 + soul_pct / 100.0)))
    damage, crit = apply_crit(damage, crit_stats)
    if damage_mult != 1.0:
        damage = max(1, int(damage * max(0.0, damage_mult)))
    return {
        "damage": max(1, int(damage)),
        "crit": crit,
        "elementMsg": element_msg,
        "soulPct": soul_pct,
    }


def _roll_chance(rate) -> bool:
    try:
        value = float(rate or 0.0)
    except (TypeError, ValueError):
        return False
    if value <= 0.0:
        return False
    return random.random() < min(1.0, value)


def _roll_armor_pierce(pet_skills: dict | None, extra_rate: float = 0.0) -> bool:
    """`_apply_pet_armor_pierce_defense`：技能触发率 + 法宝破甲率。"""
    cfg = (pet_skills or {}).get("armor_pierce")
    base = 0.0
    if isinstance(cfg, dict):
        try:
            base = float(cfg.get("trigger_rate", 0.0) or 0.0)
        except (TypeError, ValueError):
            base = 0.0
    return _roll_chance(base + max(0.0, float(extra_rate or 0.0)))


def _best_skill_cfg(skill_keys, skill_id: str, skill_defs: dict) -> dict:
    """`PetSkillConfig.get_best_skill_by_ids`：同 skill_id 取最高等级。"""
    best: dict = {}
    best_level = -1
    for key in skill_keys or []:
        cfg = skill_defs.get(str(key))
        if not isinstance(cfg, dict):
            continue
        if str(cfg.get("skill_id", "")) != skill_id:
            continue
        try:
            level = int(cfg.get("level", 0) or 0)
        except (TypeError, ValueError):
            level = 0
        if level > best_level:
            best, best_level = cfg, level
    return best


# 服务端战斗模拟需要消费的宠物技能（和 `pet_skill_config.gd` 的 skill_id 一一对应）：
#   主动   splash 溅墨 / double_shot 连击 / chase 追杀 / life_steal 吸血 /
#          counter_attack 反打 / guard 挡伤 / divine_protect 护主 / self_heal 自愈 /
#          taunt 挑衅 / loyal 守主 / armor_pierce 破甲
#   被动   fatal 必杀（暴击率）/ fierce 凶猛（暴击伤害）
#          —— 强壮/迅捷/长寿/坚固已进客户端上报的 final_attack/agility/max_hp/defense，
#             服务端直接用 final 值，不需要再叠加。
ACTIVE_PET_SKILL_IDS = (
    "splash", "double_shot", "chase", "life_steal", "counter_attack",
    "guard", "divine_protect", "self_heal", "taunt", "loyal", "armor_pierce",
)
PASSIVE_PET_SKILL_IDS = ("fatal", "fierce")


def resolve_pet_skills(skill_keys, skill_defs: dict | None) -> dict:
    """把宠物技能槽（skill_key 列表）解析成 {skill_id: 最佳等级配置}。"""
    defs = skill_defs if isinstance(skill_defs, dict) else {}
    out: dict = {}
    for skill_id in ACTIVE_PET_SKILL_IDS + PASSIVE_PET_SKILL_IDS:
        cfg = _best_skill_cfg(skill_keys, skill_id, defs)
        if cfg:
            out[skill_id] = cfg
    return out


def pet_crit_with_skills(pet_crit: dict | None, pet_skills: dict | None) -> dict:
    """`_get_pet_crit_stats`：宠物暴击在法宝/装备基础上再叠「必杀」「凶猛」。"""
    stats = dict(pet_crit) if isinstance(pet_crit, dict) else dict(DEFAULT_CRIT)
    try:
        rate = float(stats.get("rate", BASE_CRIT_RATE))
    except (TypeError, ValueError):
        rate = BASE_CRIT_RATE
    try:
        mult = float(stats.get("mult", DEFAULT_CRIT_MULTIPLIER))
    except (TypeError, ValueError):
        mult = DEFAULT_CRIT_MULTIPLIER
    fatal = (pet_skills or {}).get("fatal")
    if isinstance(fatal, dict):
        try:
            rate += float(fatal.get("crit_rate_bonus_percent", 0) or 0) / 100.0
        except (TypeError, ValueError):
            pass
    fierce = (pet_skills or {}).get("fierce")
    if isinstance(fierce, dict):
        try:
            mult += float(fierce.get("crit_damage_bonus_percent", 0) or 0) / 100.0
        except (TypeError, ValueError):
            pass
    return {"rate": max(0.0, min(1.0, rate)), "mult": max(1.0, mult)}


def preroll_splash(pet_skills: dict | None) -> dict:
    """`_preroll_pet_splash_plan`：先掷是否触发 + 额外目标数 + 伤害比例。"""
    cfg = (pet_skills or {}).get("splash")
    if not isinstance(cfg, dict):
        return {}
    plan = {
        "triggered": False,
        "ratio": max(0.0, float(cfg.get("damage_ratio", 1.0) or 1.0)),
        "extra_count": 0,
        "skillKey": str(cfg.get("skill_id", "splash")),
    }
    if not _roll_chance(cfg.get("trigger_rate", 0.0)):
        return plan
    plan["triggered"] = True
    try:
        target_count = int(cfg.get("target_count", 2) or 2)
    except (TypeError, ValueError):
        target_count = 2
    plan["extra_count"] = max(0, target_count - 1)
    plan["ratio"] = max(0.0, float(cfg.get("damage_ratio", 1.0) or 1.0))
    return plan


# ---------------------------------------------------------------- 事件构造

def _hit_event(attacker: Fighter, target: Fighter, hit: dict, rnd: int, *,
               event_type: str = "attack", combo: bool = False, joint: bool = False,
               arm_pierced: bool = False) -> dict:
    element_msg = str(hit.get("elementMsg", "") or "")
    soul_pct = int(hit.get("soulPct", 0) or 0)
    if soul_pct > 0:
        element_msg = (element_msg + _soul_suffix(soul_pct)).strip()
    return {
        "type": event_type,
        "round": rnd,
        "attacker": attacker.name,
        "attackerKind": attacker.kind,
        "attackerSide": attacker.side,
        "attackerIndex": attacker.index,
        "damage": int(hit.get("damage", 0)),
        "isCritical": bool(hit.get("crit", False)),
        "isPetAttack": attacker.kind == "pet",
        "isEnemyAttack": attacker.kind == "monster",
        "isArmorPierce": bool(arm_pierced),
        "isCombo": bool(combo),
        "isJointAttack": bool(joint),
        "target": target.name,
        "targetKind": target.kind,
        "targetSide": target.side,
        "targetIndex": target.index,
        "targetHp": int(target.hp),
        "targetMaxHp": int(target.max_hp),
        "elementMsg": element_msg,
        "petSoulBonusPercent": soul_pct,
    }


def _splash_event(attacker: Fighter, target: Fighter, hit: dict, rnd: int,
                  arm_pierced: bool = False) -> dict:
    ev = _hit_event(attacker, target, hit, rnd, event_type="skill_splash",
                    arm_pierced=arm_pierced)
    ev["isPetAttack"] = True
    return ev


def _dead_event(target: Fighter, rnd: int) -> dict:
    return {
        "type": "enemy_dead" if target.side == "B" else "pet_dead",
        "name": target.name,
        "target": target.name,
        "targetIndex": target.index,
        "round": rnd,
    }


def _alive(monsters: list[Fighter]) -> list[Fighter]:
    return [m for m in monsters if m.alive]


def _pick_monster(monsters: list[Fighter], exclude=None):
    pool = [m for m in monsters if m.alive and m not in (exclude or [])]
    if not pool:
        pool = _alive(monsters)
    return random.choice(pool) if pool else None


def _triple_shot_targets(monsters: list[Fighter], primary: Fighter, count: int = 3) -> list[Fighter]:
    """`_build_joint_triple_shot_targets`：主目标在前，其余随机补齐（不重复）。"""
    targets: list[Fighter] = []
    if primary is not None and primary.alive:
        targets.append(primary)
    if len(targets) >= count:
        return targets
    pool = [m for m in monsters if m.alive and m not in targets]
    random.shuffle(pool)
    for extra in pool[: max(0, count - len(targets))]:
        if extra not in targets:
            targets.append(extra)
    return targets


# ---------------------------------------------------------------- 结算主体

def resolve(
    player: Fighter,
    pet: Fighter | None,
    monsters: list[Fighter],
    max_rounds: int = 10,
    player_label: str = "",
    *,
    player_element: str = "",
    player_triple_ratio: float = 0.0,
    player_crit: dict | None = None,
    pet_crit: dict | None = None,
    pet_soul_points: dict | None = None,
    pet_skills: dict | None = None,
    armor_pierce_extra_rate: float = 0.0,
) -> dict:
    """跑一场战斗，返回 {victory, battleLog, monsters, rounds, jointCount, splashHits}。

    合击 / 连击互斥（敏捷条件正好相反）：
      人宠都比所有活怪慢 → 合击
      人宠都不比怪慢     → 连击
      其余（一快一慢）   → 各打各的
    """
    for i, m in enumerate(monsters):
        m.index = i
    log: list[dict] = [{"type": "battle_start", "text": "战斗开始"}]

    # 宠物技能里的被动加成（必杀提高暴击率、凶猛提高暴击伤害）
    pet_crit = pet_crit_with_skills(pet_crit, pet_skills)
    divine_uses: dict[str, int] = {}

    pet_alive = pet is not None
    combo_ready = bool(pet) and bool(monsters) and all(
        player.agility >= m.agility and pet.agility >= m.agility for m in monsters
    )

    agi_desc = "敏捷：人物%d" % int(player.agility)
    # 原版没有出战宠时这里也照样写「宠物0」，不是省略
    agi_desc += "，宠物%d" % int(pet.agility if pet else 0)
    agi_desc += "，怪物 " + "、".join("%s%d" % (m.name, int(m.agility)) for m in monsters) + "。"
    # 原版这句是三段式（实测逐字）：
    #   先给「宠物 vs 怪物」的结论（快或持平 / 慢会打空 / 没出战宠），
    #   再恒定补一句「人物更慢只影响人物自己的命中」——人物其实更快时也照写。
    # 这句只看「宠物 vs 怪物」，和连击是否成立（还要看人物）不是一回事：
    # 人物慢、宠物快时，原版照样写「宠物比怪物快或持平」。
    if not pet:
        agi_desc += "没有出战宠。"
    elif all(pet.agility >= m.agility for m in monsters):
        agi_desc += "宠物比怪物快或持平。"
    else:
        agi_desc += "宠物比怪物慢，宠物攻击会经常打空。"
    agi_desc += "人物更慢只影响人物自己的命中，不影响宠物。"
    log.append({"type": "info", "message": agi_desc})

    last_round = 0
    joint_count = 0
    splash_hits = 0

    def _joint_ready() -> bool:
        """`_check_joint_attack_condition`：人宠都在、都不防御、都慢于所有活怪。"""
        if not pet or not pet_alive or not pet.alive or not player.alive:
            return False
        alive = _alive(monsters)
        if not alive:
            return False
        for m in alive:
            if player.agility >= m.agility or pet.agility >= m.agility:
                return False
        return True

    def _player_attack(rnd: int, *, target=None, joint: bool = False) -> None:
        """人物出手：普通一击；合击时带一击三伤的多目标。"""
        nonlocal splash_hits
        if not player.alive:
            return
        primary = target if (target is not None and target.alive) else _pick_monster(monsters)
        if primary is None:
            return
        ratio = max(0.0, float(player_triple_ratio or 0.0))
        if joint and ratio > 0.0:
            targets = _triple_shot_targets(monsters, primary, 3)
            eff_attack = max(1.0, float(player.attack) * ratio)
            for idx, tgt in enumerate(targets):
                if not tgt.alive:
                    continue
                saved = player.attack
                player.attack = eff_attack
                hit = _hit(player, tgt, crit_stats=player_crit,
                           attacker_element=player_element, damage_mult=1.0)
                player.attack = saved
                tgt.take(hit["damage"])
                log.append(_hit_event(player, tgt, hit, rnd,
                                      event_type="triple_shot_hit", joint=True))
                log[-1]["hitIndex"] = idx + 1
                log[-1]["skill"] = "triple_shot"
                if not tgt.alive:
                    log.append(_dead_event(tgt, rnd))
            return
        hit = _hit(player, primary, crit_stats=player_crit,
                   attacker_element=player_element, damage_mult=1.0)
        primary.take(hit["damage"])
        log.append(_hit_event(player, primary, hit, rnd, joint=joint))
        if not primary.alive:
            log.append(_dead_event(primary, rnd))

    def _pet_life_steal(rnd: int, total_damage: int) -> None:
        """吸血：宠物本次出手总伤害 × 比例回血（原版每回合只掷一次）。"""
        if not pet or not pet.alive or total_damage <= 0:
            return
        cfg = (pet_skills or {}).get("life_steal")
        if not isinstance(cfg, dict) or not _roll_chance(cfg.get("trigger_rate", 0.0)):
            return
        try:
            ratio = max(0.0, min(1.0, float(cfg.get("lifesteal_ratio", 0.0) or 0.0)))
        except (TypeError, ValueError):
            ratio = 0.0
        if ratio <= 0.0:
            return
        heal = max(1, int(float(total_damage) * ratio))
        before = int(pet.hp)
        pet.hp = min(pet.max_hp, pet.hp + heal)
        actual = int(pet.hp) - before
        if actual <= 0:
            return
        log.append({
            "type": "skill_life_steal",
            "round": rnd,
            "attacker": pet.name,
            "target": pet.name,
            "attackerKind": "pet",
            "targetKind": "pet",
            "targetSide": "A",
            "targetIndex": -1,
            "hpRestored": actual,
            "targetHp": int(pet.hp),
            "targetMaxHp": int(pet.max_hp),
            "skillName": str(cfg.get("skill_name", "吸血")),
            "isPetAttack": True,
            "isEnemyAttack": False,
        })

    def _pet_attack(rnd: int, *, target=None, joint: bool = False, allow_splash: bool = True) -> None:
        """宠物出手：破甲 → 五行 → 魂魄 → 暴击；再按技能槽补 溅射/连射/追击/吸血。"""
        nonlocal splash_hits
        if not pet or not pet.alive:
            return
        primary = target if (target is not None and target.alive) else _pick_monster(monsters)
        if primary is None:
            return
        pierced = _roll_armor_pierce(pet_skills, armor_pierce_extra_rate)
        if pierced:
            log.append({
                "type": "skill_armor_pierce",
                "round": rnd,
                "attacker": pet.name,
                "petName": pet.name,
                "skillName": "破防",
            })
        defense = float(primary.defense) * 0.5 if pierced else None
        hit = _hit(pet, primary, crit_stats=pet_crit, attacker_element=pet.element or "",
                   evolution_rate=bool(pet.is_evolution_pet), defense_override=defense,
                   soul_points=pet_soul_points)
        primary.take(hit["damage"])
        log.append(_hit_event(pet, primary, hit, rnd, joint=joint, arm_pierced=pierced))
        if not primary.alive:
            log.append(_dead_event(primary, rnd))

        pet_total = int(hit["damage"])
        style_did = False

        # 溅墨 / 连射：攻击风格互斥，只掷一个；合击的宠物补击不触发（和原版一致）
        if not joint and allow_splash:
            style_id = "splash"
            style_cfg = (pet_skills or {}).get("splash")
            if not isinstance(style_cfg, dict):
                style_id = "double_shot"
                style_cfg = (pet_skills or {}).get("double_shot")
            if isinstance(style_cfg, dict) and _roll_chance(style_cfg.get("trigger_rate", 0.0)):
                if style_id == "splash":
                    extra_count = max(0, int(style_cfg.get("target_count", 2) or 2) - 1)
                    if extra_count > 0:
                        style_did = True
                        extras = [m for m in monsters if m.alive and m is not primary]
                        random.shuffle(extras)
                        for extra in extras[:extra_count]:
                            if not extra.alive:
                                continue
                            extra_pierce = _roll_armor_pierce(pet_skills, armor_pierce_extra_rate)
                            extra_def = float(extra.defense) * 0.5 if extra_pierce else None
                            splash_hit = _hit(pet, extra, crit_stats=pet_crit,
                                              attacker_element=pet.element or "",
                                              evolution_rate=bool(pet.is_evolution_pet),
                                              damage_mult=float(style_cfg.get("damage_ratio", 1.0) or 1.0),
                                              defense_override=extra_def,
                                              soul_points=pet_soul_points)
                            extra.take(splash_hit["damage"])
                            splash_hits += 1
                            pet_total += int(splash_hit["damage"])
                            log.append(_splash_event(pet, extra, splash_hit, rnd, arm_pierced=extra_pierce))
                            if not extra.alive:
                                log.append(_dead_event(extra, rnd))
                else:
                    # 连射：同一目标追加 hit_count-1 段，每段取主击伤害 × damage_ratio_per_hit
                    extra_swings = max(0, int(style_cfg.get("hit_count", 3) or 3) - 1)
                    ratio2 = float(style_cfg.get("damage_ratio_per_hit", 1.0) or 1.0)
                    if extra_swings > 0:
                        style_did = True
                        for seg in range(extra_swings):
                            if not primary.alive:
                                break
                            seg_dmg = max(1, int(float(hit["damage"]) * ratio2))
                            seg_dmg, seg_crit = apply_crit(seg_dmg, pet_crit)
                            primary.take(seg_dmg)
                            pet_total += seg_dmg
                            ev = _hit_event(pet, primary,
                                            {"damage": seg_dmg, "crit": seg_crit,
                                             "elementMsg": "", "soulPct": 0},
                                            rnd, event_type="skill_double_shot")
                            ev["hitIndex"] = seg + 1
                            ev["skill"] = "double_shot"
                            log.append(ev)
                            if not primary.alive:
                                log.append(_dead_event(primary, rnd))

        # 追击：宠物击杀目标（本击未触发溅射/连射、场上还有别的活怪）时，
        #       用这一击的伤害 × chase_ratio 追打另一只，单独掷暴击（原版同口径）
        chase_cfg = (pet_skills or {}).get("chase")
        if (not style_did and not primary.alive and pet.alive
                and isinstance(chase_cfg, dict)
                and _roll_chance(chase_cfg.get("chase_trigger_rate", 0.0))):
            others = [m for m in monsters if m.alive and m is not primary]
            if others:
                chase_target = random.choice(others)
                try:
                    chase_ratio = max(0.0, float(chase_cfg.get("chase_ratio", 1.0) or 1.0))
                except (TypeError, ValueError):
                    chase_ratio = 1.0
                chase_dmg = max(1, int(float(hit["damage"]) * chase_ratio))
                chase_dmg, chase_crit = apply_crit(chase_dmg, pet_crit)
                chase_target.take(chase_dmg)
                pet_total += chase_dmg
                log.append(_hit_event(pet, chase_target,
                                      {"damage": chase_dmg, "crit": chase_crit,
                                       "elementMsg": "", "soulPct": 0},
                                      rnd, event_type="skill_chase"))
                if not chase_target.alive:
                    log.append(_dead_event(chase_target, rnd))

        _pet_life_steal(rnd, pet_total)

    for rnd in range(1, max_rounds + 1):
        if not _alive(monsters):
            break
        last_round = rnd
        log.append({"type": "round", "round": rnd, "text": "第 %d 回合" % rnd})

        if combo_ready and pet and pet.alive:
            log.append(
                {
                    "type": "combo_trigger",
                    "round": rnd,
                    "message": "★ 连击触发! 你和宠物的敏捷都高于所有敌人! ★",
                    "attacker": player.name,
                    "playerAgility": int(player.agility),
                    "petAgility": int(pet.agility),
                    # 原版这里填的是「基础敏捷」（不含装备/变异倍率），不是 final
                    "petBaseAgility": int(pet.base_agility or pet.agility),
                }
            )

        if _joint_ready():
            joint_count += 1
            first = _pick_monster(monsters)
            if first is not None:
                log.append({
                    "type": "joint_attack",
                    "round": rnd,
                    # 原版这条标记就是 {type, round, attacker, message:"合击"}，没有伤害也没有 target
                    "message": "合击",
                    "player": player.name,
                    "pet": pet.name,
                    "attacker": player.name,
                    "target": first.name,
                    "text": "★ 合击触发! %s 和 %s 联手攻击 %s! ★" % (player.name, pet.name, first.name),
                })
                _player_attack(rnd, target=first, joint=True)
                _pet_attack(rnd, target=first, joint=True)
        else:
            _player_attack(rnd)
            if pet and pet.alive:
                _pet_attack(rnd)
                if combo_ready and _alive(monsters) and pet.alive:
                    extra = _pick_monster(monsters)
                    if extra is not None:
                        # 原版先在追击伤害前发一条 combo_attack 标记（无 damage），
                        # 客户端靠它把这一击识别成「连击追击」并压缩前置动画。
                        log.append({
                            "type": "combo_attack",
                            "round": rnd,
                            "attacker": pet.name,
                            "isPetAttack": True,
                            "target": extra.name,
                            "targetIndex": extra.index,
                        })
                        hit = _hit(pet, extra, crit_stats=pet_crit,
                                   attacker_element=pet.element or "",
                                   evolution_rate=bool(pet.is_evolution_pet),
                                   soul_points=pet_soul_points)
                        extra.take(hit["damage"])
                        log.append(_hit_event(pet, extra, hit, rnd, combo=True))
                        if not extra.alive:
                            log.append(_dead_event(extra, rnd))

        # 怪物反击（玩家倒下后由宠物继续承伤，和原版「修士力竭，灵兽独战」一致）
        for monster in monsters:
            if not monster.alive:
                continue
            if not player.alive and not (pet and pet.alive):
                break
            # 选目标：嘲讽（概率强制打宠物）→ 随机 → 忠心（概率替主人承伤）
            target = None
            taunt_cfg = (pet_skills or {}).get("taunt")
            if pet and pet.alive and isinstance(taunt_cfg, dict) and \
                    _roll_chance(taunt_cfg.get("trigger_rate", 0.0)):
                target = pet
                log.append({
                    "type": "skill_taunt",
                    "round": rnd,
                    "attacker": pet.name,
                    "target": pet.name,
                    "text": "%s 触发【嘲讽】，吸引了怪物注意!" % pet.name,
                })
            if target is None:
                if pet and pet.alive and (not player.alive or random.random() < 0.4):
                    target = pet
                else:
                    target = player
                    loyal_cfg = (pet_skills or {}).get("loyal")
                    if pet and pet.alive and isinstance(loyal_cfg, dict) and \
                            _roll_chance(loyal_cfg.get("block_chance", 0.0)):
                        target = pet
                        log.append({
                            "type": "skill_loyal",
                            "round": rnd,
                            "attacker": pet.name,
                            "target": pet.name,
                            "text": "%s 触发【忠心】，替主人承受攻击!" % pet.name,
                        })
            if random.random() < 0.12 and player.agility > monster.agility and target is player:
                log.append(
                    {
                        "type": "miss",
                        "round": rnd,
                        "attacker": monster.name,
                        "attackerKind": "monster",
                        "attackerSide": "B",
                        "attackerIndex": monster.index,
                        "isPetAttack": False,
                        "isEnemyAttack": True,
                        "isCombo": False,
                        "isJointAttack": False,
                        # 原版 miss 也带 turnBatch（怪物波次序号），客户端按它归波
                        "turnBatch": 1,
                        "target": target.name,
                        "targetKind": target.kind,
                        "targetSide": "A",
                        "targetIndex": target.index,
                        "targetHp": int(target.hp),
                        "targetMaxHp": int(target.max_hp),
                    }
                )
                continue
            if target is pet:
                hit = _hit(monster, pet, attacker_element=monster.element or "",
                           evolution_rate=False)
                damage = int(hit["damage"])
                # 格挡：概率按比例减伤
                guard_cfg = (pet_skills or {}).get("guard")
                if isinstance(guard_cfg, dict) and _roll_chance(guard_cfg.get("trigger_rate", 0.0)):
                    try:
                        reduction = max(0.0, min(1.0, float(guard_cfg.get("damage_reduction_ratio", 0.5) or 0.5)))
                    except (TypeError, ValueError):
                        reduction = 0.5
                    if reduction > 0.0:
                        damage = max(1, int(damage * (1.0 - reduction)))
                        log.append({
                            "type": "skill_guard",
                            "round": rnd,
                            "attacker": monster.name,
                            "target": pet.name,
                            "text": "【格挡】%s 减免部分伤害" % pet.name,
                        })
                hit["damage"] = damage
                pet.take(damage)
                log.append(_hit_event(monster, pet, hit, rnd))
                if not pet.alive:
                    # 神佑：阵亡时概率复生（每场最多 max_use_per_battle 次）
                    revived = False
                    divine_cfg = (pet_skills or {}).get("divine_protect")
                    if isinstance(divine_cfg, dict):
                        key = pet.pet_id or "pet"
                        used = divine_uses.get(key, 0)
                        try:
                            max_use = max(1, int(divine_cfg.get("max_use_per_battle", 5) or 5))
                        except (TypeError, ValueError):
                            max_use = 5
                        if used < max_use and _roll_chance(divine_cfg.get("trigger_rate", 0.0)):
                            try:
                                restore_ratio = max(0.0, float(divine_cfg.get("hp_restore_ratio", 0.5) or 0.5))
                            except (TypeError, ValueError):
                                restore_ratio = 0.5
                            restore = max(1.0, float(pet.max_hp) * restore_ratio)
                            divine_uses[key] = used + 1
                            pet.hp = restore
                            pet.alive = True
                            revived = True
                            log.append({
                                "type": "skill_divine_protect",
                                "round": rnd,
                                "attacker": pet.name,
                                "target": pet.name,
                                "targetKind": "pet",
                                "targetIndex": -1,
                                "hpRestored": int(restore),
                                "targetHp": int(restore),
                                "targetMaxHp": int(pet.max_hp),
                                "skillName": "护主",
                            })
                    if not revived:
                        log.append({"type": "pet_dead", "name": pet.name,
                                    "target": pet.name, "round": rnd})
                        if not player.alive:
                            break
                        continue
                # 反击：被怪物攻击后（含被神佑救回的那次）概率反击
                counter_cfg = (pet_skills or {}).get("counter_attack")
                if pet.alive and isinstance(counter_cfg, dict) and \
                        _roll_chance(counter_cfg.get("trigger_rate", 0.0)):
                    try:
                        counter_ratio = max(0.0, float(counter_cfg.get("damage_ratio", 0.5) or 0.5))
                    except (TypeError, ValueError):
                        counter_ratio = 0.5
                    counter_hit = _hit(pet, monster, crit_stats=pet_crit,
                                       attacker_element=pet.element or "",
                                       evolution_rate=bool(pet.is_evolution_pet),
                                       damage_mult=counter_ratio,
                                       soul_points=pet_soul_points)
                    monster.take(counter_hit["damage"])
                    log.append(_hit_event(pet, monster, counter_hit, rnd,
                                          event_type="skill_counter_attack"))
                    if not monster.alive:
                        log.append(_dead_event(monster, rnd))
                continue
            hit = _hit(monster, player, attacker_element=monster.element or "",
                       evolution_rate=False)
            player.take(hit["damage"])
            log.append(_hit_event(monster, player, hit, rnd))
            if not player.alive:
                log.append({"type": "player_dead", "name": player.name,
                            "target": player.name, "round": rnd})
                if not (pet and pet.alive):
                    break

        # 自愈：每回合结束按最大生命百分比回血（原版无概率，每回合都回）
        heal_cfg = (pet_skills or {}).get("self_heal")
        if pet and pet.alive and isinstance(heal_cfg, dict):
            try:
                heal_ratio = max(0.0, min(1.0, float(heal_cfg.get("heal_ratio", 0.0) or 0.0)))
            except (TypeError, ValueError):
                heal_ratio = 0.0
            if heal_ratio > 0.0:
                heal = max(1, int(float(pet.max_hp) * heal_ratio))
                before = int(pet.hp)
                pet.hp = min(pet.max_hp, pet.hp + heal)
                actual = int(pet.hp) - before
                if actual > 0:
                    log.append({
                        "type": "skill_self_heal",
                        "round": rnd,
                        "attacker": pet.name,
                        "target": pet.name,
                        "targetKind": "pet",
                        "targetIndex": -1,
                        "hpRestored": actual,
                        "targetHp": int(pet.hp),
                        "targetMaxHp": int(pet.max_hp),
                        "skillName": "自复",
                    })

        if not player.alive and not (pet and pet.alive):
            break

    # 玩家倒下、宠物还活着 → 原版仍然继续打（宠物单挑），不直接判负
    player_ok = player.alive or bool(pet and pet.alive)
    victory = bool(monsters) and all(not m.alive for m in monsters) and player_ok
    if victory:
        log.append({"type": "victory", "text": "战斗胜利"})
    else:
        log.append({"type": "defeat", "text": "战斗失败"})

    return {
        "victory": victory,
        "battleLog": log,
        "monsters": monsters,
        "rounds": last_round,
        "jointCount": joint_count,
        "splashHits": splash_hits,
        # 本场阵亡状态：原版结算时「死的一方不给经验」（game_manager.gd:2492/2506）
        "playerAlive": bool(player.alive),
        "petAlive": bool(pet.alive) if pet is not None else True,
    }


def empty_player(name: str, level: int) -> Fighter:
    return Fighter(name=name, kind="player", side="A", index=0, hp=1, max_hp=1,
                   attack=1, defense=1, agility=1, level=level)
