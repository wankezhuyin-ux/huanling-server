#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""藏宝洞「天机幻战」：镜像对镜像的逐场战斗、单败签表与战报。

原版口径（客户端规则说明 + 藏宝洞界面已内置的战报 UI）：
  - 报名截止后转入争夺，各箱分别开打，箱与箱互不影响；
  - 争夺是**单败淘汰**：每箱独立一张签表，奇数人时末位轮空（bye）；
  - 每场都是「镜像 vs 镜像」，镜像 = 报名时定格的 人物 + 出战宠；
  - 战后留日志，客户端「查看战斗日志」按 chestId 拉
    GET /treasure/battle-log?chestId=&page=&pageSize=
    逐场列「第X轮 第Y场：【甲】 vs 【乙】 → 胜者【丙】」，可点「查看本场回放」。

日志字段口径严格对齐客户端 _format_treasure_battle_log_line()：
  start / info        message
  player_info         name hp maxHp attack defense agility element
  pet_info            同上（我方灵兽）
  enemy_info / enemy_pet_info   同上（对方）
  round               round
  attack              attacker target damage targetHp targetMaxHp
                      attackerRole isEnemyAttack isPetAttack crit
  miss                attacker target attackerRole
  dodge               attacker target targetRole
  combo_trigger       message
  combo_attack        attacker attackerRole
  joint_attack        player pet target
  pet_dead / player_dead / enemy_dead   name
  victory             message

