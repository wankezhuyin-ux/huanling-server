#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐个宠物技能验证服务端战斗模拟是否产出对应事件。

用法：cd outputs/deploy/server && python3 tools/test_pet_skills_combat.py
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import combat  # noqa: E402

# 从线上同款 skillDefinitions 取值（这里直接内联一份，和 runtime-config 一致）
SKILL_DEFS = {
    "splash_lv3": {"skill_id": "splash", "skill_name": "溅射", "trigger_rate": 1.0, "target_count": 3, "damage_ratio": 1.0, "priority": 100},
    "double_shot_lv3": {"skill_id": "double_shot", "skill_name": "连射", "trigger_rate": 1.0, "hit_count": 3, "damage_ratio_per_hit": 1.0, "priority": 10},
    "chase_lv5": {"skill_id": "chase", "skill_name": "追击", "level": 5, "chase_trigger_rate": 1.0, "chase_ratio": 1.0},
    "counter_attack_lv5": {"skill_id": "counter_attack", "skill_name": "反击", "level": 5, "trigger_rate": 1.0, "damage_ratio": 1.2},
    "life_steal_lv5": {"skill_id": "life_steal", "skill_name": "吸血", "level": 5, "trigger_rate": 1.0, "lifesteal_ratio": 0.3, "priority": 5},
    "guard_lv5": {"skill_id": "guard", "skill_name": "格挡", "level": 5, "trigger_rate": 1.0, "damage_reduction_ratio": 0.9},
    "divine_protect_lv5": {"skill_id": "divine_protect", "skill_name": "神佑", "level": 5, "trigger_rate": 1.0, "hp_restore_ratio": 1.0, "max_use_per_battle": 6},
    "self_heal_lv5": {"skill_id": "self_heal", "skill_name": "自愈", "level": 5, "heal_ratio": 0.25},
    "taunt_lv5": {"skill_id": "taunt", "skill_name": "嘲讽", "level": 5, "trigger_rate": 1.0},
    "loyal_lv5": {"skill_id": "loyal", "skill_name": "忠心", "level": 5, "block_chance": 1.0},
    "armor_pierce_lv5": {"skill_id": "armor_pierce", "skill_name": "破甲", "level": 5, "trigger_rate": 1.0},
    "fatal_lv5": {"skill_id": "fatal", "skill_name": "必杀", "level": 5, "crit_rate_bonus_percent": 25},
    "fierce_lv5": {"skill_id": "fierce", "skill_name": "凶猛", "level": 5, "crit_damage_bonus_percent": 25},
}


def make_pet(hp=100000, attack=50000, defense=1000, agility=10):
    return combat.Fighter(name="小灵兽", kind="pet", side="A", index=1, hp=hp, max_hp=hp,
                          attack=attack, defense=defense, agility=agility,
                          pet_id="pet-1", element="火")


def make_player(agility=1000):
    # 人物敏捷高于所有怪、宠物低于怪 → 既不合击也不连击，宠物走普通攻击分支
    return combat.Fighter(name="修士", kind="player", side="A", index=0,
                          hp=1e9, max_hp=1e9, attack=1, defense=1, agility=agility,
                          element="金")


def run(keys, monster_count=4, monster_level=100, rounds=3, pet=None, player=None):
    pet = pet or make_pet()
    player = player or make_player()
    monsters = [combat.make_monster("m%d" % i, "妖怪%d" % i, monster_level, "水")
                for i in range(monster_count)]
    for i, m in enumerate(monsters):
        m.index = i
    res = combat.resolve(
        player, pet, monsters, rounds,
        player_element="金",
        pet_crit={"rate": 0.05, "mult": 1.5},
        pet_skills=combat.resolve_pet_skills(keys, SKILL_DEFS),
        armor_pierce_extra_rate=0.0,
    )
    counts = Counter(e.get("type", "") for e in res["battleLog"])
    return res, counts


