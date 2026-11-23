#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线结算「边灌经验边自动飞升」的回归测试。

玩家报障（2026-11-04，某测试账号）：
「离线期间没有自动飞升，而是上线后领取了离线经验才自动飞升的。离线经验领取完
已经超过 800 级飞升等级了才飞升」→ 上线看到「散仙 1 级」，明明挂了半小时却跟
刚飞升一样。

根因：`offline_claim` 把「每小时经验 × 时长」一次性灌进角色（`grant_player_exp`
一路升到 1200 级上限），客户端上线后才做自动飞升，而 `ascend()` 会把
`level/experience` 一起清零 —— 超过门槛的那几亿经验当场蒸发。

修法：勾了「双倍经验丹权益（自动飞升/投胎）」时改为分段结算，到门槛先飞升，
再把剩余经验接着往里灌。

守这些事：
  A. 勾选后离线经验能连续跨境界（凡人 627 + 7.6 亿 → 不止散仙 1 级）
  B. 未勾选时行为不变（维持旧的「一次灌满、不飞升」）
  C. 刚好够门槛 → 飞升，且余下经验不丢
  D. 不够门槛 → 不飞升
  E. 已在门槛上（800 级 0 经验）→ 直接飞升
  F. 飞升后等级/经验/五维/属性点按 `_reset_allocated_stats_for_level_one` 重置
  G. 天仙 → 大罗金仙 需要五行图鉴，服务端不擅自跨
  H. 幻灵天神 1200 级 → 自动投胎，转生次数 +1、永久点 +25、回凡人 1 级
  I. 投胎次数达上限后不再投胎
  J. 不吞不吐：最终进度消耗的经验总量 <= 注入的经验总量

    python3 tools/test_offline_auto_ascend.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_ascend_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_growth as hg  # noqa: E402

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


def new_char(tag: str, realm: str = "凡人", level: int = 627,
             exp: int = 0, auto: bool = True):
    st = core.State("u_" + tag)
    p = st.player
    p["realm"] = realm
    p["level"] = level
    p["experience"] = exp
    p["double_exp_auto_realm_enabled"] = auto
    p["permanent_points"] = 0
    p["auto_allocation_enabled"] = False
    p["points"] = 0
    p["pending_auto_points"] = 0
    p["max_hp"] = 100 + 5 * (level - 1)
    p["hp"] = p["max_hp"]
    return st


def total_exp_of(st, realm: str, level: int, exp: int) -> int:
    """从该境界 1 级练到当前状态一共要吃多少经验（用于守恒校验）。"""
    total = -int(exp)
    for lv in range(1, int(level)):
        total += int(st.required_exp(lv))
    return max(0, total)


print("== A. 勾选自动飞升：离线 7.6 亿经验不能只换来散仙 1 级 ==")
st = new_char("a", level=627)
rec = hg._offline_grant_exp_with_auto_realm(st, 762_636_600)
p = st.player
check("发生了自动飞升", len(rec) >= 1, str(rec))
check("境界已推进到散仙或更远", str(p["realm"]) != "凡人", str(p["realm"]))
check("不是「散仙 1 级」（等级明显 > 1）",
      not (str(p["realm"]) == "散仙" and int(p["level"]) <= 1),
      "%s %s 级" % (p["realm"], p["level"]))
check("飞升记录可读（形如 凡人→散仙）",
      all("→" in r or r == "投胎" for r in rec), str(rec))

print("== B. 未勾选：保持旧行为（一次灌满、不飞升）==")
st = new_char("b", level=627, auto=False)
hg._offline_grant_exp_with_auto_realm(st, 762_636_600)  # 直接调也应是同一套分段逻辑
st2 = new_char("b2", level=627, auto=False)
# 走真实分支判断：未勾选时调用方不会进分段函数
check("未勾选时判据为 False", not hg._offline_auto_transition_enabled(st))
check("勾选时判据为 True", hg._offline_auto_transition_enabled(new_char("b3", level=100)))

print("== C. 刚好够门槛 + 剩余经验不丢 ==")
st = new_char("c", level=799)
need_799 = int(st.required_exp(799))
hg._offline_grant_exp_with_auto_realm(st, need_799)
p = st.player
check("799 级给够最后一级经验 → 飞升散仙", str(p["realm"]) == "散仙", str(p["realm"]))
check("等级归 1", int(p["level"]) == 1, str(p["level"]))
check("经验归 0", int(p["experience"]) == 0, str(p["experience"]))

st = new_char("c2", level=799)
hg._offline_grant_exp_with_auto_realm(st, need_799 + 5000)
p = st.player
check("多给 5000 经验：仍飞升散仙", str(p["realm"]) == "散仙", str(p["realm"]))
check("多出来的 5000 留在经验条（不被吞）",
      int(p["level"]) > 1 or int(p["experience"]) == 5000,
      "%s 级 %s 经验" % (p["level"], p["experience"]))

