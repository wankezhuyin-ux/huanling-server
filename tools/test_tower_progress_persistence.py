#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无限天关「闯到多少层就一直保留」回归测试。

玩家报障（2026-11-05）：打到 200 层，退出天关再进来又从第 1 关开始。
线上实测 liu1 存档：tower_highest=211 / tower_floor=1 —— 服务端从来没存过当前层。

根因链：
  1. 客户端通关只调 /tower/update 上报 highestFloor；
  2. 旧服务端只写 tower_highest，不写 tower_floor；
  3. /tower/info 因此永远回 towerFloor=1；
  4. tower_ui.gd:_sync_tower_data 无条件用 server_floor 覆盖本地进度。

修法：tower_floor 变成服务端权威字段（存档上传不能覆盖），
/tower/update 收 currentFloor（旧客户端用「刷新历史最高」兼容推导）。

    python3 tools/test_tower_progress_persistence.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

core.STATE_DIR = Path(tempfile.mkdtemp(prefix="lytowerprog_"))
(core.STATE_DIR / "accounts").mkdir(parents=True, exist_ok=True)

import handlers_account as ha  # noqa: E402
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


def new_state(tag: str):
    st = core.State("tower_" + tag)
    st.save.setdefault("playerData", {})
    st.player["tower_floor"] = 1
    st.player["tower_highest"] = 0
    st.flush(force=True)
    return st


def call(fn, st, body=None):
    return fn(core.Ctx(st, body or {}, {}, {"username": st.username}, {}))


def update(st, highest, current=None):
    body = {"highestFloor": highest}
    if current is not None:
        body["currentFloor"] = current
    return call(hp.tower_update, st, body)


def info(st):
    return call(hp.tower_info, st)


def upload(st, floor, highest):
    """模拟客户端整包上传（本地还是旧快照）。"""
    incoming = {
        "playerData": {
            "tower_floor": floor,
            "tower_highest": highest,
            "level": 10,
            "realm": "天仙",
            "reincarnation": 4,
        },
        "gameSettings": {"current_map": "浮光水城外郊（0,0）"},
    }
    return call(ha.save_upload, st, {"saveData": incoming})


print("=== A. 初始状态 ===")
st = new_state("a")
r = info(st)
check("新号 towerFloor=1", int(r.get("towerFloor", -1)) == 1, str(r))
check("新号 towerHighest=0", int(r.get("towerHighest", -1)) == 0, str(r))
check("开放上限字段齐全", all(k in r for k in ("openMaxFloor", "openMaxFloorGlobal", "maxFloor")), str(r))

print("=== A2. 历史老账号自愈（打开天关页即恢复）===")
st4 = new_state("heal")
st4.player["tower_highest"] = 211
r = info(st4)
check("老号 floor=1/highest=211 自愈为 212", int(r.get("towerFloor", -1)) == 212, str(r))
check("自愈写回存档", int(st4.player.get("tower_floor", -1)) == 212)
check("自愈不重复触发", int(info(st4).get("towerFloor", -1)) == 212)

print("=== B. 旧客户端（只上报历史最高）===")
r = update(st, 211)
check("回包带 towerFloor", int(r.get("towerFloor", -1)) == 212, str(r))
check("历史最高=211", int(r.get("towerHighest", -1)) == 211, str(r))
check("存档 tower_floor=212", int(st.player.get("tower_floor", -1)) == 212)
r = info(st)
check("/tower/info 返回 212（不再回 1）", int(r.get("towerFloor", -1)) == 212, str(r))

print("=== C. 新客户端（带当前层）===")
r = update(st, 212, 213)
check("currentFloor 生效=213", int(st.player.get("tower_floor", -1)) == 213, str(r))
r = update(st, 213, 212)
check("当前层只增不减（上报更小值不回退）", int(st.player.get("tower_floor", -1)) == 213, str(r))

