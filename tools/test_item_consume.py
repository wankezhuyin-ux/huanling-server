#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""道具名 / 消耗链路 / 离线加成 回归测试。

守这几件事（都是实际踩过的坑）：
  1. 服务端 `_CONSUME_RULES` 的名字必须与客户端 ALL_ITEMS 一致 —— 名字对不上时
     take_item 精确匹配失败，玩家点「使用」只会看到「道具数量不足」。
     踩过的：双倍经验符→丹、蓄灵丹→聚灵丹、经验丹→升级丹、开悟丹锦囊→悟道仙丹礼包。
  2. 帮贡商店卖的东西必须真的能用（客户端道具表里有、服务端消耗规则里有）。
  3. 离线结算要含「新人 10 天 +100%」和「贵族 %」（客户端帮助文案承诺过）。
  4. 宠物双倍经验丹只加灵兽：pet_exp_multiplier=2.0，但不改人物经验的倍率。

    python3 tools/test_item_consume.py
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402
import handlers_gang  # noqa: E402
import handlers_growth  # noqa: E402
import handlers_play  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    flag = "OK " if got == want else "FAIL"
    if got != want:
        FAILED.append(name)
    print("[%s] %-50s got=%r want=%r" % (flag, name, got, want))


def make_account(username: str, player_extra: dict | None = None,
                 items: list[dict] | None = None, meta: dict | None = None) -> core.State:
    p = {"level": 10, "experience": 0, "gold": 1000, "name": "测试侠",
         "current_map": core.DEFAULT_MAP}
    p.update(player_extra or {})
    save = {
        "playerData": p,
        "inventoryData": {"items": items or [], "pet_equipment_items": [],
                          "warehouse_items": [], "xianmo_chests": []},
        "gameSettings": {"current_map": core.DEFAULT_MAP},
        "martialArtsData": {"martial_arts": [], "equipped_ids": []},
        "petsData": {"pets": [{"id": "pet1", "name": "灵兽", "level": 10,
                               "experience": 0, "growth_rates": {}}],
                     "active_pet_id": "pet1"},
    }
    path = core.STATE_DIR / "accounts" / (core._safe_name(username) + ".json")
    path.write_text(json.dumps({"save": save, "meta": meta or {}}, ensure_ascii=False),
                    encoding="utf-8")
    return core.State(username)


def offline_exp(st: core.State, seconds: int = 3 * 3600) -> dict:
    """跑一次离线结算，返回回包 data。"""
    st.meta["offlinePending"] = {"seconds": seconds}
    ctx = core.Ctx(st, {"operationId": "op_%d" % time.time_ns()}, {},
                   {"username": st.username}, {})
    res = handlers_growth.offline_claim(ctx)
    return res.get("data", res)


