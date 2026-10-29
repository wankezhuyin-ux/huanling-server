#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""野外结算：人物阵亡不给人物经验（原版口径）的回归测试。

场景：人物被打死、灵兽独战把怪清完 → 战斗仍算胜利（victory=True），
金币/掉落照常，但人物不涨经验；宠物经验照发。

守三件事：
  1. 服务端开战记录的 playerAlive=False → playerExpGranted 必须是 0
  2. 同一场如果人物活着 → playerExpGranted 正常 > 0（不能一刀切）
  3. 拿不到服务端记录时（旧 token）客户端上报 playerDied=true 也能兜住

    python3 tools/test_battle_loot_player_death.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lylootdeath_"))

import handlers_battle as hb  # noqa: E402  （import 即注册路由）

MAP_ID = "徘徊林（0,0）"
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


def make_state(account: str) -> core.State:
    st = core.State(account)
    st.player.update({"level": 120, "experience": 0, "cultivation": 0})
    return st


def run_loot(account: str, *, player_alive, client_player_died=None):
    """跑一场固定内容的野外结算，返回回包。"""
    st = make_state(account)
    token = "tok_" + account
    pending = {
        "kind": "wild",
        "mapId": MAP_ID,
        "victory": True,
        "monsters": [
            {"level": 150, "exp_reward": 150 * 90, "gold_reward": 300},
            {"level": 150, "exp_reward": 150 * 90, "gold_reward": 300},
        ],
    }
    if player_alive is not None:
        pending["playerAlive"] = player_alive
    st.meta.setdefault("battles", {})[token] = pending

    body = {"fightToken": token}
    if client_player_died is not None:
        body["playerDied"] = client_player_died
    ctx = core.Ctx(st, body, {}, {}, {})
    out = hb.battle_loot(ctx)
    out["_state"] = st
    return out


def main() -> int:
    print("① 人物阵亡（服务端记录 playerAlive=False）")
    out = run_loot("probe_died", player_alive=False)
    check("victory 仍为 True", out.get("victory") is True, str(out.get("victory")))
    check("playerExpGranted = 0", int(out.get("playerExpGranted", -1)) == 0,
          str(out.get("playerExpGranted")))
    check("expGained(旧字段) = 0", int(out.get("expGained", -1)) == 0,
          str(out.get("expGained")))
    check("petExpGranted 照发 > 0", int(out.get("petExpGranted", 0)) > 0,
          str(out.get("petExpGranted")))
    check("金币照发 > 0", int(out.get("goldGranted", 0)) > 0, str(out.get("goldGranted")))
    check("playerDied 标记回传", out.get("playerDied") is True, str(out.get("playerDied")))
    check("人物经验没落库", int(out["_state"].player.get("experience", 0)) == 0,
          str(out["_state"].player.get("experience")))

    print("② 人物存活（playerAlive=True）")
    out = run_loot("probe_alive", player_alive=True)
    check("playerExpGranted > 0", int(out.get("playerExpGranted", 0)) > 0,
          str(out.get("playerExpGranted")))
    check("playerDied 为 False", out.get("playerDied") is False, str(out.get("playerDied")))
    check("人物经验已落库（升级或加经验）",
          int(out["_state"].player.get("level", 0)) > 120
          or int(out["_state"].player.get("experience", 0)) > 0,
          "level=%s exp=%s" % (out["_state"].player.get("level"),
                               out["_state"].player.get("experience")))

    print("③ 旧 token 无 playerAlive，客户端上报 playerDied=true 兜底")
    out = run_loot("probe_legacy", player_alive=None, client_player_died=True)
    check("playerExpGranted = 0", int(out.get("playerExpGranted", -1)) == 0,
          str(out.get("playerExpGranted")))

    print("④ 旧 token 无 playerAlive，客户端也没报 → 不误伤")
    out = run_loot("probe_none", player_alive=None, client_player_died=None)
    check("playerExpGranted > 0", int(out.get("playerExpGranted", 0)) > 0,
          str(out.get("playerExpGranted")))

    print("⑤ 服务端记录活着、客户端误报 playerDied=true → 以服务端为准，照样给经验")
    out = run_loot("probe_conflict", player_alive=True, client_player_died=True)
    check("playerExpGranted > 0", int(out.get("playerExpGranted", 0)) > 0,
          str(out.get("playerExpGranted")))

    print("\n结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