print("=== D. 重打已通关层不跳层（投胎重练的旧客户端）===")
r = update(st, 213)
check("highest 未刷新时 floor 不动", int(st.player.get("tower_floor", -1)) == 213, str(r))
st2 = new_state("d")
st2.player["tower_highest"] = 200
st2.player["tower_floor"] = 1
r = update(st2, 200, 2)
check("投胎后新客户端从第 2 层续上", int(st2.player.get("tower_floor", -1)) == 2, str(r))
r = update(st2, 200)
check("投胎后旧客户端不跳回 201", int(st2.player.get("tower_floor", -1)) == 2, str(r))

print("=== E. 存档上传不能把进度打回第 1 层 ===")
r = upload(st, 1, 0)
check("save/upload 成功", bool(r.get("success")), str(r))
check("上传旧快照后服务端仍 213", int(st.player.get("tower_floor", -1)) == 213,
      str(st.player.get("tower_floor")))
check("上传旧快照后历史最高仍 213", int(st.player.get("tower_highest", -1)) == 213,
      str(st.player.get("tower_highest")))
r = info(st)
check("再次打开天关页拿到 213", int(r.get("towerFloor", -1)) == 213, str(r))

print("=== F. 投胎重置仍然生效，且不被自愈误推 ===")
st.player["tower_floor"] = 1
st.meta["towerFloorResetPending"] = True  # handlers_growth 投胎时写入
st.bump()
r = info(st)
check("投胎后 /tower/info 仍回 1", int(r.get("towerFloor", -1)) == 1, str(r))
check("投胎后存档不被自愈改写", int(st.player.get("tower_floor", -1)) == 1)
r = update(st, 213, 2)
check("投胎后重打上报 currentFloor=2 生效", int(st.player.get("tower_floor", -1)) == 2, str(r))
check("重打后解除投胎保护标记", not bool(st.meta.get("towerFloorResetPending")))

print("=== G. 扫荡与服务端战斗结算不受影响 ===")
st3 = new_state("g")
st3.player["tower_highest"] = 100
r = call(hp.tower_sweep, st3)
check("扫荡推到 101", int(r.get("towerFloor", -1)) == 101, str(r))
check("扫荡后 /tower/info=101", int(info(st3).get("towerFloor", -1)) == 101)

print("=== H. 客户端代码静态守卫 ===")
CLIENT = ROOT.parent.parent / "recovered-godot-project"
net = (CLIENT / "scripts" / "network.gd").read_text(encoding="utf-8")
ui = (CLIENT / "ui" / "city" / "tower_ui.gd").read_text(encoding="utf-8")
gm = (CLIENT / "scripts" / "game_manager.gd").read_text(encoding="utf-8")
check("network 上报 currentFloor", "currentFloor" in net and "update_tower_highest" in net)
check("network 函数带 second 参数", "current_floor: int" in net)
check("tower_ui 通关上报带当前层", "update_tower_highest(game_manager.player.tower_highest, game_manager.player.tower_floor)" in ui)
check("tower_ui 同步用 max 保护", "maxi(int(game_manager.player.tower_floor), server_floor)" in ui)
check("game_manager 同步带当前层", "network.update_tower_highest(floor, player.tower_floor)" in gm)
growth = (ROOT / "handlers_growth.py").read_text(encoding="utf-8")
account = (ROOT / "handlers_account.py").read_text(encoding="utf-8")
check("两处投胎重置都写保护标记", growth.count("state.meta[\"towerFloorResetPending\"] = True") == 2)
check("存档上传把塔层当服务端权威", "_tower_key in (\"tower_floor\", \"tower_highest\")" in account)
check("/tower/info 有老账号自愈", "towerFloorResetPending" in (ROOT / "handlers_play.py").read_text(encoding="utf-8"))

print("\n通过 %d / 失败 %d" % (PASS, FAIL))
raise SystemExit(0 if FAIL == 0 else 1)
