#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法宝「地图难度」与「碎片掉落」回归（2026-11-04 对照原版体检后补的链路）。

体检发现的三处断链：
  1. 服务端 `_MAP_TIER_ALIASES` 把 hell 当成第 3 档（+15%），客户端 hell 是第 2 档
     （+10%）；epic/legend/supreme/chaos/wushang 根本没收录 → 切传说/无上经验加成归零。
  2. `/battle/wild-run` 不记难度、返回写死 `mapDifficulty="normal"`，结算取不到档位，
     客户端预览显示「经验 +X%」但到账没有 → 玩家觉得被骗。
  3. `fabao.dropDenom` / `fabao.minMapLevel` 两个运行时配置一直没人消费，
     普通~无上法宝碎片根本没有掉落来源（线上 22 个号 0 件法宝）。

守这些事：
  A. 八档难度 → 经验加成 0/5/10/15/20/25/30/35%，与客户端 `FabaoConfig.map_exp_bonus_pct` 一致
  B. 开战票据记住难度、回包不再写死 normal
  C. 结算真的按难度加经验（无上 = 普通 × 1.35）
  D. 碎片只在 1000 级以上地图掉，且掉的是「当前难度」那一档
  E. 掉率按 dropDenom（1/2000）判定，命中才进背包
  F. 离线采样同样吃难度加成（与在线同口径）

    python3 tools/test_fabao_difficulty_drop.py
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_fabao_diff_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_battle as hb  # noqa: E402
import handlers_growth as hg  # noqa: E402

PASS = 0
FAIL = 0

HIGH_MAP = "盈露台守望区（39,28）"   # level 1500
LOW_MAP = "浮光水城外郊（0,0）"      # level 4


