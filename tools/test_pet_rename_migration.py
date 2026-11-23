#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宠物 IP 改名（B2）的回归测试。

2026-11-09 B2：把 17 个高危角色名按「意思相近、老玩家一看就知道」的原则改成同源新名。
2026-11-10 复核：其中 7 个公有领域 / 低风险名恢复原版名，并为「B2 新名 → 原版名」
补 7 条反向迁移键（共 24 条），让已迁移过的线上存档能自动回退。这里守住五件事：

  A. 改名表本身：pet-rename.json 非空、键是 8 位 md5、成功加载；
  B. 迁移链路：老存档里的旧宠物名会被 core.State 读档时换成现用名
     （宠物栏 + 宠物袋 + 邮件附件三处），且重复读档幂等；
  C. 两端一致：客户端 game_data.gd 的 PET_NAME_MIGRATION 与服务端同一份键；
  D. 联动改名：随宠物改名的礼包，道具迁移表里也有对应项；
  E. 反向回退：B2 新名能迁回原版名，原版名本身读档幂等，两端表都是 24 条。

A–D 组用合成名字验证，不写任何仍需脱敏的旧名；E 组出现的 7 个原版名已恢复使用，属合法名称。

    python3 tools/test_pet_rename_migration.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="ly_pet_rename_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

GAME = ROOT.parent.parent / "recovered-godot-project"
GAME_DATA = GAME / "scripts" / "game_data.gd"
PET_DATA_GD = GAME / "scripts" / "pet_data_complete.gd"

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


# --------------------------------------------------------------------------
print("== A. 改名表 ==")
table = core.pet_rename_map()
check("A1 pet-rename.json 已加载且非空", bool(table), "条数 %d" % len(table))
check("A2 条数 == 24（17 个原名键 + 7 个反向回退键）", len(table) == 24, str(len(table)))
check("A3 键全部是 8 位小写 md5（磁盘不留明文旧名）",
      all(re.fullmatch(r"[0-9a-f]{8}", k) for k in table),
      str([k for k in table if not re.fullmatch(r"[0-9a-f]{8}", k)][:5]))
check("A4 值全部是非空中文名", all(isinstance(v, str) and v.strip() for v in table.values()))
check("A5 没有把旧名当值又写回去（值里不含 md5 键）",
      all(k not in v for k, v in table.items()))

pets = json.load(open(ROOT / "data" / "pets.json", encoding="utf-8"))["pets"]
names = [str(v.get("name", "")) for v in pets.values()]
check("A6 宠物目录 337 条名字互不重复", len(set(names)) == len(names),
      "重复 %s" % [n for n in set(names) if names.count(n) > 1][:5])
missing = [n for n in table.values() if n not in names]
check("A7 新名全部能在 pets.json 里查到", not missing, str(missing[:5]))

# --------------------------------------------------------------------------
print("\n== B. 老存档迁移（宠物栏 / 宠物袋 / 邮件）==")
SYN_OLD = "测试旧宠名甲"
SYN_NEW = "测试新宠名乙"
core._PET_RENAME = dict(table)
core._PET_RENAME[md5_8(SYN_OLD)] = SYN_NEW

st = core.State("rename_case")
st.save.setdefault("petsData", {})["pets"] = [
    {"id": "syn_1", "pet_id": "syn", "name": SYN_OLD, "level": 1},
]
st.save["petsData"]["pet_bag"] = [{"id": "syn_2", "pet_id": "syn", "name": SYN_OLD, "level": 1}]
st.meta["mails"] = [{
    "id": "m1", "title": "宠物奖励",
    "attachments": [{"type": "pet", "name": SYN_OLD}],
}]
st.flush(force=True)

again = core.State("rename_case")
bag_names = [p.get("name") for p in again.save["petsData"].get("pet_bag") or []]
mail_name = again.meta["mails"][0]["attachments"][0].get("name")
check("B1 宠物栏旧名 -> 新名", again.save["petsData"]["pets"][0]["name"] == SYN_NEW,
      again.save["petsData"]["pets"][0]["name"])
check("B2 宠物袋旧名 -> 新名", bag_names == [SYN_NEW], str(bag_names))
check("B3 邮件附件旧名 -> 新名", mail_name == SYN_NEW, str(mail_name))

st2 = core.State("rename_case")
check("B4 重复读档幂等（不会二次改名 / 回退）",
      st2.save["petsData"]["pets"][0]["name"] == SYN_NEW, st2.save["petsData"]["pets"][0]["name"])

untouched = core.State("rename_other")
untouched.save.setdefault("petsData", {})["pets"] = [{"id": "x", "name": "苦行龟"}]
untouched.flush(force=True)
check("B5 没在改名表里的宠物名原样保留",
      core.State("rename_other").save["petsData"]["pets"][0]["name"] == "苦行龟")

# 上传链路：客户端内存里还是旧名时整包上传，服务端要就地归一，不能把旧名写回磁盘
import handlers_account as ha  # noqa: E402

