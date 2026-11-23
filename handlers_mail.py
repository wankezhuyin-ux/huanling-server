# -*- coding: utf-8 -*-
"""邮箱模块：按客户端 scripts/mail/mail_api.gd 的协议实现。

要点：附件是**客户端本地入包**的（mail_manager._add_attachments_to_game），
服务端只负责存取邮件、记录已领取状态。所以这里绝不能重复发金币/道具，
否则玩家领一次会拿到两份（服务端一份 + 客户端本地一份）。

流程对齐：
  GET  /mail/list                列表
  GET  /mail/detail/{id}         详情
  POST /mail/claim/{id}          返回 attachments 给客户端本地入包（不置已领）
  POST /mail/finalize-claims     客户端入包成功后才置 is_claimed
  GET  /mail/unread              未读数
  POST /mail/claim-all           返回所有可领邮件的 attachments
  POST /mail/delete-claimed      删除已领取
  POST /mail/delete              按 id 删除
  POST /mail/self                自查/补发（只允许宠物类附件，防止刷货币）
  POST /mail/pet-overflow        宠物背包满时补发到邮箱
"""
from __future__ import annotations

from datetime import datetime

from core import Ctx, fail, iso, now, ok, rnd_id, route

MAX_MAIL = 200


def _mails(state) -> list:
    return state.meta.setdefault("mails", [])


def _to_ts(raw) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _is_expired(mail: dict) -> bool:
    ts = _to_ts(mail.get("expire_at"))
    return bool(ts and now().timestamp() > ts)