def check(name, keys, expect, **kwargs):
    res, counts = run(keys, **kwargs)
    ok = counts.get(expect, 0) > 0
    print("%-14s %-22s 事件数=%-3d %s" % (name, expect, counts.get(expect, 0),
                                          "PASS" if ok else "FAIL"))
    if not ok:
        print("   日志类型:", dict(counts))
    return ok


def main():
    all_ok = True
    # 单体技能：靠强制触发率 1.0 保证必现
    all_ok &= check("追击 chase", ["chase_lv5"], "skill_chase", monster_count=4)
    # 连射要目标扛得住第一击（一击秒杀时追加段没有目标，和客户端一致），
    # 人物要快于怪、宠物慢于怪，避免走合击/连击分支
    all_ok &= check("连射 double_shot", ["double_shot_lv3"], "skill_double_shot", monster_count=2,
                    monster_level=4000, pet=make_pet(attack=5000), player=make_player(agility=10000))
    all_ok &= check("溅射 splash", ["splash_lv3"], "skill_splash", monster_count=4)
    all_ok &= check("破甲 armor_pierce", ["armor_pierce_lv5"], "skill_armor_pierce", monster_count=2)
    # 被攻击类：用嘲讽 100% 把怪物拉到宠物身上
    all_ok &= check("反击 counter", ["taunt_lv5", "counter_attack_lv5"], "skill_counter_attack",
                    monster_count=3, monster_level=200, pet=make_pet(attack=5000))
    all_ok &= check("格挡 guard", ["taunt_lv5", "guard_lv5"], "skill_guard",
                    monster_count=3, monster_level=200, pet=make_pet(attack=5000))
    all_ok &= check("嘲讽 taunt", ["taunt_lv5"], "skill_taunt", monster_count=2,
                    monster_level=200, pet=make_pet(attack=5000))
    # 神佑：怪物攻击高到能秒掉宠物，必触发复生
    weak_pet = make_pet(hp=10, attack=100, defense=1, agility=10)
    all_ok &= check("神佑 divine", ["taunt_lv5", "divine_protect_lv5"], "skill_divine_protect",
                    monster_count=2, monster_level=500, pet=weak_pet)
    # 忠心：不嘲讽时怪物有概率打玩家，忠心 100% 拦截
    all_ok &= check("忠心 loyal", ["loyal_lv5"], "skill_loyal", monster_count=3,
                    monster_level=200, pet=make_pet(attack=5000))
    # 吸血：宠物有伤害就必触发
    all_ok &= check("吸血 life_steal", ["life_steal_lv5"], "skill_life_steal",
                    monster_count=3, monster_level=100, pet=make_pet(attack=2000))
    # 自愈：每回合结束回血（宠物先被打掉血）
    all_ok &= check("自愈 self_heal", ["taunt_lv5", "self_heal_lv5"], "skill_self_heal",
                    monster_count=2, monster_level=300, pet=make_pet(attack=300))

    # 被动：必杀/凶猛进暴击参数
    skills = combat.resolve_pet_skills(["fatal_lv5", "fierce_lv5"], SKILL_DEFS)
    crit = combat.pet_crit_with_skills({"rate": 0.05, "mult": 1.5}, skills)
    ok = abs(crit["rate"] - 0.30) < 1e-6 and abs(crit["mult"] - 1.75) < 1e-6
    all_ok &= ok
    print("%-14s %-22s %s  %s" % ("必杀/凶猛", "crit rate/mult", crit, "PASS" if ok else "FAIL"))

    # 无技能回归：不应出现任何 skill_ 事件（破甲/溅射/追击…）
    res, counts = run([], monster_count=3)
    stray = [k for k in counts if k.startswith("skill_")]
    ok = not stray and counts.get("attack", 0) > 0
    all_ok &= ok
    print("%-14s %-22s %-20s %s" % ("无技能回归", "attack 存在且无 skill_*",
                                    "stray=%s" % stray, "PASS" if ok else "FAIL"))

    print("\n结果:", "全部通过" if all_ok else "有失败项")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