core._ITEM_RENAME = dict(core.item_rename_map())
core._ITEM_RENAME[md5_8(SYN_OLD)] = SYN_NEW   # 同一对合成名，复用到道具维度
st_up = core.State("rename_upload")
ha.save_upload(core.Ctx(st_up, {"saveData": {
    "petsData": {"pets": [{"id": "u1", "pet_id": "syn", "name": SYN_OLD}]},
    "inventoryData": {"items": [{"name": SYN_OLD, "count": 2}]},
}}, {}, {"username": st_up.username}, {}))
up = core.State("rename_upload")
check("B6 上传包里的旧宠物名被就地归一",
      up.save["petsData"]["pets"][0]["name"] == SYN_NEW,
      str(up.save["petsData"]["pets"][0].get("name")))
rows = (up.save.get("inventoryData") or {}).get("items") or []
check("B7 上传包里的旧道具名被就地归一",
      rows and rows[0].get("name") == SYN_NEW, str(rows[:1]))

# --------------------------------------------------------------------------
print("\n== C. 客户端 / 服务端同一份改名表 ==")
src = GAME_DATA.read_text(encoding="utf-8")
m = re.search(r"const PET_NAME_MIGRATION: = \{(.*?)\n\}", src, re.S)
check("C1 客户端有 PET_NAME_MIGRATION 表", bool(m))
client_keys = set(re.findall(r'"([0-9a-f]{8})"\s*:', m.group(1))) if m else set()
check("C2 客户端键集合与服务端完全一致", client_keys == set(table),
      "客户端多 %s / 少 %s" % (sorted(client_keys - set(table))[:5], sorted(set(table) - client_keys)[:5]))

pet_gd = GAME_DATA.read_text(encoding="utf-8") + PET_DATA_GD.read_text(encoding="utf-8")
# 客户端图鉴只收了一部分宠物（九只赞助定制宠是服务端独有，靠存档里的 name 显示），
# 所以只校验「客户端确实有这个图鉴键」的那些名字跟服务端一致。
client_only = []
for key, row in pets.items():
    if '"%s"' % key in pet_gd:
        want = '"name": "%s"' % row.get("name", "")
        if want not in pet_gd:
            client_only.append((key, row.get("name")))
check("C3 客户端有的图鉴键，名字也改成新名", not client_only, str(client_only[:5]))
server_only = [k for k in table.values() if ('"name": "%s"' % k) not in pet_gd]
print("         （服务端独有、客户端无图鉴条目：%d 个，靠存档 name 显示）" % len(server_only))

# --------------------------------------------------------------------------
print("\n== D. 联动改名：随宠物改名的礼包 ==")
item_table = core.item_rename_map()
item_new = "7-9冥狼崽礼包"
hit = [k for k, v in item_table.items() if v == item_new]
check("D1 道具迁移表里有该礼包的旧名映射", bool(hit), str(hit[:3]))
check("D2 键是 8 位 md5", all(re.fullmatch(r"[0-9a-f]{8}", k) for k in hit), str(hit[:3]))
whitelist = json.load(open(ROOT / "data" / "client-item-names.json", encoding="utf-8"))["items"]
check("D3 客户端白名单有现用名", item_new in whitelist)
gifts = json.load(open(ROOT / "data" / "gift-rewards.json", encoding="utf-8"))
check("D4 服务端礼包奖励表用现用名", item_new in (gifts.get("pet_gifts") or {}),
      str(list((gifts.get("pet_gifts") or {}).keys())[:3]))
gd_has = ('"%s": {"name": "%s"' % (item_new, item_new)) in src
check("D5 客户端 ALL_ITEMS 里键与显示名都是现用名", gd_has)

# --------------------------------------------------------------------------
print("\n== E. 复核回退：7 个公有领域 / 低风险名恢复原版名 ==")
RESTORED = [
    ("阿努比斯", "冥狼神"),
    ("哪吒", "灵珠童子"),
    ("西门吹雪", "西陵吹雪"),
    ("阿格留斯", "幽冥狼"),
    ("小猪比特", "小猪贝特"),
    ("小猪安特", "小猪安泰"),
    ("超级阿努", "冥狼王"),
]
core._PET_RENAME = dict(table)
check("E1 每组都有「B2 新名 md5 -> 原版名」反向键",
      all(table.get(md5_8(new)) == old for old, new in RESTORED),
      str([(new, table.get(md5_8(new))) for old, new in RESTORED if table.get(md5_8(new)) != old]))
check("E2 前向键仍保留且指向原版名（读档幂等）",
      all(table.get(md5_8(old)) == old for old, new in RESTORED))
check("E3 迁移函数能把 B2 新名回退成原版名",
      all(core.migrate_pet_name(new) == old for old, new in RESTORED))
check("E4 原版名再次读档不会被改走",
      all(core.migrate_pet_name(old) == old for old, new in RESTORED))
check("E5 宠物目录用原版名、不再残留这 7 个 B2 新名",
      all(old in names and new not in names for old, new in RESTORED))
check("E6 两端表都是 24 条、键集合一致",
      len(table) == 24 and len(client_keys) == 24 and client_keys == set(table))
KEPT = ["冥狼崽", "齐天宝", "八岐炎君", "火舞姬", "白刃机娘",
        "九尾炎狐", "星塔智灵", "青柳仙尊", "绝情大帝", "六道影忍"]
check("E7 其余 10 个现代 IP / 商标谐音名仍保持 B2 新名",
      all(k in names for k in KEPT), str([k for k in KEPT if k not in names]))

print("\n结果: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
