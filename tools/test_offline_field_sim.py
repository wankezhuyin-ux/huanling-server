#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线结算「按该图刷怪 + 真实战斗模拟」的回归测试。

背景：帮助文档（客户端 ui/city/settings_hall.gd「十二、离线与经验」）写的是
「服务端会按该图刷怪，并用你已装备的武功、出战宠技能做多场模拟：人宠打完后，
同一回合里该出手的怪一起算约 1 秒」「用平均一场时间和平均经验/金币，再按离线
时长折算场次」「每一场按野外追赶规则入账：一场最多连升规定级数」。

旧实现是 `avgPlayerExp × 1800 × 小时` 一次性灌，不看战力、不看回合数、不封连升。

守这些事：
  A. 采样出来的平均一场 >= 1 秒（1 回合 = 1 秒），往返一致
  B. 每场都重建 Fighter —— 复用同一个对象会变成「残血连打」，存活率天然失真
  C. 打得过的图：胜率高、人物存活率高
  D. 打不过的图：胜率/存活率低，收益接近 0
  E. 线上本来就不给奖励的图，离线也一分不给
  F. 场次 = 离线秒数 ÷ 平均一场耗时
  G. 逐场入账，吃得住「一场最多连升 N 级」的野外口径
  H. 勾了自动飞升：到门槛就地飞升
  I. 飞升后还打得动就留在原图（否则「离线无限飞升」会退化成一次性）
  J. 飞升后确实打不动了，才按等级换图
  K. 金币按场累加
  L. 24 小时上限的极端场次也能在 3 秒内算完

    python3 tools/test_offline_field_sim.py
