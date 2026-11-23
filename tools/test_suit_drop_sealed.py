#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""套装副本「到底会不会掉套装」回归测试。

玩家报障（2026-11-04）：
  「检查一下套装副本产出套装的概率，实际会不会得到套装到邮箱」

排查结论：**一件都不会掉**。`handlers_play._suit_drop_roll` 里两种掉落都被
写死成「普通解封石」，注释理由是「客户端 ALL_ITEMS 里没有封印套装」——
但客户端 `mail_manager._add_attachments_to_game` 明确支持
`type == "sealed_suit"` 的邮件附件（带 data 或按 `<套名><部位>（封印）` 解析），
`inventory_manager` 也有 `add_item("sealed_suit", 1, data)` 的入口。

现在按玩家 2026-11-06 的口径实现（原版是每槽位 10%，玩家要求提速）：
  · 每场挑战/扫荡 100% 必出 1 件封印套装
  · 部位按层轮换、不重复：5 场正好凑齐该层一整套，集齐后自动开新一轮
  · 解封石仍独立按 5% 判定；1~10 层普通、11~21 层高级
  · 掉落一律走邮件（封印套装写服务端 items 会被 add_item 按名字合并成 count=2，
    客户端解析不出 data，等于白掉）

守这些事：
  A. 生成的封印套装名字/结构对齐客户端解析规则
  B. 每场必出 1 件套装（大样本），解封石仍是 5%
  C. 层数决定解封石等级
  D. 通关结算：套装进邮件附件，不进 st.items
  E. 扫荡结算：同上
  F. 幂等：同 operationId 不重复发邮件
  G. 部位不重复 / 5 场凑齐 / 集齐重置 / 按层分池 / 通关扫荡共用池 / 幂等不消耗

    python3 tools/test_suit_drop_sealed.py
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lysuitdrop_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_extra as hx  # noqa: E402
import handlers_play as hp  # noqa: E402

PASS = 0
FAIL = 0

SLOT_CN = {"weapon": "武器", "helmet": "头盔", "armor": "衣服",
           "boots": "鞋子", "accessory": "饰品"}


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def new_state(tag: str, progress: int = 0):
    st = core.State("suit_" + tag)
    st.save.setdefault("playerData", {})
    if progress:
        st.player["suit_dungeon_progress"] = progress
    st.player["gold"] = 0
    st.meta["mails"] = []
    st.flush(force=True)
    return st


def clear(st, level: int, op_id: str = "") -> dict:
    body = {"level": level}
    if op_id:
        body["operationId"] = op_id
    return hx.suit_clear_reward(core.Ctx(st, body, {}, None, {}))


def sweep(st, level: int, op_id: str = "") -> dict:
    body = {"level": level}
    if op_id:
        body["operationId"] = op_id
    return hx.suit_sweep_reward(core.Ctx(st, body, {}, None, {}))


def mail_items(st) -> list:
    rows = []
    for m in (st.meta.get("mails") or []):
        attach = m.get("attachments") or {}
        for row in (attach.get("items") or []):
            if isinstance(row, dict):
                rows.append(row)
    return rows


print("== A. 封印套装生成结构 ==")
db = hp._suit_db()
check("A1 套装库已加载（21 层）", len((db.get("suits") or {})) == 21,
      str(len(db.get("suits") or {})))
row = hp._suit_sealed_drop(1, "weapon")
check("A2 第1层武器能生成", isinstance(row, dict), str(row))
if isinstance(row, dict):
    check("A3 type 是客户端认的 sealed_suit", row.get("type") == "sealed_suit", str(row.get("type")))
    check("A4 名字格式 <套名><部位>（封印）", row.get("name") == "青云套装武器（封印）", str(row.get("name")))
    data = row.get("data") or {}
    check("A5 data 带 suit_level", int(data.get("suit_level", 0)) == 1, str(data))
    check("A6 data 带 slot", data.get("slot") == "weapon", str(data))
    check("A7 data 标了 is_sealed", data.get("is_sealed") is True, str(data))
    check("A8 data 带 base_stats（攻击 420）", int((data.get("base_stats") or {}).get("attack", 0)) == 420,
          str(data.get("base_stats")))
    check("A9 data 带五行（金）", data.get("element") == "金", str(data.get("element")))

print("== A2. 21 层 × 5 部位全部能生成 ==")
bad = []
for lv in range(1, 22):
    for slot in hp.SUIT_SLOTS:
        r = hp._suit_sealed_drop(lv, slot)
        if not isinstance(r, dict) or not str(r.get("name", "")).endswith("（封印）"):
            bad.append("%d/%s" % (lv, slot))
check("A10 105 种组合全部可生成", not bad, str(bad[:6]))

print("== A3. 名称能被客户端按名字解析 ==")
r21 = hp._suit_sealed_drop(21, "accessory")
check("A11 第21层饰品名 = 无上套装饰品（封印）",
      str(r21.get("name")) == "无上套装饰品（封印）", str(r21.get("name")))
