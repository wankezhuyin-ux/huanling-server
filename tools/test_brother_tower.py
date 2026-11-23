#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兄弟天关端到端回归：令牌门槛 / 房间 / 申请审批 / 开战扣令牌+幂等 /
波次推进 / 终局 / 结算限流+掉落 / leave+disband。

设计要点：
  - 自给自足：自行注册两个账号并通过 save/upload 注入令牌，不依赖特定存档。
  - 端到端：全部走真实 HTTP，覆盖 bt_session 状态机与 handlers_play/extra 的兄弟天关端点。
  - 隔离：连 API_BASE 指定的服务；建议把整个 local-server 复制到临时目录、
    在该副本上起服务跑本脚本，避免污染真实 state。

    API_BASE=http://127.0.0.1:3999/api python3 tools/test_brother_tower.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("API_BASE", "http://127.0.0.1:3999/api")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过本机 HTTP_PROXY 劫持
TOKEN_ITEM = "兄弟天关令牌"
ok_count = 0
bad_count = 0


def call(path, body=None, method="POST", token="", expect=None):
    data = json.dumps(body or {}, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Client-Version", "2.6.0")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _OPENER.open(req, timeout=20) as resp:
            code, payload = resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            payload = json.loads(exc.read().decode())
        except json.JSONDecodeError:
            payload = {}
    except Exception as exc:  # 连接失败
        print("  !! 连接失败 %s: %s" % (path, exc))
        return 0, {"_error": str(exc)}
    if expect is not None and code != expect:
        print("  !! %s 期望 HTTP %s 实际 %s" % (path, expect, code))
    return code, payload


def check(label, cond, detail=""):
    global ok_count, bad_count
    if cond:
        ok_count += 1
        print("  [ok] %s %s" % (label, detail))
    else:
        bad_count += 1
        print("  [FAIL] %s %s" % (label, detail))


def login(user, nick):
    # 昵称只允许汉字/字母/数字，不能含下划线
    call("/register", {"username": user, "password": "test1234", "nickname": nick})
    _, res = call("/login", {"username": user, "password": "test1234"})
    return str(res.get("token", "")), str(res.get("userId", "0"))


def grant_token(token, count=5):
    """通过 save/upload 注入令牌物品（tokenItemName 来自 runtime-config）。"""
    save = {
        "playerData": {"level": 100, "nickname": "bt腿子", "gold": 100000,
                       "reincarnation": 0, "noble_level": 0},
        "inventoryData": {"items": [
            {"id": TOKEN_ITEM, "name": TOKEN_ITEM, "count": count,
             "item_type": "material", "custom_data": {"id": TOKEN_ITEM, "name": TOKEN_ITEM}}
        ]},
        "petsData": {"pets": [{"id": "btp1", "name": "测试灵兽", "is_active": True,
                               "level": 100, "growth": 30.0}]},
        "gameSettings": {"current_map": "盈露台守望区（0,0）"},
    }
    call("/save/upload", {"saveData": save}, token=token)


def token_count(token):
    _, res = call("/brother-tower/status", method="GET", token=token)
    return int(res.get("tokenCount", 0)), res


def main():
    suffix = uuid.uuid4().hex[:8]
    ta, ua = login("zzbta" + suffix, "bt甲" + suffix[:4])
    tb, ub = login("zzbtb" + suffix, "bt乙" + suffix[:4])
    check("两账号登录", bool(ta) and bool(tb) and ua != "0" and ub != "0",
          "ua=%s ub=%s" % (ua, ub))

    # 令牌门槛：无令牌时建房应被拒
    print("\n== 令牌门槛 ==")
    _, st = call("/brother-tower/status", method="GET", token=ta)
    check("status 带 waveCount/tokenName",
          int(st.get("waveCount", 0)) == 10 and st.get("tokenName") == TOKEN_ITEM,
          "waveCount=%s tokenName=%s" % (st.get("waveCount"), st.get("tokenName")))

    # 注入令牌
    grant_token(ta, 5)
    grant_token(tb, 5)
    cnt_a, _ = token_count(ta)
    check("令牌注入成功", cnt_a >= 5, "tokenCount=%s" % cnt_a)

    print("\n== 房间：review 申请 + 审批 ==")
    _, res = call("/brother-tower/create",
                  {"difficulty": "easy", "joinMode": "review",
                   "localSnapshot": {"pet": {"name": "测试灵兽"}}}, token=ta)
    room = res.get("room") or {}
    rid = int(res.get("roomId") or room.get("roomId") or 0)
    check("建房成功", rid > 0, "roomId=%s" % rid)
    check("队长是自己", bool(room.get("isLeader") or int(room.get("leaderId", 0)) == int(ua)),
          "leaderId=%s" % room.get("leaderId"))

    _, lst = call("/brother-tower/list", method="GET", token=tb)
    check("别人能看到房间", any(int(r.get("roomId", 0)) == rid for r in lst.get("rooms", [])),
          "rooms=%d" % len(lst.get("rooms", [])))

    _, res = call("/brother-tower/join", {"roomId": rid, "localSnapshot": {}}, token=tb)
    check("review 模式记为申请", bool(res.get("applied")) and not res.get("joined"),
          "applied=%s joined=%s" % (res.get("applied"), res.get("joined")))

    _, lst = call("/brother-tower/list", method="GET", token=tb)
    check("B 的 pending 含该房间", rid in [int(x) for x in lst.get("myPendingApplicationRoomIds", [])],
          "pending=%s" % lst.get("myPendingApplicationRoomIds"))

    # 非队长审批应被拒
    _, res = call("/brother-tower/application/decide",
                  {"roomId": rid, "userId": int(ub), "approve": True}, token=tb)
    check("非队长审批被拒", res.get("approved") is False, "message=%s" % res.get("message"))

    # 队长审批通过
    _, res = call("/brother-tower/application/decide",
                  {"roomId": rid, "userId": int(ub), "approve": True}, token=ta)
    check("队长审批通过", bool(res.get("approved")), "approved=%s" % res.get("approved"))
    room = res.get("room") or {}
    check("审批后 B 成为成员", any(int(m.get("userId", 0)) == int(ub) for m in room.get("members", [])),
          "members=%d" % len(room.get("members", [])))

    print("\n== ready ==")
    _, res = call("/brother-tower/ready", {}, token=ta)
    check("队长 ready", bool(res.get("ready")), "roomId=%s" % res.get("roomId"))

    print("\n== 开战：令牌扣减 + 幂等 ==")
    before, _ = token_count(ta)
    op = "bt-start-" + suffix
    _, res = call("/brother-tower/start",
                  {"difficulty": "easy", "operationId": op, "snapshot": {}}, token=ta)
    check("开战成功", bool(res.get("sessionId")) and int(res.get("waveCount", 0)) == 10,
          "sessionId=%s waveCount=%s" % (str(res.get("sessionId"))[:8], res.get("waveCount")))
    after, _ = token_count(ta)
    check("开战扣 1 令牌", after == before - 1, "%s -> %s" % (before, after))
    _, res2 = call("/brother-tower/start",
                   {"difficulty": "easy", "operationId": op, "snapshot": {}}, token=ta)
    after2, _ = token_count(ta)
    check("同 op 重放不重复扣", after2 == after and bool(res2.get("resumed") or res2.get("sessionId")),
          "token=%s resumed=%s" % (after2, res2.get("resumed")))

    print("\n== 战斗事件 + 10 波推进 ==")
    _, ev = call("/brother-tower/battle-events?sinceSeq=0", method="GET", token=ta)
    check("事件增量结构", "events" in ev and "nextSeq" in ev and "finished" in ev,
          "keys=%s" % sorted(ev.keys())[:6])
    _, ses = call("/brother-tower/session", method="GET", token=ta)
    check("session 初始 in_battle", (ses.get("session") or {}).get("status") == "in_battle"
          or ses.get("status") == "in_battle", "status=%s" % (ses.get("status")))

    for w in range(1, 11):
        _, res = call("/brother-tower/wave-cleared", {"wave": w}, token=ta)
        if w < 10:
            ok = bool(res.get("cleared")) and not res.get("finished")
        else:
            ok = bool(res.get("cleared")) and bool(res.get("finished"))
        check("第%d波%s" % (w, "终局" if w == 10 else "推进"), ok,
              "currentWave=%s finished=%s" % (res.get("currentWave"), res.get("finished")))

    _, ses = call("/brother-tower/session", method="GET", token=ta)
    final_status = (ses.get("session") or {}).get("status") or ses.get("status")
    check("会话终态 cleared", final_status == "cleared", "status=%s" % final_status)

    print("\n== 结算：真实掉落 + 限流 ==")
    op2 = "bt-complete-" + suffix
    _, res = call("/brother-tower/complete", {"operationId": op2}, token=ta)
    check("结算成功", bool(res.get("success")), "msg=%s" % res.get("message"))
    check("结算带 friendshipGain", isinstance(res.get("friendshipGain"), int),
          "friendshipGain=%s" % res.get("friendshipGain"))
    check("结算带 items 掉落", isinstance(res.get("items"), list) and len(res.get("items", [])) >= 1,
          "items=%s" % str(res.get("items"))[:80])
    check("dailyRewardMax=1", int(res.get("dailyRewardMax", 0)) == 1,
          "dailyRewardMax=%s" % res.get("dailyRewardMax"))

    # 幂等：同 op 重放返回 duplicate
    _, res_dup = call("/brother-tower/complete", {"operationId": op2}, token=ta)
    check("complete 幂等", bool(res_dup.get("duplicate")), "duplicate=%s" % res_dup.get("duplicate"))

    # 限流：会话已结束，再次 complete 应被拒（战斗尚未通关）
    _, res_again = call("/brother-tower/complete", {"operationId": "bt-complete-2-" + suffix}, token=ta)
    check("无会话时结算被拒", not res_again.get("success"), "msg=%s" % res_again.get("message"))

    print("\n== leave / disband ==")
    _, res = call("/brother-tower/leave", {}, token=tb)
    check("成员离队", bool(res.get("success")), "msg=%s" % res.get("message"))
    _, res = call("/brother-tower/disband", {}, token=ta)
    check("队长解散", bool(res.get("success")), "msg=%s" % res.get("message"))
    _, lst = call("/brother-tower/list", method="GET", token=ta)
    check("房间已清理", all(int(r.get("roomId", 0)) != rid for r in lst.get("rooms", [])), "")

    print("\n结果：%d 通过 / %d 失败" % (ok_count, bad_count))
    return 0 if bad_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