def main() -> int:
    accounts: list[Path] = []
    try:
        # ---------------- 1. 消耗规则道具名 ↔ 客户端
        rules = handlers_growth._CONSUME_RULES
        for key, (item_name, _kind) in sorted(rules.items()):
            check("消耗规则道具名在客户端：%s" % key, core.client_has_item(item_name), True)

        expect = {
            "double_exp_day": "双倍经验丹",
            "player_upgrade_dan": "人物升级丹",
            "pet_upgrade_dan": "宠物升级丹",
            "pet_double_exp_day": "宠物双倍经验丹",
            "wudao_pill_gift": "悟道仙丹礼包",
            "limit_equipment_gift": "极限装备礼包",
            "cultivation_pill_juling": "聚灵丹",
            "cultivation_pill_ningshen": "凝神丹",
            "cultivation_pill_wudao": "悟道仙丹",
        }
        for key, name in expect.items():
            check("规则名对齐 %s" % key, rules.get(key, ("", ""))[0], name)

        # ---------------- 2. 帮贡商店买得到也用得了
        for row in handlers_gang.SHOP_ITEMS:
            name = row["itemName"]
            check("帮贡商店道具在客户端：%s" % name, core.client_has_item(name), True)
        usable = {
            "双倍经验丹": "double_exp_day",
            "宠物双倍经验丹": "pet_double_exp_day",
            "凝神丹": "cultivation_pill_ningshen",
        }
        for row in handlers_gang.SHOP_ITEMS:
            key = usable.get(row["itemName"])
            if key:
                check("帮贡商店道具可消耗：%s" % row["itemName"], key in rules, True)

        # ---------------- 3. 七日签到奖励名都能发货
        family = handlers_play._SIGNIN7_ITEM_FAMILY
        for row in handlers_play.SIGNIN7_REWARDS:
            if row["type"] != "item":
                continue
            name = row["item_id"]
            ok_name = core.client_has_item(name) or name in family
            check("签到奖励可发货：%s" % name, bool(ok_name), True)
        for name, pool in family.items():
            check("签到族名成员都在客户端：%s" % name,
                  all(core.client_has_item(x) for x in pool), True)

        # ---------------- 4. 宠物双倍经验丹：消耗 → 灵兽 ×2，人物不变
        acc = "itemprobe_petdbl"
        accounts.append(core.STATE_DIR / "accounts" / (core._safe_name(acc) + ".json"))
        st = make_account(acc, items=[{"id": "i1", "name": "宠物双倍经验丹", "count": 2}])
        check("未吃丹 pet_exp_multiplier", st.pet_exp_multiplier(), 1.0)
        ctx = core.Ctx(st, {"consumeKey": "pet_double_exp_day", "batchCount": 1,
                            "operationId": "pd1"}, {}, {"username": acc}, {})
        res = handlers_growth.whitelist_item_consume(ctx)
        check("吃丹成功", bool(res.get("success", res.get("ok", False))), True)
        check("吃丹后宠物倍率", st.pet_exp_multiplier(), 2.0)
        check("剩 1 颗", st.item_count("宠物双倍经验丹"), 1)
        check("宠物双倍不影响人物倍率", st.exp_multiplier() > 0 and st.double_exp_active_count(), 0)
        check("到期时间已写入", st.pet_double_exp_until() > int(time.time()), True)
        # 顺延而不是覆盖
        first_until = st.pet_double_exp_until()
        second = st.add_pet_double_exp()
        check("再吃一颗是顺延", second > first_until, True)

        # ---------------- 5. 离线结算含新人 / 贵族
        acc2 = "itemprobe_off1"
        accounts.append(core.STATE_DIR / "accounts" / (core._safe_name(acc2) + ".json"))
        st2 = make_account(acc2, player_extra={"newbie_exp_until": 0})
        # 离线结算现在走「按该图刷怪 + 战斗模拟」，每场的怪/伤害都是随机的，
        # 要比倍率就得把随机种子固定住，否则两次跑出来的基数都不一样。
        random.seed(20261104)
        base = offline_exp(st2)
        base_mult = float(base.get("expMultiplier", 0))
        check("无加成离线倍率=1.0", round(base_mult, 4), 1.0)

        acc3 = "itemprobe_off2"
        accounts.append(core.STATE_DIR / "accounts" / (core._safe_name(acc3) + ".json"))
        future = int(time.time()) + 5 * 86400
        st3 = make_account(acc3, player_extra={"newbie_exp_until": future})
        random.seed(20261104)
        with_newbie = offline_exp(st3)
        check("新人加成离线倍率=2.0", round(float(with_newbie.get("expMultiplier", 0)), 4), 2.0)
        check("新人加成后经验翻倍",
              int(with_newbie["exp"]) >= int(base["exp"]) * 2 * 0.98 - 10, True)

        acc4 = "itemprobe_off3"
        accounts.append(core.STATE_DIR / "accounts" / (core._safe_name(acc4) + ".json"))
        st4 = make_account(acc4, player_extra={"newbie_exp_until": 0, "noble_level": 10})
        noble = offline_exp(st4)
        want = round(1.0 + core.State.NOBLE_EXP_BONUS[10] / 100.0, 4)
        check("贵族 10 级离线倍率", round(float(noble.get("expMultiplier", 0)), 4), want)
        check("离线明细带贵族项",
              round(float(noble.get("expBonusBreakdown", {}).get("noble", -1)), 4),
              round(core.State.NOBLE_EXP_BONUS[10] / 100.0, 4))

        # 宠物双倍丹：只加宠物经验，人物经验不受影响。
        # 用两个同配置账号对比（同一个账号连跑两次不行：第一次结算会升级，
        # 等级变了每小时经验基准就变了，比出来的差值不是加成的锅）。
        acc5 = "itemprobe_off4"
        accounts.append(core.STATE_DIR / "accounts" / (core._safe_name(acc5) + ".json"))
        st5 = make_account(acc5, player_extra={"newbie_exp_until": 0})
        random.seed(424242)
        no_petbuff = offline_exp(st5)

        acc6 = "itemprobe_off5"
        accounts.append(core.STATE_DIR / "accounts" / (core._safe_name(acc6) + ".json"))
        st6 = make_account(acc6, player_extra={"newbie_exp_until": 0},
                           meta={"petDoubleExpUntil": int(time.time()) + 1800})
        check("宠物丹生效时倍率为 2", st6.pet_exp_multiplier(), 2.0)
        random.seed(424242)
        with_petbuff = offline_exp(st6)
        check("宠物丹不改人物离线经验", with_petbuff["exp"], no_petbuff["exp"])
        check("宠物丹不改人物离线倍率",
              round(float(with_petbuff.get("expMultiplier", 0)), 4),
              round(float(no_petbuff.get("expMultiplier", 0)), 4))
    finally:
        for path in accounts:
            path.unlink(missing_ok=True)

    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
