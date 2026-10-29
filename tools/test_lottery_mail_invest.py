#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽奖占位奖励展开 + 灵兽投入图鉴 + 存量邮件修复 的回归测试。

守三件事（都是玩家实际踩到的）：

  1. `/lottery/draw` 抽到 `item_pool`（普通技能书之一 / 稀有技能书之一 /
     极限装备之一 / 五行属性宠物蛋）时必须展开成**具体道具**再进邮件，
     否则客户端点「打开」弹「附件添加失败：未知物品：普通技能书之一」。
  2. `/pet/collection/invest` 必须真的把宠物从服务端存档里吃掉并记进
     `meta.consumedPetIds`，否则 `/save/upload` 的 petsData 并集会把旧宠物
     塞回去（喂一只、重登一次它就回到列表）。
  3. `tools/fix_mail_placeholder_items.py` 能把已经发出去的坏附件换掉。

    python3 tools/test_lottery_mail_invest.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="lylottery_"))
core.STATE_DIR = TMP

import handlers_growth as hg  # noqa: E402  （import 即注册路由）
import handlers_account as ha  # noqa: E402

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


def make_state(name: str, seed: dict) -> core.State:
    """造号后强制注入测试存档。

    不能直接把 seed 传给构造器 —— 新号会走 `_apply_registration_gift()`，
    把 petsData 换成注册礼包那只苦行龟，金币/元宝也会被重置。
    """
    st = core.State(name)
    st.save.setdefault("playerData", {}).update(seed["playerData"])
    st.save["petsData"] = json.loads(json.dumps(seed["petsData"]))
    st.save["petCollectionData"] = json.loads(json.dumps(seed["petCollectionData"]))
    st.bump()
    st.flush()
    return st


def base_seed() -> dict:
    return {
        "playerData": {"name": "测试道友", "level": 120, "gold": 10 ** 13,
                       "yuanbao_currency": 1_000_000},
        "petsData": {
            "active_pet_id": "linlin_111",
            "pets": [{"id": "linlin_111", "pet_id": "linlin", "name": "琳琳",
                      "level": 1, "total_growth": 6.0, "mutation_count": 0},
                     {"id": "nawahuo_222", "pet_id": "nawahuo", "name": "纳瓦霍",
                      "level": 1, "total_growth": 3.8, "mutation_count": 0}],
            "pet_bag": [{"id": "linlin_333", "pet_id": "linlin", "name": "琳琳",
                         "level": 1, "total_growth": 6.0, "mutation_count": 0}],
        },
        "petCollectionData": {"progress": {"linlin": 12}, "mutation_progress": {}},
    }


PLACEHOLDERS = set(hg.LOTTERY_ITEM_POOLS.keys()) | {
    "普通技能书之一", "稀有技能书之一", "极限装备之一", "五行属性宠物蛋"}


