#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""地图自造名同源改写（B5）的回归测试。

2026-11-10：把 134 个地图基名里的 74 个原版自造名改成意思相近的新名，
真实地名 / 民俗 / 通用描述 60 个保持不动。这里守住四件事：

  A. 改名表本身：map-rename.json 74 条、格式是「旧名 -> {name: 新名}」、
     maps.json 279 个 key 已全部换成现用名，且 134 个基名仍唯一；
  B. 两端一致：客户端 game_data.gd 的 MAP_NAME_MIGRATION 用 md5 前 8 位做键，
     与服务端表逐条语义一致；
  C. 迁移链路：老存档的 current_map / previous_map、questData 的 kill_map_*
     击杀计数键、宠物栏与宠物袋的 capture_map 都会在读档时换名，坐标后缀保留，
     非地名标识符不动，计数可合并，重复读档幂等；
  D. 前缀联动与边界：带编号 / 分区 / 桥头 / 洞口后缀的地名整组改到位，
     60 个不该改的名字仍原样保留。

    python3 tools/test_map_rename_migration.py
"""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT.parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="ly_map_rename_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

GAME_DATA = OUT / "recovered-godot-project" / "scripts" / "game_data.gd"
MAPS_JSON = ROOT / "data" / "maps.json"

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def md5_8(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def base_names(raw: dict) -> set:
    return {re.sub(r"（[^）]*）$", "", k) for k in raw}


# --------------------------------------------------------------------------
print("== A. 迁移表与地图数据 ==")
table = core.map_rename_map()
flat = {k: v["name"] for k, v in table.items() if isinstance(v, dict) and v.get("name")}
check("A1 map-rename.json 已加载且非空", bool(table), "条数 %d" % len(table))
check("A2 条数 == 74", len(table) == 74, str(len(table)))
check("A3 每条值都是含 name 的 dict", len(flat) == len(table), "%d/%d" % (len(flat), len(table)))
check("A4 新旧名不相等", all(k != v for k, v in flat.items()))
check("A5 74 个新名互不重复", len(set(flat.values())) == 74)

raw_maps = json.load(open(MAPS_JSON, encoding="utf-8"))["maps"]
bases = base_names(raw_maps)
check("A6 maps.json 仍是 279 个 key", len(raw_maps) == 279, str(len(raw_maps)))
check("A7 134 个基名仍唯一", len(bases) == 134, str(len(bases)))
check("A8 旧地名在 maps.json 里已清零", not (bases & set(flat)), str(sorted(bases & set(flat))[:4]))
check("A9 74 个新地名全部落地", len(bases & set(flat.values())) == 74,
      str(len(bases & set(flat.values()))))
check("A10 新名与 60 个不动名不撞名", len(bases) == 134)

# --------------------------------------------------------------------------
print("\n== B. 两端迁移表一致 ==")
gd = io.open(GAME_DATA, encoding="utf-8").read()
m = re.search(r"const MAP_NAME_MIGRATION: = \{(.*?)\n\}", gd, re.S)
check("B1 客户端表存在", m is not None)
pairs = re.findall(r'"([0-9a-f]{8})"\s*:\s*"([^"]+)"', m.group(1)) if m else []
cli = {k: v for k, v in pairs}
check("B2 客户端 74 条且无重复键", len(cli) == 74 and len(pairs) == 74,
      "条数 %d / 行数 %d" % (len(cli), len(pairs)))
check("B3 键全部是 8 位小写 md5", all(re.fullmatch(r"[0-9a-f]{8}", k) for k in cli))
check("B4 两端逐条语义一致",
      len(cli) == len(flat) and all(cli.get(md5_8(k)) == v for k, v in flat.items()))

# --------------------------------------------------------------------------
print("\n== C. 老存档迁移 ==")
st = core.State("map_rename_case")
st.save["gameSettings"] = {
    "current_map": "浮影水城外郊（0,0）",
    "previous_map": "徘徊林（39,54）",
}
st.save["questData"] = {"quests": {
    "quest_001": {"progress": {"kill_map_浮影水城外郊（0,0）": 3}},
    "quest_004": {"progress": {
        "kill_map_徘徊林（0,0）": 5,
        "kill_map_盘桓林（0,0）": 2,
    }},
    "quest_009": {"progress": {"kill_map_翔云幻城（12,8）": 1}},
}}
st.save["petsData"] = {
    "pets": [
        {"id": "p1", "capture_map": "古铜洞窟2（30,47）"},
        {"id": "p2", "capture_map": "mayday_event"},
        {"id": "p3", "capture_map": ""},
    ],
    "pet_bag": [{"id": "p4", "capture_map": "清幽古洞二区（5,9）"}],
}
st.save["playerData"] = {"recent_maps": [
    "翔云幻城（12,8）", "徘徊林（0,0）", "华山腰道（18,49）",
]}
st.meta["arena"] = {"mirror": {"player": {"recent_maps": ["清幽古洞（9,9）"]}}}
st.meta["battles"] = {"b1": {"mapId": "观日桥头（37,51）"}}
st.meta["note"] = "在徘徊林附近打怪时遇到旧地名"
st.flush(force=True)

again = core.State("map_rename_case")
gs = again.save.get("gameSettings", {})
check("C1 current_map 换名且坐标保留", gs.get("current_map") == "浮光水城外郊（0,0）",
      str(gs.get("current_map")))
check("C2 previous_map 换名且坐标保留", gs.get("previous_map") == "盘桓林（39,54）",
      str(gs.get("previous_map")))
prog = again.save["questData"]["quests"]
p1 = prog["quest_001"]["progress"]
p4 = prog["quest_004"]["progress"]
p9 = prog["quest_009"]["progress"]
check("C3 kill_map_* 键换名", "kill_map_浮光水城外郊（0,0）" in p1 and p1.get("kill_map_浮光水城外郊（0,0）") == 3,
      str(p1))
check("C4 旧计数键已移除", not any(k.startswith("kill_map_浮影水城") for k in p1))
check("C5 新旧键并存时计数合并 5+2=7", p4.get("kill_map_盘桓林（0,0）") == 7, str(p4))
check("C6 深层任务节点也覆盖", "kill_map_凌云幻城（12,8）" in p9, str(p9))
pets = again.save["petsData"]["pets"]
bag = again.save["petsData"]["pet_bag"]
check("C7 宠物栏 capture_map 换名", pets[0].get("capture_map") == "玄铜洞窟2（30,47）",
      str(pets[0].get("capture_map")))
check("C8 非地名标识符原样保留", pets[1].get("capture_map") == "mayday_event",
      str(pets[1].get("capture_map")))
check("C9 空值不动", pets[2].get("capture_map") == "")
check("C10 宠物袋 capture_map 换名", bag[0].get("capture_map") == "清邃古洞二区（5,9）",
      str(bag[0].get("capture_map")))

third = core.State("map_rename_case")
check("C11 重复读档幂等", third.save["gameSettings"]["current_map"] == "浮光水城外郊（0,0）"
      and third.save["questData"]["quests"]["quest_004"]["progress"].get("kill_map_盘桓林（0,0）") == 7
      and third.save["petsData"]["pets"][0]["capture_map"] == "玄铜洞窟2（30,47）"
      and third.save["playerData"]["recent_maps"][0] == "凌云幻城（12,8）")

untouched = core.State("map_rename_other")
untouched.save["gameSettings"] = {"current_map": "华山腰道（18,49）"}
untouched.flush(force=True)
check("C12 不在表里的地名原样保留",
      core.State("map_rename_other").save["gameSettings"]["current_map"] == "华山腰道（18,49）")

recent = again.save.get("playerData", {}).get("recent_maps", [])
check("C13 playerData.recent_maps 换名且不动名保留",
      recent[:2] == ["凌云幻城（12,8）", "盘桓林（0,0）"] and recent[2] == "华山腰道（18,49）",
      str(recent))
mirror = again.meta.get("arena", {}).get("mirror", {}).get("player", {}).get("recent_maps", [])
check("C14 meta.arena.mirror 的 recent_maps 也迁移",
      mirror == ["清邃古洞（9,9）"], str(mirror))
check("C15 meta.battles.*.mapId 也迁移",
      again.meta["battles"]["b1"]["mapId"] == "望日桥头（37,51）",
      str(again.meta["battles"]["b1"]["mapId"]))
check("C16 普通文案里顺带提到地名不会被误改",
      again.meta.get("note") == "在徘徊林附近打怪时遇到旧地名",
      str(again.meta.get("note")))

# --------------------------------------------------------------------------
print("\n== D. 前缀联动与边界 ==")
def strip_bg_alias(txt: str) -> str:
    # 背景图别名表必须写旧图片文件名，这是唯一合法的旧名出现位置。
    return re.sub(r"const BATTLE_BG_ALIASES: Dictionary = \{.*?\n\}", "", txt, flags=re.S)

linked = ["玄铜洞窟2", "蟠龙洞窟2", "紫藓洞窟2", "望日桥头", "清邃古洞二区",
          "清邃古洞三区", "清邃古洞四区", "幽怨洞口", "惊魂洞窟1", "惊魂洞窟2",
          "浮光水城外郊", "深碧海底", "逆之往昔", "逆之心绪", "逆之幻影"]
check("D1 带后缀的联动名全部落地", all(n in bases for n in linked),
      str([n for n in linked if n not in bases]))

keep = ["华山亭", "华山瀑布", "华山脚", "华山腰道", "华山顶", "宣武门", "黄泥岗",
        "冰封地狱", "油锅地狱", "铡头地狱", "小房间", "小小房间", "过道", "断壁",
        "九曲回廊", "东码头", "西码头", "松竹林", "彩虹桥", "羊肠道"]
check("D2 60 个不动名中的代表项仍原样保留", all(n in bases for n in keep),
      str([n for n in keep if n not in bases]))

runtime = [
    "deploy/server/data/maps.json", "deploy/server/data/monsters.json",
    "deploy/server/data/new_player_seed.json", "deploy/server/data/save_seed.json",
    "local-server/data/maps.json", "local-server/data/monsters.json",
    "local-server/data/new_player_seed.json", "local-server/data/save_seed.json",
    "recovered-godot-project/scripts/data/monster_database.gd",
    "recovered-godot-project/scripts/data/quest_database.gd",
    "recovered-godot-project/scripts/game_data.gd",
    "recovered-godot-project/scripts/game_manager.gd",
    "recovered-godot-project/scripts/maps_data_complete.gd",
    "recovered-godot-project/scripts/ui/battle_scroll_world.gd",
    "recovered-godot-project/scripts/gang/gang_config.gd",
    "recovered-godot-project/ui/main_ui.gd",
]
leftover = []
for rel in runtime:
    p = OUT / rel
    if not p.exists():
        continue
    txt = strip_bg_alias(io.open(p, encoding="utf-8").read())
    leftover += [(rel, k) for k in flat if k in txt]
check("D3 16 个运行时文件里旧地名残留为 0（别名表除外）", not leftover, str(leftover[:5]))

gang = io.open(OUT / "recovered-godot-project/scripts/gang/gang_config.gd", encoding="utf-8").read()
check("D4 帮派文案里的独立主城写法同步", "高级领地（浮光水城、榕树之城、凝露山城）" in gang)

# 迁移表本身与迁移回归测试必须保留旧名，其余运行时路径（含测试脚本 / fixture /
# 本地调试服的 DEFAULT_MAP）不许再有旧地图名，否则地图会加载失败或断言查不到 key。
EXEMPT = {
    "deploy/server/data/map-rename.json",
    "local-server/data/map-rename.json",
    os.path.relpath(Path(__file__).resolve(), OUT),
}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".webp", ".apk", ".pck", ".zip", ".gz",
            ".ogg", ".mp3", ".wav", ".ttf", ".otf", ".ctex", ".import",
            ".stex", ".bin", ".so", ".dylib", ".jar", ".keystore", ".pyc"}
guarded = []
for pattern in ("deploy/server/**/*", "local-server/**/*",
                "recovered-godot-project/scripts/**/*", "recovered-godot-project/ui/**/*"):
    for p in glob.glob(str(OUT / pattern), recursive=True):
        rel = os.path.relpath(p, OUT)
        if rel in EXEMPT or not os.path.isfile(p):
            continue
        if os.path.splitext(rel)[1].lower() in SKIP_EXT:
            continue
        if "/state/" in "/" + rel or "__pycache__" in rel or ".bak" in rel or rel.endswith(".log"):
            continue
        try:
            txt = io.open(p, encoding="utf-8").read()
        except Exception:
            continue
        guarded += [(rel, k) for k in flat if k in strip_bg_alias(txt)]
check("D5 运行时路径（除迁移表 / 迁移测试 / 背景图别名表外）旧地名残留为 0", not guarded,
      str(guarded[:5]))

# 战斗背景是按「地图名.jpg」拼路径的。地名改了但图片文件仍叫旧名，
# 没有别名表的话 65 张背景图会全部回落到默认洞穴图。
ui = io.open(OUT / "recovered-godot-project/ui/main_ui.gd", encoding="utf-8").read()
mu = re.search(r"const BATTLE_BG_ALIASES: Dictionary = \{(.*?)\n\}", ui, re.S)
aliases = dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', mu.group(1))) if mu else {}
bg_files = set(os.listdir(OUT / "recovered-godot-project/assets/backgrounds"))
def img_exists(n: str) -> bool:
    return any((n + ext) in bg_files for ext in (".jpg", ".png", ".webp"))
need_bg = {v: k for k, v in flat.items() if img_exists(k) and not img_exists(v)}
check("D6 65 个有背景图的改名项都在别名表里", len(need_bg) == 65 and all(n in aliases for n in need_bg),
      "缺 %s" % [n for n in need_bg if n not in aliases][:5])
check("D7 别名表每条 value 都指向真实存在的图片", all(img_exists(v) for v in aliases.values()),
      str([v for v in aliases.values() if not img_exists(v)][:5]))
check("D8 别名指向的是旧图片名而非新名",
      all(aliases.get(n) == o for n, o in need_bg.items()),
      str([(n, aliases.get(n), o) for n, o in need_bg.items() if aliases.get(n) != o][:3]))
check("D9 旧地名只出现在别名块内，main_ui.gd 其余部分无残留",
      not [k for k in flat if k in strip_bg_alias(ui)])

print("\n结果: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
