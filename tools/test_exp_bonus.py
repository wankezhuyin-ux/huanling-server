#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验倍率回归测试：帮派加成、双倍丹道具名、旧档迁移。

守这几件事（都是实际踩过的坑）：
  1. 帮派加成要真的进 exp_multiplier —— /gang/buffs 早就下发 2.5%/级，
     但服务端算经验时没读它，帮里的人白少拿 2.5%×等级。
  2. 双倍丹的道具名必须与客户端 game_data 一致（双倍经验丹），
     写成「双倍经验符」时 take_item 按名字精确匹配 → 一颗都吃不上。
  3. 旧存档里的「双倍经验符」道具和通道键要能自动迁到「双倍经验丹」。

    python3 tools/test_exp_bonus.py
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

GANG_PATH = core.STATE_DIR / "gangs.json"
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    flag = "OK " if got == want else "FAIL"
    if got != want:
        FAILED.append(name)
    print("[%s] %-46s got=%r want=%r" % (flag, name, got, want))


def make_account(username: str, items: list[dict], channels: dict) -> core.State:
    save = {
        "playerData": {"level": 10, "experience": 0, "gold": 1000,
                       "current_map": core.DEFAULT_MAP,
                       "double_exp_until": 0, "double_exp_channels": channels},
        "inventoryData": {"items": items, "pet_equipment_items": [],
                          "warehouse_items": [], "xianmo_chests": []},
        "gameSettings": {"current_map": core.DEFAULT_MAP},
        "martialArtsData": {"martial_arts": [], "equipped_ids": []},
        "petsData": {"pets": [], "active_pet_id": ""},
    }
    path = core.STATE_DIR / "accounts" / (core._safe_name(username) + ".json")
    path.write_text(json.dumps({"save": save, "meta": {}}, ensure_ascii=False), encoding="utf-8")
    return core.State(username)


def main() -> int:
    backup = None
    if GANG_PATH.exists():
        backup = GANG_PATH.with_suffix(".json.test-bak")
        shutil.copyfile(GANG_PATH, backup)
    accounts: list[Path] = []

    try:
        # ---- 1. 没帮派：0
        acc = "expprobe1"
        accounts.append(core.STATE_DIR / "accounts" / (core._safe_name(acc) + ".json"))
        st = make_account(acc, [], {})
        check("无帮派 gang_exp_bonus_percent", st.gang_exp_bonus_percent(), 0.0)

        # ---- 2. 4 级帮派：2.5 × 4 = 10%
        GANG_PATH.parent.mkdir(parents=True, exist_ok=True)
        GANG_PATH.write_text(json.dumps({
            "nextId": 2, "nextAppId": 1,
            "gangs": {"1": {"id": 1, "name": "测试帮", "level": 4,
                            "members": [{"username": acc, "role": 1}]}},
        }, ensure_ascii=False), encoding="utf-8")
        core._GANG_CACHE["mtime"] = None
        st = core.State(acc)
        check("4 级帮派 gang_exp_bonus_percent", st.gang_exp_bonus_percent(), 10.0)
        base = core.State.VIP_EXP_MULT[st.vip_tier()] + st.double_exp_active_count() \
            + st.NOBLE_EXP_BONUS[st.noble_level()] / 100.0 \
            + (1.0 if int(time.time()) < st.newbie_exp_until() else 0.0) \
            + st.weekly_field_exp_bonus()
        check("exp_multiplier 含帮派加成", round(st.exp_multiplier(), 4), round(base + 0.10, 4))

        # ---- 3. 双倍丹道具名与客户端一致 + 可消耗
        import handlers_growth  # noqa: E402
        names = {rule[0] for rule in handlers_growth._CONSUME_RULES.values()}
        check("消耗规则覆盖三种双倍丹",
              {"双倍经验丹", "双倍经验丹(周)", "双倍经验丹(月)"} <= names, True)
        for name in ("双倍经验丹", "双倍经验丹(周)", "双倍经验丹(月)"):
            check("道具名在客户端道具表：%s" % name, core.client_has_item(name), True)
        check("道具名在通道表：双倍经验丹", "双倍经验丹" in core.State.DOUBLE_EXP_CHANNELS, True)
        check("道具名在通道表：双倍经验丹(周)", "双倍经验丹(周)" in core.State.DOUBLE_EXP_CHANNELS, True)
        check("道具名在通道表：双倍经验丹(月)", "双倍经验丹(月)" in core.State.DOUBLE_EXP_CHANNELS, True)
        import handlers_gang  # noqa: E402
        pill = [x for x in handlers_gang.SHOP_ITEMS if x["id"] == "gang_exp_pill"][0]
        check("帮贡商店双倍丹名可用", core.client_has_item(pill["itemName"]), True)

        # ---- 4. 旧档迁移：道具名 符→丹、通道键归一
        acc2 = "expprobe2"
        accounts.append(core.STATE_DIR / "accounts" / (core._safe_name(acc2) + ".json"))
        future = int(time.time()) + 86400
        st2 = make_account(acc2,
                           [{"id": "i1", "name": "双倍经验符", "count": 2},
                            {"id": "i2", "name": "双倍经验符·周", "count": 1}],
                           {"双倍经验符": future, "双倍经验符·月": future})
        held = sorted(x["name"] for x in (st2.save.get("inventoryData") or {}).get("items") or [])
        check("旧道具名已迁移", held, ["双倍经验丹", "双倍经验丹(周)"])
        check("旧通道键已归一", sorted(st2.double_exp_channels().keys()),
              ["双倍经验丹", "双倍经验丹(月)"])
        check("归一后仍算两种生效", st2.double_exp_active_count(), 2)
        check("消耗扣得到道具", st2.take_item("双倍经验丹", 1), True)
        check("扣完后剩 1", st2.item_count("双倍经验丹"), 1)
    finally:
        for path in accounts:
            path.unlink(missing_ok=True)
        if backup is not None:
            shutil.copyfile(backup, GANG_PATH)
            backup.unlink(missing_ok=True)
        else:
            GANG_PATH.unlink(missing_ok=True)
        core._GANG_CACHE["mtime"] = None

    print()
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
