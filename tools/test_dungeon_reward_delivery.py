#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三副本奖励「发放途径 + 领取链路」端到端回归。

覆盖 2026-11-06 报障：
  · 套装副本通关/扫荡的封印套装发到邮箱后，玩家点领取弹
    「领取确认失败，封印套装未同步到背包」——服务端 /mail/finalize-claims
    只标 is_claimed，既不落装备、也不回 saveData；客户端对装备类附件
    偏偏要求回包带 saveData 且 inventoryData 里有对应行数。
  · 五行副本 /five-elements-dungeon/clear 把按难度分组的掉落表当列表遍历，
    `row.get` 直接 AttributeError → 接口 500，一次奖励都发不出来；
    且回包字段 items/rewards 与客户端认的 reward/mailSent/duplicate 全对不上。
  · 图鉴副本 /atlas-dungeon/finish 的捕捉结果必须随 saveData 回传，
    否则客户端 apply_cloud_save 拿不到图鉴进度，打完等于白打。

运行：python3 tools/test_dungeon_reward_delivery.py
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core
import handlers_extra as he
import handlers_growth as hg
import handlers_mail as hm
import handlers_play as hp

FAILS = []
TOTAL = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    TOTAL[0] += 1
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def fresh_state(name: str = "probe"):
    d = Path(tempfile.mkdtemp(prefix="lydungeon_"))
    (d / "accounts").mkdir()
    core.STATE_DIR = d
    st = core.State(name)
    st.meta["mails"] = []          # 去掉种子里的历史邮件
    st.player["suit_dungeon_progress"] = 5
    st.player["suit_dungeon_sweeps"] = 9
    st.player["suit_dungeon_sweeps_date"] = core.today()
    return st


def ctx(st, body=None):
    return core.Ctx(st, body or {}, {}, None, {})


def mails_with(st, word: str) -> list:
    return [m for m in st.meta.get("mails", []) if word in str(m.get("title", ""))]


def sealed_rows(st) -> list:
    return [r for r in st.items if r.get("item_type") == "sealed_suit"]


def claim_all(st) -> dict:
    out = hm.mail_claim_all(ctx(st))
    ids = [int(e.get("mail_id", 0)) for e in out.get("claimed", [])]
    return hm.mail_finalize_claims(ctx(st, {"mailIds": ids}))


# ---------------------------------------------------------------- A 套装副本扫荡
print("== A. 套装副本扫荡：掉落 → 邮件 → 领取 → 装备入背包 ==")
st = fresh_state("suit_sweep")
orig_roll = hp._suit_drop_roll
hp._suit_drop_roll = lambda state, level=1: [
    hp._suit_sealed_drop(level, "weapon"),
    hp._suit_sealed_drop(level, "helmet"),
    {"name": "普通解封石", "count": 1, "type": "stone"},
]
sweep = he.suit_sweep_reward(ctx(st, {"level": 3, "operationId": "sweep_op1"}))
hp._suit_drop_roll = orig_roll
check("A1 扫荡回包标记已发邮件", bool(sweep.get("suitDropMailSent")), str(sweep))
mail_rows = mails_with(st, "套装副本扫荡")
check("A2 生成一封扫荡掉落邮件", len(mail_rows) == 1, str(len(mail_rows)))
attach = mail_rows[0].get("attachments", {}) if mail_rows else {}
items = attach.get("items", [])
check("A3 附件含 2 件封印套装 + 1 解封石", len(items) == 3, json.dumps(items, ensure_ascii=False)[:200])
check("A4 封印套装带 type/data",
      all(i.get("type") == "sealed_suit" and isinstance(i.get("data"), dict)
          for i in items if "封印" in str(i.get("name", ""))),
      json.dumps(items, ensure_ascii=False)[:200])

res = claim_all(st)
sd = res.get("saveData") or {}
check("A5 finalize 回 saveData", bool(sd), str(list(res.keys())))
check("A6 saveData 只含 inventoryData 切片（不冲掉宠物/人物装备栏）",
      set(sd.keys()) == {"inventoryData"}, str(list(sd.keys())))
check("A7 inventoryData 里封印套装逐件独立成行", len(sealed_rows(st)) == 2,
      str(len(sealed_rows(st))))
check("A8 每行 count=1 且 custom_data 完整",
      all(r.get("count") == 1 and r.get("custom_data", {}).get("suit_level") == 3
          and r.get("custom_data", {}).get("slot") for r in sealed_rows(st)),
      json.dumps(sealed_rows(st), ensure_ascii=False)[:250])
