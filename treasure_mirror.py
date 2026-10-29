#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""藏宝洞镜像：快照「人物 + 当前出战中的宠物」，报名时锁定。

原版口径（客户端规则说明 + 抓包字段）：
  - 「须先点『更新镜像』，争夺用该镜像互打；入阵后本轮不再随你改阵容。」
  - 镜像读的是**当前出战中的宠物**（petsData.active_pet_id 指向的那只），
    连同人物等级/境界/转世一起定格；
  - 报名（入阵）时把当时那份镜像钉进本轮，之后换宠、换角色都不影响本轮，
    要到下一轮重新「更新镜像」才换。

原来的服务端是桩：GET /treasure/mirror 恒回 hasMirror=False，
POST /treasure/mirror/update 只回个 true 什么也不存 —— 于是弹窗永远显示
「尚未更新镜像」，争夺也没有镜像可用。

存储：state.meta["treasureMirror"] = {summary, snapshot, updatedAt}
"""
from __future__ import annotations

from core import iso, now


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def active_pet(state) -> dict:
    """当前出战中的宠物（active_pet_id 优先，退化为第一只）。"""
    pets = getattr(state, "pets", None) or []
    if not pets:
        return {}
    try:
        active_id = str((state.save.get("petsData") or {}).get("active_pet_id", ""))
    except Exception:
        active_id = ""
    for pet in pets:
        if active_id and str(pet.get("id", "")) == active_id:
            return dict(pet)
    return dict(pets[0])


def pet_growth(pet: dict) -> float:
    """宠物总成长（四项成长率之和；没有成长率就退回单值字段）。"""
    rates = pet.get("growth_rates")
    if isinstance(rates, dict) and rates:
        total = 0.0
        for value in rates.values():
            total += _num(value)
        if total > 0:
            return round(total, 3)
    for key in ("growth", "growth_rate", "lineage_seed_growth"):
        if pet.get(key) not in (None, ""):
            return round(_num(pet.get(key)), 3)
    return 0.0


def _player_snapshot(state) -> dict:
    try:
        state.sync_final_stats()
    except Exception:
        pass
    return dict(state.player or {})


def snapshot(state) -> dict:
    """拍一份镜像：人物 + 出战宠（深拷贝，之后换宠不回头影响）。"""
    return {"player": _player_snapshot(state), "pet": active_pet(state)}


def power_of(snap: dict) -> int:
    """镜像战力：宠物四项终值 + 人物终值的一半（只用于争夺胜率权重）。"""
    pet = snap.get("pet") or {}
    player = snap.get("player") or {}
    pet_power = (_num(pet.get("final_attack", pet.get("attack")))
                 + _num(pet.get("final_defense", pet.get("defense")))
                 + _num(pet.get("final_agility", pet.get("agility")))
                 + _num(pet.get("final_max_hp", pet.get("max_hp"))) * 0.5)
    pl_power = (_num(player.get("final_attack", player.get("attack_base")))
                + _num(player.get("final_defense", player.get("defense_base")))
                + _num(player.get("final_agility", player.get("agility_base")))
                + _num(player.get("final_max_hp", player.get("max_hp"))) * 0.5)
    return int(max(1.0, pet_power + pl_power * 0.5))


def summary_of(state, snap: dict) -> dict:
    player = snap.get("player") or {}
    pet = snap.get("pet") or {}
    return {
        "hasMirror": True,
        "playerName": str(player.get("name") or state.nickname()),
        "playerLevel": int(_num(player.get("level"), 1) or 1),
        "playerRealm": str(player.get("realm") or "凡人"),
        "playerReincarnation": int(_num(player.get("reincarnation"), 0)),
        "characterId": int(_num(player.get("player_character_id"), 0)),
        "petId": str(pet.get("pet_id") or pet.get("id") or ""),
        "petName": str(pet.get("name") or ""),
        "petLevel": int(_num(pet.get("level"), 0)),
        "petGrowth": pet_growth(pet),
        "petElement": str(pet.get("element") or ""),
        "power": power_of(snap),
        "updatedAt": iso(now()),
    }


def _box(state) -> dict:
    box = state.meta.setdefault("treasureMirror", {})
    if not isinstance(box.get("summary"), dict):
        box["summary"] = {}
    return box


def update(state) -> dict:
    """点「更新镜像」：按当前阵容重拍一份并落库。"""
    snap = snapshot(state)
    summ = summary_of(state, snap)
    box = _box(state)
    box["summary"] = summ
    box["snapshot"] = snap
    box["updatedAt"] = summ["updatedAt"]
    state.bump()
    return dict(summ)


def current(state) -> dict:
    """GET /treasure/mirror：已存镜像；没更新过就 hasMirror=False。"""
    box = _box(state)
    summ = box.get("summary") or {}
    if not summ:
        return {"success": True, "hasMirror": False, "mirror": {}}
    out = dict(summ)
    out["success"] = True
    out["mirror"] = dict(summ)
    return out


def resolve_for_join(state) -> dict:
    """报名用哪份镜像：有就用手上这份（钉住），没有就按当前阵容现拍一份。

    返回的 dict 会连同 `autoSnapshot` 一起存进本轮，供结算算战力；
    `combat` 是这一刻的四维快照（人物 + 出战宠），争夺逐场战斗按它打，
    之后再换宠/换装也动不了本轮已经钉住的那份。
    """
    import treasure_battle

    box = _box(state)
    summ = box.get("summary") or {}
    snap = box.get("snapshot") or {}
    if summ and snap:
        row = dict(summ)
        row["autoSnapshot"] = False
        row["combat"] = treasure_battle.combat_of(snap)
        return row
    snap = snapshot(state)
    summ = summary_of(state, snap)
    box["summary"] = summ
    box["snapshot"] = snap
    box["updatedAt"] = summ["updatedAt"]
    row = dict(summ)
    row["autoSnapshot"] = True
    row["combat"] = treasure_battle.combat_of(snap)
    return row
