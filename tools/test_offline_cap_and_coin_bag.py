#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-07 两项线上修复的回归测试。

  A. 离线时间 12 小时硬上限：超出部分不计入，/offline/info 预览与
     /economy/offline-claim 实发必须同口径，且不补领、不滚存。
  B. 钱币袋掉率从 13% 降到 5%：原版 rules.drop_rates 里根本没有这个道具，
     客户端本地表却残留 drop_rate=0.13，线上一个号攒到 12 万个 / 121 格 999。
     服务端 /economy/drop-config 下发显式覆盖值，客户端无需装热更包。
  F. 历史存量等值兑换：超过 999 个的钱币袋按 10 万金币/个 入账并清空，
     下载/上传两条存档链路收敛，旧快照不会把钱袋写回来、金币不双发。

同时守住：data/drop-config.json 本体仍与原版逐项一致（parity 不被破坏）。

    python3 tools/test_offline_cap_and_coin_bag.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="ly_offline_cap_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_account as ha  # noqa: E402
import handlers_growth as hg  # noqa: E402

PASS = 0
FAIL = 0
CAP_12H = 12 * 3600

ORIG_DROP = {
    "醉剑": 0.01, "神的遗愿": 0.01, "风歌夜曲": 0.01, "摆烂松": 0.01,
    "人间大炮": 0.01, "我的理想": 0.01, "板砖": 0.01, "立羽": 0.01,
    "铭大大": 0.01, "长空秋雁": 0.01, "大橘大利": 0.01, "人间事": 0.01,
    "思念": 0.01, "灵草": 0.0005, "灵芝": 0.0005, "天山雪莲": 0.0002,
    "九转还魂草": 0.0001, "龙涎草": 0.0001,
}


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (name, detail))


def call(fn, st, body=None):
    return fn(core.Ctx(st, body or {}, {}, {"username": st.username}, {}))


def new_state(tag: str, offline_seconds: int = 100 * 3600, vip: int = 0):
    st = core.State("cap_" + tag)
    st.meta["nickname"] = "测试角色" + tag
    st.meta["offlinePending"] = {"seconds": offline_seconds}
    p = st.player
    p["gold"] = 0
    p["level"] = 10
    p["catchup_enabled"] = False
    p["double_exp_auto_realm_enabled"] = False
    if vip > 0:
        p["is_vip"] = True
        p["vip_level"] = vip
        p["vip_expire_time_str"] = "2099-12-31 23:59:59"
    # 零收益图：模拟路径会立刻 break 走兜底，四个 VIP 的用例不会跑几十万场
    st.save.setdefault("gameSettings", {})["current_map"] = "彩虹桥（7,15）"
    return st


print("== A. 12 小时硬上限（纯口径函数）==")
cfg = hg._offline_cfg()
check("A1 runtime-config 里 maxHours=12", float(cfg.get("maxHours", 0)) == 12.0, str(cfg.get("maxHours")))
check("A2 非 VIP 仍是原版 8h", hg._offline_cap_seconds(cfg, 0) == 8 * 3600,
      str(hg._offline_cap_seconds(cfg, 0)))
for vip in (1, 2, 3):
    got = hg._offline_cap_seconds(cfg, vip)
    check("A3 VIP%s 被压到 12h（原表 12/16/24）" % vip, got == CAP_12H, str(got))
check("A4 全部 VIP 都不超过 12h",
      all(hg._offline_cap_seconds(cfg, v) <= CAP_12H for v in range(4)))
tight = dict(cfg, maxHours=6)
check("A5 上限可调：改成 6h 后所有 VIP 都 <= 6h",
      all(hg._offline_cap_seconds(tight, v) <= 6 * 3600 for v in range(4)),
      str([hg._offline_cap_seconds(tight, v) for v in range(4)]))
loose = dict(cfg, maxHours=24)
check("A6 放开上限后黄金 VIP 恢复 24h（参数确实生效）",
      hg._offline_cap_seconds(loose, 3) == 24 * 3600,
      str(hg._offline_cap_seconds(loose, 3)))

print("\n== B. /economy/drop-config 下发钱币袋 5% ==")
resp = hg.drop_config(None)
rates = resp.get("dropRates") or {}
check("B1 下发的掉率表里有「钱币袋」", "钱币袋" in rates, str(list(rates)[:5]))
want_rate = float((hg.RULES.m("dropRateOverrides") or {}).get("钱币袋", -1))
check("B2 钱币袋掉率按用户口径降到 5%（原 13%）",
      want_rate == 0.05 and float(rates.get("钱币袋", -1)) == want_rate,
      "配置 %s / 下发 %s" % (want_rate, rates.get("钱币袋")))
check("B2b 5% 明显低于早期 13%", 0.0 < want_rate < 0.13, str(want_rate))
check("B3 原版 18 项逐项未被改动",
      {k: v for k, v in rates.items() if k != "钱币袋"} == ORIG_DROP,
      "多 %s / 缺 %s" % (sorted(set(rates) - set(ORIG_DROP) - {"钱币袋"}),
                        sorted(set(ORIG_DROP) - set(rates))))
