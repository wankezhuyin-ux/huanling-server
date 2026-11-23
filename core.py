#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地游戏服务端 · 核心层。

把原来只在服务端存在的东西搬到本机：
  - 全局配置（runtime-config / drop-config / festival / 商店目录）
  - 玩家状态（真实存档 saveData，服务端用它做权威结算）
  - 时间闸门（每日次数、活动时间窗、跨天重置）
  - 权威发奖（金币、元宝、道具、宠物、邮件）
  - 排行榜/竞技场/世界 Boss 等"其他玩家"数据（用真实榜单当机器人）

设计约束：
  - 只用标准库，单文件可读，方便 Codex 直接改。
  - 存档结构完全沿用客户端 saveData，客户端不需要任何改动。
  - 所有写操作先改内存再落盘，落盘是原子替换。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_DIR = ROOT / "state"

CN_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 时间与工具

def now() -> datetime:
    return datetime.now(CN_TZ)


def today() -> str:
    return now().strftime("%Y-%m-%d")


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def rnd_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- 客户端物品名校验
#
# 客户端 `inventory_manager.add_item()` 只认 `game_data.gd ALL_ITEMS` 里的名字，
# 名字对不上就 `未知物品: xxx`，邮件点「打开」直接失败、奖励永久卡在邮箱。
# 线上真实踩过的坑：神兽蛋 / 高级技能书 / 洗髓丹 / 灵兽经验丹 / 硬皮lv1技能书，
# 还有把数量写进名字的「周年法宝碎片*20」。
_CLIENT_ITEM_NAMES: set | None = None


def client_item_names() -> set:
    names = _CLIENT_ITEM_NAMES
    if names is None:
        rows = load_json(DATA / "client-item-names.json", {}) or {}
        items = rows.get("items") if isinstance(rows, dict) else rows
        names = {str(x) for x in (items or [])}
        globals()["_CLIENT_ITEM_NAMES"] = names
    return names


def client_has_item(name: str) -> bool:
    """白名单没加载成功时放行（不能让整条链路因为丢文件而挂掉）。"""
    names = client_item_names()
    return (not names) or (str(name) in names)


_ITEM_RENAME: dict | None = None


def item_rename_map() -> dict:
    """道具改名表：旧名 md5 前 8 位 → 现用名。

    键用哈希而不是明文，是为了磁盘和日志里都不留历史名字；
    生成方是客户端工程的 tools/rename_pet_skills.py。
    """
    table = _ITEM_RENAME
    if table is None:
        raw = load_json(DATA / "item-rename.json", {}) or {}
        table = raw.get("map") if isinstance(raw, dict) else None
        table = table if isinstance(table, dict) else {}
        globals()["_ITEM_RENAME"] = table
    return table


def migrate_item_name(name: str) -> str:
    """单个物品名的归一化；没命中就原样返回。"""
    text = str(name or "")
    if not text:
        return text
    return item_rename_map().get(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], text)


_PET_RENAME: dict | None = None


def pet_rename_map() -> dict:
    """宠物图鉴改名表：旧名 md5 前 8 位 → 现用名。

    和道具表分开存：宠物名是玩家看得见的个体名，以后单独再改不会牵动道具白名单；
    键同样是哈希，磁盘和日志里都不留历史名字。
    """
    table = _PET_RENAME
    if table is None:
        raw = load_json(DATA / "pet-rename.json", {}) or {}
        table = raw.get("map") if isinstance(raw, dict) else None
        table = table if isinstance(table, dict) else {}
        globals()["_PET_RENAME"] = table
    return table


def migrate_pet_name(name: str) -> str:
    """单个宠物名的归一化；没命中就原样返回。"""
    text = str(name or "")
    if not text:
        return text
    return pet_rename_map().get(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], text)


_MAP_RENAME: dict | None = None


def map_rename_map() -> dict:
    """地图改名表：原版名 → {name, domain, terrain, setting}。

    mapId 形如「名字（x,y）」，改名只换括号前的名字，坐标不动。
    存档里的 current_map / previous_map 靠这张表迁移。
    """
    table = _MAP_RENAME
    if table is None:
        raw = load_json(DATA / "map-rename.json", {}) or {}
        table = raw.get("map") if isinstance(raw, dict) else None
        table = table if isinstance(table, dict) else {}
        globals()["_MAP_RENAME"] = table
    return table


def migrate_map_id(map_id: str) -> str:
    """把 mapId 里的旧地名换成现用名；坐标后缀原样保留。"""
    text = str(map_id or "")
    if not text:
        return text
    cut = text.rfind("（")
    name, tail = (text[:cut], text[cut:]) if cut > 0 else (text, "")
    row = map_rename_map().get(name)
    if isinstance(row, dict) and row.get("name") and row["name"] != name:
        return str(row["name"]) + tail
    return text


_ITEM_QTY_RE = re.compile(r"^(.*?)\s*[*xX×]\s*(\d+)$")


