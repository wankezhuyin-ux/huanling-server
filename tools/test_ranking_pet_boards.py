#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排行榜（宠物两个榜）口径回归测试。

守两条线上出过的问题（2026-11-01 玩家截图发现）：
  1. 「可进化榜」里混进不可进化的宠。原因：判定写成
     `bool(is_evolution_pet) or ("进化" in form) or form == "终极"`，
     而不可进化宠的 form 正好是「不可进化」，`"进化" in "不可进化"` 为真
     → 线上 66 条可进化榜里 43 条其实是不可进化，不可进化榜只剩 1 条。
  2. 榜单「总成长:」后面一直空白。原因：读 pet["growth_rate"]，存档里
     没这个键（真实字段是 total_growth = 攻+防+敏成长之和）。

    python3 tools/test_ranking_pet_boards.py
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    flag = "OK " if got == want else "FAIL"
    if got != want:
        FAILED.append(name)
    print("[%s] %-56s got=%r want=%r" % (flag, name, got, want))


def check_true(name: str, cond) -> None:
    check(name, bool(cond), True)


def pet(form: str, *, flag=None, level=100, total_growth=None, name="测试宠") -> dict:
    row = {
        "id": "p_%s_%d" % (form, level),
        "pet_id": "p_%s_%d" % (form, level),
        "name": name,
        "level": level,
        "form": form,
        "generation": 0,
        "element": "火",
        "final_attack": level * 10,
        "final_max_hp": level * 100,
    }
    if flag is not None:
        row["is_evolution_pet"] = flag
    if total_growth is not None:
        row["total_growth"] = total_growth
    return row


def test_classifier() -> None:
    print("\n-- 可进化判定 --")
    # 线上真实数据：变异阿努 form=不可进化 / is_evolution_pet=False
    check_true("不可进化 且 flag=False → 不算可进化",
               not core.pet_is_evolution_pet(pet("不可进化", flag=False)))
    check_true("幼年 flag=True → 可进化",
               core.pet_is_evolution_pet(pet("幼年", flag=True)))
    check_true("一进 flag=True → 可进化",
               core.pet_is_evolution_pet(pet("一进", flag=True)))
    check_true("二进 flag=True → 可进化",
               core.pet_is_evolution_pet(pet("二进", flag=True)))
    check_true("终极 flag=True → 可进化",
               core.pet_is_evolution_pet(pet("终极", flag=True)))
    check_true("变异体（不可进化）→ 不算可进化",
               not core.pet_is_evolution_pet(pet("不可进化", flag=False, name="变异阿努")))

    # 老存档没有 is_evolution_pet 字段时的兜底
    check_true("缺字段 + form=不可进化 → 不算可进化",
               not core.pet_is_evolution_pet(pet("不可进化")))
    check_true("缺字段 + form=幼年 → 可进化",
               core.pet_is_evolution_pet(pet("幼年")))
    check_true("缺字段 + form=终极 → 可进化",
               core.pet_is_evolution_pet(pet("终极")))
    check_true("缺字段 + form=未知形态 → 不算可进化",
               not core.pet_is_evolution_pet(pet("神秘形态")))


def test_growth() -> None:
    print("\n-- 总成长取值 --")
    check("total_growth=62.114 → 62.114",
          core.pet_total_growth({"total_growth": 62.114}), 62.114)
    check("只有 growth_rates → 攻+防+敏之和（不含生命）",
          core.pet_total_growth({"growth_rates": {"hp": 62.1, "attack": 20.5,
                                                  "defense": 20.5, "agility": 21.114}}),
          62.114)
    check("全缺 → 0.0", core.pet_total_growth({}), 0.0)
    check("脏数据不炸", core.pet_total_growth({"total_growth": "abc"}), 0.0)


class FakeState:
    def __init__(self, username: str, pets: list, level: int = 500) -> None:
        self.username = username
        self.pets = pets
        self.player = {"level": level, "cultivation": 100, "reincarnation": 0,
                       "tower_highest": level // 10, "suit_dungeon_progress": 0,
                       "realm": "天仙", "name": username}
        self.meta = {"nickname": username}


def fake_store(states: dict) -> core.AccountStore:
    store = core.AccountStore.__new__(core.AccountStore)
    store.accounts = {u: {"userId": i + 1} for i, u in enumerate(states)}
    store._cache = dict(states)
    # get() 里有 `with self.lock`，不补上会抛 AttributeError 并被榜单逻辑
    # 静默跳过（表现为「榜单 0 条」）
    store.lock = threading.RLock()
    return store


def test_boards() -> None:
    print("\n-- 榜单分流 --")
    st = FakeState("u1", [
        pet("不可进化", flag=False, level=1500, name="变异阿努", total_growth=62.114),
        pet("终极", flag=True, level=1200, name="蓝鸟龙", total_growth=88.5),
        pet("幼年", flag=True, level=900, name="嘟嘟鸟", total_growth=20.0),
        pet("不可进化", flag=False, level=400, name="苦行龟", total_growth=10.5),
        pet("一进", flag=True, level=300, name="嗷日犬", total_growth=30.0),
    ])
    store = fake_store({"u1": st})

    evo = store.rankings("pets_evolution")
    plain = store.rankings("pets_non_evolution")

    check("可进化榜条数", len(evo), 3)
    check("不可进化榜条数", len(plain), 2)
    check("可进化榜里没有不可进化的宠",
          sorted(r["pet_name"] for r in evo), ["嗷日犬", "嘟嘟鸟", "蓝鸟龙"])
    check("不可进化榜里只有不可进化的宠",
          sorted(r["pet_name"] for r in plain), ["变异阿努", "苦行龟"])
    check("榜单里每条 is_evolution_pet 与所在榜一致",
          [r["is_evolution_pet"] for r in evo], [1, 1, 1])
    check("总成长已填数字（不是空串）",
          [r["growth_rate"] for r in evo], [88.5, 20.0, 30.0])
    check("总成长取 total_growth 而不是 0",
          evo[0]["growth_rate"], 88.5)
    check("排序：等级降序", [r["level"] for r in evo], [1200, 900, 300])

    # 其它榜不应受影响
    players = store.rankings("players")
    check("玩家榜仍有数据", len(players), 1)
    tower = store.rankings("tower")
    check("天关榜按 tower_highest 贡献", len(tower), 1)


def test_exclude() -> None:
    print("\n-- 测试号排除名单 --")
    real = FakeState("player_a", [pet("幼年", flag=True, name="蓝鸟龙")], level=900)
    fake = FakeState("emu2026", [pet("幼年", flag=True, name="测试宠")], level=1200)
    store = fake_store({"player_a": real, "emu2026": fake})
    store.ranking_excluded_usernames = lambda: {"emu2026"}

    players = store.rankings("players")
    check("排除后玩家榜只剩真实号", [r["nickname"] for r in players], ["player_a"])
    evo = store.rankings("pets_evolution")
    check("排除后宠物榜也不含测试号的宠", [r["pet_name"] for r in evo], ["蓝鸟龙"])
    check("名单文件在 data/ 下",
          core.DATA.joinpath("ranking-exclude.json").exists(), True)
    names = core.AccountStore.ranking_excluded_usernames(store)
    check_true("名单里的测试号被读到", "emu2026" in names)


def main() -> int:
    test_classifier()
    test_growth()
    test_boards()
    test_exclude()
    print()
    if FAILED:
        print("FAILED %d 项:" % len(FAILED))
        for name in FAILED:
            print("  -", name)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