# ---------------------------------------------------------------- ① 抽奖展开
def test_lottery_expand() -> None:
    print("\n① 抽奖「之一」奖励展开")
    st = make_state("lottery_a", base_seed())
    seen: set[str] = set()
    bad: list[str] = []
    for _ in range(160):
        ctx = core.Ctx(st, {"pool": "gold", "times": 1}, {}, None, {})
        res = hg.lottery_draw(ctx)
        if not res.get("success"):
            bad.append("gold draw failed: %s" % res.get("message"))
            break
        for row in res.get("rewards", []):
            name = str(row.get("name", ""))
            seen.add(name)
            if name in PLACEHOLDERS:
                bad.append("gold 池返回占位名 %s" % name)
    check("金币池 160 抽没有占位名", not bad, "; ".join(bad[:3]))

    bad = []
    for _ in range(160):
        ctx = core.Ctx(st, {"pool": "yuanbao", "times": 1}, {}, None, {})
        res = hg.lottery_draw(ctx)
        if not res.get("success"):
            bad.append("yuanbao draw failed: %s" % res.get("message"))
            break
        for row in res.get("rewards", []):
            name = str(row.get("name", ""))
            if name in PLACEHOLDERS:
                bad.append("元宝池返回占位名 %s" % name)
    check("元宝池 160 抽没有占位名", not bad, "; ".join(bad[:3]))

    mails = st.meta.get("mails") or []
    check("抽奖发了邮件", len(mails) > 0, "mails=%d" % len(mails))
    mail_names = [str(it.get("name", "")) for m in mails
                  for it in ((m.get("attachments") or {}).get("items") or [])]
    check("邮件附件里没有占位名", not (set(mail_names) & PLACEHOLDERS),
          str(set(mail_names) & PLACEHOLDERS))
    # 160+160 抽里，技能书池（金 2% / 元宝 5%）几乎必出，确认确实展开成了技能书
    books = [n for n in mail_names if n.endswith("技能书")]
    check("技能书奖励已落成具体书名", len(books) > 0, "示例=%s" % str(books[:3]))
    check("抽到的具体书名都在客户端道具表里",
          (not CLIENT_ITEMS) or all(n in CLIENT_ITEMS for n in mail_names if n.endswith("技能书")),
          str([n for n in mail_names if n.endswith("技能书") and n not in CLIENT_ITEMS]))

    # 元宝必须真扣（服务端权威），并且回包带 newYuanbao 供客户端刷新
    ctx = core.Ctx(st, {"pool": "yuanbao", "times": 1}, {}, None, {})
    before = st.yuanbao()
    res = hg.lottery_draw(ctx)
    check("元宝池扣 100 元宝", st.yuanbao() == before - 100,
          "%s → %s" % (before, st.yuanbao()))
    check("回包带 newYuanbao", int(res.get("newYuanbao", -1)) == st.yuanbao(),
          str(res.get("newYuanbao")))