def split_item_qty(text: str) -> tuple[str, int]:
    """`法宝碎片*20` → (`法宝碎片`, 20)。没有后缀就是 (原名, 1)。

    配置里把数量写进名字是历史遗留（runtime-config 的抽奖保底选项），
    直接当物品名发出去客户端一定认不出。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", 1
    hit = _ITEM_QTY_RE.match(raw)
    if not hit:
        return raw, 1
    name = hit.group(1).strip()
    try:
        qty = max(1, int(hit.group(2)))
    except ValueError:
        return raw, 1
    # 只有当「去掉后缀的名字」确实是个物品时才拆，避免误伤「双倍经验丹(月)」这类
    if not name or (client_item_names() and name not in client_item_names()):
        return raw, 1
    return name, qty


def in_window(start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    """是否在当日时间窗内（左闭右开）。"""
    cur = now()
    start = cur.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = cur.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start <= cur < end


def load_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {} if default is None else default


# 落盘是否带缩进。默认不带：`indent=1` 让 124KB 的存档序列化从 1.26ms 涨到 6.76ms
# （线上实测，占总落盘耗时的 98%），换来的只是肉眼可读。排查时用
# `python3 -m json.tool state/accounts/xxx.json` 一样能看。
# 本机调试想看缩进就 `LY_PRETTY_JSON=1 python3 server.py`。
PRETTY_JSON = str(os.environ.get("LY_PRETTY_JSON", "") or "").strip().lower() in ("1", "true", "yes", "on")

# 落盘签名兜底时的告警是否逐条打（默认 5 分钟一条）。排查用，生产不用开。
_DIRTY_TRACE = str(os.environ.get("LY_DIRTY_TRACE", "") or "").strip().lower() in ("1", "true", "yes", "on")


def dump_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if PRETTY_JSON:
        text = json.dumps(value, ensure_ascii=False, indent=1)
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------- 宠物生命成长（原版口径）
# 物种表里 growth（攻+防+敏总成长）和 hp_growth（生命成长）是两个独立数值：
#   雏龙 growth 3.6 / hp_growth 6.0 → 血成长是战斗成长的 1.67 倍
#   神甲 growth 4.0 / hp_growth 2.5 → 0.625 倍
# 旧公式拿 total_growth 直接当血成长（max_hp = base.hp + total_growth*(lv-1)），
# 面板上的「生命成长」从来不参与计算，而且会把错误值写回存档。
# 这里按物种 hp_growth/growth 的比例跟随当前总成长浮动，
# 因此历史存档里被污染成 total_growth 的 growth_rates.hp 会自动被纠正。
_PET_SPECIES_CACHE: dict | None = None


def pet_species_table() -> dict:
    global _PET_SPECIES_CACHE
    if _PET_SPECIES_CACHE is None:
        raw = load_json(DATA / "pets.json", {})
        pets = raw.get("pets") if isinstance(raw, dict) else None
        _PET_SPECIES_CACHE = pets if isinstance(pets, dict) else {}
    return _PET_SPECIES_CACHE


def pet_hp_growth(pet: dict, combat_total: float) -> float:
    """生命成长 = 物种 hp_growth × (当前总成长 / 物种基础成长)。"""
    if not isinstance(pet, dict):
        return max(0.0, float(combat_total or 0))
    pet_id = str(pet.get("pet_id") or pet.get("name") or "")
    species = pet_species_table().get(pet_id) or {}
    if not isinstance(species, dict):
        species = {}
    species_hp = float(species.get("hp_growth", 0) or 0)
    species_total = float(species.get("growth", 0) or 0)
    if species_hp <= 0 or species_total <= 0:
        # 物种表里没有（节日宠 / 蛋 / 合体宠）：退回「生命成长 = 总成长」的老口径
        return round(max(0.0, float(combat_total or 0)), 3)
    # 成长丹 / 进化 / 变异能把总成长堆到物种基准的十几倍，上下限只用来拦脏数据
    ratio = max(0.05, min(100.0, float(combat_total or 0) / species_total))
    return round(species_hp * ratio, 3)


# ---------------------------------------- 宠物捕获 / 图鉴投喂（原版口径）
# 复刻 pet_manager.capture_from_monster + pet.gd create_from_data：
#   1. 10% 概率捕获（原版 `randi() % 100 >= 10` 即失败）
#   2. 精英 5‰（VIP 10‰）→ 成长倍率 1.3~1.5；普通 0.9~1.1
#   3. 资质按物种 ratio 抖动 ±5% 后归一化到 100
#   4. total_growth 低于玩家设定阈值 → 投喂图鉴（petCollectionData.progress +1，
#      宠物消失）；该物种图鉴满 50 只 → 丢弃
#   5. 主战栏满 10 只 → 放生（**原版就是放生，不再往宠物袋里塞**）
#
# 第 5 条是重点：旧实现每场战斗按 5% 静默 append 一只模板宠进 pet_bag，
# 挂机一晚宠物袋能堆到 67/50 —— 玩家看到的「宠物袋莫名多出很多宠物」。
CAPTURE_RATE = 0.10          # 配置缺失时的兜底；实际值见 capture_rate()
ELITE_RATE = 0.005
ELITE_RATE_VIP = 0.010
CAPTURE_MAX_PETS = 10        # GameData.MAX_PETS：主战栏 10 格
CAPTURE_MAX_PET_BAG = 50     # GameData.MAX_PET_BAG：宠物袋 50 格（捕获不写入）
COLLECTION_MAX_PER_SPECIES = 50


def capture_rate() -> float:
    """野外捕捉基础概率。

    以前这里写死用 CAPTURE_RATE 常量，runtime-config 里的
    `atlasDungeon.captureRate` 从头到尾没有任何代码读取 —— 运营改配置
    一点反应都没有。现在统一以配置为准，缺配置/非法值才回落常量。
    测试可以直接覆盖本函数（core.capture_rate = lambda: 1.0）钉死概率。
    """
    try:
        raw = RULES.v("atlas_dungeon", "captureRate", None)
        if raw is not None:
            return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        pass
    return float(CAPTURE_RATE)

# pet_collection_data.gd：图鉴每 +1 只按物种累计属性加成，
# 属性类型 = BONUS_TYPES[abs(String.hash(pet_id)) % 4]。
COLLECTION_BONUS_TYPES = ("attack", "defense", "agility", "hp")
COLLECTION_BONUS_VALUES = [
    0.0, 0.0008, 0.0016, 0.0024, 0.0032, 0.004, 0.0048, 0.0056, 0.0064, 0.0072,
    0.008, 0.0084, 0.0088, 0.0092, 0.0096, 0.01, 0.0104, 0.0108, 0.0112, 0.0116,
    0.012, 0.0124, 0.0128, 0.0132, 0.0136, 0.014, 0.0144, 0.0148, 0.0152, 0.0156,
    0.016, 0.0161, 0.0162, 0.0163, 0.0164, 0.0165, 0.0166, 0.0167, 0.0168, 0.0169,
    0.017, 0.0171, 0.0172, 0.0173, 0.0174, 0.0175, 0.0176, 0.0177, 0.0178, 0.0179,
    0.018,
]


def godot_str_hash(text: str) -> int:
    """复刻 Godot 4 `String.hash()`：djb2，h = h*33 + codepoint，32 位溢出。

    实测对齐（tools/hash_probe.gd）："" → 5381、"a" → 177670、"ouou" → 2090593805。
    """
    h = 5381
    for ch in str(text or ""):
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    return h


def collection_bonus_type(pet_id: str) -> str:
    """图鉴加成属性：原版按 pet_id 的 hash 取模选四项之一。"""
    return COLLECTION_BONUS_TYPES[godot_str_hash(pet_id) % 4]


def collection_progress(save: dict) -> dict:
    pc = save.setdefault("petCollectionData", {})
    if not isinstance(pc, dict):
        pc = {}
        save["petCollectionData"] = pc
    prog = pc.get("progress")
    if not isinstance(prog, dict):
        prog = {}
        pc["progress"] = prog
    return prog


def invest_pet_to_collection(save: dict, pet_id: str) -> bool:
    """投喂图鉴：progress +1，并按档位累加对应属性加成。

    返回 False 表示该物种图鉴已满 50（原版此时直接丢弃，不再增加）。
    """
    pid = str(pet_id or "")
    if not pid:
        return False
    prog = collection_progress(save)
    try:
        count = int(prog.get(pid, 0) or 0)
    except (TypeError, ValueError):
        count = 0
    if count >= COLLECTION_MAX_PER_SPECIES:
        return False
    prog[pid] = count + 1

    pc = save.setdefault("petCollectionData", {})
    bonuses = pc.get("bonuses")
    if not isinstance(bonuses, dict):
        bonuses = {}
        pc["bonuses"] = bonuses
    btype = collection_bonus_type(pid)
    try:
        cur = float(bonuses.get(btype, 0) or 0)
    except (TypeError, ValueError):
        cur = 0.0
    bump = COLLECTION_BONUS_VALUES[count + 1] - COLLECTION_BONUS_VALUES[count]
    bonuses[btype] = round(cur + bump, 6)
    return True


def _roll_stat_ratio(species: dict) -> dict:
    """pet.gd `apply_ratio_fluctuation`：攻 +5% 抖动、防敏反向下浮，再归一化到 100。"""
    base = species.get("ratio") if isinstance(species.get("ratio"), list) else [33, 33, 34]
    try:
        b_atk = float(base[0])
    except (TypeError, ValueError, IndexError):
        b_atk = 33.0
    try:
        b_def = float(base[1])
    except (TypeError, ValueError, IndexError):
        b_def = 33.0
    try:
        b_agi = float(base[2])
    except (TypeError, ValueError, IndexError):
        b_agi = 34.0
    fl = random.uniform(-0.05, 0.05)
    a = max(15.0, min(55.0, b_atk + fl * 100.0))
    d = max(15.0, min(55.0, b_def - fl * 50.0))
    g = max(15.0, min(55.0, b_agi - fl * 50.0))
    total = a + d + g
    if total <= 0:
        return {"attack": 33.33, "defense": 33.33, "agility": 33.34}
    a = round(a / total * 100.0, 2)
    d = round(d / total * 100.0, 2)
    return {"attack": a, "defense": d, "agility": round(100.0 - a - d, 2)}


def build_captured_pet(state, pet_id: str, map_name: str = "", is_elite: bool = False) -> dict | None:
    """按 pet.gd `create_from_data` 造一只 1 级捕获宠（带完整成长资质）。

    旧实现是手搓字典（growth_rates 为空），造出来的宠属性成长全 0，
    抓到也是废的 —— 这也是「宠物袋里一堆 Lv.1 的 ouou」的另一半原因。
    """
    species = pet_species_table().get(str(pet_id))
    if not isinstance(species, dict):
        return None
    ratio = _roll_stat_ratio(species)
    try:
        base_growth = float(species.get("growth", 3.8) or 3.8)
    except (TypeError, ValueError):
        base_growth = 3.8
    if base_growth <= 0:
        base_growth = 3.8
    mult = random.uniform(1.3, 1.5) if is_elite else random.uniform(0.9, 1.1)
    total_growth = round(base_growth * mult, 3)
    try:
        hp_growth = float(species.get("hp_growth", 0) or 0)
    except (TypeError, ValueError):
        hp_growth = 0.0
    is_evo = bool(species.get("is_evolution_pet", False))
    pet = {
        "id": "%s_%d" % (pet_id, int(random.random() * 1e9)),
        "pet_id": str(pet_id),
        "name": str(species.get("name", pet_id)),
        "level": 1,
        "experience": 0,
        "generation": 0,
        "locked": False,
        "is_elite": bool(is_elite),
        "is_mutant": False,
        "mutation_count": 0,
        "element": str(species.get("element", "")),
        "type": str(species.get("type", "")),
        "form": str(species.get("form", "幼年" if is_evo else "不可进化")),
        "max_level": int(species.get("max_level", 1500 if is_evo else 0) or (1500 if is_evo else 0)),
        "capture_map": str(map_name or ""),
        "base_stats": {
            "hp": float(species.get("hp", 30.0) or 30.0),
            "attack": float(species.get("attack", 5.0) or 5.0),
            "defense": float(species.get("defense", 5.0) or 5.0),
            "agility": float(species.get("agility", 5.0) or 5.0),
        },
        "stat_ratio": ratio,
        "growth_rates": {
            "attack": round(total_growth * ratio["attack"] / 100.0, 3),
            "defense": round(total_growth * ratio["defense"] / 100.0, 3),
            "agility": round(total_growth * ratio["agility"] / 100.0, 3),
            "hp": round(hp_growth * mult, 3) if hp_growth > 0 else total_growth,
        },
        "total_growth": total_growth,
        "lineage_seed_growth": total_growth,
        "is_evolution_pet": is_evo,
        "evolution_origin": str(species.get("evolution_origin", "")),
        "evolution_stage": int(species.get("evolution_stage", 0) or 0),
        "skill_slots": [
            {"slot": i, "level": 0, "skill_id": "", "skill_key": "", "skill_name": ""}
            for i in (1, 2, 3)
        ],
        "medal_counts": {"bold": 0, "calm": 0, "brave": 0},
        "reincarnation_count": 0,
        "pet_raise_race_used": False,
        "pending_medal_sync": False,
        "pending_medal_sync_reason": "",
        "pending_skill_learn_sync": False,
        "pending_skill_learn_prev_level": 0,
        "last_max_level_medal_cap": 0,
        "last_max_level_skill_learn_cap": 0,
    }
    state._recalc_pet_stats(pet)
    return pet


# ---------------------------------------------------------------- 新号注册礼包
# 2026-10-28 起：新号不再送种子里的「变异阿努」（五一活动遗留），改送一只
# 随机总成长 10~11 的苦行龟；另外在角色邮箱发一封欢迎邮件，附件是
# 七阶可进化宠锦囊 x1 + 溅墨lv1技能书 x1，附言「欢迎来到幻灵修真世界」。
REGISTRATION_PET_ID = "kǔxínggūi"        # 苦行龟
REGISTRATION_GROWTH = (10.0, 11.0)       # 总成长随机区间（攻防敏成长率之和）
WELCOME_MAIL_TITLE = "新人欢迎礼包"
WELCOME_MAIL_CONTENT = "欢迎来到幻灵修真世界"
# 2026-10-29：新手锦囊改成「7+可成长宠礼包 + 溅射宠物技能书」各一份
WELCOME_GIFT_ITEMS = (
    ("七阶可进化宠锦囊", 1),
    ("溅墨lv1技能书", 1),
)
WELCOME_GIFT_ITEM = WELCOME_GIFT_ITEMS[0][0]   # 兼容老引用


def build_registration_pet(state, pet_id: str = REGISTRATION_PET_ID,
                           growth_lo: float = REGISTRATION_GROWTH[0],
                           growth_hi: float = REGISTRATION_GROWTH[1]) -> dict | None:
    """新号注册赠送宠：指定物种 + 总成长在区间内随机。

    沿用 build_captured_pet 的同一条构建链（比例抖动 roll、1 级 0 代、技能位空），
    只把 total_growth 强制成区间内随机值，再按物种比例分摊回攻防敏三条成长率，
    HP 成长率按 hp_growth×(total/base) 缩放 —— 和野外捕捉完全同一套口径。
    """
    species = pet_species_table().get(str(pet_id))
    if not isinstance(species, dict):
        return None
    pet = build_captured_pet(state, pet_id, "registration", False)
    if pet is None:
        return None
    try:
        base_growth = float(species.get("growth", 3.8) or 3.8)
    except (TypeError, ValueError):
        base_growth = 3.8
    if base_growth <= 0:
        base_growth = 3.8
    lo, hi = (growth_lo, growth_hi) if growth_lo <= growth_hi else (growth_hi, growth_lo)
    total = round(random.uniform(lo, hi), 3)
    mult = total / base_growth
    ratio = pet.get("stat_ratio") if isinstance(pet.get("stat_ratio"), dict) else _roll_stat_ratio(species)
    try:
        hp_growth = float(species.get("hp_growth", 0) or 0)
    except (TypeError, ValueError):
        hp_growth = 0.0
    pet["total_growth"] = total
    pet["lineage_seed_growth"] = total
    pet["growth_rates"] = {
        "attack": round(total * float(ratio.get("attack", 33.0)) / 100.0, 3),
        "defense": round(total * float(ratio.get("defense", 33.0)) / 100.0, 3),
        "agility": round(total * float(ratio.get("agility", 34.0)) / 100.0, 3),
        "hp": round(hp_growth * mult, 3) if hp_growth > 0 else total,
    }
    state._recalc_pet_stats(pet)
    return pet


def capture_wild_pet(state, pet_id: str, map_name: str = "", min_growth: float = 0.0,
                     vip: bool = False) -> dict:
    """野外捕获一只宠，返回原版口径的结果。

    返回字段与客户端 game_manager `_apply_battle_loot_capture_notice` 对齐：
      captured / reason（对应 captureSkipReason）/ pet（入栏时）/ growth / toCollection
    reason 取值：
      ""                             成功入主战栏
      "escaped"                      10% 概率没中，怪物跑掉
      "not_capturable"               物种表里没有这个 pet_id
      "below_min_growth_to_collection" 低于阈值 → 投图鉴
      "below_min_growth_discarded"   低于阈值但该物种图鉴已满 50 → 丢弃
      "roster_full"                  主战栏 10 格满 → 放生
    """
    out = {
        "captured": False,
        "reason": "",
        "pet": None,
        "growth": 0.0,
        "toCollection": False,
        "petId": str(pet_id or ""),
        # 中文名：投图鉴/放生时 pet 不进栏（out["pet"] 为 None），
        # 只靠 petId 会让客户端战报显示 "linlin 成长值…" 这种英文物种键
        "name": "",
        "isElite": False,
    }
    if not str(pet_id or "").strip():
        out["reason"] = "not_capturable"
        return out
    if random.random() >= capture_rate():
        out["reason"] = "escaped"
        return out

    is_elite = random.random() < (ELITE_RATE_VIP if vip else ELITE_RATE)
    pet = build_captured_pet(state, pet_id, map_name, is_elite)
    if pet is None:
        out["reason"] = "not_capturable"
        return out
    out["isElite"] = bool(is_elite)
    out["name"] = str(pet.get("name", "") or pet_id)
    try:
        growth = float(pet.get("total_growth", 0) or 0)
    except (TypeError, ValueError):
        growth = 0.0
    out["growth"] = round(growth, 3)

    # ① 低于玩家设定阈值 → 投喂图鉴（原版：图鉴 +1，灵兽消失）
    try:
        threshold = float(min_growth or 0)
    except (TypeError, ValueError):
        threshold = 0.0
    if threshold > 0 and growth < threshold:
        if invest_pet_to_collection(state.save, pet_id):
            out["captured"] = True
            out["toCollection"] = True
            out["reason"] = "below_min_growth_to_collection"
        else:
            out["reason"] = "below_min_growth_discarded"
        return out

    # ② 主战栏还有位置就进栏
    if len(state.pets) < CAPTURE_MAX_PETS:
        state.pets.append(pet)
        out["captured"] = True
        out["pet"] = pet
        out["reason"] = ""
        return out

    # ③ 主战栏满 → 放生（原版口径，不塞宠物袋）
    out["reason"] = "roster_full"
    return out


# ---------------------------------------------------------------- 全局配置

class Rules:
    """服务端下发过的所有配置，本地一份。"""

    def __init__(self) -> None:
        raw = load_json(DATA / "runtime-config.json")
        # 服务端模块名 camelCase -> snake_case，与本地快照 rules_modules 一致
        self.modules: dict[str, dict] = {}
        for name, payload in raw.items():
            if name in ("success", "serverUnixTime"):
                continue
            self.modules[snake(name)] = payload if isinstance(payload, dict) else {}
        self.drop = load_json(DATA / "drop-config.json")
        self.festival = load_json(DATA / "festival.json")
        self.shop_catalog = load_json(DATA / "shop-catalog.json")
        self.arena_shop = load_json(DATA / "arena-shop-items.json")
        self.arena_titles = load_json(DATA / "arena-titles.json")
        self.xianmo_shop = load_json(DATA / "xianmo-shop.json")
        self.rankings = {
            "all": load_json(DATA / "ranking.json"),
            "players": load_json(DATA / "ranking-players.json"),
            "pets_evolution": load_json(DATA / "ranking-pets-evolution.json"),
            "pets_non_evolution": load_json(DATA / "ranking-pets-non-evolution.json"),
        }

    def m(self, name: str) -> dict:
        return self.modules.get(snake(name), {})

    def v(self, name: str, path: str, default=None):
        cur = self.m(name)
        for token in path.split("."):
            if isinstance(cur, dict):
                if token not in cur:
                    return default
                cur = cur[token]
            elif isinstance(cur, list):
                if not token.lstrip("-").isdigit():
                    return default
                idx = int(token)
                if idx < 0 or idx >= len(cur):
                    return default
                cur = cur[idx]
            else:
                return default
        return cur

    def roll(self, table: list[dict], weight_key: str = "weight"):
        """按权重抽一条；表为空返回 None。"""
        rows = [row for row in table if isinstance(row, dict)]
        if not rows:
            return None
        total = sum(max(1, int(row.get(weight_key, 1))) for row in rows)
        pick = random.randint(1, total)
        acc = 0
        for row in rows:
            acc += max(1, int(row.get(weight_key, 1)))
            if pick <= acc:
                return row
        return rows[-1]


def snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_" + ch.lower())
        else:
            out.append(ch)
    return "".join(out).lstrip("_")


# ---------------------------------------------------------------- 玩家状态

DEFAULT_MAP = "墨影城郊（0,0）"

# 钱袋统一道具名（2026-10 合并）。旧档里这些赞助者命名的钱袋会在读档时并入钱币袋。
COIN_BAG_NAME = "钱币袋"
COIN_BAG_LEGACY_NAMES = {
    "醉剑", "1万金币袋（醉）", "神的遗愿", "风歌夜曲", "摆烂松", "人间大炮",
    "我的理想", "板砖", "立羽", "铭大大", "长空秋雁", "大橘大利", "人间事", "思念",
}


# ---------------------------------------------------------------- 帮派加成
#
# 帮会数据是全局的（state/gangs.json，一个帮会跨多个账号），和玩家存档分开。
# core 里不能 import handlers_gang（handlers_gang 反过来 import core），所以这里
# 直接按文件读，用 mtime 做缓存：一次请求里 exp_multiplier 可能被调好几次，
# 不能每次都解析 JSON。
GANG_EXP_PERCENT_PER_LEVEL = 2.5  # 与 handlers_gang.gang_buffs 的 expBonusPercent 同口径
_GANG_CACHE = {"mtime": None, "gangs": {}}


def gang_snapshot() -> dict:
    path = STATE_DIR / "gangs.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _GANG_CACHE["mtime"] = None
        _GANG_CACHE["gangs"] = {}
        return {}
    if _GANG_CACHE["mtime"] != mtime:
        raw = load_json(path, {}) or {}
        gangs = raw.get("gangs") if isinstance(raw, dict) else {}
        _GANG_CACHE["gangs"] = gangs if isinstance(gangs, dict) else {}
        _GANG_CACHE["mtime"] = mtime
    return _GANG_CACHE["gangs"]


class State:
    """单个玩家的存档 + 服务端侧状态（次数、活动进度）。

    多人联机：一个账号一份文件 `state/accounts/<username>.json`，
    通过 `AccountStore.get(username)` 取用；不再有全局共享存档。
    """

    def __init__(self, username: str = "local", seed: dict | None = None) -> None:
        self.username = username
        self.lock = threading.RLock()
        self.dir = STATE_DIR / "accounts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / (_safe_name(username) + ".json")
        # 落盘去重状态：dirty 由 bump()/mark_dirty() 置位，表示「一定有改动」，
        # 置位时直接写、不必再算签名；没置位才比签名，兜住那些改完忘记 bump 的老代码。
        self.dirty = False
        self._persisted_sig: str | None = None
        self._seen_flush_ts = 0.0
        self._sig_warn_ts = 0.0
        self.fresh = not self.path.exists()
        if not self.fresh:
            self.raw = load_json(self.path)
            # 磁盘上这份就是「最后一次落盘的内容」，先记下它的指纹，
            # 这样读档后什么都没改的话，第一次 flush 就能直接跳过。
            self._persisted_sig = self._signature()
        else:
            self.raw = self._initial(seed)
            self._seed_mails()
            self._apply_registration_gift()
        # 种子/旧档里 current_map 为空会让客户端主界面报「地图数据加载失败」
        changed = self._ensure_map()
        if self._migrate_coin_bags():
            changed = True
        if self._migrate_item_names():
            changed = True
        if self._migrate_pet_names():
            changed = True
        if self._migrate_map_names():
            changed = True
        if self.fresh or changed:
            self.flush()

    def _migrate_map_names(self) -> bool:
        """地图改名：把老存档里所有 mapId 换成现用地名。

        mapId 是「名字（x,y）」，地名全改了但老存档里还留着旧名字，不迁移的话
        玩家下次登录会「地图数据加载失败」，位置回默认点。

        早年只迁移 gameSettings.current_map / previous_map，后来发现存档里还有
        一堆以 mapId 为值或键的地方：playerData.recent_maps（最近地图）、
        meta.arena.mirror.player.recent_maps（竞技场镜像）、meta.battles.*.mapId
        （战斗记录）、petsData.*.capture_map（捕获地），以及 questData 里
        kill_map_<mapId> 形式的击杀计数键。逐字段列举永远会漏，所以这里直接
        递归遍历 save + meta 两棵树，对所有字符串值做 migrate_map_id()，并对
        kill_map_ 前缀的键单独换名 + 合并计数。

        migrate_map_id 只认「名字（x,y）」或恰好等于旧名的字符串，普通文案里
        顺带提到地名不会被误改；mayday_event / refine / tower / gift 这类
        非地名标识符原样返回。重复读档幂等。
        """
        if not map_rename_map():
            return False
        dirty = [False]

        def conv(text: str) -> str:
            if not isinstance(text, str) or not text:
                return text
            new = migrate_map_id(text)
            if new != text:
                dirty[0] = True
            return new

        def walk(node) -> None:
            if isinstance(node, dict):
                for key in list(node.keys()):
                    if isinstance(key, str) and key.startswith("kill_map_"):
                        new_key = "kill_map_" + conv(key[len("kill_map_"):])
                        if new_key != key:
                            moved = node.pop(key)
                            other = node.get(new_key)
                            if isinstance(other, (int, float)) and isinstance(moved, (int, float)):
                                node[new_key] = other + moved
                            else:
                                node[new_key] = moved
                            dirty[0] = True
                    value = node.get(key)
                    if isinstance(value, str):
                        new_value = conv(value)
                        if new_value != value:
                            node[key] = new_value
                    elif isinstance(value, (dict, list)):
                        walk(value)
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    if isinstance(value, str):
                        new_value = conv(value)
                        if new_value != value:
                            node[i] = new_value
                    elif isinstance(value, (dict, list)):
                        walk(value)

        walk(self.save)
        walk(self.meta)
        return dirty[0]

    def _migrate_item_names(self) -> bool:
        """道具改名：老存档里的旧物品名换成现用名。

        换皮过程中会陆续改道具显示名（宠物技能书、丹药、材料……）。客户端只在
        「玩家看到」的层面对得上不够 —— 服务端 take_item/邮件领取是按名字找物品的，
        存档里留着旧名就会出现「背包看得见、点使用说数量不足」。

        改名表在 data/item-rename.json，键是旧名的 md5 前 8 位，磁盘上不留明文旧名。
        只扫背包与邮件，不碰宠物昵称之类同名字段（哈希不会撞）。
        """
        table = item_rename_map()
        if not table:
            return False
        changed = False

        def walk(node) -> None:
            nonlocal changed
            if isinstance(node, list):
                for x in node:
                    walk(x)
                return
            if not isinstance(node, dict):
                return
            name = node.get("name")
            if isinstance(name, str) and name:
                new = table.get(hashlib.md5(name.encode("utf-8")).hexdigest()[:8])
                if new and new != name:
                    node["name"] = new
                    changed = True
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

        walk(self.save.get("inventoryData") or {})
        walk(self.meta.get("mails") or [])
        return changed

    def _migrate_pet_names(self) -> bool:
        """宠物改名：老存档里的旧宠物名换成图鉴现用名。

        宠物存档里 name 是显示名，客户端缺 pet_id 时还要靠它反查图鉴键；
        服务端自己也按名字在邮件里发宠、在榜单上展示。只改客户端会出现
        「宠物栏看到新名、邮件里还是旧名」。这里扫宠物栏、宠物袋与邮件。

        改名表在 data/pet-rename.json，键是旧名的 md5 前 8 位。
        """
        table = pet_rename_map()
        if not table:
            return False
        changed = False

        def walk(node) -> None:
            nonlocal changed
            if isinstance(node, list):
                for x in node:
                    walk(x)
                return
            if not isinstance(node, dict):
                return
            name = node.get("name")
            if isinstance(name, str) and name:
                new = table.get(hashlib.md5(name.encode("utf-8")).hexdigest()[:8])
                if new and new != name:
                    node["name"] = new
                    changed = True
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

        walk(self.save.get("petsData") or {})
        walk(self.meta.get("mails") or [])
        return changed

    def _migrate_coin_bags(self) -> bool:
        """钱袋合并：14 种赞助钱袋 → 单一「钱币袋」（每个仍是 10 万金币）。

        客户端 from_dict 也会做同样的迁移，但客户端只是"看到"合并后的样子，
        真正扣道具是在服务端 take_item 里做的 —— 服务端不同步改名就会出现
        「背包里显示钱币袋 x52，点使用却提示道具数量不足」。这里在读档时一次性搬完。
        """
        total = 0
        hit = False
        coin = None
        items = self.items
        for item in list(items):
            name = str(item.get("name", "")).strip()
            if name == COIN_BAG_NAME:
                coin = item
                total += max(0, int(item.get("count", 0) or 0))
            elif name in COIN_BAG_LEGACY_NAMES:
                total += max(0, int(item.get("count", 0) or 0))
                items.remove(item)
                hit = True
        if not hit:
            return False
        if total <= 0:
            if coin is not None:
                items.remove(coin)
            return True
        if coin is None:
            coin = {"id": rnd_id("i"), "name": COIN_BAG_NAME, "count": total,
                    "item_type": "gift", "custom_data": {}}
            items.append(coin)
        coin["count"] = total
        return True

    def _ensure_map(self) -> bool:
        """兜底修正 gameSettings.current_map。

        客户端 game_manager.gd 恢复存档时是无条件 `current_map = settings.current_map`，
        空串会直接覆盖掉默认的"墨影城郊（0,0）"，接着
        `GameData.MAPS_DATA.get("")` 取不到 → 主界面「地图数据加载失败」、
        不刷怪、也不显示人物和宠物。
        """
        save = self.save
        gs = save.get("gameSettings")
        if not isinstance(gs, dict):
            gs = {}
            save["gameSettings"] = gs
        if str(gs.get("current_map", "") or "").strip():
            return False
        gs["current_map"] = DEFAULT_MAP
        if not str(gs.get("previous_map", "") or "").strip():
            gs["previous_map"] = DEFAULT_MAP
        return True

    @staticmethod
    def _initial(seed: dict | None) -> dict:
        return {
            "save": copy.deepcopy(seed) if seed else {},
            "meta": {
                "revision": 1,
                "createdAt": iso(now()),
                "daily": {"date": today(), "v": {}},
                "mails": [],
                "mailSeq": 900000,
                "lotteryPity": 0,
                "arena": {"week": 0, "points": 0, "titles": [], "wins": 0},
                "worldBoss": {"date": "", "damage": 0, "used": 0},
                "treasure": {"date": "", "join": 0, "chests": []},
                "xianmo": {"faction": "xian", "score": 0, "treasureMaps": [], "chests": []},
                "brotherTower": {"rooms": [], "joined": None},
                "shengsi": {"signups": [], "battles": []},
                "overflow": [],
            },
        }

    # -- 落盘
    #
    # 签名里排除的字段：每次请求都会被刷新，但「内容真的变了」才需要整档重写。
    #   updatedAt / saveRevision —— flush 自己写的，不排除会导致永远判定为变化
    #   lastSeen                 —— 心跳刷新，见 touch_seen 的 60 秒节流
    #   battles                  —— 野外战斗的临时 token，见 handlers_battle._store_battle：
    #                               打出战斗 → 1 秒内就被 battle-loot 消费掉，纯属进程内暂存。
    #                               今日 1.08 万次 /battle/wild-run 全都在为它整档落盘，
    #                               排除后这项写盘直接消失（代价：进程重启时未结算的战斗
    #                               会丢 token，客户端提示「战斗记录已失效，请重新发起战斗」，
    #                               与本来的超时表现一致）。
    _VOLATILE_SAVE_KEYS = ("updatedAt", "saveRevision")
    _VOLATILE_META_KEYS = ("lastSeen", "battles")

    def _signature(self) -> str | None:
        """当前内容的指纹（不含易变字段）。内容相同 → 同一个值。"""
        try:
            save = self.raw.get("save") or {}
            meta = self.raw.get("meta") or {}
            body = {
                "s": {k: v for k, v in save.items() if k not in self._VOLATILE_SAVE_KEYS},
                "m": {k: v for k, v in meta.items() if k not in self._VOLATILE_META_KEYS},
            }
            blob = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            return hashlib.blake2b(blob, digest_size=8).hexdigest()
        except Exception:
            # 算不出来就当「有变化」，宁可多写一次也不要漏存
            return None

    def flush(self, force: bool = False) -> bool:
        """落盘；内容与上次落盘完全一致时直接跳过，返回 False。

        原来 server.py 是每个已鉴权请求结束都无条件 `state.flush()`，
        等于把整份存档（中位数 9KB、最大 124KB）重写一遍。线上实测单次 6.9ms
        （6.76ms 花在 indent=1 的序列化上），而今日 78% 的请求是纯读
        （/user/info、/offline/heartbeat、/announcements、/ranking、/treasure/status…），
        这些请求根本不需要写盘。

        判定顺序：
          1. dirty（bump/mark_dirty 置位）→ 一定有改动，直接写，省一次签名计算
          2. 否则比签名：和上次落盘一致 → 跳过；不一致 → 写（说明有代码改了数据
             却没 bump，顺手打一条告警，方便以后把这些地方补上）
        """
        if not force and not self.dirty:
            sig = self._signature()
            if sig is not None and sig == self._persisted_sig:
                return False
            if sig is not None and self._persisted_sig is not None:
                now_ts = time.time()
                # 默认 5 分钟只打一条，避免一个漏 bump 的接口把日志刷爆；
                # 排查时 LY_DIRTY_TRACE=1 可以逐条打，直接看出是哪个接口。
                if _DIRTY_TRACE or now_ts - self._sig_warn_ts >= 300:
                    self._sig_warn_ts = now_ts
                    print("[state] %s 数据变了但没 bump()，已按签名兜底落盘（%s）"
                          % (self.username, getattr(self, "_req_path", "") or "?"), flush=True)
        self.raw["save"]["saveRevision"] = self.raw["meta"].get("revision", 1)
        self.raw["save"]["updatedAt"] = iso(now())
        dump_json(self.path, self.raw)
        self.dirty = False
        self._persisted_sig = self._signature()
        return True

    def bump(self, step: int = 1) -> int:
        self.raw["meta"]["revision"] = int(self.raw["meta"].get("revision", 1)) + step
        self.dirty = True
        return self.raw["meta"]["revision"]

    def mark_dirty(self) -> None:
        """显式标脏：改了 raw 里任何地方都可以调用，保证一定落盘。"""
        self.dirty = True

    # -- 已消耗宠物黑名单（放生 / 图鉴投入 / 幻化副宠）
    #
    # `/save/upload` 落库前会用 `_merge_authoritative_progress` 把 petsData 按 id
    # 求并集（为了保证服务端刚发的宠不被客户端旧存档冲掉）。副作用是：
    # 只要某个客户端手里的快照还残留着这只宠（切后台的旧会话、双端登录、
    # 上传失败留下的旧包），它下一次整包上传就把**已经被消耗掉的宠原样塞回去**。
    # 玩家看到的就是「投进图鉴 / 放生 / 幻化吃掉的宠过一会儿又回来了」。
    #
    # 所以消耗宠物时记一笔 id，上传时按这个名单把它们剔掉。
    # 名单放在 meta（不会被客户端整包覆盖），只保留最近 800 条。
    CONSUMED_PET_CAP = 800

    def mark_pet_consumed(self, pet_id: str) -> None:
        pid = str(pet_id or "").strip()
        if not pid:
            return
        ids = self.raw["meta"].setdefault("consumedPetIds", [])
        if not isinstance(ids, list):
            ids = []
            self.raw["meta"]["consumedPetIds"] = ids
        if pid in ids:
            return
        ids.append(pid)
        if len(ids) > self.CONSUMED_PET_CAP:
            del ids[: len(ids) - self.CONSUMED_PET_CAP]

    def drop_consumed_pets(self, save: dict) -> int:
        """把已消耗的宠物从待落库存档里剔掉，返回剔除数量。"""
        ids = self.raw["meta"].get("consumedPetIds")
        if not isinstance(ids, list) or not ids:
            return 0
        drop = {str(x) for x in ids}
        pd = save.get("petsData") if isinstance(save, dict) else None
        if not isinstance(pd, dict):
            return 0
        removed = 0
        for key in ("pets", "pet_bag"):
            arr = pd.get(key)
            if not isinstance(arr, list):
                continue
            kept = []
            for pet in arr:
                if isinstance(pet, dict) and str(pet.get("id", "")) in drop:
                    removed += 1
                    continue
                kept.append(pet)
            if removed:
                pd[key] = kept
        return removed

    # -- 快捷访问
    @property
    def save(self) -> dict:
        return self.raw.setdefault("save", {})

    @property
    def meta(self) -> dict:
        return self.raw.setdefault("meta", {})

    @property
    def player(self) -> dict:
        return self.save.setdefault("playerData", {})

    @property
    def pets(self) -> list:
        return self.save.setdefault("petsData", {}).setdefault("pets", [])

    @property
    def pet_bag(self) -> list:
        return self.save.setdefault("petsData", {}).setdefault("pet_bag", [])

    @property
    def inv(self) -> dict:
        return self.save.setdefault("inventoryData", {})

    @property
    def items(self) -> list:
        return self.inv.setdefault("items", [])

    @property
    def equipment(self) -> dict:
        return self.save.setdefault("equipmentData", {})

    @property
    def revision(self) -> int:
        return int(self.meta.get("revision", 1))

    # -- 在线/离线 -------------------------------------------------------
    def open_session(self) -> int:
        """登录时调用：冻结一份离线时长快照，返回离线秒数。

        客户端登录后会立刻调 `/economy/offline-claim`
        （game_manager.gd `_on_cloud_save_downloaded`、`run_post_login_offline_claim_async`）。
        如果登录时就无条件刷新 lastSeen，结算时算出来永远是 0 秒，
        玩家永远看不到「离线奖励」弹窗。所以登录时先按旧的 lastSeen 算出离线时长
        存进 meta.offlinePending，再把 lastSeen 推到当前时间。
        """
        cur = now()
        prev = self.meta.get("lastSeen")
        seconds = 0
        if prev:
            try:
                seconds = max(0, int((cur - datetime.fromisoformat(str(prev))).total_seconds()))
            except (TypeError, ValueError):
                seconds = 0
        # 客户端登录失败重试 / 断线重连会再调一次 /login。第二次算出来接近 0 秒，
        # 如果直接覆盖，玩家这一整段离线收益就没了 —— 保留更大的那一份。
        existing = self.meta.get("offlinePending")
        if isinstance(existing, dict):
            try:
                old = max(0, int(existing.get("seconds", 0) or 0))
            except (TypeError, ValueError):
                old = 0
            if old > seconds:
                seconds = old
        self.meta["offlinePending"] = {"seconds": seconds, "capturedAt": iso(cur)}
        self.meta["lastSeen"] = iso(cur)
        # 登录序号：`offline_claim` 靠它判断「这是同一次登录里的第二次请求」
        # （下载存档 + 主界面各领一次），跨登录就不回放旧的离线弹窗数据。
        self.meta["loginSeq"] = int(self.meta.get("loginSeq", 0) or 0) + 1
        return seconds

    def touch_seen(self) -> None:
        """已鉴权请求都会刷新最后在线时间（`/economy/offline-claim` 除外）。"""
        self.meta["lastSeen"] = iso(now())
        # lastSeen 在落盘签名里被排除（否则每个请求都会整档重写），
        # 但也不能永远不落盘：进程被 kill 后按磁盘上的旧 lastSeen 算离线时长，
        # 玩家会白拿一大笔离线奖励。这里 60 秒节流，最多一分钟写一次。
        now_ts = time.time()
        if now_ts - self._seen_flush_ts >= 60:
            self._seen_flush_ts = now_ts
            self.dirty = True

    def peek_offline_seconds(self) -> int:
        """取离线秒数：优先用登录时冻结的快照，没有快照才按 lastSeen 现算。"""
        snap = self.meta.get("offlinePending")
        if isinstance(snap, dict) and "seconds" in snap:
            try:
                return max(0, int(snap.get("seconds", 0) or 0))
            except (TypeError, ValueError):
                return 0
        prev = self.meta.get("lastSeen")
        if not prev:
            return 0
        try:
            return max(0, int((now() - datetime.fromisoformat(str(prev))).total_seconds()))
        except (TypeError, ValueError):
            return 0

    # -- 每日计数（跨天自动归零）
    def _daily(self) -> dict:
        box = self.meta.setdefault("daily", {})
        if box.get("date") != today():
            box["date"] = today()
            box["v"] = {}
        return box.setdefault("v", {})

    def daily(self, key: str, default: int = 0) -> int:
        return int(self._daily().get(key, default))

    def daily_set(self, key: str, value: int) -> None:
        self._daily()[key] = int(value)

    def daily_inc(self, key: str, step: int = 1) -> int:
        value = self.daily(key) + step
        self.daily_set(key, value)
        return value

    def daily_left(self, key: str, cap: int) -> int:
        return max(0, cap - self.daily(key))

    def daily_try(self, key: str, cap: int) -> bool:
        """消耗一次机会；用满返回 False。"""
        if cap <= 0:
            return True
        if self.daily(key) >= cap:
            return False
        self.daily_inc(key)
        return True

    # -- VIP / 双倍经验 / 经验倍率 -------------------------------------
    # 数值来自客户端 scripts/vip_benefits.gd（原版客户端自带，非抓包）：
    #   exp_multiplier            [1.0, 1.3, 1.6, 2.0]
    #   max_offline_hours         [8, 12, 16, 24]
    #   offline_gift_chance       [0.25, 0.4, 0.45, 0.5]
    #   xianmo_max_action_points  [24, 30, 40, 48]
    #   xianmo_recover_interval   [60, 50, 40, 30] 分钟/点
    #   martial_daily_limit       [3, 4, 5, 6]
    VIP_EXP_MULT = (1.0, 1.3, 1.6, 2.0)
    VIP_OFFLINE_HOURS = (8, 12, 16, 24)
    VIP_OFFLINE_GIFT = (0.25, 0.4, 0.45, 0.5)
    VIP_XIANMO_POINTS = (24, 30, 40, 48)
    VIP_XIANMO_INTERVAL_MIN = (60, 50, 40, 30)
    VIP_MARTIAL_DAILY = (3, 4, 5, 6)
    # scripts/vip_benefits.gd: tower_sweep_limit [1,2,2,3] / suit_sweeps_max [2,3,4,5]
    VIP_TOWER_SWEEPS = (1, 2, 2, 3)
    VIP_SUIT_SWEEPS = (2, 3, 4, 5)

    # -- 贵族（noble）---------------------------------------------------
    # ui/city/settings_hall.gd:869 THRESHOLDS：按「累计消耗元宝」分 15 档
    NOBLE_THRESHOLDS = (1000, 2000, 5000, 8000, 11000, 15000, 20000, 26000,
                        43000, 51000, 60000, 70000, 81000, 93000, 106000)
    # settings_hall.gd:867 RANK_NAMES（0 级 = 平民）
    NOBLE_NAMES = ("平民", "一等贵族", "二等贵族", "三等贵族", "四等贵族", "五等贵族",
                   "六等贵族", "七等贵族", "八等贵族", "九等贵族", "亲王", "郡王",
                   "亲公", "国公", "皇帝")
    # scripts/noble_benefits.gd：幻化完美% / 自创武功% / 宠物伤害%（经验同表）
    NOBLE_MERGE_BONUS = (0, 2, 4, 6, 8, 9, 10, 16, 23, 30, 37, 43, 50, 57, 63, 70)
    NOBLE_MARTIAL_BONUS = (0, 3, 7, 10, 13, 17, 20, 23, 27, 30, 33, 37, 40, 43, 47, 50)
    NOBLE_PET_DAMAGE = (0, 0, 0, 0, 0, 0, 3, 5, 7, 10, 13, 16, 19, 22, 25, 30)
    NOBLE_EXP_BONUS = NOBLE_PET_DAMAGE
    # 三种双倍经验丹的叠加通道与时长。
    # 键必须是**原版道具名**（game_data.gd ITEMS：双倍经验丹 / (周) / (月)）：
    # take_item / find_item 是按名字精确匹配的，写成「双倍经验符」时背包里的丹
    # 永远找不到，玩家点使用只会收到「道具数量不足」。
    DOUBLE_EXP_CHANNELS = {
        "双倍经验丹": 24 * 3600,
        "双倍经验丹(周)": 7 * 24 * 3600,
        "双倍经验丹(月)": 30 * 24 * 3600,
    }
    # 历史遗留的「符」写法：旧存档通道键、旧配置里的商品名，读到一律归一到丹。
    DOUBLE_EXP_ALIASES = {
        "双倍经验符": "双倍经验丹",
        "双倍经验符·周": "双倍经验丹(周)",
        "双倍经验符·月": "双倍经验丹(月)",
        "双倍经验符(周)": "双倍经验丹(周)",
        "双倍经验符(月)": "双倍经验丹(月)",
    }

    def vip_tier(self) -> int:
        """VipBenefits.tier_from_player 的服务端复刻：过期或非 VIP 一律 0。"""
        p = self.player
        if not bool(p.get("is_vip", False)):
            return 0
        try:
            level = int(p.get("vip_level", 0) or 0)
        except (TypeError, ValueError):
            level = 0
        if level <= 0:
            return 0
        raw = str(p.get("vip_expire_time_str", "") or "").strip()
        if not raw:
            return 0
        expire = 0
        text = raw.replace("Z", "+00:00").replace(" ", "T")
        try:
            expire = int(datetime.fromisoformat(text).timestamp())
        except ValueError:
            try:
                expire = int(datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=CN_TZ).timestamp())
            except ValueError:
                expire = 0
        if expire <= int(time.time()):
            return 0
        return max(0, min(3, level))

    def double_exp_channels(self) -> dict:
        """三种双倍经验丹各自的到期时间（服务端权威）。"""
        box = self.player.get("double_exp_channels")
        if not isinstance(box, dict):
            box = {}
        # 老存档：只有单个 double_exp_until 时，把它当成「日」通道
        legacy = int(self.player.get("double_exp_until", 0) or 0)
        if legacy > 0 and not box:
            box = {"双倍经验丹": legacy}
        # 旧写法「双倍经验符 / ·周 / ·月」统一归一成原版道具名，
        # 否则改名后旧档里那几条通道会变成「查不到的孤儿键」，加成直接失效。
        merged: dict = {}
        for key, value in box.items():
            name = self.DOUBLE_EXP_ALIASES.get(str(key), str(key))
            merged[name] = max(int(merged.get(name, 0) or 0), int(value or 0))
        return merged

    def double_exp_active_count(self) -> int:
        """当前生效的双倍经验丹种类数（每多一种 +100% 经验）。"""
        ts = int(time.time())
        box = self.double_exp_channels()
        return sum(1 for v in box.values() if int(v or 0) > ts)

    def sync_double_exp_fields(self) -> None:
        """把多通道到期时间同步成客户端认的 double_exp_until（取最晚的那条）。"""
        box = self.double_exp_channels()
        self.player["double_exp_channels"] = {k: int(v) for k, v in box.items() if int(v or 0) > 0}
        latest = max([int(v) for v in box.values() if int(v or 0) > 0] or [0])
        self.player["double_exp_until"] = latest

    # ---- 宠物双倍经验丹（只加灵兽，不加人物）---------------------------
    #
    # 帮贡商店卖「宠物双倍经验丹」。原版里灵兽经验是独立结算的，这类道具
    # 不该像「双倍经验丹」那样把人宠一起翻倍，所以单独一条通道：
    # 到期时间放 meta（服务端私有），不塞进 playerData，避免被客户端存档盖掉。
    PET_DOUBLE_EXP_SECONDS = 30 * 60

    def pet_double_exp_until(self) -> int:
        """宠物双倍经验的到期时间戳（0 = 没有 / 已过期）。"""
        try:
            until = int(self.meta.get("petDoubleExpUntil", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            until = 0
        return until if until > int(time.time()) else 0

    def add_pet_double_exp(self, seconds: int = 0, stack: bool = True) -> int:
        """开通 / 顺延宠物双倍经验，返回新的到期时间戳。"""
        seconds = int(seconds or self.PET_DOUBLE_EXP_SECONDS)
        now_ts = int(time.time())
        base = self.pet_double_exp_until() if stack else now_ts
        until = max(now_ts, base) + max(60, seconds)
        self.meta["petDoubleExpUntil"] = until
        return until

    def pet_exp_multiplier(self) -> float:
        """灵兽经验倍率：宠物双倍经验生效中就是 2.0。"""
        return 2.0 if self.pet_double_exp_until() > 0 else 1.0

    NEWBIE_EXP_DAYS = 10

    def newbie_exp_until(self) -> int:
        """新人经验加成到期时间戳（注册起 10 天，原版 player.is_newbie_exp_active）。

        老存档没有这个字段：按 meta.createdAt 补算一次并存回去，
        否则客户端永远播不出「新人经验加成：经验 +100%」，经验也少一块。
        """
        p = self.player
        if "newbie_exp_until" in p:
            return int(p.get("newbie_exp_until", 0) or 0)
        until = 0
        raw = str(self.meta.get("createdAt", "") or "")
        if raw:
            try:
                until = int(datetime.fromisoformat(raw).timestamp()) + self.NEWBIE_EXP_DAYS * 86400
            except (TypeError, ValueError):
                until = 0
        p["newbie_exp_until"] = until
        return until

    def weekly_field_exp_bonus(self) -> float:
        """周常「野外战斗经验 +100%」（runtime-config weeklyBuff.days，weekday 6/7）。

        原版是加算：exp_mult = VIP + 双倍丹种类数 + 帮派 + 贵族 + 新人 + 周末。
        这里只取 label 含「野外」且 value>=100 的那条（周末），其它周常（掉率翻倍等）不碰。
        """
        cfg = RULES.m("weekly_buff")
        if not bool(cfg.get("enabled", True)):
            return 0.0
        today = now().isoweekday()
        best = 0.0
        for day in (cfg.get("days") or []):
            try:
                if int(day.get("weekday", 0) or 0) != today:
                    continue
                if not bool(day.get("enabled", True)):
                    continue
                label = str(day.get("label", "") or "")
                value = float(day.get("value", 0) or 0)
            except (TypeError, ValueError):
                continue
            if "野外" in label and value >= 100:
                best = max(best, value / 100.0)
        return best

    # ------------------------------------------------------------------
    #  周常加成（runtime-config weeklyBuff）
    #  周一 幻化完美几率 +5%（每日上限 20 次）
    #  周五 进化变异几率翻倍 / 进化领悟技能几率翻倍（每日上限 20 次）
    #  周末 野外战斗经验 +100%（加算进 exp_multiplier）
    #  计数按「玩家 + 北京时间日期」存在 meta.weeklyUsage，跨天自动归零。
    # ------------------------------------------------------------------
    def _weekly_day(self, weekday: int | None = None) -> dict:
        cfg = RULES.m("weekly_buff")
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", True)):
            return {}
        today = int(weekday if weekday is not None else now().isoweekday())
        for day in (cfg.get("days") or []):
            if not isinstance(day, dict):
                continue
            try:
                if int(day.get("weekday", 0) or 0) != today:
                    continue
            except (TypeError, ValueError):
                continue
            if not bool(day.get("enabled", True)):
                continue
            return day
        return {}

    def weekly_usage(self) -> dict:
        """当日周常用量（mergeUsed / evolveUsed），跨天归零。"""
        today = now().date().isoformat()
        usage = self.meta.get("weeklyUsage")
        if not isinstance(usage, dict) or str(usage.get("date", "")) != today:
            usage = {"date": today, "mergeUsed": 0, "evolveUsed": 0}
            self.meta["weeklyUsage"] = usage
        for key in ("mergeUsed", "evolveUsed"):
            try:
                usage[key] = int(usage.get(key, 0) or 0)
            except (TypeError, ValueError):
                usage[key] = 0
        return usage

    def _weekly_cap_left(self, day: dict, usage_key: str) -> bool:
        """当日这条周常还有没有剩余次数（dailyCap<=0 视为不限次数）。"""
        try:
            cap = int(day.get("dailyCap", 0) or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap <= 0:
            return True
        return self.weekly_usage().get(usage_key, 0) < cap

    def weekly_merge_bonus(self) -> int:
        """周一「幻化完美几率 +5%」：返回要补给 A 档的百分点（没触发返回 0）。"""
        day = self._weekly_day()
        if not day or "幻化" not in str(day.get("label", "")):
            return 0
        if not self._weekly_cap_left(day, "mergeUsed"):
            return 0
        try:
            return max(0, int(float(day.get("value", 0) or 0)))
        except (TypeError, ValueError):
            return 0

    def weekly_evolve_mult(self) -> float:
        """周五「进化变异 / 领悟几率翻倍」：返回几率乘数（没触发返回 1.0）。"""
        day = self._weekly_day()
        if not day or "进化" not in str(day.get("label", "")):
            return 1.0
        if not self._weekly_cap_left(day, "evolveUsed"):
            return 1.0
        try:
            return max(1.0, float(day.get("value", 1) or 1.0))
        except (TypeError, ValueError):
            return 1.0

    def weekly_mark_used(self, usage_key: str) -> None:
        usage = self.weekly_usage()
        usage[usage_key] = int(usage.get(usage_key, 0) or 0) + 1

    def gang_level(self) -> int:
        """本账号所在帮派的等级（不在帮里就是 0）。"""
        gangs = gang_snapshot()
        if not gangs:
            return 0
        me = str(self.username or "")
        for gang in gangs.values():
            if not isinstance(gang, dict):
                continue
            for member in gang.get("members") or []:
                if isinstance(member, dict) and str(member.get("username", "")) == me:
                    try:
                        return max(0, min(10, int(gang.get("level", 1) or 0)))
                    except (TypeError, ValueError):
                        return 0
        return 0

    def gang_exp_bonus_percent(self) -> float:
        """帮派等级经验加成（%）。

        原版 game_manager.get_gang_exp_bonus_percent() → gang_manager.get_buff_percent("exp")，
        加算进 get_total_exp_multiplier。本服 /gang/buffs 的 expBonusPercent 口径是
        「帮派等级 × 2.5%」，以前只下发给客户端看、服务端算经验时没加，
        帮里的人实际少拿了这部分（帮会 4 级 = 少 10%）。
        """
        return GANG_EXP_PERCENT_PER_LEVEL * float(self.gang_level())

    def exp_multiplier(self) -> float:
        """player.gd get_total_exp_multiplier 的服务端复刻。

        = VIP 倍率 + 帮会加成 + 贵族加成 + 生效中的双倍经验丹种类数
          + 新人 10 天 +100% + 周末野外 +100%
          （本服双倍丹是「每多一种 +100%」，三丹全开 +300%，这是与原版的不同点）
        """
        total = float(self.VIP_EXP_MULT[self.vip_tier()] + self.double_exp_active_count())
        # 帮派等级加成（原版 get_gang_exp_bonus_percent，加算）
        total += self.gang_exp_bonus_percent() / 100.0
        # 贵族经验加成（game_manager.gd:9103 noble_bonus = exp_pct / 100.0，加算）
        lv = max(0, min(15, self.noble_level()))
        total += self.NOBLE_EXP_BONUS[lv] / 100.0
        if int(time.time()) < self.newbie_exp_until():
            total += 1.0
        total += self.weekly_field_exp_bonus()
        return total

    def field_max_level_ups(self) -> int:
        """player.gd get_field_max_level_ups()：追赶期内一场最多升几级（1~10）。"""
        p = self.player
        if not bool(p.get("catchup_enabled", False)):
            return 1
        try:
            ups = int(p.get("catchup_field_max_level_ups", 1) or 1)
        except (TypeError, ValueError):
            ups = 1
        return max(1, min(10, ups))

    def update_catchup(self, top_reincarnation: int = 0) -> int:
        """按「与全服第一名投胎次数的差距」刷新追赶参数。

        原服口径（反编译客户端 ui/city/settings_hall.gd:1542）：
            "投胎追赶：落后第一名 %d 投（第一名 %d），野外一场最多连升 %d 级"
            "飞升/投胎回 1 级不影响；只看投胎次数差距"
        即 catchup_gap = 全服第一投胎次数 − 自己投胎次数，单位是「投」（次），
        不是等级差。分档 gap≥300→2 级、≥500→3、≥700→4、≥900→5，
        总上限 maxFieldLevelUps=5。野外一场能升几级就是它。

        注意：本服原来按「等级差」算，而本服最高等级往往就是玩家自己 → gap 恒 0，
        客户端永远不播「投胎追赶」，野外一场只升 1 级，比原版慢一截。
        """
        p = self.player
        cfg = RULES.m("gameplay").get("reincarnationCatchup", {}) or {}
        enabled = bool(cfg.get("enabled", True))
        if top_reincarnation <= 0:
            top_reincarnation = 0
            for other in STORE.all_states():
                try:
                    top_reincarnation = max(
                        top_reincarnation, int(other.player.get("reincarnation", 0) or 0))
                except (TypeError, ValueError, AttributeError):
                    continue
        # 配置里的 first_reincarnation 作兜底下限：本服玩家少、没人投胎到几百次时，
        # 用原服第一名的投胎数当基准，才复刻得出原版的追赶节奏（0 = 不启用兜底）。
        try:
            floor = int(cfg.get("first_reincarnation", 0) or 0)
        except (TypeError, ValueError):
            floor = 0
        if floor > top_reincarnation:
            top_reincarnation = floor
        try:
            my_re = int(p.get("reincarnation", 0) or 0)
        except (TypeError, ValueError):
            my_re = 0
        gap = max(0, int(top_reincarnation) - my_re)
        ups = 1
        if enabled:
            for bracket in (cfg.get("brackets") or []):
                try:
                    if gap >= int(bracket.get("minGap", 0) or 0):
                        ups = max(ups, int(bracket.get("maxLevelUps", 1) or 1))
                except (TypeError, ValueError):
                    continue
            try:
                ups = min(ups, int(cfg.get("maxFieldLevelUps", 5) or 5))
            except (TypeError, ValueError):
                ups = 5
        p["catchup_enabled"] = bool(enabled)
        p["catchup_gap"] = gap
        p["catchup_day"] = today()
        p["catchup_first_reincarnation"] = int(top_reincarnation)
        p["catchup_field_max_level_ups"] = max(1, min(10, ups))
        return p["catchup_field_max_level_ups"]

    # -- 货币与物品
    def gold(self) -> int:
        return int(self.player.get("gold", 0))

    def add_gold(self, amount: int) -> int:
        self.player["gold"] = max(0, self.gold() + int(amount))
        return self.player["gold"]

    def yuanbao(self) -> int:
        return int(self.player.get("yuanbao_currency", 0))

    # -- 贵族：累计消耗元宝 → 等级 ------------------------------------
    @classmethod
    def noble_level_of(cls, total_consumed: int) -> int:
        """按累计消耗元宝算贵族等级（0~15，越多档越高）。"""
        total = max(0, int(total_consumed or 0))
        lv = 0
        for need in cls.NOBLE_THRESHOLDS:
            if total >= need:
                lv += 1
            else:
                break
        return lv

    @classmethod
    def noble_next_threshold(cls, level: int) -> int:
        """下一档需要的累计元宝；满级返回最后一档。"""
        lv = max(0, min(15, int(level or 0)))
        if lv >= len(cls.NOBLE_THRESHOLDS):
            return cls.NOBLE_THRESHOLDS[-1]
        return cls.NOBLE_THRESHOLDS[lv]

    def noble_consumed(self) -> int:
        return int(self.player.get("noble_points", 0) or 0)

    def noble_level(self) -> int:
        p = self.player
        return max(0, min(15, int(p.get("noble_level", 0) or 0)))

    def sync_noble_level(self) -> int:
        """按 noble_points 重算等级/称谓（老档迁移、后台改档都能自愈）。"""
        p = self.player
        lv = self.noble_level_of(self.noble_consumed())
        if int(p.get("noble_level", -1) or 0) != lv or not p.get("noble_name"):
            p["noble_level"] = lv
            p["noble_name"] = self.NOBLE_NAMES[min(lv, len(self.NOBLE_NAMES) - 1)]
            # 这里真的改了存档，必须自己 bump：调用方之一是 GET /noble/info（只读接口），
            # 原来没标脏，靠 flush 的签名兜底才存住，还会每 5 分钟打一条「没 bump」告警。
            self.bump()
        return lv

    def noble_pet_damage_pct(self) -> int:
        """出战宠物攻/防/敏的贵族加成百分比（原版 noble_benefits.gd PET_DAMAGE）。"""
        lv = max(0, min(15, self.noble_level()))
        return int(self.NOBLE_PET_DAMAGE[lv])

    def add_noble_consumption(self, amount: int) -> int:
        """累加「消耗元宝额度」并刷新贵族等级，返回新等级。"""
        amount = int(amount or 0)
        if amount <= 0:
            return self.noble_level()
        self.player["noble_points"] = self.noble_consumed() + amount
        return self.sync_noble_level()

    def add_yuanbao(self, amount: int) -> int:
        """加减元宝；真实扣掉的部分同时记入贵族累计消耗。"""
        amount = int(amount)
        before = self.yuanbao()
        after = max(0, before + amount)
        self.player["yuanbao_currency"] = after
        spent = before - after
        if spent > 0:
            self.add_noble_consumption(spent)
        return after

    def friendship(self) -> int:
        return int(self.player.get("friendship_points", 0))

    def add_friendship(self, amount: int) -> int:
        self.player["friendship_points"] = max(0, self.friendship() + int(amount))
        return self.player["friendship_points"]

    # -- 经验与升级 ------------------------------------------------------
    # 客户端这些地方用的是服务端下发的绝对 level/experience（game_manager.gd
    # apply_field_battle_loot_player_slice），服务端只加经验不判升级的话，
    # 玩家会一直停在 1 级、经验条却越积越多。这里按 player.gd / pet_manager.gd
    # 的口径把升级补上。
    PLAYER_MAX_LEVEL = 1200
    PET_MAX_LEVEL_EVOLUTION = 1200
    PET_MAX_LEVEL_NORMAL = 1500

    @staticmethod
    def required_exp(level: int) -> int:
        """GameData.get_required_exp 的服务端复刻。

        客户端会从 `/gameplay/runtime-config` 应用 `playerLevelExp`：
            L < breakLevel(500)  → preBreakCoeff × L² = 1.7 × L²
            L ≥ breakLevel       → postBreakBase + postBreakGrowthCoeff × (L-500)²
                                   = 1100000 + 4.6 × (L-500)²
        原来这里一律按 1.7×L² 算，500 级以后服务端要求的经验比客户端少一半
        （600 级：服务端 61.2 万 vs 原版 114.6 万），玩家会比原版快将近一倍升级，
        经验条还会和客户端显示对不上。
        """
        lv = max(1, min(int(level), 1200))
        cfg = RULES.v("gameplay", "playerLevelExp", {})
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", False)):
            return int(1.7 * lv * lv)
        break_lv = max(1, int(cfg.get("breakLevel", 500) or 500))
        pre_coeff = float(cfg.get("preBreakCoeff", 1.7) or 1.7)
        if lv < break_lv:
            return int(pre_coeff * lv * lv)
        post_base = max(0, int(cfg.get("postBreakBase", 0) or 0))
        post_growth = float(cfg.get("postBreakGrowthCoeff", 4.6) or 4.6)
        delta = lv - break_lv
        # 乘法顺序要和原版一致：GDScript 是 post_growth * pow(float(delta), 2.0)，
        # 写成 post_growth * delta * delta 浮点末位会差 1（Lv1200 会差 1 点经验）。
        return int(post_base + post_growth * float(delta) ** 2)

    @staticmethod
    def pet_required_exp(level: int) -> int:
        return State.required_exp(level)

    def _points_per_level(self) -> int:
        realm = str(self.player.get("realm", "凡人") or "凡人")
        if realm in ("地仙", "天仙"):
            return 4
        if realm in ("大罗金仙", "幻灵天神"):
            return 5
        return 3

    # -- 自动加点 --------------------------------------------------------
    # 原版机制（player.gd）：
    #   1. 升级时若勾了自动加点，点数进 pending_auto_points，不进 points；
    #   2. `on_level_up_for_auto_allocate()` 只有 pending >= AUTO_ALLOCATE_THRESHOLD(100)
    #      才会触发一次批量分配 —— 不足 100 点就一直攒着；
    #   3. `auto_allocate_pending_points()` 按 auto_allocation_plan（一轮合计 1~10 点）
    #      取 `full_budget = (pending // total_plan) * total_plan` 整轮分配，
    #      余数留在池里等下次。
    # 服务器模式下 `_attr_mutation_allowed` 为 false，客户端自己不加点，
    # 而是由 main_ui `_schedule_player_attr_auto_allocate_check()` 在 pending >= 100 时
    # 调 `/player/attr/auto-allocate` 让服务端落点。所以服务端必须严格照这个阈值走，
    # 否则玩家会因为「提前落点」永远看不到 100 点一次性分配的效果。
    AUTO_ALLOCATE_THRESHOLD = 100
    ATTR_PLAN_KEYS = ("vitality", "attack", "defense", "magic", "agility")
    ATTR_FIELD_MAP = {
        "vitality": "vitality",
        "magic": "magic",
        "attack": "attack_base",
        "defense": "defense_base",
        "agility": "agility_base",
    }

    def norm_attr_plan(self, raw) -> dict:
        """规整加点方案：五项整数、每项 0~10、合计 1~10，非法则返回空。"""
        out: dict = {}
        if not isinstance(raw, dict):
            return out
        for key in self.ATTR_PLAN_KEYS:
            try:
                out[key] = max(0, min(10, int(raw.get(key, 0) or 0)))
            except (TypeError, ValueError):
                out[key] = 0
        total = sum(out.values())
        if total <= 0 or total > 10:
            return {}
        return out

    def auto_allocate_pending_points(self, force: bool = False) -> int:
        """按 auto_allocation_plan 批量分配 pending_auto_points，返回实际分配点数。

        严格复刻 player.gd：
          - 未满 `AUTO_ALLOCATE_THRESHOLD`(100) 点不分配（force=True 仅供 GM/调试）；
          - 只分配「整轮」部分，余数留在 pending_auto_points 里等下次凑满。
        """
        p = self.player
        if not bool(p.get("auto_allocation_enabled", False)):
            return 0
        plan = self.norm_attr_plan(p.get("auto_allocation_plan"))
        if not plan:
            return 0
        pending = int(p.get("pending_auto_points", 0) or 0)
        per_cycle = sum(plan.values())
        if pending <= 0 or per_cycle <= 0:
            return 0
        if not force and pending < self.AUTO_ALLOCATE_THRESHOLD:
            # 原版就是攒满 100 点才批量分配一次，不足 100 留在池里
            return 0
        full_budget = (pending // per_cycle) * per_cycle
        if full_budget <= 0:
            return 0
        cycles = full_budget // per_cycle
        allocated = 0
        for _ in range(cycles):
            for key in self.ATTR_PLAN_KEYS:
                for _n in range(int(plan.get(key, 0))):
                    field = self.ATTR_FIELD_MAP[key]
                    p[field] = int(p.get(field, 0) or 0) + 1
                    allocated += 1
        p["pending_auto_points"] = pending - allocated
        self.sync_final_stats()
        return allocated

    def sync_final_stats(self) -> None:
        """把 final_* 快照同步成基础值（客户端加载后会按自身逻辑再叠加装备等加成）。"""
        p = self.player
        p["final_attack"] = 10 + int(p.get("attack_base", 0) or 0)
        p["final_defense"] = 10 + int(p.get("defense_base", 0) or 0)
        p["final_agility"] = 10 + int(p.get("agility_base", 0) or 0)
        p["final_max_hp"] = int(p.get("max_hp", 0) or 0) + int(p.get("vitality", 0) or 0) * 10
        p["final_max_mp"] = int(p.get("max_mp", 0) or 0) + int(p.get("magic", 0) or 0) * 10

    # 客户端 ui/main_ui.gd `_PLAYER_ATTR_SLICE_KEYS` 的对应清单。
    # RPC 回包不带这些键，客户端 `_apply_player_attr_authoritative_save` 会直接
    # 走 "缺少 attrSlice/saveData.playerData，未写入内存" 分支 —— 服务端存档改了、
    # 玩家界面上属性却一点没变，必须重登才看得到。
    ATTR_SLICE_KEYS = (
        "vitality", "magic", "attack_base", "defense_base", "agility_base",
        "points", "pending_auto_points", "permanent_points",
        "talent_attack", "talent_defense", "talent_agility", "talent_hp",
        "yuanbao_currency",
    )

    def attr_slice(self) -> dict:
        """属性 RPC 回包里的 `attrSlice` 段。"""
        p = self.player
        out = {key: int(p.get(key, 0) or 0) for key in self.ATTR_SLICE_KEYS}
        out["yuanbao"] = int(p.get("yuanbao", p.get("yuanbao_currency", 0)) or 0)
        out["auto_allocation_enabled"] = bool(p.get("auto_allocation_enabled", False))
        plan = p.get("auto_allocation_plan")
        out["auto_allocation_plan"] = dict(plan) if isinstance(plan, dict) else {}
        return out

    def grant_player_exp(self, amount: int, field: bool = False, max_level_ups: int = 1) -> int:
        """加经验并升级，返回本次升了几级。

        `field=True` 复刻野外口径（player.gd add_exp(allow_multiple_levels=false)）：
        一场最多升 max_level_ups 级（默认 1），升了就把多余经验清零；
        没升则把经验压到「本级需求-1」，不允许囤经验。
        """
        amount = int(amount)
        if amount <= 0:
            return 0
        player = self.player
        player["experience"] = max(0, int(player.get("experience", 0) or 0) + amount)
        limit = max(1, int(max_level_ups or 1)) if field else 10 ** 6
        ups = 0
        while ups < limit:
            level = int(player.get("level", 1) or 1)
            if level >= self.PLAYER_MAX_LEVEL:
                break
            need = self.required_exp(level)
            if need <= 0 or int(player.get("experience", 0) or 0) < need:
                break
            player["experience"] = int(player.get("experience", 0) or 0) - need
            player["level"] = level + 1
            ups += 1
            points = self._points_per_level()
            if bool(player.get("auto_allocation_enabled", False)):
                player["pending_auto_points"] = int(player.get("pending_auto_points", 0) or 0) + points
            else:
                player["points"] = int(player.get("points", 0) or 0) + points
            # 与 player.gd 升级一致：max_hp +5，血量同步补 5
            player["max_hp"] = int(player.get("max_hp", 100) or 100) + 5
            hp_cap = int(player["max_hp"])
            player["hp"] = max(0, min(int(player.get("hp", 0) or 0) + 5, hp_cap))
            if str(player.get("realm", "")) in ("地仙", "天仙", "大罗金仙", "幻灵天神"):
                player["cultivation"] = int(player.get("cultivation", 0) or 0) + 1
        if field:
            if ups > 0:
                player["experience"] = 0
            else:
                need = self.required_exp(int(player.get("level", 1) or 1))
                if int(player.get("experience", 0) or 0) > max(0, need - 1):
                    player["experience"] = max(0, need - 1)
        level = int(player.get("level", 1) or 1)
        if level >= self.PLAYER_MAX_LEVEL:
            need = self.required_exp(level)
            if int(player.get("experience", 0) or 0) > need:
                player["experience"] = need
        if ups > 0:
            # 升级后照原版 `on_level_up_for_auto_allocate()` 的口径检查一次阈值：
            # 只有 pending >= 100 才会真正分配，不足 100 继续攒着。
            self.auto_allocate_pending_points()
        return ups

    def pet_max_level(self, pet: dict) -> int:
        cap = int(pet.get("max_level", 0) or 0)
        if cap > 0:
            return cap
        return self.PET_MAX_LEVEL_EVOLUTION if bool(pet.get("is_evolution_pet", False)) else self.PET_MAX_LEVEL_NORMAL

    def _pet_roll_growth(self, pet: dict) -> None:
        """复刻 pet.gd `_level_up_stats()` 的升级随机成长。

        原版宠物每升一级都会给三项成长各加一点随机值（合计在
        `randf_range(-0.001, max_growth)` 内按 stat_ratio 分配），
        max_growth = 0.003 + 代数×0.001 + sqrt(max(0, 血统成长-6))×0.001。
        只按等级重算、不加成长的宠物，等级越高和原版差得越远
        （1000 级时总额外成长能差出几十点）。
        """
        growth = pet.get("growth_rates")
        ratio = pet.get("stat_ratio")
        if not isinstance(growth, dict):
            return
        if not isinstance(ratio, dict):
            ratio = {"attack": 33.33, "defense": 33.33, "agility": 33.34}
        try:
            generation = int(pet.get("generation", 0) or 0)
        except (TypeError, ValueError):
            generation = 0
        lineage = pet.get("lineage_seed_growth", pet.get("initial_total_growth", 3.0))
        try:
            lineage = float(lineage)
        except (TypeError, ValueError):
            lineage = 3.0
        lineage_base = (max(0.0, lineage - 6.0) ** 0.5) * 0.001
        lineage_mult = 1.0
        pm_cfg = RULES.m("pet_mutation")
        if pm_cfg.get("enabled"):
            # 复刻 pet.gd _resolve_level_up_lineage_multiplier：
            # 血统加成按宠物类型（可进化/不可进化）与变异次数再乘一档。
            lu = pm_cfg.get("lineageLevelUp", {}) or {}
            is_evo = bool(pet.get("is_evolution_pet", False)) or bool(pet.get("is_mutant", False))
            type_mult = float(lu.get("evolvableMult", 1.0) or 1.0) if is_evo else float(lu.get("nonEvolvableMult", 1.0) or 1.0)
            mm = lu.get("mutationMult", {}) or {}
            try:
                mc = max(0, min(2, int(pet.get("mutation_count", 0) or 0)))
            except (TypeError, ValueError):
                mc = 0
            mut_mult = float(mm.get(mc, mm.get(str(mc), 1.0)) or 1.0)
            lineage_mult = type_mult * mut_mult
        max_growth = 0.003 + generation * 0.001 + lineage_base * lineage_mult
        total_change = random.uniform(-0.001, max_growth)
        for stat in ("attack", "defense", "agility"):
            try:
                pct = float(ratio.get(stat, 33.33) or 33.33)
            except (TypeError, ValueError):
                pct = 33.33
            prev = float(growth.get(stat, 0) or 0)
            growth[stat] = round(prev + total_change * (pct / 100.0), 3)

    def _recalc_pet_stats(self, pet: dict) -> None:
        """按 pet_manager._update_pet_stats 的公式重算，final_* 保留原倍率。"""
        base = pet.get("base_stats") if isinstance(pet.get("base_stats"), dict) else {}
        growth = pet.get("growth_rates") if isinstance(pet.get("growth_rates"), dict) else {}
        if not growth:
            # 成长数据缺失（礼包直接发的赤翅蛾一类没有 growth_rates）：
            # 没有基准就无从重算，保持原样，别把数值按 0 成长压回 1 级白值。
            return
        diff = max(0, int(pet.get("level", 1) or 1) - 1)
        g_atk = float(growth.get("attack", 0) or 0)
        g_def = float(growth.get("defense", 0) or 0)
        g_agi = float(growth.get("agility", 0) or 0)
        total = round(g_atk + g_def + g_agi, 3)
        values = {
            "attack": round(float(base.get("attack", 0) or 0) + g_atk * diff, 2),
            "defense": round(float(base.get("defense", 0) or 0) + g_def * diff, 2),
            "agility": round(float(base.get("agility", 0) or 0) + g_agi * diff, 2),
        }
        for key, value in values.items():
            prev = float(pet.get(key, 0) or 0)
            prev_final = float(pet.get("final_" + key, prev) or prev)
            # 倍率（变异/精英加成）沿用原值，但夹住范围，避免脏存档把倍率放大成天文数字
            # 倍率来自脏存档时会把 final 放成天文数字（新号种子曾出现
            # attack 9 → final_attack 32903，因为种子把满级宠的 final 原样抄了过来）。
            # 实测正常宠倍率只可能是 1.0 或 1.1，这里夹到 [0.5, 2.0]。
            scale = (prev_final / prev) if prev > 0.0001 else 1.0
            scale = max(0.5, min(2.0, scale))
            pet[key] = value
            pet["final_" + key] = round(value * scale, 2)
        prev_max = float(pet.get("max_hp", 0) or 0)
        prev_final_max = float(pet.get("final_max_hp", prev_max) or prev_max)
        scale_hp = (prev_final_max / prev_max) if prev_max > 0.0001 else 1.0
        scale_hp = max(0.5, min(2.0, scale_hp))
        pet["total_growth"] = total
        # 生命成长按物种 hp_growth 走（原版口径）；顺带纠正存档里被写成 total 的 hp 成长
        growth_hp = pet_hp_growth(pet, total)
        if growth:
            growth["hp"] = growth_hp
            pet["growth_rates"] = growth
        pet["max_hp"] = round(float(base.get("hp", 0) or 0) + growth_hp * diff, 2)
        pet["final_max_hp"] = round(pet["max_hp"] * scale_hp, 2)
        pet["hp"] = pet["final_max_hp"]

    def grant_pet_exp(self, pet: dict | None, amount: int, field: bool = False, max_level_ups: int = 1) -> int:
        """宠物加经验并升级（宠物每级不再随机加成长，只按等级重算属性）。"""
        if not isinstance(pet, dict):
            return 0
        amount = int(amount)
        if amount <= 0:
            return 0
        pet["experience"] = max(0, int(pet.get("experience", 0) or 0) + amount)
        cap_level = self.pet_max_level(pet)
        limit = max(1, int(max_level_ups or 1)) if field else 10 ** 6
        ups = 0
        while ups < limit:
            level = int(pet.get("level", 1) or 1)
            if level >= cap_level:
                break
            need = self.pet_required_exp(level)
            if need <= 0 or int(pet.get("experience", 0) or 0) < need:
                break
            pet["experience"] = int(pet.get("experience", 0) or 0) - need
            pet["level"] = level + 1
            ups += 1
            self._pet_roll_growth(pet)
            self._recalc_pet_stats(pet)
        if field:
            if ups > 0:
                pet["experience"] = 0
            else:
                need = self.pet_required_exp(int(pet.get("level", 1) or 1))
                if int(pet.get("experience", 0) or 0) > max(0, need - 1):
                    pet["experience"] = max(0, need - 1)
        if int(pet.get("level", 1) or 1) >= cap_level:
            need = self.pet_required_exp(int(pet.get("level", 1) or 1))
            if int(pet.get("experience", 0) or 0) > need:
                pet["experience"] = need
        return ups

    def find_item(self, name: str) -> dict | None:
        for item in self.items:
            if str(item.get("name")) == name:
                return item
        return None

    def item_count(self, name: str) -> int:
        item = self.find_item(name)
        return int(item.get("count", 0)) if item else 0

    def add_item(self, name: str, count: int = 1, item_type: str = "material", custom: dict | None = None) -> dict:
        name, qty = split_item_qty(name)
        count = int(count or 0) * qty
        if count <= 0:
            return {}
        # 带自定义数据的物品（限量装备标记、藏宝箱 token、装备词条…）必须
        # 一件一行。原来 find_item(name) 命中同名就只加 count，第二件的
        # custom_data / item_type 会被整块丢掉，客户端拿到的是退化成普通
        # 堆叠的一行 —— 限量判定、丢弃保护、开箱 token 全部失效。
        # 完全相同 custom_data 的仍允许合并，避免把 20 个同名礼包拆成 20 行。
        custom = dict(custom or {})
        if custom:
            for row in self.items:
                if (str(row.get("name")) == name
                        and (row.get("custom_data") or {}) == custom):
                    row["count"] = int(row.get("count", 0)) + int(count)
                    return row
            item = {
                "id": rnd_id("i"),
                "name": name,
                "count": int(count),
                "item_type": item_type,
                "custom_data": custom,
            }
            self.items.append(item)
            return item
        item = self.find_item(name)
        if item is not None:
            item["count"] = int(item.get("count", 0)) + int(count)
            return item
        item = {
            "id": rnd_id("i"),
            "name": name,
            "count": int(count),
            "item_type": item_type,
            "custom_data": {},
        }
        self.items.append(item)
        return item

    def take_item(self, name: str, count: int = 1) -> bool:
        item = self.find_item(name)
        if item is None or int(item.get("count", 0)) < count:
            return False
        item["count"] = int(item.get("count", 0)) - count
        if item["count"] <= 0:
            self.items.remove(item)
        return True

    # -- 邮件（服务端发货的唯一出口，和线上一致）
    def send_mail(self, title: str, content: str, items: list[dict] | None = None,
                  attachments: dict | None = None, expire_days: int = 7) -> dict:
        """发邮件。`items` 是旧口径（只有道具），`attachments` 支持
        {gold, yuanbao, exp, items:[{name,count}], pets:[...]}。"""
        self.meta["mailSeq"] = int(self.meta.get("mailSeq", 900000)) + 1
        attach: dict = {}
        if isinstance(attachments, dict):
            attach.update(attachments)
        if items:
            merged = list(attach.get("items") or [])
            merged.extend(items)
            attach["items"] = merged
        if "items" not in attach:
            attach["items"] = []
        # 「法宝碎片*20」这类把数量写进名字的配置，在落库前统一拆成 name + count，
        # 否则客户端收邮件时按名字查不到 → 附件添加失败、奖励卡在邮箱。
        fixed_items = []
        for row in attach.get("items") or []:
            if not isinstance(row, dict):
                fixed_items.append(row)
                continue
            fixed = dict(row)
            item_name, qty = split_item_qty(str(fixed.get("name", "") or ""))
            fixed["name"] = item_name
            try:
                fixed["count"] = max(1, int(fixed.get("count", 1) or 1)) * qty
            except (TypeError, ValueError):
                fixed["count"] = qty
            fixed_items.append(fixed)
        attach["items"] = fixed_items
        mail = {
            "id": self.meta["mailSeq"],
            "sender": "系统",
            "title": title,
            "content": content,
            "attachments": attach,
            "is_read": False,
            "is_claimed": False,
            "expire_at": iso(now() + timedelta(days=max(1, int(expire_days)))),
            "created_at": iso(now()),
        }
        self.meta.setdefault("mails", []).insert(0, mail)
        return mail

    def _seed_mails(self) -> None:
        seed = load_json(DATA / "mail-seed.json")
        mails = seed.get("mails", [])
        if mails:
            self.meta["mails"] = mails
            ids = [int(m.get("id", 0)) for m in mails if str(m.get("id", "")).isdigit()]
            self.meta["mailSeq"] = max(ids + [900000])

    def _apply_registration_gift(self) -> None:
        """新号注册礼包：

        ① 把种子里的「变异阿努」换成一只随机总成长 10~11 的苦行龟（主战栏唯一宠）；
        ② 角色邮箱发欢迎邮件：七阶可进化宠锦囊 x1 + 溅墨lv1技能书 x1。
        """
        save = self.raw.get("save") if isinstance(self.raw.get("save"), dict) else {}
        pets_data = save.get("petsData")
        if not isinstance(pets_data, dict):
            pets_data = {}
            save["petsData"] = pets_data
        pet = build_registration_pet(self)
        if pet is not None:
            pets_data["pets"] = [pet]
            pets_data["pet_bag"] = []
            pets_data["active_pet_id"] = str(pet.get("id", ""))
        self.send_mail(WELCOME_MAIL_TITLE, WELCOME_MAIL_CONTENT,
                       items=[{"name": name, "count": count}
                              for name, count in WELCOME_GIFT_ITEMS])

    # -- 上榜用的"其他玩家"
    def ranking_rows(self, kind: str = "all") -> list[dict]:
        # 2026-10-28 起改走实时榜（原先是原服采集快照，会把原服玩家名带给本服玩家）
        return STORE.rankings(kind)

    def nickname(self) -> str:
        return str(self.meta.get("nickname") or self.player.get("name") or self.username)


_SAFE = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


def _safe_name(name: str) -> str:
    """账号名转安全文件名（保留中文，其余替换为下划线）。"""
    cleaned = _SAFE.sub("_", str(name or "")).strip("_")
    return (cleaned or "player")[:40]


# ---------------------------------------------------------------- 宠物榜口径

# 可进化链上会出现过的形态文本。判断「这只宠能不能进化」只能认这几个，
# 不能用「包含『进化』」——成体不可进化宠的 form 正好是「不可进化」。
_EVOLVABLE_FORMS = ("幼年", "一进", "二进", "终极")


def pet_is_evolution_pet(pet: dict) -> bool:
    """这只宠是否属于可进化链。

    存档里每只宠都带 is_evolution_pet（捕获/礼包/进化时写入），优先用它。
    老存档万一缺字段，才退回看形态文本；「不可进化」必须先排除，否则
    `"进化" in form` 会把它们全判成可进化（2026-11-01 修的线上 bug：
    66 条可进化榜里 43 只其实不可进化，不可进化榜只剩 1 条）。
    """
    if "is_evolution_pet" in pet:
        return bool(pet.get("is_evolution_pet"))
    form = str(pet.get("form", "") or "")
    if form == "不可进化":
        return False
    return form in _EVOLVABLE_FORMS or form.startswith("进化")


def pet_total_growth(pet: dict) -> float:
    """总成长 = 攻/防/敏成长之和（客户端 pet.gd 的口径，不含生命成长）。

    旧实现读 pet["growth_rate"]，存档里根本没这个键，榜单上「总成长:」后面
    一直是空白。
    """
    try:
        value = float(pet.get("total_growth") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return round(value, 3)
    growth = pet.get("growth_rates")
    if isinstance(growth, dict):
        total = 0.0
        for key in ("attack", "defense", "agility"):
            try:
                total += float(growth.get(key) or 0)
            except (TypeError, ValueError):
                pass
        return round(total, 3)
    return 0.0


class AccountStore:
    """账号表 + 令牌表 + 每账号 State 缓存。

    - `state/accounts.json`：账号索引（用户名、昵称、密码哈希、盐、userId）
    - `state/tokens.json`  ：令牌 -> 用户名（多端可同时在线）
    - `state/accounts/<用户名>.json`：该玩家的整份存档
    """

    TOKEN_TTL = 30 * 24 * 3600
    # 同一账号最多保留几个令牌（多端在线够用即可）。
    # 登录一次就发一个新令牌、从来不回收，线上 4 天攒了 598 条（实际只有 13 个账号），
    # 其中绝大多数是被客户端自己覆盖掉的旧令牌，永远不会再被用到。
    TOKEN_MAX_PER_USER = 5
    # 会话空闲超时：客户端退后台/锁屏后心跳即停，超过这个时长再回来就判「登录已超时」，
    # 由客户端弹「账号已下线」并回登录页（主流手游做法）。0 = 关闭。
    # 可在 runtime-config.json 顶层 session.idleTimeoutSeconds 覆盖。
    TOKEN_IDLE_TIMEOUT = 300

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.dir = STATE_DIR / "accounts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = STATE_DIR / "accounts.json"
        self.tokens_path = STATE_DIR / "tokens.json"
        self.accounts: dict[str, dict] = load_json(self.index_path, {}) or {}
        self.tokens: dict[str, dict] = load_json(self.tokens_path, {}) or {}
        self._boot_ts = int(time.time())
        self._token_flush_ts = self._boot_ts
        self._cache: dict[str, State] = {}
        self.seed = load_json(DATA / "new_player_seed.json") or load_json(DATA / "save_seed.json")
        self._migrate_legacy()
        self._reset_token_idle_clock()

    # -- 兼容：把旧的单存档 state.json 收成 owner 账号
    def _migrate_legacy(self) -> None:
        legacy = STATE_DIR / "state.json"
        if not legacy.exists() or self.accounts:
            return
        name = "owner"
        try:
            raw = load_json(legacy)
            nickname = str((raw.get("save", {}).get("playerData", {}) or {}).get("name") or name)
        except Exception:
            raw, nickname = {}, name
        target = self.dir / (_safe_name(name) + ".json")
        try:
            target.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            return
        self.accounts[name] = {
            "userId": 10000,
            "nickname": nickname,
            "salt": secrets.token_hex(8),
            "hash": self.hash_password("owner123", secrets.token_hex(8)),
            "createdAt": iso(now()),
            "legacy": True,
        }
        # 旧密码固定为 owner123（盐和哈希重新生成一次，保持一致）
        salt = self.accounts[name]["salt"]
        self.accounts[name]["hash"] = self.hash_password("owner123", salt)
        self.flush_accounts()

    # -- 密码
    @staticmethod
    def hash_password(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()

    def exists(self, username: str) -> bool:
        return str(username) in self.accounts

    def create(self, username: str, password: str, nickname: str = "") -> tuple[bool, str, dict]:
        username = str(username or "").strip()
        if not (2 <= len(username) <= 20):
            return False, "用户名需 2~20 个字符", {}
        if not re.fullmatch(r"[0-9A-Za-z_\u4e00-\u9fff]+", username):
            return False, "用户名只能用中文、字母、数字、下划线", {}
        if len(str(password or "")) < 4:
            return False, "密码至少 4 位", {}
        with self.lock:
            if username in self.accounts:
                return False, "账号已存在", {}
            salt = secrets.token_hex(8)
            info = {
                "userId": 10000 + len(self.accounts) + 1,
                # 角色名可以为空：新流程是「先注册账号 → 登录后创建角色」，
            # 空名表示还没创建，客户端据此弹出创建角色面板。
            "nickname": str(nickname or "").strip()[:16],
                "salt": salt,
                "hash": self.hash_password(str(password), salt),
                "createdAt": iso(now()),
            }
            self.accounts[username] = info
            self.flush_accounts()
            return True, "", info

    def verify(self, username: str, password: str) -> tuple[bool, str]:
        info = self.accounts.get(str(username))
        if not info:
            return False, "账号不存在"
        if self.hash_password(str(password), info["salt"]) != info["hash"]:
            return False, "密码错误"
        return True, ""

    # -- 角色名（角色名全局唯一，注册后由「创建角色」接口写入）
    @staticmethod
    def _name_key(name) -> str:
        return str(name or "").strip().casefold()

    def nickname_taken(self, nickname: str, exclude_username: str = "") -> bool:
        """角色名是否已被别的账号占用。

        · 大小写不敏感（「Linlin」和「linlin」算同一个）
        · 先查账号索引（新建号都写在这里），再兜底扫一遍内存里的存档——
          历史账号的昵称可能只落在存档 playerData.name 里，索引里是空的，
          不兜底就会出现「重名能创建成功」。
        """
        key = self._name_key(nickname)
        if not key:
            return False
        exclude = self._name_key(exclude_username)
        for username, info in list(self.accounts.items()):
            if self._name_key(username) == exclude:
                continue
            if self._name_key((info or {}).get("nickname")) == key:
                return True
        for username, st in list(self._cache.items()):
            if self._name_key(username) == exclude:
                continue
            if self._name_key(st.meta.get("nickname")) == key:
                return True
            if self._name_key(st.player.get("name")) == key:
                return True
        return False

    def set_nickname(self, username: str, nickname: str) -> None:
        with self.lock:
            info = self.accounts.get(str(username))
            if info is None:
                return
            info["nickname"] = str(nickname or "").strip()[:16]
            self.flush_accounts()

    # -- 令牌
    def issue_token(self, username: str) -> str:
        token = secrets.token_hex(24)
        info = self.accounts[str(username)]
        with self.lock:
            now_ts = int(time.time())
            self.tokens[token] = {
                "username": str(username),
                "userId": info.get("userId", 0),
                "ts": now_ts,
                "lastSeen": now_ts,
            }
            self.accounts[str(username)]["lastLoginAt"] = iso(now())
            self.flush_tokens()
            self.flush_accounts()
        return token

    def _idle_timeout(self) -> int:
        """空闲超时秒数（runtime-config 顶层 session.idleTimeoutSeconds 可覆盖）。"""
        try:
            raw = RULES.m("session").get("idleTimeoutSeconds", self.TOKEN_IDLE_TIMEOUT)
            return max(0, int(raw))
        except Exception:
            return self.TOKEN_IDLE_TIMEOUT

    def _reset_token_idle_clock(self) -> None:
        """服务端重启后给所有令牌重新计时，避免「刚重启就把在线玩家全踢下线」。"""
        if not self.tokens:
            return
        changed = False
        for row in self.tokens.values():
            if isinstance(row, dict):
                row["lastSeen"] = self._boot_ts
                changed = True
        if changed:
            with self.lock:
                self.flush_tokens()

    def resolve(self, token: str) -> dict | None:
        row, _reason = self.resolve_ex(token)
        return row

    def resolve_ex(self, token: str) -> tuple[dict | None, str]:
        """返回 (令牌行, 原因)。原因取值：ok / missing / expired / idle。"""
        if not token:
            return None, "missing"
        row = self.tokens.get(token)
        if not row:
            return None, "missing"
        now_ts = int(time.time())
        if now_ts - int(row.get("ts", 0)) > self.TOKEN_TTL:
            return None, "expired"
        idle = self._idle_timeout()
        last = int(row.get("lastSeen", row.get("ts", 0)) or 0)
        if idle > 0 and last > 0 and now_ts - last > idle:
            return None, "idle"
        return row, "ok"

    def touch_token(self, token: str) -> None:
        """刷新令牌最后活跃时间（只改内存，最多 60 秒落一次盘）。"""
        row = self.tokens.get(token)
        if not isinstance(row, dict):
            return
        now_ts = int(time.time())
        row["lastSeen"] = now_ts
        if now_ts - int(self._token_flush_ts or 0) >= 60:
            self._token_flush_ts = now_ts
            with self.lock:
                self.flush_tokens()

    def revoke(self, token: str) -> None:
        with self.lock:
            self.tokens.pop(token, None)
            self.flush_tokens()

    def prune_tokens(self, max_per_user: int | None = None, ttl: int | None = None) -> int:
        """删掉再也用不上的令牌，返回删除条数。

        三类垃圾：
        1) 签发超过 TTL 的：resolve_ex 本来就判 expired，删掉只是让 401 的 error
           从「过期」变成「未登录」，客户端提示都是「请重新登录」，玩家无感；
        2) 账号已经不存在的：早期测试号（zz_flush_probe 之类）删了账号却把令牌留在表里，
           这些令牌还能通过鉴权、给不存在的账号现造一份空存档；
        3) 同一账号只留最近 max_per_user 个。

        不能拿 lastSeen 当年龄：服务每次重启都会把全部令牌的 lastSeen 刷成启动时刻
        （_reset_token_idle_clock，为了不把在线玩家全踢下线），只有 ts 是真实签发时间。
        """
        cap = max(1, int(max_per_user if max_per_user is not None else self.TOKEN_MAX_PER_USER))
        life = int(ttl if ttl is not None else self.TOKEN_TTL)
        now_ts = int(time.time())
        with self.lock:
            doomed: set[str] = set()
            alive: dict[str, list[tuple[int, str]]] = {}
            for token, row in list(self.tokens.items()):
                if not isinstance(row, dict):
                    doomed.add(token)          # 结构坏掉的脏数据
                    continue
                try:
                    ts = int(row.get("ts", 0) or 0)
                except (TypeError, ValueError):
                    ts = 0
                username = str(row.get("username", "") or "")
                if ts <= 0 or now_ts - ts > life or username not in self.accounts:
                    doomed.add(token)
                    continue
                alive.setdefault(username, []).append((ts, token))
            for rows in alive.values():
                if len(rows) <= cap:
                    continue
                rows.sort(reverse=True)        # 新的在前
                doomed.update(tok for _ts, tok in rows[cap:])
            for token in doomed:
                self.tokens.pop(token, None)
            if doomed:
                self.flush_tokens()
            return len(doomed)

    def flush_tokens(self) -> None:
        dump_json(self.tokens_path, self.tokens)

    def flush_accounts(self) -> None:
        dump_json(self.index_path, self.accounts)

    # -- 状态
    def get(self, username: str) -> State:
        username = str(username or "local")
        with self.lock:
            st = self._cache.get(username)
            if st is None:
                st = State(username, seed=self.seed)
                # 新号存档是从种子复制的，playerData.name 还是种子里的「新人」，
                # 战报/聊天里就会显示「新人」而不是角色名。建号时用账号昵称覆盖。
                if getattr(st, "fresh", False):
                    nick = str((self.accounts.get(username) or {}).get("nickname") or "").strip()
                    if nick and nick != str(st.player.get("name", "")):
                        st.player["name"] = nick
                        st.bump()
                        st.flush()
                self._cache[username] = st
            return st

    def all_states(self) -> list[State]:
        return list(self._cache.values())

    def online_count(self) -> int:
        return len(self.accounts)

    # -- 排行榜
    def ranking_excluded_usernames(self) -> set:
        """榜单排除名单（data/ranking-exclude.json）。

        开发期建的测试号/探针号不该占着榜单前排（emutest 投胎 66 一直霸着玩家榜第一）。
        只影响展示：账号、存档、其它玩法一律不动，名单随时可改，改完即时生效。
        """
        raw = load_json(DATA / "ranking-exclude.json", {}) or {}
        names = raw.get("usernames") if isinstance(raw, dict) else raw
        return {str(n).strip() for n in (names or []) if str(n).strip()}

    def rankings(self, kind: str = "all") -> list:
        """真实玩家榜单，全部由本服账号实时计算。

        旧实现不足 100 条时用原服采集快照（ranking*.json）补位，会把
        「人间大炮」等原服玩家展示给别人；2026-10-28 起彻底移除，榜单只含
        本服真实玩家。
        """
        live = self._live_ranking_rows(kind)
        merged: list = []
        seen: set = set()
        for row in live:
            key = self._ranking_key(row, kind)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(row)
        return merged[:100]

    @staticmethod
    def _ranking_key(row: dict, kind: str) -> str:
        if kind.startswith("pets"):
            return str(row.get("pet_id") or ("%s@%s" % (row.get("pet_name", ""), row.get("level", 0))))
        return str(row.get("username") or row.get("user_id") or "")

    def _live_ranking_rows(self, kind: str) -> list:
        players: list = []
        pets_evo: list = []
        pets_plain: list = []
        tower: list = []
        suit: list = []
        excluded = self.ranking_excluded_usernames()
        for username in list(self.accounts.keys()):
            if username in excluded:
                continue
            try:
                st = self.get(username)
            except Exception:
                continue
            p = st.player
            if not p:
                continue
            nickname = str(st.meta.get("nickname") or p.get("name") or username)
            user_id = int(self.accounts.get(username, {}).get("userId", 0) or 0)
            players.append({
                "user_id": user_id,
                "level": int(p.get("level", 1) or 1),
                "battle_wins": int((st.meta.get("arena") or {}).get("wins", 0) or 0),
                "pets_count": len(st.pets),
                "realm": str(p.get("realm", "") or ""),
                "cultivation": int(p.get("cultivation", 0) or 0),
                "reincarnation": int(p.get("reincarnation", 0) or 0),
                "nickname": nickname,
                "username": username,
            })
            tower_highest = int(p.get("tower_highest", 0) or 0)
            if tower_highest > 0:
                tower.append({"user_id": user_id, "nickname": nickname,
                              "tower_highest": tower_highest})
            suit_progress = int(p.get("suit_dungeon_progress", 0) or 0)
            if suit_progress > 0:
                suit.append({"user_id": user_id, "nickname": nickname,
                             "suit_dungeon_progress": suit_progress})
            for pet in st.pets:
                form = str(pet.get("form", "") or "")
                is_evo = pet_is_evolution_pet(pet)
                max_hp = int(pet.get("final_max_hp", pet.get("max_hp", 0)) or 0)
                row = {
                    "id": int(pet.get("rank_id", 0) or 0),
                    "user_id": user_id,
                    "pet_id": str(pet.get("id", "") or pet.get("pet_id", "") or ""),
                    "pet_name": str(pet.get("name", "") or ""),
                    "level": int(pet.get("level", 1) or 1),
                    "growth_rate": pet_total_growth(pet),
                    "form": form,
                    "generation": int(pet.get("generation", 0) or 0),
                    "element": str(pet.get("element", "") or ""),
                    "hp": max_hp,
                    "max_hp": max_hp,
                    "attack": int(pet.get("final_attack", pet.get("attack", 0)) or 0),
                    "defense": int(pet.get("final_defense", pet.get("defense", 0)) or 0),
                    "agility": int(pet.get("final_agility", pet.get("agility", 0)) or 0),
                    "updated_at": iso(now()),
                    "reincarnation_count": int(pet.get("reincarnation_count", 0) or 0),
                    "is_evolution_pet": 1 if is_evo else 0,
                    "mutation_count": int(pet.get("mutation_count", 0) or 0),
                    "nickname": nickname,
                }
                (pets_evo if is_evo else pets_plain).append(row)

        players.sort(key=lambda r: (r["reincarnation"], r["level"], r["cultivation"]), reverse=True)
        for group in (pets_evo, pets_plain):
            group.sort(key=lambda r: (r["level"], r["attack"]), reverse=True)
        tower.sort(key=lambda r: r["tower_highest"], reverse=True)
        suit.sort(key=lambda r: r["suit_dungeon_progress"], reverse=True)

        if kind == "pets_evolution":
            return pets_evo
        if kind == "pets_non_evolution":
            return pets_plain
        if kind == "tower":
            return tower
        if kind == "suit_dungeon":
            return suit
        return players


RULES = Rules()
STORE = AccountStore()
# 兼容旧引用：默认落到第一个账号（没有账号时用 local 占位）
STATE = STORE.get(next(iter(STORE.accounts), "local"))


# ---------------------------------------------------------------- 请求上下文

class Ctx:
    def __init__(self, state: State, body: dict, query: dict, user: dict | None, headers: dict) -> None:
        self.state = state
        self.body = body or {}
        self.q = query or {}
        self.user = user or {}
        self.headers = headers or {}

    # 常用取值
    def i(self, key: str, default: int = 0) -> int:
        try:
            return int(self.body.get(key, default))
        except (TypeError, ValueError):
            return default

    def s(self, key: str, default: str = "") -> str:
        value = self.body.get(key, default)
        return str(value) if value is not None else default

    def qi(self, key: str, default: int = 0) -> int:
        try:
            return int(self.q.get(key, default))
        except (TypeError, ValueError):
            return default


ROUTES: dict[tuple[str, str], object] = {}
# 带路径参数的路由，如 "/mail/detail/{mailId}"；精确路由匹配不到时再按顺序尝试
DYNAMIC_ROUTES: list[tuple[str, object, list[str], object]] = []


def route(method: str, path: str):
    def deco(fn):
        if "{" in path:
            names: list[str] = []
            parts: list[str] = []
            for seg in path.strip("/").split("/"):
                if seg.startswith("{") and seg.endswith("}"):
                    names.append(seg[1:-1])
                    parts.append(r"([^/]+)")
                else:
                    parts.append(re.escape(seg))
            pattern = re.compile("^/" + "/".join(parts) + "/?$")
            DYNAMIC_ROUTES.append((method.upper(), pattern, names, fn))
        else:
            ROUTES[(method.upper(), path)] = fn
        return fn

    return deco


def match_dynamic(method: str, path: str):
    """返回 (handler, path_params)；没有匹配则 (None, {})。"""
    for reg_method, pattern, names, fn in DYNAMIC_ROUTES:
        if reg_method not in (method, "ANY"):
            continue
        hit = pattern.match(path)
        if hit:
            return fn, dict(zip(names, hit.groups()))
    return None, {}


def fail(message: str, **extra) -> dict:
    payload = {"success": False, "message": message, "error": message}
    payload.update(extra)
    return payload


def ok(**extra) -> dict:
    payload = {"success": True}
    payload.update(extra)
    return payload
