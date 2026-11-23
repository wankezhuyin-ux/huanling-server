#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三份服务端配置与原版口径对齐的守卫。

2026-11-06 对照原版查出：
  · drop-config.json   线上是「钱币袋 13%」早期版本，原版 rules.drop_rates
                       是 13 个 1% 特殊道具 + 5 种草，没有钱币袋 → 本地对。
  · maps.json          原版实测 21 张地图 avgGold/avgPlayerExp 全 0，本地用
                       rewardEnabled=False 复现；线上缺这层配置，在发原版
                       不该发的奖励。另 1 张 spawnDist 被换成权重表。
  · new_player_seed    原版新号 base=10/10/10 + points=20（实机属性界面截图
                       + 客户端 game_manager.gd 新号默认），线上对；本地曾
                       被高等级存档污染（宠物 final_attack=3.3 万 / base=5）。

这份测试把结论钉死：以后谁把本地配置改回错误值，全量回归直接失败。

运行：python3 tools/test_original_parity.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
OUTPUTS = Path(__file__).resolve().parents[3]
GD = OUTPUTS / "recovered-godot-project"
MANIFEST = SERVER.parent / "update_server_all.sh"

FAILS: list[str] = []
TOTAL = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    TOTAL[0] += 1
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


print("=== A. drop-config.json（原版 rules.drop_rates）===")
ORIG_DROP = {
    "醉剑": 0.01, "神的遗愿": 0.01, "风歌夜曲": 0.01, "摆烂松": 0.01,
    "人间大炮": 0.01, "我的理想": 0.01, "板砖": 0.01, "立羽": 0.01,
    "铭大大": 0.01, "长空秋雁": 0.01, "大橘大利": 0.01, "人间事": 0.01,
    "思念": 0.01, "灵草": 0.0005, "灵芝": 0.0005, "天山雪莲": 0.0002,
    "九转还魂草": 0.0001, "龙涎草": 0.0001,
}
drop = load(SERVER / "data" / "drop-config.json").get("dropRates") or {}
check("A1 掉率表与原版 18 项逐项一致", drop == ORIG_DROP,
      "缺 %s / 多 %s" % (sorted(set(ORIG_DROP) - set(drop)), sorted(set(drop) - set(ORIG_DROP))))
check("A2 原版没有「钱币袋」掉落", "钱币袋" not in drop, str(list(drop)[:5]))
snap = GD / "dev" / "local_snapshot.json"
if snap.exists():
    check("A3 与原版导出快照 rules.drop_rates 完全一致",
          drop == (load(snap).get("rules") or {}).get("drop_rates"))
else:
    print("  SKIP A3 原版快照不在本机（已用硬编码基准守住）")

print("\n=== B. maps.json（原版实测刷怪 / 零收益地图）===")
maps = load(SERVER / "data" / "maps.json").get("maps") or {}
zero = [k for k, v in maps.items() if v.get("rewardEnabled") is False]
check("B1 21 张零收益地图标记齐全", len(zero) == 21, str(len(zero)))
unknown = [k for k in zero if maps[k].get("rewardCode") == "UNKNOWN_MAP"]
check("B2 其中 13 张是原版服务端不认识的 mapId", len(unknown) == 13, str(len(unknown)))
check("B3 青幽洞（43,40）spawnDist 是原版实测采样，不是权重表",
      maps.get("青幽洞（43,40）", {}).get("spawnDist") == {"1": 2, "3": 1},
      str(maps.get("青幽洞（43,40）", {}).get("spawnDist")))
orig_maps = GD / "dev" / "local_maps.json"
if orig_maps.exists():
    om = load(orig_maps).get("maps") or {}

    def coord(k: str) -> str:
        m = re.search(r"（(\d+),(\d+)）", k)
        return m.group(0) if m else k

    O = {(str(v.get("clientId")), coord(k)): v for k, v in om.items()}
    check("B4 每张零收益地图在原版实测里 avgGold/avgPlayerExp 都是 0",
          all((O.get((str(maps[k].get("clientId")), coord(k))) or {}).get("avgGold") == 0.0
              and (O.get((str(maps[k].get("clientId")), coord(k))) or {}).get("avgPlayerExp") == 0.0
              for k in zero))
    okv = O.get((str(maps.get("青幽洞（43,40）", {}).get("clientId")), "（43,40）")) or {}
    check("B5 青幽洞原版 spawnDist 与本地一致",
          okv.get("spawnDist") == {"1": 2, "3": 1}, str(okv.get("spawnDist")))
    # 全量：除 1 张外所有地图 spawnDist 应保持原版实测值
    mismatches = []
    for k, v in maps.items():
        ov = O.get((str(v.get("clientId")), coord(k))) or {}
        if ov.get("spawnDist") is not None and ov.get("spawnDist") != v.get("spawnDist"):
            mismatches.append(k)
    check("B6 全部 279 张地图 spawnDist 不偏离原版", not mismatches,
          ", ".join(mismatches[:5]))
else:
    print("  SKIP B4-B6 原版地图导出不在本机（已用硬编码基准守住）")

print("\n=== C. new_player_seed.json（原版新号）===")
seed = load(SERVER / "data" / "new_player_seed.json")
p = seed.get("playerData") or {}
check("C1 攻击/防御/敏捷 base 都是 10",
      all(p.get(k) == 10 for k in ("attack_base", "defense_base", "agility_base")),
      str({k: p.get(k) for k in ("attack_base", "defense_base", "agility_base")}))
check("C2 新号属性点 20", p.get("points") == 20, str(p.get("points")))
check("C3 final_defense/final_agility 与 base 一致（没被污染）",
      p.get("final_defense") == 10 and p.get("final_agility") == 10,
      str({k: p.get(k) for k in ("final_defense", "final_agility")}))
pets = (seed.get("petsData") or {}).get("pets") or []
if pets:
    pet = pets[0]
    check("C4 初始宠是 1 级正常数值（final_attack 未被高等级存档污染）",
          float(pet.get("final_attack") or 0) < 100.0, str(pet.get("final_attack")))
gm = GD / "scripts" / "game_manager.gd"
if gm.exists():
    check("C5 客户端新号默认属性点仍是 20", '"points": 20' in gm.read_text(encoding="utf-8"))

print("\n=== D. 部署策略 ===")
txt = MANIFEST.read_text(encoding="utf-8")
block = re.search(r"^FILES=\((.*?)^\)", txt, re.S | re.M)
files = {l.split("#")[0].strip() for l in block.group(1).splitlines()
         if l.split("#")[0].strip().endswith((".py", ".json"))} if block else set()
check("D1 drop-config / maps 纳入 FILES（会自动同步 + md5 复核）",
      {"data/drop-config.json", "data/maps.json"} <= files)
check("D2 new_player_seed 不进 FILES（线上为准，防止误推污染版）",
      "data/new_player_seed.json" not in files)
check("D3 预检按 FILES 动态遍历", 'for f in "${FILES[@]}"' in txt)

print("\n通过 %d，失败 %d" % (TOTAL[0] - len(FAILS), len(FAILS)))
sys.exit(1 if FAILS else 0)
