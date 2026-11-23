#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前配置/物品完整性回归。

盯住 2026-11-05 对照原版审计查出的三类「看起来能配、其实没生效」：
  1. State.add_item 把带 custom_data 的同名物品按名字合并 → 第二件的
     限量标记 / 词条 / 开箱 token 整块丢失
  2. atlasDungeon.captureRate 配置零消费点，实际写死 core.CAPTURE_RATE
  3. martialCreate 与 martial_create 两段经 snake_case 后互相覆盖

跑法：python3 tools/test_release_config_integrity.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lype_cfg_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0


def check(name: str, cond, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("[OK]   " + name)
    else:
        FAIL += 1
        print("[FAIL] " + name + ("  → " + detail if detail else ""))


def st(name: str) -> core.State:
    return core.State(name)


print("== A. 带 custom_data 的物品不能再按名字合并 ==")
s = st("cfg_a")
r1 = s.add_item("极限装备", 1, "equipment", {"is_limit_equip": True, "slot": "weapon"})
r2 = s.add_item("极限装备", 1, "equipment", {"is_limit_equip": True, "slot": "armor"})
check("A1 同名不同词条拆成两行", len([i for i in s.items if i["name"] == "极限装备"]) == 2,
      str(len([i for i in s.items if i["name"] == "极限装备"])))
check("A2 第一件词条完整保留", (r1.get("custom_data") or {}).get("slot") == "weapon",
      str(r1.get("custom_data")))
check("A3 第二件词条完整保留", (r2.get("custom_data") or {}).get("slot") == "armor",
      str(r2.get("custom_data")))
check("A4 两行 id 不同", r1.get("id") != r2.get("id"))
check("A5 item_type 保留", r2.get("item_type") == "equipment", str(r2.get("item_type")))
r3 = s.add_item("极限装备", 2, "equipment", {"is_limit_equip": True, "slot": "armor"})
check("A6 完全相同词条仍合并 count", r3 is r2 and int(r2["count"]) == 3, str(r2["count"]))
s.add_item("普通解封石", 3, "material")
s.add_item("普通解封石", 4, "material")
check("A7 无 custom 的普通材料仍按名字堆叠", s.item_count("普通解封石") == 7,
      str(s.item_count("普通解封石")))
check("A8 普通材料只有一行", len([i for i in s.items if i["name"] == "普通解封石"]) == 1)
add_src = __import__("inspect").getsource(core.State.add_item)
check("A9 add_item 源码里同名合并前有 custom 分支",
      "custom" in add_src and add_src.index("if custom:") < add_src.index("item = self.find_item(name)"))

print("\n== B. 野外捕捉率真的读 runtime-config ==")
mod = core.RULES.modules.get("atlas_dungeon", {})
old = mod.get("captureRate", None)
check("B1 默认读配置值 0.1", abs(core.capture_rate() - 0.1) < 1e-9, str(core.capture_rate()))
mod["captureRate"] = 0.77
check("B2 改配置立即生效", abs(core.capture_rate() - 0.77) < 1e-9, str(core.capture_rate()))
mod["captureRate"] = 2.5
check("B3 超过 1 被 clamp 到 1.0", core.capture_rate() == 1.0, str(core.capture_rate()))
mod["captureRate"] = "bad"
check("B4 非法值回落常量", core.capture_rate() == core.CAPTURE_RATE, str(core.capture_rate()))
mod.pop("captureRate", None)
old_const = core.CAPTURE_RATE
core.CAPTURE_RATE = 0.33
check("B5 配置缺键回落到 CAPTURE_RATE", abs(core.capture_rate() - 0.33) < 1e-9,
      str(core.capture_rate()))
core.CAPTURE_RATE = old_const
if old is not None:
    mod["captureRate"] = old
cap_src = __import__("inspect").getsource(core.capture_wild_pet)
check("B6 捕捉流程调用 capture_rate() 而不是直接读常量",
      "capture_rate()" in cap_src and ">= CAPTURE_RATE" not in cap_src)

print("\n== C. runtime-config 段名不冲突、开关真被消费 ==")
cfg = json.loads((ROOT / "data/runtime-config.json").read_text(encoding="utf-8"))
check("C1 已删除重复的 martialCreate 段", "martialCreate" not in cfg)
check("C2 serverAuthorityEnabled 合并进 martial_create",
      cfg.get("martial_create", {}).get("serverAuthorityEnabled") is True,
      str(cfg.get("martial_create")))
check("C3 RULES 能读到该开关",
      core.RULES.v("martial_create", "serverAuthorityEnabled", None) is True)


names = [core.snake(k) for k in cfg if isinstance(cfg[k], dict)]
dups = sorted({n for n in names if names.count(n) > 1})
check("C4 全仓配置段 snake_case 后无重名覆盖", not dups, str(dups))
hg_src = (ROOT / "handlers_growth.py").read_text(encoding="utf-8")
check("C5 /martial/create 真实读取该开关",
      'RULES.v("martial_create", "serverAuthorityEnabled"' in hg_src)

print("\n== D. 关键可调参数必须有消费点（防回退） ==")
scan = []
for f in list(ROOT.glob("*.py")) + list(ROOT.parent.parent.joinpath(
        "recovered-godot-project/scripts").glob("**/*.gd")):
    n = str(f)
    if any(x in n for x in ["/tools/", "/dev/", "__pycache__", ".snapshot", ".bak"]):
        continue
    try:
        scan.append(f.read_text(encoding="utf-8"))
    except Exception:
        pass
blob = "\n".join(scan)
for key in ["captureRate", "serverAuthorityEnabled"]:
    n = len(re.findall(r'["\']' + re.escape(key) + r'["\']', blob))
    check("D key=%s 引用次数 > 0" % key, n > 0, str(n))

print("\n通过 %d，失败 %d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