def _has_attachments(mail: dict) -> bool:
    attach = mail.get("attachments")
    if isinstance(attach, dict):
        for key in ("gold", "yuanbao", "exp"):
            try:
                if int(attach.get(key, 0) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        for key in ("items", "pets"):
            val = attach.get(key)
            if isinstance(val, list) and val:
                return True
        if attach.get("type"):
            return True
    elif isinstance(attach, list) and attach:
        return True
    return False


def _attachments_of(mail: dict) -> dict:
    attach = mail.get("attachments")
    if isinstance(attach, dict):
        return attach
    if isinstance(attach, list):
        for entry in attach:
            if isinstance(entry, dict):
                return entry
    return {}


def _mail_item_rows(mail: dict) -> list:
    """把新旧两种附件结构统一成 [{name,count,type,data,custom_data}]。"""
    attach = _attachments_of(mail)
    rows: list = []
    items = attach.get("items")
    if isinstance(items, list):
        rows.extend([r for r in items if isinstance(r, dict)])
    # 老 GM / 线上历史邮件是平铺结构：{type:"item", item_id, quantity, ...}
    if not rows and attach.get("type") and (attach.get("item_id") or attach.get("name")):
        rows.append({
            "name": attach.get("item_id") or attach.get("name"),
            "count": attach.get("quantity", attach.get("count", 1)),
            "type": attach.get("type", "item"),
            "data": attach.get("data"),
            "custom_data": attach.get("custom_data"),
        })
    return rows


def _mail_needs_authority_save(mail: dict) -> bool:
    """附件里是否含「客户端等服务端存档才能入装备栏」的装备。"""
    for row in _mail_item_rows(mail):
        if str(row.get("type") or row.get("item_type") or "") == "sealed_suit":
            return True
        if "·" in str(row.get("name") or ""):
            return True
    return False


def _grant_mail_attachments(state, mail: dict, write_items: bool = True) -> dict:
    """finalize 确认时把附件真正落到服务端存档，返回本次发放摘要。

    原实现只把邮件打上 is_claimed，什么都不发：客户端领取时
    `_add_attachments_to_game` 对封印套装是 `defer_sealed_suits_to_finalize=true`
    （不在本地入包，等服务端回包），而 finalize 又「不回 saveData」——
    两边互相等，装备永远领不到，UI 弹「领取确认失败，封印套装未同步到背包」。
    套装副本通关/扫荡掉落的封印套装就卡死在这里。

    修法：服务端在确认时按附件逐件写 inventoryData，并回一份**只含
    inventoryData 的 saveData**（不带 petsData/equipmentData/playerData，
    避免 apply_cloud_save 把客户端本地宠物/人物装备栏整块冲掉）。
    """
    out = {"equipment": False, "gold": 0, "yuanbao": 0}
    attach = _attachments_of(mail)
    if not attach:
        return out
    for row in _mail_item_rows(mail):
        name = str(row.get("name") or row.get("item_id") or "").strip()
        if not name:
            continue
        try:
            count = max(1, int(row.get("count", row.get("quantity", 1)) or 1))
        except (TypeError, ValueError):
            count = 1
        typ = str(row.get("type") or row.get("item_type") or "")
        custom = dict(row.get("custom_data")) if isinstance(row.get("custom_data"), dict) else {}
        # 藏宝箱等把 token 写在附件顶层的老邮件，入存档时要带进 custom_data，
        # 否则云档里这口箱子会因为缺 token 打不开。
        if row.get("treasure_token") and "treasure_token" not in custom:
            custom["treasure_token"] = str(row.get("treasure_token"))
        if not write_items:
            continue
        if typ == "sealed_suit":
            # 封印套装必须一件一行：客户端按 item_type=="sealed_suit" 的**行数**
            # 校验领取数量，同名合并成 count=2 的一行会被判定「少发一件」。
            out["equipment"] = True
            data = dict(row.get("data")) if isinstance(row.get("data"), dict) else custom
            if not data.get("name"):
                data["name"] = name
            for i in range(count):
                cd = dict(data)
                cd["mailClaimId"] = "%d-%d-%s" % (int(mail.get("id", 0) or 0), i, rnd_id("s"))
                state.items.append({
                    "id": rnd_id("i"),
                    "name": "sealed_suit",
                    "count": 1,
                    "item_type": "sealed_suit",
                    "custom_data": cd,
                })
        elif "·" in name:
            # 「XX套装·武器」这类成品装备由客户端 equipment_manager 直接进
            # 人物装备栏，服务端不写背包；但仍要回 saveData 供领取确认。
            out["equipment"] = True
        elif typ not in ("gold", "yuanbao", "exp", "pet", "chest"):
            state.add_item(name, count, typ or "item", custom)
    # 客户端领取时 skip_server_currency=true，金币/元宝不加本地，必须服务端补发；
    # exp 是客户端本地加的，这里绝不能再发一次。
    try:
        gold = int(attach.get("gold", 0) or 0)
        if gold <= 0 and str(attach.get("type", "")) == "gold":
            gold = int(attach.get("amount", 0) or 0)
    except (TypeError, ValueError):
        gold = 0
    try:
        yb = int(attach.get("yuanbao", 0) or 0)
    except (TypeError, ValueError):
        yb = 0
    if gold > 0:
        state.add_gold(gold)
        out["gold"] = gold
    if yb > 0:
        state.add_yuanbao(yb)
        out["yuanbao"] = yb
    return out


def _find_mail(state, mail_id: int):
    for mail in _mails(state):
        if int(mail.get("id", 0) or 0) == int(mail_id):
            return mail
    return None


def _sort_mails(rows: list) -> list:
    return sorted(rows, key=lambda m: _to_ts(m.get("created_at")), reverse=True)


@route("GET", "/mail/list")
def mail_list(ctx: Ctx) -> dict:
    rows = [m for m in _mails(ctx.state) if isinstance(m, dict) and not _is_expired(m)]
    return ok(mails=_sort_mails(rows))


@route("GET", "/mail/self")
def mail_self_get(ctx: Ctx) -> dict:
    return mail_list(ctx)


@route("GET", "/mail/detail/{mailId}")
def mail_detail(ctx: Ctx) -> dict:
    mail = _find_mail(ctx.state, ctx.qi("mailId"))
    if mail is None:
        return fail("邮件不存在或已过期")
    return ok(mail=mail)


@route("POST", "/mail/read")
def mail_read(ctx: Ctx) -> dict:
    mail_id = ctx.i("mailId") or ctx.i("id") or ctx.qi("mailId")
    mail = _find_mail(ctx.state, mail_id)
    if mail is None:
        return fail("邮件不存在")
    mail["is_read"] = True
    ctx.state.bump()
    return ok()


@route("POST", "/mail/claim/{mailId}")
def mail_claim(ctx: Ctx) -> dict:
    mail_id = ctx.qi("mailId")
    mail = _find_mail(ctx.state, mail_id)
    if mail is None:
        return fail("邮件不存在或已过期")
    if mail.get("is_claimed"):
        return fail("附件已领取")
    attach = _attachments_of(mail)
    if not attach:
        return fail("这封邮件没有附件")
    # 只回传附件，客户端本地入包后再调 finalize-claims 标记已领
    return ok(attachments=attach, mailId=int(mail_id))


@route("POST", "/mail/claim")
def mail_claim_legacy(ctx: Ctx) -> dict:
    return mail_claim(ctx)


@route("GET", "/mail/unread")
def mail_unread(ctx: Ctx) -> dict:
    count = 0
    for mail in _mails(ctx.state):
        if not isinstance(mail, dict) or _is_expired(mail):
            continue
        if not mail.get("is_read"):
            count += 1
    return ok(count=count)


@route("POST", "/mail/claim-all")
def mail_claim_all(ctx: Ctx) -> dict:
    claimed = []
    for mail in _sort_mails(_mails(ctx.state)):
        if not isinstance(mail, dict) or _is_expired(mail):
            continue
        if mail.get("is_claimed"):
            continue
        attach = _attachments_of(mail)
        if not attach:
            continue
        claimed.append({
            "id": int(mail.get("id", 0)),
            "mail_id": int(mail.get("id", 0)),
            "title": str(mail.get("title", "")),
            "attachments": attach,
        })
    if not claimed:
        return ok(claimed=[], message="没有可领取的邮件")
    return ok(claimed=claimed)


@route("POST", "/mail/finalize-claims")
def mail_finalize_claims(ctx: Ctx) -> dict:
    raw = ctx.body.get("mailIds")
    ids: list[int] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                mid = int(item)
            except (TypeError, ValueError):
                continue
            if mid > 0:
                ids.append(mid)
    if not ids:
        return fail("邮件 ID 为空")

    pending = [m for m in _mails(ctx.state)
               if isinstance(m, dict) and int(m.get("id", 0) or 0) in ids
               and not m.get("is_claimed")]
    # 只有这一批里含装备附件时才走「服务端权威写背包 + 回 saveData」。
    # 纯道具/宠物邮件保持原来的客户端本地入包流程，一行都不改，
    # 避免它们被服务端存档整块覆盖。
    has_equipment = any(_mail_needs_authority_save(m) for m in pending)
    updated = 0
    gold_granted = 0
    yuanbao_granted = 0
    for mail in pending:
        # 先落地再打 claimed：写入失败就不会把邮件标记成已领取。
        granted = _grant_mail_attachments(ctx.state, mail, write_items=has_equipment)
        gold_granted += granted["gold"]
        yuanbao_granted += granted["yuanbao"]
        mail["is_claimed"] = True
        mail["is_read"] = True
        updated += 1
    if updated:
        ctx.state.bump()
    resp = ok(updated=updated, duplicate=updated == 0)
    # 只有真的发了货币才回钱包字段，避免用落后快照把本地金币冲掉
    if gold_granted > 0:
        resp["newGold"] = ctx.state.gold()
    if yuanbao_granted > 0:
        resp["newYuanbao"] = int(ctx.state.player.get("yuanbao_currency", 0) or 0)
    if has_equipment:
        # 装备类附件：客户端 _reconcile_finalize_claim_save 要求回包带 saveData
        # 且 inventoryData 里的封印套装行数 >= 本地原有 + 本次领取数，
        # 拿到后 apply_cloud_save 才把装备显示到人物装备栏。只回背包切片，
        # 不带 playerData/petsData/equipmentData，避免冲掉本地其它进度。
        resp["saveData"] = {"inventoryData": ctx.state.inv}
    return resp


@route("POST", "/mail/delete-claimed")
def mail_delete_claimed(ctx: Ctx) -> dict:
    mails = _mails(ctx.state)
    keep = [m for m in mails if not (isinstance(m, dict) and m.get("is_claimed"))]
    deleted = len(mails) - len(keep)
    ctx.state.meta["mails"] = keep
    if deleted:
        ctx.state.bump()
    return ok(deleted=deleted)


@route("POST", "/mail/delete")
def mail_delete(ctx: Ctx) -> dict:
    raw = ctx.body.get("mailIds")
    ids: set[int] = set()
    if isinstance(raw, list):
        for item in raw:
            try:
                ids.add(int(item))
            except (TypeError, ValueError):
                continue
    if not ids:
        single = ctx.i("mailId") or ctx.i("id")
        if single:
            ids.add(int(single))
    if not ids:
        return fail("邮件 ID 为空")
    mails = _mails(ctx.state)
    keep = [m for m in mails
            if not (isinstance(m, dict) and int(m.get("id", 0) or 0) in ids)]
    deleted = len(mails) - len(keep)
    ctx.state.meta["mails"] = keep
    if deleted:
        ctx.state.bump()
    return ok(deleted=deleted)


def _trim(state) -> None:
    mails = _mails(state)
    if len(mails) > MAX_MAIL:
        state.meta["mails"] = mails[:MAX_MAIL]


@route("POST", "/mail/self")
def mail_self_post(ctx: Ctx) -> dict:
    """客户端补发邮件（宠物背包溢出等）。

    只接受宠物/纯文本，忽略 gold/yuanbao/exp —— 这个接口客户端能自己调，
    放开货币等于给了玩家一个无限刷钱的入口。
    """
    title = ctx.s("title").strip()[:40] or "系统邮件"
    content = ctx.s("content").strip()[:500] or "附件补发"
    attach = ctx.body.get("attachments")
    if not isinstance(attach, dict):
        attach = {}
    pets = attach.get("pets")
    if not isinstance(pets, list):
        pets = []
    safe_pets = [p for p in pets if isinstance(p, dict) and (p.get("pet_id") or p.get("id"))]
    if not safe_pets:
        return fail("不支持在该接口发放除宠物外的附件")
    mail = ctx.state.send_mail(title, content, attachments={"pets": safe_pets[:5]})
    _trim(ctx.state)
    ctx.state.bump()
    return ok(mailId=int(mail.get("id", 0)))


@route("POST", "/mail/pet-overflow")
def mail_pet_overflow(ctx: Ctx) -> dict:
    body = ctx.body
    pet = body.get("pet") if isinstance(body.get("pet"), dict) else None
    if pet is None and (body.get("pet_id") or body.get("petId")):
        pet = body
    if not pet:
        return fail("缺少宠物数据")
    title = ctx.s("title").strip()[:40] or "宠物补发"
    content = ctx.s("content").strip()[:500] or "宠物背包已满，宠物已通过邮件补发，请及时领取。"
    mail = ctx.state.send_mail(title, content, attachments={"pets": [pet]})
    _trim(ctx.state)
    ctx.state.bump()
    return ok(mailId=int(mail.get("id", 0)), mail=mail)