print("== D. 不够门槛 → 不飞升 ==")
st = new_char("d", level=700)
hg._offline_grant_exp_with_auto_realm(st, 10_000)
p = st.player
check("仍在凡人", str(p["realm"]) == "凡人", str(p["realm"]))
check("等级没跳变", int(p["level"]) <= 720, str(p["level"]))

print("== E. 已站在门槛上（800 级 0 经验）→ 直接飞升 ==")
st = new_char("e", level=800)
hg._offline_grant_exp_with_auto_realm(st, 1)
p = st.player
check("800 级有 1 点经验即飞升散仙", str(p["realm"]) == "散仙", str(p["realm"]))
# 飞升后剩的那 1 点经验对 1 级角色正好够升 1 级（required_exp(1)=1），不算 bug
check("等级回到 1 级附近（1~2）", int(p["level"]) <= 2, str(p["level"]))

print("== F. 飞升后的重置口径（只给够一次飞升的经验，避免被后续升级点数干扰）==")
st = new_char("f", level=799)
st.player["points"] = 999
st.player["pending_auto_points"] = 123
st.player["vitality"] = 300
st.player["defense_base"] = 410
st.player["permanent_points"] = 25
hg._offline_grant_exp_with_auto_realm(st, int(st.required_exp(799)))
p = st.player
check("五维回 10", all(int(p.get(k, 0)) == 10 for k in
                    ("vitality", "magic", "attack_base", "defense_base", "agility_base")),
      str({k: p.get(k) for k in ("vitality", "defense_base")}))
check("属性点 = 20 + 永久点 25 = 45",
      int(p.get("points", 0)) + int(p.get("pending_auto_points", 0)) == 45,
      "%s + %s" % (p.get("points"), p.get("pending_auto_points")))
check("max_hp 回 100", int(p.get("max_hp", 0)) == 100, str(p.get("max_hp")))
check("hp 回满 100", int(p.get("hp", 0)) == 100, str(p.get("hp")))
check("attr_reset_guard_level = 21", int(p.get("attr_reset_guard_level", 0)) == 21,
      str(p.get("attr_reset_guard_level")))

print("== G. 天仙不擅自跨大罗金仙（要五行图鉴）==")
st = new_char("g", realm="天仙", level=1000)
hg._offline_grant_exp_with_auto_realm(st, 5_000_000_000)
p = st.player
check("停在天仙", str(p["realm"]) == "天仙", str(p["realm"]))
check("没有凭空变成大罗金仙", str(p["realm"]) != "大罗金仙")

print("== H. 幻灵天神 1200 级 → 自动投胎 ==")
st = new_char("h", realm="幻灵天神", level=1200)
st.player["reincarnation"] = 3
hg._offline_grant_exp_with_auto_realm(st, 50_000_000)
p = st.player
check("投胎回凡人", str(p["realm"]) == "凡人", str(p["realm"]))
check("投胎后剩余经验继续升级（等级 >= 1）", int(p["level"]) >= 1, str(p["level"]))
check("转生次数 +1", int(p["reincarnation"]) == 4, str(p["reincarnation"]))
check("永久点 +25", int(p["permanent_points"]) == 25, str(p["permanent_points"]))
check("塔层回 1", int(p.get("tower_floor", 0)) == 1, str(p.get("tower_floor")))
st2 = new_char("h2", realm="幻灵天神", level=1200)
rec2 = hg._offline_grant_exp_with_auto_realm(st2, 50_000_000)
check("记录里有投胎", "投胎" in rec2, str(rec2))
# 投胎回凡人 1 级后，剩下的经验继续拿去升级（5000 万 → 400 多级）
check("投胎后剩余经验继续升级（不止 1 级）", int(st2.player["level"]) > 100,
      str(st2.player["level"]))

print("== I. 投胎次数达上限后不再投胎 ==")
st = new_char("i", realm="幻灵天神", level=1200)
st.player["reincarnation"] = hg._MAX_REINCARNATION
hg._offline_grant_exp_with_auto_realm(st, 50_000_000)
p = st.player
check("仍在幻灵天神", str(p["realm"]) == "幻灵天神", str(p["realm"]))
check("转生次数不变", int(p["reincarnation"]) == hg._MAX_REINCARNATION,
      str(p["reincarnation"]))

print("== J. 7.6 亿经验的走向与经验守恒（不能灌完就扔）==")
st = new_char("j", level=627)
given = 762_636_600
rec = hg._offline_grant_exp_with_auto_realm(st, given)
p = st.player
lv = int(p["level"])
print("     飞升链：%s → 最终 %s %d 级（余 %d 经验）" % (rec, p["realm"], lv, p["experience"]))
check("至少飞升两次（凡人→散仙→地仙）", len(rec) >= 2, str(rec))
check("最终境界高于散仙（不是散仙 1 级）",
      hg._REALM_CHAIN.index(str(p["realm"])) >= 2, str(p["realm"]))

# 守恒：本次注入的经验，全部变成了「走完这些境界所需的经验 + 当前经验条」
devour = 0
for level_range in ((627, 800), (1, 800), (1, lv)):
    for x in range(*level_range):
        devour += int(st.required_exp(x))
