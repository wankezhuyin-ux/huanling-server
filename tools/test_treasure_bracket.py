#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""藏宝洞争夺（单败签表 + 逐场战报）离线仿真。

不依赖真实账号与网络：伪造时钟 + 伪造 Storage，把一整轮
「开轮 → 入阵 → 报名截止 → 结算（打签表）→ 发奖 → 战报可查 → 下一轮」
从头跑一遍，并校验整点换轮后战报仍能从留档里查回来。

    python3 tools/test_treasure_bracket.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="treasure-sim-"))
core.STATE_DIR = TMP

import broadcasts  # noqa: E402
broadcasts.append = lambda *a, **k: None      # 仿真不写公屏

import treasure_round as tr  # noqa: E402

tr.ROUND_PATH = TMP / "treasure_round.json"
tr._DB = None

CLOCK = {"t": datetime(2026, 10, 28, 19, 0, 0, tzinfo=core.CN_TZ)}
tr.now = lambda: CLOCK["t"]


def at(hh: int, mm: int, day: int = 28) -> None:
    CLOCK["t"] = datetime(2026, 10, day, hh, mm, 0, tzinfo=core.CN_TZ)


# ---------------------------------------------------------------- 假存档

class FakeState:
    def __init__(self, name: str, vip: int = 0) -> None:
        self.username = name
        self._name = name
        self.vip = vip
        self.items: list[dict] = []
        self.mails: list[dict] = []
        self.meta: dict = {}

    def vip_tier(self) -> int:
        return self.vip

    def nickname(self) -> str:
        return self._name

    def send_mail(self, title: str, body: str, **kw) -> dict:
        row = {"id": 900000 + len(self.mails) + 1, "title": title, "body": body}
        self.mails.append(row)
        return row

    def bump(self) -> None:
        pass

    def flush(self) -> None:
        pass


class FakeStore:
    def __init__(self) -> None:
        self.map: dict[str, FakeState] = {}

    def get(self, account: str) -> FakeState:
        if account not in self.map:
            self.map[account] = FakeState(account)
        return self.map[account]


STORE = FakeStore()
tr.STORE = STORE


def mirror_of(name: str, factor: float) -> dict:
    """一份报名时钉住的镜像（summary + combat 四维）。"""
    pet_hp = 6000 * factor
    return {
        "playerName": name,
        "playerLevel": int(100 * factor),
        "petName": name + "的灵兽",
        "petLevel": int(100 * factor),
        "petElement": "金",
        "power": int(10000 * factor),
        "autoSnapshot": False,
        "combat": {
            "player": {"name": name, "hp": 600 * factor, "attack": 60 * factor,
                       "defense": 400 * factor, "agility": 200 * factor, "element": "无"},
            "pet": {"name": name + "的灵兽", "hp": pet_hp, "attack": 2600 * factor,
                    "defense": 2200 * factor, "agility": 2600 * factor, "element": "金"},
        },
    }


def seed_joins(rnd: dict, plan: dict[int, list[tuple[str, float]]]) -> None:
    joins = rnd.setdefault("joins", {})
    for chest_no, rows in plan.items():
        for name, factor in rows:
            joins[name] = {"chest_no": chest_no, "name": name,
                           "at": "2026-10-28T11:05:00.000Z",
                           "mirror": mirror_of(name, factor),
                           "power": int(10000 * factor)}