on_disk = json.loads((ROOT / "data" / "drop-config.json").read_text(encoding="utf-8"))
check("B4 drop-config.json 本体仍是原版 18 项",
      on_disk.get("dropRates") == ORIG_DROP, str(on_disk.get("dropRates")))
check("B5 drop-config.json 本体没有被塞进钱币袋（parity 不破）",
      "钱币袋" not in (on_disk.get("dropRates") or {}))

overrides = hg.RULES.m("dropRateOverrides")
check("B6 runtime-config 有 dropRateOverrides 配置段", isinstance(overrides, dict), str(overrides))
orig_rate = overrides.get("钱币袋")
overrides["钱币袋"] = 0.01
try:
    tuned = (hg.drop_config(None).get("dropRates") or {}).get("钱币袋")
    check("B7 想恢复少量掉落只改配置即可（无需重打客户端包）",
          float(tuned) == 0.01, str(tuned))
finally:
    if orig_rate is None:
        overrides.pop("钱币袋", None)
    else:
        overrides["钱币袋"] = orig_rate
gameplay = hg.gameplay_config(None)
check("B8 玩法配置不重复下发 drop_rate_overrides",
      "drop_rate_overrides" not in gameplay and "dropRateOverrides" not in gameplay)

print("\n== C. /offline/info 预览口径 ==")
for vip in (0, 3):
    st = new_state("info%s" % vip, vip=vip)
    info = call(ha.offline_info, st)
    eff = int(info.get("effectiveSeconds", -1))
    expect = hg._offline_cap_seconds(cfg, st.vip_tier())
    check("C%s effectiveSeconds 与 12h 上限一致" % vip, eff == expect,
          "%s vs %s" % (eff, expect))
    check("C%s 超出的时长被记为不计入" % vip,
          int(info.get("unusedOfflineSeconds", -1)) == 100 * 3600 - expect,
          str(info.get("unusedOfflineSeconds")))

print("\n== D. /economy/offline-claim 实发口径 ==")
for vip in (0, 3):
    st = new_state("claim%s" % vip, vip=vip)
    expect = hg._offline_cap_seconds(cfg, st.vip_tier())
    out = call(hg.offline_claim, st, {"operationId": "cap-test-%s" % vip})
    eff = int(out.get("effectiveSeconds", -1))
    check("D%s 实发 effectiveSeconds = 上限" % vip, eff == expect,
          "%s vs %s" % (eff, expect))
    check("D%s 满了 12h 上限，没有按 100h 发" % vip, eff <= CAP_12H, str(eff))
    check("D%s 回包标记 offlineCapped" % vip, bool(out.get("offlineCapped")) is True,
          str(out.get("offlineCapped")))
    check("D%s unusedOfflineSeconds 对得上" % vip,
          int(out.get("unusedOfflineSeconds", -1)) == 100 * 3600 - eff,
          str(out.get("unusedOfflineSeconds")))
    check("D%s 回包带 maxOfflineSeconds 供对账" % vip,
          int(out.get("maxOfflineSeconds", -1)) == expect,
          str(out.get("maxOfflineSeconds")))
    # 超出部分不滚存：offlinePending 已被消费，第二次同登录不能补领
    again = call(hg.offline_claim, st, {"operationId": "cap-test2-%s" % vip})
    check("D%s 同一次登录再领不会补发被砍掉的时长" % vip,
          int(again.get("exp", 0) or 0) == 0 or bool(again.get("duplicate")) is True,
          str({k: again.get(k) for k in ("exp", "gold", "duplicate")}))

print("\n== E. 短离线不受影响 ==")
st = new_state("short", offline_seconds=2 * 3600)
out = call(hg.offline_claim, st, {"operationId": "cap-test-short"})
check("E1 2 小时离线按原时长结算（不被误砍）",
      int(out.get("effectiveSeconds", -1)) == 2 * 3600, str(out.get("effectiveSeconds")))
check("E2 短离线不标记 capped", not bool(out.get("offlineCapped")),
      str(out.get("offlineCapped")))

print("\n== F. 历史钱币袋等值兑换（10 万金币/个）==")


def make_save(bag_counts, gold=1000, warehouse_bags=500):
    return {
        "playerData": {"gold": gold},
        "inventoryData": {
            "items": ([{"id": "b%d" % i, "name": "钱币袋", "count": c, "item_type": "gift"}
                       for i, c in enumerate(bag_counts)]
                      + [{"id": "keep", "name": "悟道仙丹", "count": 2, "item_type": "material"}]),
            "warehouse_items": ([{"id": "w1", "name": "钱币袋", "count": warehouse_bags,
                                  "item_type": "gift"}] if warehouse_bags else []),
        },
    }


def bag_total(save_dict):
    inv = save_dict.get("inventoryData") or {}
    total = 0
    for key in ("items", "warehouse_items"):
        for row in (inv.get(key) or []):
            if isinstance(row, dict) and str(row.get("name", "")) == "钱币袋":
                total += int(row.get("count", 0) or 0)
    return total


small = make_save([3, 4], warehouse_bags=500)
s_bags, s_gold = ha._convert_coin_bags_in_save(small)
check("F1 阈值（999）以下不兑换", (s_bags, s_gold) == (0, 0) and bag_total(small) == 507,
      "%s %s" % (s_bags, bag_total(small)))
