#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热更新（PCK 补丁）下发。

客户端 `scripts/update_manager.gd` 已有的协议：

    GET /updates/manifest.json
        {
          "base_version": "2.5.9",            # 底包版本，客户端 CLIENT_VERSION 必须 >= 它
          "minimum_base_version": "2.5.9",    # 低于它则不发补丁（需重装 APK）
          "patch_version": "2.5.9.3",         # 补丁版本，必须比客户端本地补丁新
          "releaseNotes": "更新说明",
          "platforms": {
            "android": {"url": "...", "size": 123, "sha256": "..."}
          }
        }

    GET <url>   # 补丁本体，客户端会核对 size 与 sha256，然后落盘到
                # user://patches/hotfix.pck，重启进程后 load_resource_pack 生效

关键点：`version` 字段绝不能抬高。客户端 main.gd 的 _check_version() 读它，
一旦高于本地 CLIENT_VERSION 就弹「必须更新」挡住登录，和热更是两条独立通道。

补丁元信息存放在 state/patches/patch.json，由 tools/publish_patch.py 写入。
服务端每次请求都重读该文件，所以发布/回滚补丁都不需要重启服务。
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATCH_DIR = ROOT / "state" / "patches"
META_PATH = PATCH_DIR / "patch.json"
CHUNK = 256 * 1024


def _hdr(headers, name: str) -> str:
    """HTTP 头名大小写不敏感地取值。

    注意：BaseHTTPRequestHandler.headers 转成 dict 之后会丢掉大小写不敏感的特性，
    客户端实际发的是 `X-Client-Version`，但任何一层改写都可能变成别的写法。
    """
    if hasattr(headers, "get"):
        for key in (name, name.lower(), name.title()):
            value = headers.get(key)
            if value:
                return str(value)
    lowered = name.lower()
    try:
        for key, value in headers.items():
            if str(key).lower() == lowered and value:
                return str(value)
    except AttributeError:
        pass
    return ""


def read_meta() -> dict:
    if not META_PATH.exists():
        return {}
    try:
        data = json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(meta: dict) -> None:
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def public_base(headers) -> str:
    """补丁下载地址的前缀。

    优先用客户端实际访问的 Host 拼（客户端连哪个地址，就从哪个地址下载）；
    如果 patch.json 里显式写了 public_base（例如将来挂 CDN），以它为准。
    """
    meta = read_meta()
    forced = str(meta.get("public_base", "") or "").strip().rstrip("/")
    if forced:
        return forced
    host = _hdr(headers, "Host") or "127.0.0.1:3000"
    proto = _hdr(headers, "X-Forwarded-Proto") or "http"
    return "%s://%s" % (proto, host)


def build_manifest(headers) -> dict:
    client_ver = _hdr(headers, "X-Client-Version").strip() or "2.5.9"

    # 默认形态：不推送 APK 整包更新，也不推补丁。
    manifest = {
        "version": client_ver,
        "url": "",
        "notes": "",
        "forceUpdate": False,
        "platforms": {},
    }

    meta = read_meta()
    if not meta or meta.get("enabled", True) is False:
        return manifest

    patch_version = str(meta.get("patch_version", "") or "").strip()
    if not patch_version:
        return manifest

    base = public_base(headers)
    platforms = {}
    for key, info in (meta.get("files") or {}).items():
        if not isinstance(info, dict):
            continue
        name = Path(str(info.get("name", "")).strip()).name
        if not name:
            continue
        path = PATCH_DIR / name
        if not path.is_file():
            # 元信息和文件对不上时宁可不下发，否则客户端会白下载一次再失败
            continue
        platforms[key] = {
            "url": "%s/updates/%s" % (base, name),
            "size": int(path.stat().st_size),
            "sha256": str(info.get("sha256", "") or "").strip().lower(),
        }

    if not platforms:
        return manifest

    base_version = str(meta.get("base_version", client_ver) or client_ver).strip()
    manifest.update({
        "base_version": base_version,
        "minimum_base_version": str(meta.get("minimum_base_version", base_version) or base_version).strip(),
        "patch_version": patch_version,
        "releaseNotes": str(meta.get("release_notes", "") or "内容更新").strip(),
        "platforms": platforms,
    })
    return manifest


def resolve(name: str) -> Path | None:
    """只允许取补丁目录下的平铺文件，挡住 ../ 之类的穿越。"""
    safe = Path(str(name or "").strip()).name
    if not safe or safe.startswith("."):
        return None
    path = PATCH_DIR / safe
    if not path.is_file():
        return None
    return path


def serve_file(handler, name: str) -> bool:
    """流式下发补丁文件。返回 False 表示文件不存在（调用方回 404）。"""
    path = resolve(name)
    if path is None:
        return False

    size = path.stat().st_size
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    start, end = 0, size - 1
    partial = False
    rng = _hdr(handler.headers, "Range")
    if rng.startswith("bytes="):
        spec = rng[len("bytes="):].split(",")[0].strip()
        try:
            head, _, tail = spec.partition("-")
            if head:
                start = max(0, int(head))
                end = int(tail) if tail else size - 1
            elif tail:
                # bytes=-N 表示最后 N 字节
                start = max(0, size - int(tail))
            end = min(end, size - 1)
            if start > end:
                start, end = 0, size - 1
            else:
                partial = True
        except ValueError:
            start, end, partial = 0, size - 1, False

    length = end - start + 1
    handler.send_response(206 if partial else 200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    if partial:
        handler.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()

    try:
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        # 玩家中途取消下载很正常，别把异常抛到上层打断服务
        pass
    return True


def status() -> dict:
    """给 GM 后台看的当前补丁状态。"""
    meta = read_meta()
    files = {}
    for key, info in (meta.get("files") or {}).items():
        if not isinstance(info, dict):
            continue
        name = Path(str(info.get("name", "")).strip()).name
        path = PATCH_DIR / name
        files[key] = {
            "name": name,
            "exists": path.is_file(),
            "size": path.stat().st_size if path.is_file() else 0,
            "declared_sha256": str(info.get("sha256", "")),
        }
    return {
        "enabled": meta.get("enabled", True) if meta else False,
        "patch_version": str(meta.get("patch_version", "")),
        "base_version": str(meta.get("base_version", "")),
        "minimum_base_version": str(meta.get("minimum_base_version", "")),
        "release_notes": str(meta.get("release_notes", "")),
        "published_at": str(meta.get("published_at", "")),
        "public_base": str(meta.get("public_base", "")),
        "files": files,
    }