def main() -> int:
    ok = True

    def check(label: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        print(("  [OK]   " if cond else "  [FAIL] ") + label + ((" · " + extra) if extra else ""))
        if not cond:
            ok = False

    print("① 19:00 开轮")
    at(19, 0)
    db = tr.tick()
    cur = db["current"]
    check("本轮 round_key", cur["round_key"] == "treasure_20261028_19", cur["round_key"])
    check("今日第 11 轮", cur["round_index_today"] == 11)
    check("3 个秘箱", len(cur["chests"]) == 3)

    print("② 入阵：1 号箱 5 人、2 号箱 3 人、3 号箱空")
    seed_joins(cur, {
        1: [("甲", 1.00), ("乙", 1.05), ("丙", 0.95), ("丁", 1.10), ("戊", 1.02)],
        2: [("己", 1.00), ("庚", 1.30), ("辛", 0.90)],
    })
    tr._mark(db)
    tr._flush(db)

    print("③ 19:10 报名截止 → 争夺")
    at(19, 10)
    db = tr.tick()
    check("状态转 battle_running", db["current"]["status"] == "battle_running")

    print("④ 19:21 结算（跑签表 + 发奖）")
    at(19, 21)
    db = tr.tick()
    cur = db["current"]
    check("已结算", bool(cur.get("settled")))
    chest1, chest2, chest3 = cur["chests"]
    check("1 号箱有得主", bool(chest1.get("winner_name")), str(chest1.get("winner_name")))
    check("2 号箱有得主", bool(chest2.get("winner_name")), str(chest2.get("winner_name")))
    check("3 号箱空箱", chest3.get("winner_name") is None)

    # 5 人单败签表：第1轮 2 场 + 1 场轮空、第2轮 1 场 + 1 场轮空、第3轮 1 场 = 6 条
    m1 = chest1.get("matches") or []
    m2 = chest2.get("matches") or []
    check("1 号箱场次 = 6", len(m1) == 6, "实际 %d" % len(m1))
    check("2 号箱场次 = 3", len(m2) == 3, "实际 %d" % len(m2))

    byes = [m for m in m1 if not m.get("b_user_id")]
    # 5 人 → 第1轮 1 人轮空、第2轮（剩 3 人）再 1 人轮空
    check("轮空场次 = 2", len(byes) == 2, "实际 %d" % len(byes))
    check("轮空者晋级", byes[0]["winner_nickname"] == byes[0]["a_nickname"] if byes else False)

    log0 = m1[0].get("battle_log") or []
    types = [e.get("type") for e in log0]
    check("战报有 start", types[:1] == ["start"])
    check("战报有回合", "round" in types)
    check("战报有攻击", "attack" in types)
    check("战报以 victory 收尾", types[-1] == "victory", types[-1] if types else "")
    check("场次带 bracket_round/match_no", "bracket_round" in m1[0] and "match_no" in m1[0])

    # 每场胜者必须能在签表里对上：上一轮胜者进入下一轮
    adv = [m["winner_user_id"] for m in m1 if m["bracket_round"] == 1]
    r2 = []
    for m in m1:
        if m["bracket_round"] == 2:
            r2.extend([m["a_user_id"], m["b_user_id"]])
    check("第 1 轮胜者全部进第 2 轮", all(x in r2 for x in adv), str(adv) + " / " + str(r2))
    check("冠军唯一", len([m for m in m1 if m["bracket_round"] == max(x["bracket_round"] for x in m1)]) == 1)

    win1 = STORE.get(chest1["winner_user_id"])
    check("冠军背包有藏宝箱", any(i.get("item_type") == "treasure_chest" for i in win1.items))
    check("冠军收到邮件", bool(win1.mails))

    print("⑤ 战报接口（本轮）")
    got = tr.battle_log(int(chest1["id"]), 1, 3)
    check("chestId 对得上", got["chestId"] == int(chest1["id"]))
    check("total = 6", got["total"] == 6, str(got["total"]))
    check("第一页 3 场", len(got["matches"]) == 3)
    got2 = tr.battle_log(int(chest1["id"]), 2, 3)
    check("第二页 3 场", len(got2["matches"]) == 3, str(len(got2["matches"])))
    check("第三页 0 场", len(tr.battle_log(int(chest1["id"]), 3, 3)["matches"]) == 0)
    check("越界页为空", len(tr.battle_log(int(chest1["id"]), 9, 3)["matches"]) == 0)

    print("⑥ 20:00 换轮后仍能查上一轮战报（留档）")
    at(20, 0)
    db = tr.tick()
    check("已开新一轮", db["current"]["round_key"] == "treasure_20261028_20")
    check("留档 1 轮", len(db.get("history") or []) == 1)
    old = tr.battle_log(int(chest1["id"]), 1, 10)
    check("旧箱战报仍可查", old["total"] == 6, str(old["total"]))
    check("旧箱有得主", bool(old.get("winnerName")), str(old.get("winnerName")))
    check("空箱战报为空", tr.battle_log(int(chest3["id"]), 1, 10)["total"] == 0)

    print("⑦ 风控：不存在的 chestId")
    bad = tr.battle_log(999999, 1, 10)
    check("返回空而不是报错", bad["success"] and bad["total"] == 0)

    print("⑧ state 体积")
    size = tr.ROUND_PATH.stat().st_size
    print("      treasure_round.json = %.1f KB" % (size / 1024.0))
    check("单轮含战报 < 400KB", size < 400 * 1024, "%.1f KB" % (size / 1024.0))

    print("⑨ 每日入阵次数按 VIP 档位（配置 [2,4,5,8]）")
    for tier, want in ((0, 2), (1, 4), (2, 5), (3, 8)):
        got_limit = tr._daily_limit(FakeState("vip%d" % tier, vip=tier))
        check("VIP%d → %d 次" % (tier, want), got_limit == want, "实际 %d" % got_limit)
    check("不传 state 时按非 VIP 2 次", tr._daily_limit() == 2, str(tr._daily_limit()))

    print("\n结果：%s" % ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