# ---------------------------------------------------------------- ② 图鉴投入
def test_collection_invest() -> None:
    print("\n② 灵兽投入图鉴（服务端真删 + 记消耗）")
    st = make_state("invest_a", base_seed())
    pet_ids = [str(p.get("id")) for p in st.pets] + [str(p.get("id")) for p in st.pet_bag]
    check("初始 3 只宠", len(pet_ids) == 3, str(pet_ids))

    ctx = core.Ctx(st, {"petInstanceId": "linlin_333", "operationId": "op-1"}, {}, None, {})
    res = hg.pet_collection_invest(ctx)
    check("投入返回成功", bool(res.get("success")), str(res.get("message")))

    remaining = [str(p.get("id")) for p in st.pets] + [str(p.get("id")) for p in st.pet_bag]
    check("宠物袋里那只被删掉", "linlin_333" not in remaining, str(remaining))
    check("图鉴进度 +1（12→13）", st.save["petCollectionData"]["progress"].get("linlin") == 13,
          str(st.save["petCollectionData"]["progress"]))
    check("记进 consumedPetIds", "linlin_333" in (st.meta.get("consumedPetIds") or []),
          str(st.meta.get("consumedPetIds")))
    save_data = res.get("saveData") or {}
    check("回包带 saveData.petsData", isinstance(save_data.get("petsData"), dict))
    check("saveData 里已无该宠物",
          all(str(p.get("id")) != "linlin_333"
              for p in (save_data.get("petsData", {}).get("pet_bag") or [])))
    check("result 带上 old/new count",
          res.get("result", {}).get("oldCount") == 12 and res.get("result", {}).get("newCount") == 13,
          str(res.get("result")))

    # 幂等：同一个 operationId 重复提交不再 +1
    ctx2 = core.Ctx(st, {"petInstanceId": "linlin_333", "operationId": "op-1"}, {}, None, {})
    res2 = hg.pet_collection_invest(ctx2)
    check("重复提交走 duplicate", bool(res2.get("duplicate")), str(res2.get("duplicate")))
    check("重复提交图鉴不再 +1",
          st.save["petCollectionData"]["progress"].get("linlin") == 13,
          str(st.save["petCollectionData"]["progress"]))

    # 出战宠投入：active_pet_id 要清空（否则客户端会指向不存在的宠）
    ctx3 = core.Ctx(st, {"petInstanceId": "linlin_111", "operationId": "op-2"}, {}, None, {})
    res3 = hg.pet_collection_invest(ctx3)
    check("出战宠可以投入", bool(res3.get("success")), str(res3.get("message")))
    check("active_pet_id 已清空",
          not str(st.save.get("petsData", {}).get("active_pet_id", "") or "").strip(),
          str(st.save.get("petsData", {}).get("active_pet_id")))

    # 变异升华：普通图鉴满 50 且宠物 mutation_count>=1 → 只加变异层数
    seed = base_seed()
    seed["petCollectionData"]["progress"]["nawahuo"] = 50
    seed["petsData"]["pet_bag"] = [{"id": "nawahuo_999", "pet_id": "nawahuo",
                                    "name": "纳瓦霍", "level": 1,
                                    "total_growth": 5.0, "mutation_count": 1}]
    st2 = make_state("invest_b", seed)
    ctx4 = core.Ctx(st2, {"petInstanceId": "nawahuo_999", "operationId": "op-m"}, {}, None, {})
    res4 = hg.pet_collection_invest(ctx4)
    pc2 = st2.save["petCollectionData"]
    check("变异升华成功", str(res4.get("result", {}).get("investKind")) == "mutation",
          str(res4.get("result")))
    check("普通进度仍 50", pc2["progress"].get("nawahuo") == 50, str(pc2["progress"]))
    check("变异层数 +1", pc2["mutation_progress"].get("nawahuo") == 1, str(pc2["mutation_progress"]))

    # 防刷：服务端没有的 petId 一律拒绝（以前空桩是直接 +1，图鉴可以无限刷）
    before_prog = dict(st.save["petCollectionData"]["progress"])
    ctx_bad = core.Ctx(st, {"petInstanceId": "linlin_999999", "operationId": "op-fake"}, {}, None, {})
    res_bad = hg.pet_collection_invest(ctx_bad)
    check("伪造 petId 被拒绝", not res_bad.get("success"), str(res_bad.get("message")))
    check("伪造 petId 不会加图鉴进度",
          st.save["petCollectionData"]["progress"] == before_prog,
          str(st.save["petCollectionData"]["progress"]))
    check("伪造 petId 不会进消耗名单",
          "linlin_999999" not in (st.meta.get("consumedPetIds") or []),
          str(st.meta.get("consumedPetIds")))

    # 图鉴满 50 且宠物不是变异 → 拒绝，别白吃玩家一只宠
    seed_full = base_seed()
    seed_full["petCollectionData"]["progress"]["linlin"] = 50
    st3 = make_state("invest_c", seed_full)
    ctx_full = core.Ctx(st3, {"petInstanceId": "linlin_333", "operationId": "op-full"}, {}, None, {})
    res_full = hg.pet_collection_invest(ctx_full)
    check("满 50 非变异被拒绝", not res_full.get("success"), str(res_full.get("message")))
    check("被拒绝时宠物还在",
          "linlin_333" in [str(p.get("id")) for p in st3.pet_bag],
          str([p.get("id") for p in st3.pet_bag]))

    # 核心回归：客户端手里那份旧快照（还带着这只宠）再上传，不能把它塞回来
    old_snapshot = json.loads(json.dumps(base_seed()))
    old_snapshot["petCollectionData"]["progress"]["linlin"] = 13
    ha._merge_authoritative_progress(st.save, old_snapshot)
    st.drop_consumed_pets(old_snapshot)
    revived = [str(p.get("id")) for p in (old_snapshot["petsData"].get("pet_bag") or [])]
    check("旧快照上传不会复活已投入的宠", "linlin_333" not in revived, str(revived))
    check("旧快照里另一只宠还在（并集逻辑没被破坏）", "nawahuo_222" in
          [str(p.get("id")) for p in (old_snapshot["petsData"].get("pets") or [])],
          str([p.get("id") for p in old_snapshot["petsData"].get("pets", [])]))

    # 放生是同一类问题：服务端删了宠，但没记消耗名单的话，客户端旧快照上传
    # 会按 id 并集把这只宠又塞回服务端。
    ctx_rel = core.Ctx(st, {"petId": "nawahuo_222"}, {}, None, {})
    res_rel = hg.pet_release(ctx_rel)
    check("放生成功", bool(res_rel.get("success")), str(res_rel.get("message")))
    check("放生记进 consumedPetIds",
          "nawahuo_222" in (st.meta.get("consumedPetIds") or []),
          str(st.meta.get("consumedPetIds")))
    snap2 = json.loads(json.dumps(base_seed()))
    ha._merge_authoritative_progress(st.save, snap2)
    st.drop_consumed_pets(snap2)
    check("旧快照上传不会复活已放生的宠",
          "nawahuo_222" not in [str(p.get("id")) for p in (snap2["petsData"].get("pets") or [])],
          str([p.get("id") for p in snap2["petsData"].get("pets", [])]))