数值口径：日志里显示的 HP / 伤害直接用镜像里宠物与人物的**真实终值**（玩家看得懂，
和面板对得上），但扣血是按目标「最大生命的百分比」结算的 —— 本作数值膨胀得厉害
（3 万攻 / 9 万血），照搬公式两下就结束，战报没有任何内容可看。
"""
from __future__ import annotations

import random

MAX_ROUNDS = 12          # 单场最多回合数，打不完按剩余血量比例判
LOG_MAX = 90             # 单场日志条数上限（超出后停止记录，避免 state 膨胀）
COMBO_CHANCE = 0.18      # 连击
COMBO_RATE = 0.55        # 连击追击伤害系数
JOINT_CHANCE = 0.10      # 合击
JOINT_RATE = 0.50        # 合击附加伤害系数
CRIT_CHANCE = 0.10
CRIT_RATE = 1.8
DODGE_BASE = 0.06
DODGE_SPAN = 0.10


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(src: dict, keys: tuple[str, ...], default: float) -> float:
    for key in keys:
        if src.get(key) not in (None, ""):
            return _num(src.get(key), default)
    return default


# ---------------------------------------------------------------- 战斗四维

def combat_of(snap: dict) -> dict:
    """从镜像快照里抽「人物 + 出战宠」的战斗四维（抓不到就按默认值兜底）。"""
    snap = snap or {}
    player = snap.get("player") or {}
    pet = snap.get("pet") or {}
    out: dict = {}
    if pet:
        out["pet"] = {
            "name": str(pet.get("name") or "灵兽"),
            "level": int(_num(pet.get("level"))),
            "element": str(pet.get("element") or "无"),
            "hp": max(1.0, _first(pet, ("final_max_hp", "max_hp", "hp"), 1200.0)),
            "attack": max(1.0, _first(pet, ("final_attack", "attack"), 80.0)),
            "defense": max(0.0, _first(pet, ("final_defense", "defense"), 60.0)),
            "agility": max(0.0, _first(pet, ("final_agility", "agility"), 60.0)),
        }
    if player:
        out["player"] = {
            "name": str(player.get("name") or "修士"),
            "level": int(_num(player.get("level"))),
            "element": str(player.get("element") or "无"),
            "hp": max(1.0, _first(player, ("final_max_hp", "max_hp"), 2000.0)),
            "attack": max(1.0, _first(player, ("final_attack", "attack_base"), 120.0)),
            "defense": max(0.0, _first(player, ("final_defense", "defense_base"), 100.0)),
            "agility": max(0.0, _first(player, ("final_agility", "agility_base"), 100.0)),
        }
    return out


def _fallback_combat(entry: dict) -> dict:
    """老镜像只存了 summary（战力/宠物名），没有四维 —— 按战力折算一套。"""
    power = max(1.0, _num(entry.get("power"), 1.0))
    mirror = entry.get("mirror") or {}
    pet_name = str(mirror.get("petName") or "灵兽")
    element = str(mirror.get("petElement") or "无")
    return {
        "pet": {
            "name": pet_name,
            "level": int(_num(mirror.get("petLevel"))),
            "element": element,
            "hp": 600.0 + power * 2.2,
            "attack": 60.0 + power * 0.55,
            "defense": 50.0 + power * 0.42,
            "agility": 55.0 + power * 0.30,
        },
        "player": {
            "name": str(mirror.get("playerName") or entry.get("name") or "修士"),
            "level": int(_num(mirror.get("playerLevel"))),
            "element": "无",
            "hp": 400.0 + power * 1.1,
            "attack": 40.0 + power * 0.20,
            "defense": 30.0 + power * 0.18,
            "agility": 40.0 + power * 0.15,
        },
    }


def _combat(entry: dict) -> dict:
    combat = entry.get("combat")
    if isinstance(combat, dict) and combat.get("pet"):
        return combat
    return _fallback_combat(entry)


# ---------------------------------------------------------------- 单位

def _unit(stats: dict, side: str, kind: str) -> dict:
    return {
        "name": str(stats.get("name") or ("修士" if kind == "player" else "灵兽")),
        "side": side,           # "a" / "b"
        "kind": kind,           # "player" / "pet"
        "element": str(stats.get("element") or "无"),
        "max_hp": max(1.0, _num(stats.get("hp"), 1.0)),
        "hp": max(1.0, _num(stats.get("hp"), 1.0)),
        "atk": max(1.0, _num(stats.get("attack"), 1.0)),
        "def": max(0.0, _num(stats.get("defense"), 0.0)),
        "agi": max(0.0, _num(stats.get("agility"), 0.0)),
        "alive": True,
    }


def _roster(entry: dict, side: str) -> list[dict]:
    combat = _combat(entry)
    player = combat.get("player") or {}
    pet = combat.get("pet") or {}
    if not player and pet:
        player = {"name": str(entry.get("name") or "修士")}
    if not pet and player:
        pet = {"name": "灵兽"}
    rows = []
    if player:
        rows.append(_unit(player, side, "player"))
    if pet:
        rows.append(_unit(pet, side, "pet"))
    return rows


def _living(units: list[dict]) -> list[dict]:
    return [u for u in units if u["alive"]]


# ---------------------------------------------------------------- 数值

def _dmg_share(atk: float, dfd: float) -> float:
    """单次攻击打掉目标最大生命的比例（0.045~0.14）。

    两处口径都做过压缩，不然战报要么秒杀要么打不动：
      1) 用「攻防占比」而不是「攻-防」——本作数值跨度太大（新手宠 4 攻、
         满级宠 3.3 万攻），减法公式高端局两下结束、低端局永远破不了防；
      2) 占比再开根号 —— 面板差 1.5 倍若线性折算，胜率直接 99%，
         开根后约 1.22 倍，强者仍占优但留得住翻盘。
    这样同等镜像对打约 6 回合见分晓，战力高一截的大约 4 回合拿下。
    """
    return max(0.05, min(0.16, 0.07 + 0.12 * _power_ratio(atk, dfd)))


def _power_ratio(atk: float, dfd: float) -> float:
    """开根号压缩后的「我强 / 双方之和」，0.5 = 势均力敌。"""
    a = max(0.0, atk) ** 0.5
    d = max(0.0, dfd) ** 0.5
    total = a + d
    return (a / total) if total > 0 else 0.5


def _dodge_chance(atk_agi: float, dfd_agi: float) -> float:
    gap = _power_ratio(dfd_agi, atk_agi) - 0.5     # -0.5 ~ 0.5
    return max(0.02, min(0.20, DODGE_BASE + DODGE_SPAN * gap * 4))


def _roll_damage(attacker: dict, target: dict, rng: random.Random,
                 rate: float = 1.0) -> tuple[int, bool]:
    share = _dmg_share(attacker["atk"], target["def"])
    crit = rng.random() < CRIT_CHANCE
    factor = rng.uniform(0.88, 1.12)
    if crit:
        factor *= CRIT_RATE
    dmg = target["max_hp"] * share * factor * rate
    return max(1, int(round(dmg))), crit


# ---------------------------------------------------------------- 单场战斗

def fight(a: dict, b: dict, seed: int = 0) -> dict:
    """打一场镜像对决，返回 {"log": [...], "winner_side": "a"/"b", ...}。"""
    rng = random.Random(int(seed) or 0)
    units_a = _roster(a, "a")
    units_b = _roster(b, "b")
    log: list[dict] = []
    truncated = False

    def add(entry: dict, force: bool = False) -> None:
        nonlocal truncated
        if truncated and not force:
            return
        if not force and len(log) >= LOG_MAX:
            log.append({"type": "info", "message": "（战况过于胶着，后续记录从略）"})
            truncated = True
            return
        log.append(entry)

    name_a = str(a.get("name") or "甲")
    name_b = str(b.get("name") or "乙")
    add({"type": "start", "message": "天机幻战 · 【%s】 对阵 【%s】" % (name_a, name_b)})
    for unit in units_a:
        add({
            "type": "player_info" if unit["kind"] == "player" else "pet_info",
            "name": unit["name"], "hp": int(unit["hp"]), "maxHp": int(unit["max_hp"]),
            "attack": int(unit["atk"]), "defense": int(unit["def"]),
            "agility": int(unit["agi"]), "element": unit["element"],
        })
    for unit in units_b:
        add({
            "type": "enemy_info" if unit["kind"] == "player" else "enemy_pet_info",
            "name": unit["name"], "hp": int(unit["hp"]), "maxHp": int(unit["max_hp"]),
            "attack": int(unit["atk"]), "defense": int(unit["def"]),
            "agility": int(unit["agi"]), "element": unit["element"],
        })

    def side_units(side: str) -> list[dict]:
        return units_a if side == "a" else units_b

    def opp_units(side: str) -> list[dict]:
        return units_b if side == "a" else units_a

    def hit(attacker: dict, target: dict, rate: float = 1.0,
            entry_type: str = "attack", skill: str = "") -> None:
        dmg, crit = _roll_damage(attacker, target, rng, rate)
        target["hp"] = max(0.0, target["hp"] - dmg)
        row = {
            "type": entry_type,
            "attacker": attacker["name"],
            "target": target["name"],
            "damage": dmg,
            "targetHp": int(round(target["hp"])),
            "targetMaxHp": int(target["max_hp"]),
            "attackerRole": "ally" if attacker["side"] == "a" else "enemy",
            "isEnemyAttack": attacker["side"] == "b",
            "isPetAttack": attacker["kind"] == "pet",
        }
        if crit:
            row["crit"] = True
            row["skillName"] = "暴击"
        if skill:
            row["skillName"] = skill
        add(row)
        if target["hp"] <= 0 and target["alive"]:
            target["alive"] = False
            if target["side"] == "a":
                add({"type": "player_dead" if target["kind"] == "player" else "pet_dead",
                     "name": target["name"]})
            else:
                add({"type": "enemy_dead", "name": target["name"]})

    for round_no in range(1, MAX_ROUNDS + 1):
        if not _living(units_a) or not _living(units_b):
            break
        add({"type": "round", "round": round_no})
        order = [u for u in units_a + units_b if u["alive"]]
        # 敏捷定先后，附带 ±3% 抖动：同敏捷时不会永远「甲先手」，
        # 否则势均力敌的对局甲侧会稳定多出 8 个百分点的胜率。
        order.sort(key=lambda u: (-(u["agi"] * rng.uniform(0.97, 1.03)),
                                  0 if u["kind"] == "pet" else 1))
        for actor in order:
            if not actor["alive"]:
                continue
            targets = _living(opp_units(actor["side"]))
            if not targets:
                break
            # 优先打对面还活着的灵兽（本作主要战力），打空了再找修士
            pets = [t for t in targets if t["kind"] == "pet"]
            target = rng.choice(pets or targets)

            # 合击：主仆都在，且这一击是灵兽先手 —— 主人的气机一并压上
            allies = _living(side_units(actor["side"]))
            partner = next((u for u in allies if u is not actor), None)
            if (partner is not None and actor["kind"] == "pet"
                    and rng.random() < JOINT_CHANCE and not truncated):
                add({"type": "joint_attack", "player": partner["name"],
                     "pet": actor["name"], "target": target["name"]})

            if rng.random() < _dodge_chance(actor["agi"], target["agi"]):
                if rng.random() < 0.5:
                    add({"type": "dodge", "attacker": actor["name"], "target": target["name"],
                         "targetRole": "ally" if target["side"] == "a" else "enemy"})
                else:
                    add({"type": "miss", "attacker": actor["name"], "target": target["name"],
                         "attackerRole": "ally" if actor["side"] == "a" else "enemy"})
                continue

            hit(actor, target, 1.0)

            # 连击追击：敏捷压过对面时有机会再补一下
            if target["alive"] and rng.random() < COMBO_CHANCE:
                add({"type": "combo_trigger",
                     "message": "【连击】%s 身法破空，趁势追击！" % actor["name"]})
                add({"type": "combo_attack", "attacker": actor["name"],
                     "attackerRole": "ally" if actor["side"] == "a" else "enemy"})
                follow = rng.choice(_living(opp_units(actor["side"])) or [target])
                hit(actor, follow, COMBO_RATE)
            if not _living(units_a) or not _living(units_b):
                break

    alive_a = _living(units_a)
    alive_b = _living(units_b)

    def ratio(units: list[dict]) -> float:
        total = sum(u["max_hp"] for u in units) or 1.0
        return sum(max(0.0, u["hp"]) for u in units) / total

    winner_side = "a"
    if alive_a and not alive_b:
        winner_side = "a"
    elif alive_b and not alive_a:
        winner_side = "b"
    else:
        winner_side = "a" if ratio(units_a) >= ratio(units_b) else "b"

    win_name = name_a if winner_side == "a" else name_b
    lose_name = name_b if winner_side == "a" else name_a
    add({"type": "victory", "message": "【%s】力压【%s】，此场胜出。" % (win_name, lose_name)},
        force=True)
    return {
        "log": log,
        "winner_side": winner_side,
        "rounds": round_no,
        "aHpRatio": round(ratio(units_a), 4),
        "bHpRatio": round(ratio(units_b), 4),
    }


# ---------------------------------------------------------------- 单败签表

def _match_record(bracket_round: int, match_no: int, a: dict, b: dict,
                  winner: dict | None, log: list[dict] | None) -> dict:
    # 注意：客户端的「轮空」判定是 b_user_id > 0（_treasure_user_id_field 会
    # 把它当整数读），所以这里必须是**数字 id**，塞账号名会被读成 0 → 误显示成轮空。
    return {
        "bracket_round": int(bracket_round),
        "match_no": int(match_no),
        "a_user_id": int(a.get("user_id") or 0),
        "a_nickname": str(a.get("name") or ""),
        "a_username": str(a.get("account") or ""),
        "b_user_id": int((b or {}).get("user_id") or 0),
        "b_nickname": str((b or {}).get("name") or ""),
        "b_username": str((b or {}).get("account") or ""),
        "winner_user_id": int((winner or {}).get("user_id") or 0),
        "winner_username": str((winner or {}).get("account") or ""),
        "winner_nickname": str((winner or {}).get("name") or ""),
        "summary_line": "",       # 留空由客户端拼「第X轮 第Y场：…」
        "battle_log": log or [],
    }


def run_bracket(entries: list[dict], seed: int = 0) -> dict:
    """跑完一箱的单败签表，返回 {"winner": entry|None, "matches": [...], "log": [...]}。"""
    pool = [dict(e) for e in entries if isinstance(e, dict)]
    matches: list[dict] = []
    if not pool:
        return {"winner": None, "matches": [], "log": [{"type": "info", "message": "无人入阵。"}]}
    if len(pool) == 1:
        only = pool[0]
        log = [{"type": "solo_win",
                "text": "阵前只余【%s】一人，未曾交手即得此箱机缘。" % only.get("name", "")}]
        matches.append(_match_record(1, 1, only, {}, only, log))
        return {"winner": only, "matches": matches, "log": log}

    rng = random.Random(int(seed) or 0)
    alive = list(pool)
    bracket_round = 1
    while len(alive) > 1:
        nxt: list[dict] = []
        match_no = 1
        index = 0
        while index < len(alive):
            a = alive[index]
            b = alive[index + 1] if index + 1 < len(alive) else None
            if b is None:
                # 末位轮空（奇数人时）
                log = [{"type": "bye", "text": "【%s】签表轮空，静待下一轮。" % a.get("name", "")}]
                matches.append(_match_record(bracket_round, match_no, a, {}, a, log))
                nxt.append(a)
                index += 1
                match_no += 1
                continue
            result = fight(a, b, seed=seed * 31 + bracket_round * 17 + match_no)
            win = a if result["winner_side"] == "a" else b
            matches.append(_match_record(bracket_round, match_no, a, b, win, result["log"]))
            nxt.append(win)
            index += 2
            match_no += 1
        alive = nxt
        bracket_round += 1
        if bracket_round > 12:      # 保险丝，正常人数到不了
            break

    winner = alive[0] if alive else None
    return {"winner": winner, "matches": matches, "log": []}
