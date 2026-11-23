#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仙魔战场：分阵营排行榜 / 每周重选 / 周结算 / 称号 的回归测试。

玩家报障（2026-10-29 三张截图）：
  1. 排行榜「仙道排行 / 魔道排行」是同一批人，积分全 0；
  2. 加入过一次之后永远是那个阵营（原版每周要重新选仙道/魔道）；
  3. 邮箱里「仙魔战场·天降祥瑞」的【神兽蛋】点领取报「未知物品」；
  4. 捕获灵兽有提示，但邮箱/背包里都收不到。

守这些事：
  A. `_xianmo_rank_rows("xian"/"mo")` 只出自己的阵营、按周积分降序、字段对齐原版抓包
  B. 跨周 `_xianmo()` 清阵营（逼玩家重选）、清周分、记 lastWeek
  C. `_xianmo_settle_due()` 判胜负、前 3 名发称号+宝箱+金币+仙丹、其余发参与奖、幂等
  D. 称号接口只返回自己拥有的、能激活/卸下、过期不下发
  E. 所有仙魔奖励道具名都在客户端 game_data.gd ALL_ITEMS 里（不再发「神兽蛋」）
  F. 捕获灵兽改为发「仙魔礼包」邮件，且礼包名客户端认识

    python3 tools/test_xianmo_week.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lyxmweek_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_play as hp  # noqa: E402

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


def settle_mails(st) -> list:
    return [m for m in (st.meta.get("mails") or [])
            if isinstance(m, dict) and "结算" in str(m.get("title", ""))]