check("A12 部位名在客户端的 SLOT_NAMES 里",
      str(r21.get("data", {}).get("slot")) in SLOT_CN,
      str(r21.get("data", {}).get("slot")))

print("== B. 保底：每场必出 1 件套装；解封石仍 5%（20000 场抽样）==")
random.seed(20261104)
trials = 20000
suits = 0
stones = 0
runs_with_suit = 0
runs_with_multi_suit = 0
for _ in range(trials):
    drops = hp._suit_drop_roll(None, 3)   # state=None：不做轮次去重，只验保底
    n_suit = sum(1 for d in drops if d.get("type") == "sealed_suit")
    suits += n_suit
    stones += sum(1 for d in drops if d.get("type") == "stone")
    if n_suit:
        runs_with_suit += 1
    if n_suit > 1:
        runs_with_multi_suit += 1
print("      每场套装 %.4f 件 / 解封石 %.4f 颗 / 出套装场次 %d"
      % (suits / trials, stones / trials, runs_with_suit))
check("B1 场场必出套装（100% 保底）", runs_with_suit == trials,
      "%d/%d" % (runs_with_suit, trials))
check("B2 每场恰好 1 件套装", suits == trials and runs_with_multi_suit == 0,
      "%d 件 / %d 场" % (suits, trials))
check("B3 解封石每场 ≈0.05 颗（独立判定，不受保底影响）",
      abs(stones / trials - 0.05) < 0.008, "%.4f" % (stones / trials))
check("B4 套装仍然是客户端认的 sealed_suit 结构",
      all(d.get("type") == "sealed_suit" and isinstance(d.get("data"), dict)
          for d in hp._suit_drop_roll(None, 3) if "封印" in str(d.get("name", ""))))

print("== C. 解封石等级跟层数走 ==")
check("C1 第1层 → 普通解封石", hp._suit_stone_name(1) == "普通解封石", hp._suit_stone_name(1))
check("C2 第10层 → 普通解封石", hp._suit_stone_name(10) == "普通解封石", hp._suit_stone_name(10))
check("C3 第11层 → 高级解封石", hp._suit_stone_name(11) == "高级解封石", hp._suit_stone_name(11))
check("C4 第21层 → 高级解封石", hp._suit_stone_name(21) == "高级解封石", hp._suit_stone_name(21))
random.seed(7)
names = set()
for _ in range(400):
    for d in hp._suit_drop_roll(None, 15):
        if d.get("type") == "stone":
            names.add(str(d.get("name")))
check("C5 第15层只会掉高级解封石", names <= {"高级解封石"}, str(names))

print("== D. 通关结算：套装进邮件，不进服务端 items ==")
random.seed(11)
st = new_state("clear", progress=0)
# 现在是 100% 保底，首场必出；保留循环是为了兼容以后调回概率制
sealed_mail = None
for i in range(200):
    r = clear(st, 3, "op_c%d" % i)
    if not r.get("success"):
        break
    got = [d for d in (r.get("dropItems") or []) if d.get("type") == "sealed_suit"]
    if got:
        sealed_mail = got[0]
        break
check("D1 通关都能成功结算", r.get("success") is True, str(r)[:160])
check("D2 掉到了封印套装（200 次内）", sealed_mail is not None, "没掷到")
check("D3 回包带 suitDropMailSent", "suitDropMailSent" in r, str(list(r.keys())))
check("D4 服务端 items 里没有 sealed_suit 行（不能直接写）",
      not any(str(i.get("item_type", "")) == "sealed_suit" for i in st.items),
      json.dumps(st.items[-2:], ensure_ascii=False)[:200])
mi = mail_items(st)
check("D5 邮件附件里真的有封印套装", any(str(x.get("type")) == "sealed_suit" for x in mi),
      str(len(mi)))
check("D6 邮件附件里的套装带 data", all("data" in x for x in mi if x.get("type") == "sealed_suit"),
      json.dumps([x for x in mi if x.get("type") == "sealed_suit"][:1], ensure_ascii=False)[:240])
check("D7 第3层掉的是地脉套装",
      all(str(x.get("name", "")).startswith("地脉套装") for x in mi if x.get("type") == "sealed_suit"),
      str([x.get("name") for x in mi if x.get("type") == "sealed_suit"][:3]))

print("== E. 扫荡结算：同样走邮件 ==")
random.seed(23)
st2 = new_state("sweep", progress=5)
mails_before = len(st2.meta.get("mails") or [])
sweep_sealed = None
r2 = {}
for i in range(200):
    # 扫荡有每日次数上限，测试里每次重置，只验证「掉落 → 邮件」这条链路；
    # 现在首场必出套装，循环通常第一次就结束
    st2.player["suit_dungeon_sweeps"] = 99
    st2.player["suit_dungeon_sweeps_date"] = core.today()
    r2 = sweep(st2, 5, "op_s%d" % i)
    if not r2.get("success"):
        break
    got = [d for d in (r2.get("dropItems") or []) if d.get("type") == "sealed_suit"]
    if got:
        sweep_sealed = got[0]
        break