def check(name: str, cond, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("[OK]   " + name)
    else:
        FAIL += 1
        print("[FAIL] " + name + ("  → " + detail if detail else ""))


def account(username: str, difficulty: str = "normal") -> core.State:
    save = {
        "playerData": {
            "level": 1200, "experience": 0, "gold": 0,
            "current_map": HIGH_MAP,
            "reincarnation": 1200, "noble_level": 0,
            "attack_base": 100, "defense_base": 100, "agility_base": 100,
            "max_hp": 1000, "hp": 1000,
            # 战力拉满，保证每场都赢（这里测的是结算倍率，不是战斗平衡）
            "final_attack": 9_000_000, "final_defense": 9_000_000,
            "final_agility": 9_000_000, "final_max_hp": 9_000_000,
            "element": "水", "use_fabao_player_element": True,
        },
        "inventoryData": {"items": [], "pet_equipment_items": [],
                          "warehouse_items": [], "xianmo_chests": []},
        "gameSettings": {"current_map": HIGH_MAP,
                         "current_map_difficulty": difficulty},
        "fabao": {"current_difficulty": difficulty, "equipped": None,
                  "inventory": [], "warehouse": []},
        "martialArtsData": {"martial_arts": [], "equipped_ids": []},
        "petsData": {"pets": [], "active_pet_id": ""},
    }
    path = core.STATE_DIR / "accounts" / (core._safe_name(username) + ".json")
    path.write_text(json.dumps({"save": save, "meta": {}}, ensure_ascii=False), encoding="utf-8")
    return core.State(username)


def ctx(state, body=None):
    return core.Ctx(state, body or {}, {}, None, {})


def main() -> int:
    print("=== 法宝：地图难度加成 + 碎片掉落 ===")

    # ---------------------------------------------------------------- A 档位表
    table = {
        "normal": 0, "hard": 5, "hell": 10, "epic": 15,
        "legend": 20, "supreme": 25, "chaos": 30, "wushang": 35,
    }
    for tier, want in table.items():
        got = hb._map_exp_bonus_pct(None, None, tier)
        check("A 难度 %-8s → +%d%%" % (tier, want), got == want, str(got))
    # 客户端 FabaoConfig.map_exp_bonus_pct 同口径（idx × 5）
    client = ["normal", "hard", "hell", "epic", "legend", "supreme", "chaos", "wushang"]
    check("A 档位顺序与客户端 TIER_IDS 一致",
          all(hb._map_exp_bonus_pct(None, None, t) == i * 5 for i, t in enumerate(client)),
          str([hb._map_exp_bonus_pct(None, None, t) for t in client]))

    # ---------------------------------------------------------------- B 开战票据
    st = account("fabaodiff1", "legend")
    random.seed(11)
    run = hb.wild_run(ctx(st, {"mapId": HIGH_MAP}))
    pending = st.meta.get("battles", {}).get(str(run.get("fightToken", ""))) or {}
    check("B1 回包 mapDifficulty 是真实档位", run.get("mapDifficulty") == "legend",
          str(run.get("mapDifficulty")))
    check("B2 票据记住难度", pending.get("difficulty") == "legend", str(pending.get("difficulty")))

    # ---------------------------------------------------------------- C 结算倍率
    def settle_exp(tier: str, seed: int = 21):
        s = account("fabaoexp_" + tier, tier)
        random.seed(seed)
        r = hb.wild_run(ctx(s, {"mapId": HIGH_MAP}))
        random.seed(seed + 1)
        out = hb.battle_loot(ctx(s, {"fightToken": r["fightToken"]}))
        return out

    base = settle_exp("normal")
    check("C0 普通难度有经验到账", int(base.get("playerExpGranted", 0)) > 0,
          str(base.get("playerExpGranted")))
    for tier, mult in (("hard", 1.05), ("hell", 1.10), ("wushang", 1.35)):
        out = settle_exp(tier)
        got = int(out.get("playerExpGranted", 0))
        want = int(round(int(base["playerExpGranted"]) * mult))
        # 逐场经验有取整，允许 ±2 的误差
        check("C 难度 %-8s 经验 ×%.2f（%d vs %d）" % (tier, mult, got, want),
              abs(got - want) <= 2, "%d vs %d" % (got, want))

    # ---------------------------------------------------------------- D/E 碎片
    st2 = account("fabaodrop1", "hard")
    with mock.patch("handlers_battle.random.random", return_value=0.0):
        frag = hb.roll_fabao_fragment(st2, {"level": 1500})
    check("D1 1500 级图 + 困难档 → 困难法宝碎片",
          frag == {"name": "困难法宝碎片", "count": 1}, str(frag))
    check("D2 碎片进背包", st2.item_count("困难法宝碎片") == 1,
          str(st2.item_count("困难法宝碎片")))

    st3 = account("fabaodrop2", "wushang")
    with mock.patch("handlers_battle.random.random", return_value=0.0):
        frag2 = hb.roll_fabao_fragment(st3, {"level": 1000})
    check("D3 1000 级（门槛值）也掉", frag2 == {"name": "无上法宝碎片", "count": 1}, str(frag2))

    st4 = account("fabaodrop3", "hard")
    with mock.patch("handlers_battle.random.random", return_value=0.0):
        frag3 = hb.roll_fabao_fragment(st4, {"level": 999})
    check("D4 999 级图不掉", frag3 is None, str(frag3))
    check("D5 没掉就没进背包", st4.item_count("困难法宝碎片") == 0,
          str(st4.item_count("困难法宝碎片")))

    st5 = account("fabaodrop4", "normal")
    with mock.patch("handlers_battle.random.random", return_value=1.0 / 2000 + 1e-9):
        frag4 = hb.roll_fabao_fragment(st5, {"level": 1500})
    check("E1 掉率边界外（>1/2000）不掉", frag4 is None, str(frag4))
    with mock.patch("handlers_battle.random.random", return_value=1.0 / 2000 - 1e-9):
        frag5 = hb.roll_fabao_fragment(st5, {"level": 1500})
    check("E2 掉率边界内（<1/2000）掉", frag5 is not None, str(frag5))

    # 统计口径：2000 场期望 1 片
    st6 = account("fabaodrop5", "hell")
    random.seed(2026)
    hits = sum(1 for _ in range(20000) if hb.roll_fabao_fragment(st6, {"level": 1500}))
    check("E3 2 万场约 10 片（1/2000）", 3 <= hits <= 20, str(hits))

    # 结算回包里要单独带 fabaoItemGranted —— 客户端只认这条路径打日志
    st7 = account("fabaodrop6", "epic")
    random.seed(5)
    run7 = hb.wild_run(ctx(st7, {"mapId": HIGH_MAP}))
    with mock.patch("handlers_battle.random.random", return_value=0.0):
        out7 = hb.battle_loot(ctx(st7, {"fightToken": run7["fightToken"]}))
    check("E4 结算回包带 fabaoItemGranted",
          str(out7.get("fabaoItemGranted", "")) == "史诗法宝碎片",
          str(out7.get("fabaoItemGranted")))
    check("E5 碎片不混进 wildDrops",
          all("法宝碎片" not in str(d.get("name", "")) for d in (out7.get("wildDrops") or [])),
          str(out7.get("wildDrops"))[:120])

    # ---------------------------------------------------------------- F 离线
    def offline_sample(tier: str):
        s = account("fabaooff_" + tier, tier)
        random.seed(33)
        return hg._offline_field_sample(s, HIGH_MAP, 6)

    o_norm = offline_sample("normal")
    o_wu = offline_sample("wushang")
    ratio = float(o_wu["player_exp"]) / max(1e-9, float(o_norm["player_exp"]))
    check("F1 离线采样吃难度加成（无上/普通 ≈ 1.35）", abs(ratio - 1.35) < 0.02,
          "%.4f" % ratio)

    print("")
    print("通过 %d / 失败 %d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