def settle_gold(st) -> int:
    """结算邮件附件里的金币（2026-11-04 起金币走附件领取，不再直接入账）。"""
    for m in settle_mails(st):
        attach = m.get("attachments") or {}
        if isinstance(attach, dict):
            try:
                return int(attach.get("gold", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def settle_pills(st) -> int:
    for m in settle_mails(st):
        attach = m.get("attachments") or {}
        for row in (attach.get("items") or []) if isinstance(attach, dict) else []:
            if isinstance(row, dict) and str(row.get("name", "")) == "悟道仙丹":
                return int(row.get("count", 0) or 0)
    return 0


def make_player(username: str, nickname: str, faction: str, score: int,
                week: int, total: int = 0) -> object:
    st = core.State(username)
    st.player["name"] = nickname
    st.player["gold"] = 0
    st.meta["nickname"] = nickname
    box = st.meta.setdefault("xianmo", {})
    box.update({"faction": faction, "score": score, "week": week,
                "totalScore": total, "actionPoints": 3})
    st.items.clear()
    st.meta["mails"] = []
    st.flush(force=True)
    core.STORE.accounts.setdefault(username, {"userId": abs(hash(username)) % 900 + 1})
    core.STORE._cache[username] = st
    return st


def reload(username: str):
    core.STORE._cache.pop(username, None)
    return core.STORE.get(username)


def main() -> None:
    current = hp._xianmo_week_key()
    prev = current - 1

    print("== A. 分阵营排行榜 ==")
    make_player("p_xian1", "仙一", "xian", 120, current)
    make_player("p_xian2", "仙二", "xian", 60, current)
    make_player("p_mo1", "魔一", "mo", 200, current)
    make_player("p_none", "路人", "", 999, current)
    hp._XIANMO_RANK_CACHE["at"] = 0.0
    hp._XIANMO_RANK_CACHE["data"] = {}
    xian_rows = hp._xianmo_rank_rows("xian")
    mo_rows = hp._xianmo_rank_rows("mo")
    check("仙道榜只含仙道", [r["nickname"] for r in xian_rows] == ["仙一", "仙二"],
          str([r["nickname"] for r in xian_rows]))
    check("魔道榜只含魔道", [r["nickname"] for r in mo_rows] == ["魔一"],
          str([r["nickname"] for r in mo_rows]))
    check("按周积分降序", xian_rows[0]["weekly_score"] == 120)
    check("字段对齐原版",
          all(k in xian_rows[0] for k in
              ("user_id", "faction", "weekly_score", "total_score", "nickname")))
    check("未选阵营的人不混进来",
          all(r["username"] != "p_none" for r in xian_rows + mo_rows))

    print("== B. 跨周清阵营 ==")
    st = make_player("p_week", "周测", "xian", 88, prev)
    box = hp._xianmo(st)
    check("新赛季清空阵营", box.get("faction") == "", repr(box.get("faction")))
    check("周分归零", int(box.get("score", -1)) == 0)
    check("保住上周成绩", (box.get("lastWeek") or {}).get("score") == 88,
          str(box.get("lastWeek")))
    check("周号前进到本周", int(box.get("week", 0)) == current)

    print("== C. 周结算 ==")
    # B 组的 p_week 也是上周分数，会混进结算榜单，这里把它整个移出（含 lastWeek 快照）
    core.STORE._cache.pop("p_week", None)
    (core.STATE_DIR / "accounts" / "p_week.json").unlink(missing_ok=True)
    make_player("s_xian_top", "仙状元", "xian", 300, prev)
    make_player("s_xian_2", "仙榜眼", "xian", 50, prev)
    make_player("s_xian_3", "仙探花", "xian", 10, prev)
    make_player("s_xian_4", "仙陪跑", "xian", 5, prev)
    make_player("s_mo_top", "魔状元", "mo", 100, prev)
    make_player("s_idle", "只报名", "mo", 0, prev)
    try:
        (core.STATE_DIR / "xianmo-settle.json").unlink()
    except OSError:
        pass
    first = hp._xianmo_settle_due()
    check("结算出胜方=仙道", first.get("winner") == "xian", str(first))
    check("结算人数=6", first.get("players") == 6, str(first))

    top = reload("s_xian_top")
    tbox = top.meta["xianmo"]
    titles = {t["title_id"]: t for t in tbox.get("titleItems", [])}
    check("胜方第1名拿到「仙道至尊」", "xian_zhizun" in titles, str(list(titles)))
    check("称号加成 1000", int(titles.get("xian_zhizun", {}).get("attack_bonus", 0)) == 1000)
    check("称号 7 天有效期",
          int(titles.get("xian_zhizun", {}).get("expire_time_unix", 0)) > time.time() + 6 * 86400)
    check("胜方第1名金币 5 亿（邮件附件）", settle_gold(top) == 500_000_000,
          str(settle_gold(top)))
    check("胜方第1名黄金宝箱 x2",
          len([c for c in tbox.get("chests", []) if c.get("type") == "gold"]) == 2)
    check("胜方第1名悟道仙丹 x5（邮件附件）", settle_pills(top) == 5, str(settle_pills(top)))
    check("结算邮件已发", len(settle_mails(top)) == 1, str(len(settle_mails(top))))
    check("结算邮件真的有附件可领",
          bool((settle_mails(top)[0].get("attachments") or {}).get("gold")) and
          bool((settle_mails(top)[0].get("attachments") or {}).get("items")),
          json.dumps((settle_mails(top)[0].get("attachments") or {}), ensure_ascii=False))

    second = reload("s_xian_2")
    check("第2名是「仙道圣者」",
          any(t["title_id"] == "xian_shengzhe" for t in second.meta["xianmo"].get("titleItems", [])))
    check("胜方第2名金币 3 亿（邮件附件）", settle_gold(second) == 300_000_000,
          str(settle_gold(second)))

    runner = reload("s_xian_4")
    check("第4名发参与奖 1 亿（邮件附件）", settle_gold(runner) == 100_000_000,
          str(settle_gold(runner)))
    check("第4名无称号", not runner.meta["xianmo"].get("titleItems"))

    loser = reload("s_mo_top")
    ltitle = [t["title_id"] for t in loser.meta["xianmo"].get("titleItems", [])]
    check("败方第1名是「魔道先锋」", "mo_xianfeng" in ltitle, str(ltitle))
    check("败方第1名金币 3 亿（邮件附件）", settle_gold(loser) == 300_000_000,
          str(settle_gold(loser)))

    idle = reload("s_idle")
    check("只报名没积分不发奖", settle_gold(idle) == 0 and
          not idle.meta["xianmo"].get("titleItems"))

    again = hp._xianmo_settle_due()
    check("重复调用不再结算", again == {}, str(again))
    top2 = reload("s_xian_top")
    check("金币没有被重复发（结算邮件仍只有一封）", len(settle_mails(top2)) == 1,
          str(len(settle_mails(top2))))
    check("金币没有被重复发（附件金额不变）", settle_gold(top2) == 500_000_000,
          str(settle_gold(top2)))

    print("== D. 称号接口 ==")
    ctx = core.Ctx(top2, {}, {}, None, {})
    data = hp.xianmo_titles(ctx)
    check("只下发自己拥有的称号", len(data["titles"]) >= 1)
    check("未激活时 is_active=0",
          all(int(t.get("is_active", 0)) == 0 for t in data["titles"]))
    act = hp.xianmo_title_activate(core.Ctx(top2, {"titleId": "xian_zhizun"}, {}, None, {}))
    check("激活称号成功", bool(act.get("success")))
    check("激活写进 activeTitle", top2.meta["xianmo"].get("activeTitle") == "xian_zhizun")
    data2 = hp.xianmo_titles(core.Ctx(top2, {}, {}, None, {}))
    check("激活后 is_active=1",
          any(int(t.get("is_active", 0)) == 1 for t in data2["titles"]))
    de = hp.xianmo_title_deactivate(core.Ctx(top2, {}, {}, None, {}))
    check("卸下称号", bool(de.get("success")) and top2.meta["xianmo"].get("activeTitle") == "")
    bad = hp.xianmo_title_activate(core.Ctx(top2, {"titleId": "not_mine"}, {}, None, {}))
    check("不能激活没有的称号", not bad.get("success"))

    top2.meta["xianmo"]["titleItems"][0]["expire_time_unix"] = int(time.time()) - 10
    expired = hp.xianmo_titles(core.Ctx(top2, {}, {}, None, {}))
    check("过期称号不下发",
          all(t["title_id"] != "xian_zhizun" for t in expired["titles"]))

    print("== E. 奖励道具名全部是客户端认识的 ==")
    names = hp._client_item_names()
    # 阈值随客户端道具表走：ALL_ITEMS 现在是 276 项（2026-11-01 精简过一版），
    # 原来写死 280，道具表一精简这里就恒失败，跟奖励名对不对没关系。
    check("白名单已加载（≥250 项）", len(names) >= 250, str(len(names)))
    bad_pool = [n for n in hp._XIANMO_RANDOM_ITEM_POOL + hp._XIANMO_RARE_ITEM_POOL
                if n not in names]
    check("随机/稀有池无未知物品", not bad_pool, str(bad_pool))
    check("策划表里不再有「神兽蛋」", "神兽蛋" not in hp._XIANMO_RARE_ITEM_POOL)
    gifts = set(hp._XIANMO_PET_GIFT_BY_EVENT.values()) | {"仙魔灵兽礼包"}
    check("灵兽礼包名全部有效", all(g in names for g in gifts), str(gifts - names))
    check("药园秘境已改成真实道具",
          hp._xianmo_events()["medicine_garden"]["items"] == ["聚灵丹"],
          str(hp._xianmo_events()["medicine_garden"].get("items")))

    print("== F. 捕获灵兽发礼包邮件 ==")
    pet_st = core.State("pet_case")
    pet_st.meta["mails"] = []
    pet_st.player["gold"] = 0
    pet_st.meta["xianmo"] = {"faction": "xian", "score": 0, "week": current,
                             "totalScore": 0, "actionPoints": 5, "lastRecoverAt": int(time.time())}
    core.STORE._cache["pet_case"] = pet_st
    core.STORE.accounts.setdefault("pet_case", {"userId": 4242})
    ev = hp._xianmo_events()["spirit_beast_cub"]
    ev = dict(ev, no_capture_roll=True)   # 必捕获
    hp._XIANMO_EVENTS_CACHE = dict(hp._xianmo_events(), spirit_beast_cub=ev)
    hp._xianmo_pick_event = lambda: ("spirit_beast_cub", ev)
    ctx2 = core.Ctx(pet_st, {"points": 1, "operationId": "t1"}, {}, None, {})
    res = hp.xianmo_adventure(ctx2)
    check("探险成功", bool(res.get("success")), str(res.get("message")))
    mails = [m for m in pet_st.meta.get("mails", []) if "灵兽" in str(m.get("title", ""))]
    check("收到灵兽收获邮件", bool(mails), str([m.get("title") for m in pet_st.meta.get("mails", [])]))
    attach = (mails[0].get("attachments") or {}).get("items") if mails else []
    check("邮件附件是仙魔礼包", bool(attach) and attach[0]["name"].startswith("仙魔"),
          str(attach))
    check("礼包带成长值", bool(attach) and float(attach[0].get("growth", 0)) > 0,
          str(attach))
    check("礼包名客户端认识", bool(attach) and attach[0]["name"] in names, str(attach))
    check("不再往 pet_bag 塞无模板宠物",
          all(p.get("pet_id") != "xianmo_pet" for p in pet_st.pet_bag))

    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