check("F2 少量持有不影响金币", int(small["playerData"]["gold"]) == 1000)

big = make_save([999] * 120 + [520], warehouse_bags=500)
expect_bags = 999 * 120 + 520 + 500
expect_gold = expect_bags * ha.COIN_BAG_UNIT_GOLD
b_bags, b_gold = ha._convert_coin_bags_in_save(big)
check("F3 超量全部兑换（含仓库）", b_bags == expect_bags and b_gold == expect_gold,
      "%s vs %s" % (b_bags, expect_bags))
check("F4 背包和仓库里都不再有钱币袋", bag_total(big) == 0, str(bag_total(big)))
check("F5 其它道具原样保留",
      any(str(x.get("name")) == "悟道仙丹" for x in big["inventoryData"]["items"]))
check("F6 金币等值入账", int(big["playerData"]["gold"]) == 1000 + expect_gold,
      str(big["playerData"]["gold"]))
check("F7 兑换幂等（再跑一次不会再发钱）",
      ha._convert_coin_bags_in_save(big) == (0, 0))

st = new_state("dl", offline_seconds=60)
st.save.clear()
st.save.update(make_save([999] * 3, gold=1000, warehouse_bags=0))
st.player["name"] = "测试角色dl"
before_bags = bag_total(st.save)
dl = call(ha.save_download, st)
check("F8 /save/download 回包带兑换数量", int(dl.get("coinBagsConverted", -1)) == before_bags,
      str(dl.get("coinBagsConverted")))
check("F9 /save/download 回包带入账金币",
      int(dl.get("coinBagGoldGranted", -1)) == before_bags * ha.COIN_BAG_UNIT_GOLD)
check("F10 回包 saveData 里背包已清空", bag_total(dl.get("saveData") or {}) == 0)
check("F11 服务端金币已落库", st.gold() == 1000 + before_bags * ha.COIN_BAG_UNIT_GOLD,
      str(st.gold()))
check("F12 meta 记录本次兑换",
      int((st.meta.get("coinBagAutoConvert") or {}).get("count", 0)) == before_bags)

# 旧快照抢先上传：不能把 14 万个袋子写回来，也不能把金币发两遍
st2 = new_state("ul", offline_seconds=60)
st2.save.clear()
st2.save.update(make_save([], gold=1000, warehouse_bags=0))
st2.player["name"] = "测试角色ul"
old_snapshot = make_save([999] * 5, gold=1000, warehouse_bags=0)
call(ha.save_upload, st2, {"saveData": old_snapshot})
check("F13 旧快照上传后服务端背包仍然干净", bag_total(st2.save) == 0, str(bag_total(st2.save)))
check("F14 旧快照上传只发一次金币（整包覆盖不叠加）",
      st2.gold() == 1000 + 999 * 5 * ha.COIN_BAG_UNIT_GOLD, str(st2.gold()))

# download 兑换过之后，再一次旧快照上传也不能把金币翻倍
st3 = new_state("mix", offline_seconds=60)
st3.save.clear()
st3.save.update(make_save([999] * 4, gold=1000, warehouse_bags=0))
st3.player["name"] = "测试角色mix"
call(ha.save_download, st3)
first_gold = st3.gold()
call(ha.save_upload, st3, {"saveData": make_save([999] * 4, gold=1000, warehouse_bags=0)})
check("F15 download→旧 upload 组合不双发金币", st3.gold() == first_gold,
      "%s vs %s" % (st3.gold(), first_gold))

print("\n== G. 清理完成后旧快照幂等（防重复发金币）==")
st4 = new_state("idem", offline_seconds=60)
st4.save.clear()
st4.save.update(make_save([999] * 4, gold=1000, warehouse_bags=0))
st4.player["name"] = "测试角色idem"
call(ha.save_download, st4)
once_gold = st4.gold()
for _ in range(3):
    call(ha.save_upload, st4, {"saveData": make_save([999] * 4, gold=1000, warehouse_bags=0)})
check("G1 反复上传旧快照，金币只发一次", st4.gold() == once_gold,
      "%s vs %s" % (st4.gold(), once_gold))
check("G2 反复上传旧快照，背包仍为空", bag_total(st4.save) == 0, str(bag_total(st4.save)))
check("G3 旧快照被丢弃的数量有据可查",
      int(st4.meta.get("coinBagStaleDropped", 0)) == 3 * 999 * 4,
      str(st4.meta.get("coinBagStaleDropped")))
st5 = new_state("fresh", offline_seconds=60)
st5.save.clear()
st5.save.update(make_save([], gold=5000, warehouse_bags=0))
st5.meta["coinBagPurgeDone"] = True
call(ha.save_upload, st5, {"saveData": make_save([3, 4], gold=5000, warehouse_bags=0)})
check("G4 清理后新掉的少量钱袋正常入账", bag_total(st5.save) == 7, str(bag_total(st5.save)))
check("G5 少量新钱袋不会触发兑换", int(st5.player["gold"]) == 5000)

print("\n结果: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
