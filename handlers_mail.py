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

from core import Ctx, fail, iso, now, ok, route

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

    updated = 0
    for mail in _mails(ctx.state):
        if not isinstance(mail, dict):
            continue
        if int(mail.get("id", 0) or 0) in ids and not mail.get("is_claimed"):
            mail["is_claimed"] = True
            mail["is_read"] = True
            updated += 1
    if updated:
        ctx.state.bump()
    # 不回 saveData：附件是客户端本地入包的，回传服务端存档会把本地入包结果冲掉
    return ok(updated=updated, duplicate=updated == 0)


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