# ---------------------------------------------------------------- ③ 存量邮件修复
def test_mail_fix_tool() -> None:
    print("\n③ 存量坏邮件附件修复")
    sys.path.insert(0, str(ROOT / "tools"))
    import fix_mail_placeholder_items as fix  # noqa: E402

    tmp = Path(tempfile.mkdtemp(prefix="mailfix_"))
    try:
        accounts = tmp / "accounts"
        accounts.mkdir()
        payload = {
            "meta": {"mails": [
                {"id": 900024, "title": "幸运抽奖", "is_claimed": False,
                 "attachments": {"items": [{"name": "稀有技能书之一", "count": 1}]}},
                {"id": 900019, "title": "幸运抽奖", "is_claimed": False,
                 "attachments": {"items": [{"name": "普通技能书之一", "count": 1}]}},
                {"id": 900034, "title": "幸运抽奖", "is_claimed": False,
                 "attachments": {"items": [{"name": "五行属性宠物蛋", "count": 1}]}},
                {"id": 900001, "title": "欢迎", "is_claimed": True,
                 "attachments": {"items": [{"name": "7+可进化宠礼包", "count": 1}]}},
            ]},
            "save": {}, "meta2": {},
        }
        path = accounts / "unit.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        import random
        rng = random.Random(7)
        n, hits = fix.process_file(path, False, rng)
        check("预演不写盘", n == 1 and len(hits) == 3, "n=%s hits=%d" % (n, len(hits)))
        check("预演后文件仍是占位名",
              "普通技能书之一" in path.read_text(encoding="utf-8"))

        rng = random.Random(7)
        n, hits = fix.process_file(path, True, rng)
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        names = [it["name"] for m in data["meta"]["mails"]
                 for it in (m.get("attachments") or {}).get("items") or []]
        check("已写入具体道具", n == 1 and len(hits) == 3, "n=%s hits=%d" % (n, len(hits)))
        check("不再有占位名", not (set(names) & PLACEHOLDERS), str(names))
        check("没动过的正常附件原样保留", "7+可进化宠礼包" in names, str(names))
        check("替换后的名字都在客户端道具表里",
              (not CLIENT_ITEMS) or all(x in CLIENT_ITEMS for x in names if x != "7+可进化宠礼包"),
              str([x for x in names if x not in CLIENT_ITEMS]))
        check("写盘走了紧凑 JSON（体积不膨胀）", "\n" not in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 客户端道具表
def _load_client_items() -> set[str]:
    """从客户端 game_data.gd 抠出道具名，校验服务端发的东西客户端真认识。"""
    gd = Path("/Users/a1111/Documents/Codex/2026-10-21/n/outputs/recovered-godot-project/scripts/game_data.gd")
    if not gd.exists():
        return set()
    text = gd.read_text(encoding="utf-8", errors="replace")
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('"') and '": {' in line:
            key = line.split('": {')[0][1:]
            names.add(key)
    return names


CLIENT_ITEMS = _load_client_items()


def main() -> int:
    print("客户端道具表：%d 个" % len(CLIENT_ITEMS))
    test_lottery_expand()
    test_collection_invest()
    test_mail_fix_tool()
    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