check("E1 扫荡掉到了封印套装（200 次内）", sweep_sealed is not None, "没掷到")
check("E2 扫荡回包带 suitDropMailSent", r2.get("suitDropMailSent") is True, str(r2.get("suitDropMailSent")))
check("E3 扫荡邮件比开始时多", len(st2.meta.get("mails") or []) > mails_before,
      "%d -> %d" % (mails_before, len(st2.meta.get("mails") or [])))
check("E4 扫荡掉落名带层数对应套名（金罡套装）",
      all(str(x.get("name", "")).startswith("金罡套装")
          for x in mail_items(st2) if x.get("type") == "sealed_suit"),
      str([x.get("name") for x in mail_items(st2) if x.get("type") == "sealed_suit"][:3]))

print("== F. 幂等：同 operationId 不重复发 ==")
st3 = new_state("idem", progress=2)
clear(st3, 2, "op_same")
n1 = len(st3.meta.get("mails") or [])
clear(st3, 2, "op_same")
n2 = len(st3.meta.get("mails") or [])
check("F1 重放不新增邮件", n1 == n2, "%d -> %d" % (n1, n2))

print("== G. 100% 保底 + 部位不重复（按层轮次池） ==")
random.seed(31)


def drop_slots(st, level: int, n: int, prefix: str, use_sweep: bool = False) -> list:
    out = []
    for i in range(n):
        if use_sweep:
            st.player["suit_dungeon_sweeps"] = 99
            st.player["suit_dungeon_sweeps_date"] = core.today()
            r = sweep(st, level, "%s%d" % (prefix, i))
        else:
            r = clear(st, level, "%s%d" % (prefix, i))
        got = [d for d in (r.get("dropItems") or []) if d.get("type") == "sealed_suit"]
        out.append(str((got[0].get("data") or {}).get("slot", "")) if got else "")
    return out


stg = new_state("cycle", progress=5)
cycle = stg.meta.get("suitDropCycle") or {}
first5 = drop_slots(stg, 3, 5, "cy_a")
check("G1 连续 5 场场场出 1 件套装", all(first5), str(first5))
check("G2 5 场正好凑齐 5 个不同部位（一件都不重复）",
      len(first5) == 5 and set(first5) == set(hp.SUIT_SLOTS), str(first5))
cycle = stg.meta.get("suitDropCycle") or {}
check("G3 轮次池落在 state.meta.suitDropCycle[层号]",
      len(cycle.get("3") or []) == 5, str({k: len(v) for k, v in cycle.items()}))

next2 = drop_slots(stg, 3, 2, "cy_b")
check("G4 集齐后自动开新一轮，第 6/7 场又是不同部位",
      len(cycle.get("3") or []) == 2 and next2[0] != next2[1], str(cycle.get("3")))

lv5 = drop_slots(stg, 5, 5, "cy_c")
lv5_rows = [x for x in mail_items(stg)
            if x.get("type") == "sealed_suit"
            and int((x.get("data") or {}).get("suit_level", 0)) == 5]
check("G5 换层有独立池：第 5 层同样 5 场凑齐，不受第 3 层影响",
      set(lv5) == set(hp.SUIT_SLOTS) and len(cycle.get("5") or []) == 5,
      str({k: len(v) for k, v in cycle.items()}))
check("G6 第 5 层掉的是金罡套装且 5 件齐全",
      len(lv5_rows) == 5 and all(str(x.get("name", "")).startswith("金罡套装")
                                for x in lv5_rows),
      str([x.get("name") for x in lv5_rows]))

stm = new_state("mix", progress=5)
mixed = drop_slots(stm, 4, 1, "mix_c")
mixed += drop_slots(stm, 4, 4, "mix_s", use_sweep=True)
check("G7 通关 + 扫荡混着打，前 5 场部位仍不重复",
      set(mixed) == set(hp.SUIT_SLOTS), str(mixed))

stp = new_state("idem2", progress=2)
clear(stp, 2, "dup")
pool_before = list((stp.meta.get("suitDropCycle") or {}).get("2") or [])
clear(stp, 2, "dup")
pool_after = list((stp.meta.get("suitDropCycle") or {}).get("2") or [])
check("G8 同一 operationId 重放不会多消耗一个部位",
      pool_before == pool_after, "%s -> %s" % (pool_before, pool_after))

rates = core.RULES.v("suit_dungeon", "dropRates", {}) or {}
check("G9 配置 sealedSuitRate=1.0 且 uniqueSlotCycle=true",
      float(rates.get("sealedSuitRate", 0)) >= 1.0 and rates.get("uniqueSlotCycle") is True,
      json.dumps(rates, ensure_ascii=False))
check("G10 descriptionText 已同步成保底口径（不再残留「每槽位 10%」）",
      "必出" in str(core.RULES.v("suit_dungeon", "descriptionText", ""))
      and "每槽位 10%" not in str(core.RULES.v("suit_dungeon", "descriptionText", "")),
      str(core.RULES.v("suit_dungeon", "descriptionText", "")))

print("\n通过 %d，失败 %d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