check("A9 两件装备实例 id 不同（不会被客户端合并）",
      len({r["custom_data"].get("mailClaimId") for r in sealed_rows(st)}) == 2)
check("A10 同封邮件的解封石也落库",
      any(r.get("name") == "普通解封石" for r in st.items))
check("A11 邮件标记已领取", all(m.get("is_claimed") for m in mails_with(st, "套装副本扫荡")))
again = hm.mail_finalize_claims(ctx(st, {"mailIds": [mail_rows[0]["id"]]}))
check("A12 重复 finalize 幂等", again.get("updated") == 0 and again.get("duplicate") is True
      and len(sealed_rows(st)) == 2, str(again))

# ---------------------------------------------------------------- B 套装副本通关
print("== B. 套装副本通关：同一条邮件领取链路 ==")
st2 = fresh_state("suit_clear")
hp._suit_drop_roll = lambda state, level=1: [hp._suit_sealed_drop(level, "boots")]
clear = he.suit_clear_reward(ctx(st2, {"level": 4, "operationId": "clear_op1"}))
hp._suit_drop_roll = orig_roll
check("B1 通关回包标记已发邮件", bool(clear.get("suitDropMailSent")), str(clear)[:200])
check("B2 生成通关掉落邮件", len(mails_with(st2, "套装副本通关")) == 1)
res2 = claim_all(st2)
check("B3 通关装备同样落库并回 saveData",
      len(sealed_rows(st2)) == 1 and bool(res2.get("saveData")),
      str(len(sealed_rows(st2))))

# ---------------------------------------------------------------- C 普通邮件不被改写
print("== C. 纯道具邮件保持客户端本地入包流程 ==")
st3 = fresh_state("plain")
st3.send_mail("幸运抽奖", "", items=[{"name": "金属性宠物蛋", "count": 1}])
res3 = claim_all(st3)
check("C1 普通邮件不回 saveData", not res3.get("saveData"), str(list(res3.keys())))
check("C2 普通邮件不写服务端背包（避免与本地入包重复）",
      not any(r.get("name") == "金属性宠物蛋" for r in st3.items))
check("C3 标记已领取", st3.meta["mails"][0].get("is_claimed") is True)

# ---------------------------------------------------------------- D 成品装备 / 金币附件
print("== D. 成品套装装备与金币附件 ==")
st4 = fresh_state("direct_suit")
st4.send_mail("套装补偿", "", items=[{"name": "地脉套装·武器", "count": 1}])
res4 = claim_all(st4)
check("D1 成品装备邮件回 saveData 供领取确认", bool(res4.get("saveData")))
check("D2 成品装备不写服务端背包（客户端直接进人物装备栏）",
      not any("·" in str(r.get("name", "")) for r in st4.items))

st5 = fresh_state("gold_mail")
g0 = st5.gold()
st5.send_mail("仙魔周奖励", "", attachments={"gold": 12345,
                                             "items": [{"name": "悟道仙丹", "count": 1}]})
res5 = claim_all(st5)
check("D3 金币附件由服务端补发", st5.gold() - g0 == 12345, str(st5.gold() - g0))
check("D4 回包带 newGold 供客户端对齐钱包", res5.get("newGold") == st5.gold(), str(res5))

# ---------------------------------------------------------------- E 五行副本
print("== E. 五行副本通关奖励 ==")
st6 = fresh_state("fe")
random.seed(20261106)
seen_gold = seen_item = False
for idx, diff in enumerate(("简单", "困难", "地狱")):
    out = hp.fe_clear(ctx(st6, {"element": "金", "difficulty": diff,
                                "operationId": "fe_%d" % idx, "sessionId": "sess1"}))
    check("E%d %s 回包 success" % (idx + 1, diff), out.get("success") is True, str(out)[:160])
    reward = out.get("reward") or {}
    check("E%d.%s 回包有客户端认的 reward/mailSent/newGold" % (idx + 1, diff),
          "reward" in out and "mailSent" in out and "newGold" in out, str(list(out.keys())))
    gold = int(reward.get("gold", 0) or 0)
    if gold > 0:
        seen_gold = True
        check("E%d.%s 金币直发且 mailSent=false" % (idx + 1, diff),
              out.get("mailSent") is False and out.get("newGold") == st6.gold())
    else:
        seen_item = True
        check("E%d.%s 道具走邮件且 mailSent=true" % (idx + 1, diff),
              out.get("mailSent") is True and len(mails_with(st6, "五行副本通关奖励")) >= 1)
        name = str(reward.get("name", ""))
        check("E%d.%s 道具名不是占位族名" % (idx + 1, diff),
              name not in hp._FE_PLACEHOLDER_POOLS and hg.client_has_item(name), name)