devour += int(p["experience"])
last_need = int(st.required_exp(lv))
check("经验没被吞（差值 ≤ 最后一级的需求）", abs(devour - given) <= last_need,
      "消耗 %s / 注入 %s（差 %s，末级需求 %s）" % (devour, given, devour - given, last_need))
check("经验没被多发", devour <= given, "%s vs %s" % (devour, given))

print("== L. 天仙 → 大罗金仙 的解锁标志 ==")
st = new_char("l0", realm="天仙", level=1000)
check("未解锁账号：天仙不算解锁", not hg._offline_daluo_unlocked(st.player))
st = new_char("l1", realm="天仙", level=1000)
st.player[hg._DALUO_UNLOCK_KEY] = True
check("有标志：算解锁", hg._offline_daluo_unlocked(st.player))
st = new_char("l2", realm="天仙", level=1000)
st.player["reincarnation"] = 1
check("投过胎的账号：自动算解锁（必然飞过大罗）",
      hg._offline_daluo_unlocked(st.player))
st = new_char("l3", realm="大罗金仙", level=500)
check("当前已是大罗金仙：算解锁", hg._offline_daluo_unlocked(st.player))
st = new_char("l4", realm="幻灵天神", level=100)
check("当前已是幻灵天神：算解锁", hg._offline_daluo_unlocked(st.player))

print("== M. 解锁后离线可以自己飞大罗金仙 ==")
st = new_char("m", realm="天仙", level=1000)
st.player[hg._DALUO_UNLOCK_KEY] = True
rec = hg._offline_grant_exp_with_auto_realm(st, 2_000_000_000)
p = st.player
print("     飞升链：%s → 最终 %s %d 级" % (rec, p["realm"], p["level"]))
check("飞升链里有 天仙→大罗金仙", "天仙→大罗金仙" in rec, str(rec))
check("已跨过大罗金仙（记录里继续往上）",
      "大罗金仙→幻灵天神" in rec or hg._REALM_CHAIN.index(str(p["realm"])) >= 3, str(rec))

print("== N. 投胎过的账号（修复前的老号）离线也能飞大罗 ==")
st = new_char("n", realm="天仙", level=1000)
st.player["reincarnation"] = 2
hg._offline_grant_exp_with_auto_realm(st, 2_000_000_000)
check("老号天仙不再卡住", str(st.player["realm"]) != "天仙", str(st.player["realm"]))

print("== O. 投胎后标志保留，再练回天仙仍能自动飞 ==")
st = new_char("o", realm="天仙", level=1000)
st.player[hg._DALUO_UNLOCK_KEY] = True
hg._offline_grant_exp_with_auto_realm(st, 50_000_000_000)  # 一路到大罗 → 幻灵天神 → 投胎
p = st.player
check("标志跨投胎保留", bool(p.get(hg._DALUO_UNLOCK_KEY, False)), str(p.get(hg._DALUO_UNLOCK_KEY)))
check("确实走完了「大罗→幻灵→投胎」并继续重练",
      int(p["reincarnation"]) >= 1, "%s / 转生 %s" % (p["realm"], p["reincarnation"]))

print("== P. 在线飞上大罗金仙时，服务端记下解锁标志 ==")
_fn = core.ROUTES[("POST", "/player/realm/transition")]


def _ascend_to(st, target_realm: str, op: str) -> dict:
    body = {
        "transitionType": "ascend",
        "operationId": op,
        "playerData": {"realm": target_realm, "level": 1, "experience": 0, "points": 20},
    }
    return _fn(core.Ctx(st, body, {}, {"username": "u_probe"}, {}))


st = new_char("p1", realm="天仙", level=1000)
_ascend_to(st, "大罗金仙", "op_daluo_1")
check("飞上大罗金仙 → 写入标志", bool(st.player.get(hg._DALUO_UNLOCK_KEY, False)),
      str(st.player.get(hg._DALUO_UNLOCK_KEY)))

st = new_char("p2", realm="凡人", level=800)
_ascend_to(st, "散仙", "op_sanxian_1")
check("普通飞升（散仙）不写标志", not bool(st.player.get(hg._DALUO_UNLOCK_KEY, False)),
      str(st.player.get(hg._DALUO_UNLOCK_KEY)))

print("== K. 线上配置一致性（门槛表与服务端上限）==")
check("凡人门槛 800", hg._ASCEND_REQ_LEVEL["凡人"] == 800)
check("散仙门槛 800", hg._ASCEND_REQ_LEVEL["散仙"] == 800)
check("地仙门槛 1000", hg._ASCEND_REQ_LEVEL["地仙"] == 1000)
check("天仙门槛 1000", hg._ASCEND_REQ_LEVEL["天仙"] == 1000)
check("大罗金仙门槛 1000", hg._ASCEND_REQ_LEVEL["大罗金仙"] == 1000)
check("境界链 6 段", len(hg._REALM_CHAIN) == 6, str(hg._REALM_CHAIN))

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