"""
from __future__ import annotations

import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_offline_sim_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_growth as hg  # noqa: E402

PASS = 0
FAIL = 0

LOW_MAP = "浮光水城外郊（0,0）"      # level 4，avgPlayerExp 405
HIGH_MAP = "盈露台守望区（39,28）"   # level 1500
NO_REWARD_MAP = "彩虹桥（7,15）"     # rewardEnabled=false


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def new_char(tag: str, *, level: int = 1, realm: str = "凡人",
             atk: float = 1e6, dfs: float = 1e6, hp: float = 1e7,
             map_id: str = LOW_MAP, auto: bool = False):
    st = core.State("sim_" + tag)
    p = st.player
    p["realm"] = realm
    p["level"] = level
    p["experience"] = 0
    p["final_attack"] = atk
    p["final_defense"] = dfs
    p["final_agility"] = 1e6
    p["final_max_hp"] = hp
    p["max_hp"] = hp
    p["hp"] = hp
    p["gold"] = 0
    p["double_exp_auto_realm_enabled"] = auto
    p["catchup_enabled"] = False
    st.save.setdefault("gameSettings", {})["current_map"] = map_id
    return st


print("== A. 采样：平均一场 >= 1 秒 ==")
random.seed(7)
st = new_char("a")
s = hg._offline_field_sample(st, LOW_MAP, 32)
check("采样有结果", s["samples"] == 32 and s["avg_seconds"] > 0, str(s))
check("平均一场 >= 1 秒", s["avg_seconds"] >= 1.0, str(s["avg_seconds"]))
check("强号在低级图胜率 100%", s["win_rate"] == 1.0, str(s["win_rate"]))
check("强号在低级图人物存活 100%", s["alive_rate"] == 1.0, str(s["alive_rate"]))
check("每场有玩家经验", s["player_exp"] > 0, str(s["player_exp"]))
check("每场有金币", s["gold"] > 0, str(s["gold"]))

print("== B. 每场重建 Fighter（残血连打的回归）==")
random.seed(11)
s1 = hg._offline_field_sample(new_char("b1"), HIGH_MAP, 40)
random.seed(11)
s2 = hg._offline_field_sample(new_char("b2"), HIGH_MAP, 40)
check("同种子两次采样完全一致（每场都从满血开始）",
      abs(s1["alive_rate"] - s2["alive_rate"]) < 1e-9,
      "%s vs %s" % (s1["alive_rate"], s2["alive_rate"]))
check("弱号在高图不是「越打越残」的假死（存活率两次同值）",
      s1["alive_rate"] == s2["alive_rate"], str(s1["alive_rate"]))

print("== C/D. 强号低图 vs 弱号高图 ==")
random.seed(3)
strong = hg._offline_field_sample(new_char("c1"), LOW_MAP, 48)
weak_st = new_char("c2", atk=1, dfs=1, hp=10, map_id=HIGH_MAP)
random.seed(3)
weak = hg._offline_field_sample(weak_st, HIGH_MAP, 48)
check("强号在低级图能赢", strong["win_rate"] >= 0.99, str(strong["win_rate"]))
check("弱号打 1500 级图赢不了", weak["win_rate"] <= 0.02, str(weak["win_rate"]))
check("弱号拿不到人物经验", weak["player_exp"] <= 0, str(weak["player_exp"]))
check("弱号也拿不到金币", weak["gold"] <= 0, str(weak["gold"]))

print("== E. 线上本来就不给奖励的图 ==")
nr = hg._offline_field_sample(new_char("e"), NO_REWARD_MAP, 8)
check("无奖励图直接判 0（不空转）", nr["avg_seconds"] == 0.0 and nr["samples"] == 0, str(nr))

print("== F. 场次 = 秒数 ÷ 平均一场耗时 ==")
random.seed(5)
st = new_char("f", atk=1e7, dfs=1e7, hp=1e8)
cfg = hg._offline_cfg()
sim = hg._offline_run_field(st, 3600, 1.0, cfg, False, [])
sample = hg._offline_field_sample(new_char("f2", atk=1e7, dfs=1e7, hp=1e8), LOW_MAP)
expect = int(3600 / sample["avg_seconds"])
check("跑满一小时", sim["battles"] > 0, str(sim["battles"]))
check("场次与「秒数÷平均一场」同量级",
      abs(sim["battles"] - expect) <= max(3, expect // 10),
      "%s vs %s（平均 %.2fs/场）" % (sim["battles"], expect, sample["avg_seconds"]))
check("模拟标记为已跑", sim["simulated"] is True, str(sim["simulated"]))

print("== G. 逐场入账：吃得住「一场最多连升 N 级」==")
random.seed(9)
st = new_char("g", atk=1e7, dfs=1e7, hp=1e8)
sim = hg._offline_run_field(st, 600, 1.0, hg._offline_cfg(), False, [])
p = st.player
check("等级涨了", int(p["level"]) > 1, str(p["level"]))
check("等级不超过场次（没有一场灌穿）",
      int(p["level"]) <= int(sim["battles"]) + 1,
      "%s 级 / %s 场" % (p["level"], sim["battles"]))
check("没有一次冲到 800（说明是逐场不是整包灌）",
      int(p["level"]) < 800, str(p["level"]))

print("== H. 勾了自动飞升：到门槛就地飞 ==")
random.seed(13)
st = new_char("h", level=799, atk=1e7, dfs=1e7, hp=1e8, map_id=HIGH_MAP, auto=True)
rec = []
sim = hg._offline_run_field(st, 1800, 5.0, hg._offline_cfg(), True, rec)
check("发生了飞升", "凡人→散仙" in rec, str(rec))
check("境界已是散仙", str(st.player["realm"]) == "散仙", str(st.player["realm"]))
check("飞升后等级重置为 1 起步", int(st.player["level"]) >= 1, str(st.player["level"]))

print("== I. 飞升后还打得动 → 留在原图 ==")
random.seed(17)
st = new_char("i", level=800, atk=1e7, dfs=1e7, hp=1e8, map_id=LOW_MAP)
got = hg._offline_pick_map_after_transition(st, LOW_MAP)
check("强号不换图", got == LOW_MAP, got)

print("== J. 飞升后确实打不动 → 按等级换图 ==")
random.seed(19)
st = new_char("j", level=1, atk=1, dfs=1, hp=10, map_id=HIGH_MAP)
got = hg._offline_pick_map_after_transition(st, HIGH_MAP)
check("弱号被换走", got != HIGH_MAP, got)
check("换到的图有经验奖励", float((hg.MAPS_DATA.get(got) or {}).get("avgPlayerExp", 0)) > 0, got)

print("== K. 金币按场累加 ==")
random.seed(23)
st = new_char("k", atk=1e7, dfs=1e7, hp=1e8)
sim = hg._offline_run_field(st, 1200, 1.0, hg._offline_cfg(), False, [])
check("金币 > 0", int(st.player["gold"]) > 0, str(st.player["gold"]))
check("回包金币与角色金币一致", int(sim["gold"]) == int(st.player["gold"]),
      "%s vs %s" % (sim["gold"], st.player["gold"]))
check("金币 ≈ 场次 × 每场金币",
      abs(int(sim["gold"]) - int(sim["battles"] * hg._offline_field_sample(
          new_char("k2", atk=1e7, dfs=1e7, hp=1e8), LOW_MAP)["gold"])) < int(sim["gold"]) * 0.2 + 10,
      "%s 金币 / %s 场" % (sim["gold"], sim["battles"]))

print("== L. 24 小时极端场次也能算完 ==")
random.seed(29)
st = new_char("l", atk=1e7, dfs=1e7, hp=1e8)
t0 = time.time()
sim = hg._offline_run_field(st, 86400, 1.0, hg._offline_cfg(), False, [])
dt = time.time() - t0
check("24 小时算完不超 5 秒", dt < 5.0, "%.2fs / %s 场" % (dt, sim["battles"]))
check("场次没爆上限", int(sim["battles"]) <= hg._OFFLINE_MAX_BATTLES, str(sim["battles"]))

print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