check("E5 金币/道具两条途径都被覆盖", seen_gold and seen_item)
g_before, m_before = st6.gold(), len(st6.meta["mails"])
dup = hp.fe_clear(ctx(st6, {"element": "金", "difficulty": "简单",
                            "operationId": "fe_0", "sessionId": "sess1"}))
check("E6 重复结算幂等（不重复发金币/邮件）",
      dup.get("duplicate") is True and st6.gold() == g_before
      and len(st6.meta["mails"]) == m_before, str(dup)[:160])
check("E7 掉落表按难度分组且权重和为 1",
      all(abs(sum(float(r.get("rate", 0)) for r in hp._fe_reward_pool(d)) - 1.0) < 1e-6
          for d in ("简单", "困难", "地狱")))
bad_names = [n for d in ("简单", "困难", "地狱") for n in
             (str(r.get("name", "")) for r in hp._fe_reward_pool(d))
             if not n.endswith("金币") and n not in hp._FE_PLACEHOLDER_POOLS
             and not hg.client_has_item(n)]
check("E8 五行掉落表没有客户端不认识的道具名", not bad_names, str(bad_names))
check("E9 占位展开池里的名字客户端都认识",
      all(hg.client_has_item(n) for pool in hp._FE_PLACEHOLDER_POOLS.values() for n in pool))

# ---------------------------------------------------------------- F 图鉴副本
print("== F. 图鉴副本结算（捕捉直发存档，无需邮件领取） ==")
st7 = fresh_state("atlas")
species = json.load(open(Path(__file__).resolve().parents[1] / "data" / "pets.json"))["pets"]
ids = (list(species)[:4] if isinstance(species, dict)
       else [p.get("id") for p in species[:4]])
random.seed(11)
fin = hp.atlas_finish(ctx(st7, {"tier": "basic", "operationId": "atlas_op1",
                                "wavesCleared": 20, "defeatedPetIds": ids * 30,
                                "minGrowthThreshold": 99.0}))
check("F1 回包带 saveData", isinstance(fin.get("saveData"), dict))
check("F2 saveData 含 petsData / petCollectionData",
      "petsData" in fin["saveData"] and "petCollectionData" in fin["saveData"],
      str(list(fin["saveData"].keys())))
check("F3 summary 有真实捕捉数（不是全 0）",
      int(fin.get("summary", {}).get("successCount", 0)) > 0, str(fin.get("summary")))
check("F4 图鉴进度真的写进存档",
      sum((fin["saveData"].get("petCollectionData", {}).get("progress") or {}).values()) > 0)
check("F5 金币按波数发放", int(fin.get("goldGained", 0)) == 20 * 100000,
      str(fin.get("goldGained")))
dup_atlas = hp.atlas_finish(ctx(st7, {"tier": "basic", "operationId": "atlas_op1",
                                      "wavesCleared": 20, "defeatedPetIds": ids * 30}))
check("F6 重复结算幂等", dup_atlas.get("duplicate") is True)

# ---------------------------------------------------------------- G 客户端字段对齐
print("== G. 服务端回包字段与客户端 GD 脚本对齐（静态守卫） ==")
gd_root = Path(__file__).resolve().parents[3] / "recovered-godot-project"
gm_gd = (gd_root / "scripts" / "game_manager.gd").read_text(encoding="utf-8")
mail_gd = (gd_root / "scripts" / "mail" / "mail_manager.gd").read_text(encoding="utf-8")
api_gd = (gd_root / "scripts" / "mail" / "mail_api.gd").read_text(encoding="utf-8")
check("G1 客户端五行结算读 reward/mailSent/duplicate",
      all(k in gm_gd for k in ('d.get("reward"', 'd.get("mailSent"', 'd.get("duplicate"')))
check("G2 客户端 finalize 回包解析 saveData", 'data.get("saveData"' in api_gd)
check("G3 客户端领取确认校验 inventoryData 的封印套装行数",
      '_count_sealed_suits_in_save_data' in mail_gd and '"inventoryData"' in mail_gd)
check("G4 客户端图鉴结算读 saveData/summary",
      'data.get("saveData"' in gm_gd and 'data.get("summary"' in gm_gd)

print("\n结果：%d 项检查，失败 %d 项" % (TOTAL[0], len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS")
