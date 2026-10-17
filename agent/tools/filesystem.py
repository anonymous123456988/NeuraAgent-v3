# -*- coding: utf-8 -*-
"""工具：文件系统(列目录/读文件/写文件/打包下载)。"""
import datetime
import os
import shutil
import zipfile
from pathlib import Path

from agent import safety, utils
from .registry import Tool, ToolResult, ToolContext, ToolRegistry


def _fallback_path(path: str) -> str:
    """非 Windows 系统遇到盘符路径时回退到家目录。"""
    if os.name != "nt" and re_search_drive(path):
        return str(Path.home())
    return path


def re_search_drive(path: str) -> bool:
    import re
    return bool(re.match(r"^[A-Za-z]:[/\\]?", (path or "").strip()))


# 纯文件夹名 -> 用户主目录下的标准目录(解决"进入desktop文件夹却列出根目录"问题)
_HOME_DIR_ALIASES = {
    "desktop": "~/Desktop", "桌面": "~/Desktop", "desktop文件夹": "~/Desktop",
    "downloads": "~/Downloads", "下载": "~/Downloads", "downloads文件夹": "~/Downloads",
    "documents": "~/Documents", "文档": "~/Documents", "documents文件夹": "~/Documents",
    "pictures": "~/Pictures", "图片": "~/Pictures", "photos": "~/Pictures",
    "music": "~/Music", "音乐": "~/Music",
    "videos": "~/Videos", "视频": "~/Videos",
    "home": "~", "家": "~", "家目录": "~", "主目录": "~", "用户目录": "~", "user": "~",
}


def _resolve_list_path(raw: str, ctx: ToolContext) -> tuple[str, str | None]:
    """目录参数解析: 空 -> 家目录; 纯文件夹名 -> 映射到家目录标准目录;
    别名/相对目录不存在时自动回退家目录并提示(返回 path, note)。"""
    raw0 = (raw or "").strip().strip('"').strip("'")
    if not raw0 or raw0 in ("~", "~/"):
        raw0 = "~"
    elif not os.path.isabs(raw0) and "/" not in raw0 and "\\" not in raw0 and ":" not in raw0:
        alias = _HOME_DIR_ALIASES.get(raw0.lower())
        if alias:
            raw0 = alias
    path = safety.resolve_path(raw0, ctx.config)
    if not os.path.exists(path):
        # 纯名称/别名/相对目录不存在(如无桌面目录) -> 自动回退用户主目录, 不报错
        if not os.path.isabs(raw0) or raw0.startswith("~"):
            home = safety.resolve_path("~", ctx.config)
            return home, f"{path} 不存在，已自动回退到用户主目录。"
    return path, None


async def _list_directory(args: dict, ctx: ToolContext) -> ToolResult:
    raw = str(args.get("path", "~"))
    hidden = bool(args.get("hidden", False))
    note = ""
    if os.name != "nt" and re_search_drive(raw):
        note = f"当前系统不是 Windows，{raw} 不存在，已自动回退到用户主目录。"
        raw = "~"
    try:
        path, note2 = _resolve_list_path(raw, ctx)
        if note2:
            note = note2
    except safety.SafetyError as e:
        return ToolResult(ok=False, error=str(e), error_type="forbidden")
    if not os.path.exists(path):
        return ToolResult(ok=False, error=f"路径不存在: {path}", error_type="not_found", retryable=True)
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    try:
        entries = sorted(os.listdir(path))
    except PermissionError:
        return ToolResult(ok=False, error=f"无权限访问: {path}", error_type="forbidden")
    items = []
    for name in entries:
        if name.startswith(".") and not hidden:
            continue
        full = os.path.join(path, name)
        try:
            st = os.stat(full)
            is_dir = os.path.isdir(full)
            items.append({
                "name": name + ("/" if is_dir else ""),
                "type": "dir" if is_dir else "file",
                "size": st.st_size,
                "size_h": utils.human_bytes(st.st_size),
                "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        except OSError:
            continue
    items.sort(key=lambda i: (i["type"] != "dir", i["name"].lower()))
    parent = os.path.dirname(path) if path != os.path.dirname(path) else path
    return ToolResult(data={
        "path": path,
        "parent": parent,
        "note": note or None,
        "count": len(items),
        "files": items[:300],
        "truncated": len(items) > 300,
    })


async def _read_file(args: dict, ctx: ToolContext) -> ToolResult:
    raw = str(args.get("path", "~"))
    limit = int(args.get("limit", 300))
    if os.name != "nt" and re_search_drive(raw):
        raw = "~"
    try:
        path = safety.resolve_path(raw, ctx.config)
    except safety.SafetyError as e:
        return ToolResult(ok=False, error=str(e), error_type="forbidden")
    if not os.path.exists(path):
        return ToolResult(ok=False, error=f"文件不存在: {path}", error_type="not_found", retryable=True)
    if os.path.isdir(path):
        return ToolResult(ok=False, error=f"{path} 是目录，请先列出目录", error_type="invalid_args")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".mp4", ".avi", ".mkv", ".zip", ".7z", ".exe", ".dll", ".so", ".pyc"):
        return ToolResult(ok=False, error=f"{ext} 是二进制文件，不适合直接读取", error_type="invalid_args")
    for enc in (args.get("encoding"), "utf-8", "utf-8-sig", "gbk", "latin-1"):
        if not enc:
            continue
        try:
            with open(path, "r", encoding=enc) as f:
                lines = f.readlines()
            if len(lines) > 2000:
                head = lines[:limit]
                total = len(lines)
                body = head
                truncated = True
            else:
                body = lines[:limit]
                truncated = len(lines) > limit
                total = len(lines)
            text = "".join(body)
            return ToolResult(data={
                "path": path,
                "encoding": enc,
                "total_lines": total,
                "truncated": truncated,
                "preview": text[:12000],
            })
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return ToolResult(ok=False, error=f"读取失败: {e}", error_type="exec")
    return ToolResult(ok=False, error="无法解码文件(可能是二进制)", error_type="invalid_args")


async def _write_file(args: dict, ctx: ToolContext) -> ToolResult:
    raw = str(args.get("path", ""))
    content = str(args.get("content", ""))
    append = bool(args.get("append", False))
    if not raw:
        return ToolResult(ok=False, error="缺少 path", error_type="invalid_args")
    try:
        path = safety.resolve_path(raw, ctx.config)
    except safety.SafetyError as e:
        return ToolResult(ok=False, error=str(e), error_type="forbidden")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return ToolResult(ok=False, error=f"写入失败: {e}", error_type="exec", retryable=True)
    return ToolResult(data={"path": path, "bytes": len(content.encode("utf-8")), "appended": append})


async def _create_folder(args: dict, ctx: ToolContext) -> ToolResult:
    raw = str(args.get("path", ""))
    if not raw:
        return ToolResult(ok=False, error="缺少 path", error_type="invalid_args")
    try:
        path = safety.resolve_path(raw, ctx.config)
        os.makedirs(path, exist_ok=True)
    except safety.SafetyError as e:
        return ToolResult(ok=False, error=str(e), error_type="forbidden")
    except Exception as e:
        return ToolResult(ok=False, error=f"创建失败: {e}", error_type="exec")
    return ToolResult(data={"path": path})


def _guard_delete(path: str, ctx: ToolContext) -> str | None:
    """删除前保护检查: 根路径/用户主目录本身/系统关键目录一律拒绝(返回错误文案)。"""
    home = os.path.realpath(os.path.expanduser("~"))
    real = os.path.realpath(path)
    if real in (os.path.realpath("/"), os.path.realpath(home),
                os.path.realpath("/home"), os.path.realpath("/System"),
                os.path.realpath("C:\\"), os.path.realpath("C:\\Windows")):
        return "出于安全考虑, 禁止删除根路径/用户主目录/系统目录本身"
    if os.name != "nt" and (real.startswith("/etc") or real.startswith("/usr") or real.startswith("/boot")
                            or real.startswith("/proc") or real.startswith("/sys") or real.startswith("/dev")
                            or real.startswith("/var/lib")):
        return "出于安全考虑, 禁止删除系统关键目录"
    return None


async def _delete_file(args: dict, ctx: ToolContext) -> ToolResult:
    raw = str(args.get("path", ""))
    if not raw:
        return ToolResult(ok=False, error="缺少 path", error_type="invalid_args")
    try:
        path = safety.resolve_path(raw, ctx.config)
    except safety.SafetyError as e:
        return ToolResult(ok=False, error=str(e), error_type="forbidden")
    if not os.path.exists(path):
        return ToolResult(ok=False, error=f"文件不存在: {path}", error_type="not_found", retryable=True)
    if os.path.isdir(path):
        return await _delete_folder({"path": path, "_resolved": True}, ctx)
    guard = _guard_delete(path, ctx)
    if guard:
        return ToolResult(ok=False, error=guard, error_type="forbidden")
    try:
        os.remove(path)
    except Exception as e:
        return ToolResult(ok=False, error=f"删除失败: {e}", error_type="exec", retryable=True)
    return ToolResult(data={"deleted": path, "type": "file", "status": "deleted"}, admin=True)


async def _delete_folder(args: dict, ctx: ToolContext) -> ToolResult:
    raw = str(args.get("path", ""))
    if not raw:
        return ToolResult(ok=False, error="缺少 path", error_type="invalid_args")
    if args.get("_resolved"):
        path = raw
    else:
        try:
            path = safety.resolve_path(raw, ctx.config)
        except safety.SafetyError as e:
            return ToolResult(ok=False, error=str(e), error_type="forbidden")
    if not os.path.exists(path):
        return ToolResult(ok=False, error=f"目录不存在: {path}", error_type="not_found", retryable=True)
    guard = _guard_delete(path, ctx)
    if guard:
        return ToolResult(ok=False, error=guard, error_type="forbidden")
    import shutil
    try:
        shutil.rmtree(path)
    except Exception as e:
        return ToolResult(ok=False, error=f"删除目录失败: {e}", error_type="exec", retryable=True)
    return ToolResult(data={"deleted": path, "type": "folder", "status": "deleted"}, admin=True)


async def _package_download(args: dict, ctx: ToolContext) -> ToolResult:
    """将指定路径(单文件或多文件/目录)打包后放入下载区。返回下载链接。"""
    paths = args.get("paths", []) or []
    if isinstance(paths, str):
        paths = [paths]
    name = str(args.get("name", "download"))
    if not paths:
        return ToolResult(ok=False, error="缺少 paths", error_type="invalid_args")
    resolved = []
    for p in paths:
        try:
            rp = safety.resolve_path(str(p), ctx.config)
        except safety.SafetyError as e:
            return ToolResult(ok=False, error=str(e), error_type="forbidden")
        if not os.path.exists(rp):
            return ToolResult(ok=False, error=f"路径不存在: {rp}", error_type="not_found", retryable=True)
        resolved.append(rp)
    try:
        import uuid
        sid_safe = utils.safe_filename(ctx.session_id, "s")
        out_dir = os.path.join(ctx.downloads_dir, sid_safe)
        os.makedirs(out_dir, exist_ok=True)
        if len(resolved) == 1 and os.path.isfile(resolved[0]):
            dst_name = utils.safe_filename(name, os.path.basename(resolved[0]))
            dst = os.path.join(out_dir, dst_name)
            shutil.copy2(resolved[0], dst)
            rel = os.path.join(sid_safe, dst_name)
        else:
            dst_name = utils.safe_filename(name, "project") + ".zip"
            dst = os.path.join(out_dir, dst_name)
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in resolved:
                    base = os.path.basename(p.rstrip(os.sep)) or "item"
                    if os.path.isdir(p):
                        for root, dirs, files in os.walk(p):
                            for fn in files:
                                full = os.path.join(root, fn)
                                arc = os.path.join(base, os.path.relpath(full, p)).replace(os.sep, "/")
                                zf.write(full, arc)
                    else:
                        zf.write(p, os.path.join(base, os.path.basename(p)))
            rel = os.path.join(sid_safe, dst_name)
        url = f"/api/downloads/{rel.replace(os.sep, '/')}"
        return ToolResult(data={"filename": os.path.basename(dst), "url": url,
                                "multi": len(resolved) > 1, "count": len(resolved)},
                          downloads=[(os.path.basename(dst), dst)])
    except Exception as e:
        return ToolResult(ok=False, error=f"打包失败: {e}", error_type="exec", retryable=True)


async def _preview_file(args: dict, ctx: ToolContext) -> ToolResult:
    """预览可预览的内容: 图片(base64->控制板image窗口)/HTML(html_preview)/文本(code窗口)。"""
    raw = str(args.get("path", "~"))
    if os.name != "nt" and re_search_drive(raw):
        raw = "~"
    try:
        path = safety.resolve_path(raw, ctx.config)
    except safety.SafetyError as e:
        return ToolResult(ok=False, error=str(e), error_type="forbidden")
    # 目录 -> 复用 list_directory(别名映射); 相对/~ 路径且不存在 -> 走别名映射与回退
    if os.path.isdir(path):
        return await _list_directory({"path": raw, "hidden": False}, ctx)
    if (not os.path.isabs(raw) or raw.startswith("~")) and not os.path.exists(path):
        return await _list_directory({"path": raw, "hidden": False}, ctx)
    if not os.path.exists(path):
        return ToolResult(ok=False, error=f"文件不存在: {path}", error_type="not_found", retryable=True)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"):
        try:
            with open(path, "rb") as f:
                import base64
                data = f.read()
            if len(data) > 8 * 1024 * 1024:
                return ToolResult(ok=False, error="图片超过 8MB, 不适合直接预览", error_type="invalid_args")
            b64 = base64.b64encode(data).decode()
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp"}.get(ext.lstrip("."), "image/png")
            return ToolResult(data={"preview_type": "image", "path": path,
                                    "image_base64": b64, "mime": mime})
        except Exception as e:
            return ToolResult(ok=False, error=f"预览图片失败: {e}", error_type="exec", retryable=True)
    if ext in (".html", ".htm"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                html = f.read()
            return ToolResult(data={"preview_type": "html", "path": path, "html": html[:200000]})
        except Exception as e:
            return ToolResult(ok=False, error=f"预览 HTML 失败: {e}", error_type="exec")
    return await _read_file({"path": path, "limit": int(args.get("limit", 500))}, ctx)


def register(registry: ToolRegistry):
    registry.register(Tool(
        name="list_directory",
        description="列出指定目录(或盘符, 如 C:/)下的文件与文件夹。结果必须在控制板 files 弹窗展示。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径, 如 C:/、/home/user、~; 留空为家目录"},
                "hidden": {"type": "boolean", "description": "是否包含隐藏文件"},
            },
        },
        handler=_list_directory,
        category="filesystem",
    ))
    registry.register(Tool(
        name="read_file",
        description="读取文本文件内容(带行号预览)。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer", "description": "预览最大行数, 默认300"},
                "encoding": {"type": "string"},
            },
            "required": ["path"],
        },
        handler=_read_file,
        category="filesystem",
    ))
    registry.register(Tool(
        name="write_file",
        description="创建或覆盖/追加写入文本文件。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "append": {"type": "boolean"},
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
        category="filesystem",
    ))
    registry.register(Tool(
        name="create_folder",
        description="创建文件夹。",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        handler=_create_folder,
        category="filesystem",
    ))
    registry.register(Tool(
        name="delete_file",
        description="删除文件(高危操作, 需系统登录密码解锁后执行)。根路径/主目录/系统关键目录受保护拒绝。",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        handler=_delete_file,
        category="filesystem",
    ))
    registry.register(Tool(
        name="delete_folder",
        description="删除文件夹(递归, 高危操作, 需系统登录密码解锁后执行)。根路径/主目录/系统关键目录受保护拒绝。",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        handler=_delete_folder,
        category="filesystem",
    ))
    registry.register(Tool(
        name="package_download",
        description="把单文件或多文件/目录打包(多文件自动打成ZIP)并放入下载区, 返回下载链接。用户要求\"下载代码/文件\"时使用。",
        parameters={
            "type": "object",
            "properties": {
                "paths": {"description": "要打包的文件或目录路径列表(可单个字符串)", "items": {"type": "string"}},
                "name": {"type": "string", "description": "下载文件名(不含扩展名)"},
            },
            "required": ["paths"],
        },
        handler=_package_download,
        category="filesystem",
    ))
    registry.register(Tool(
        name="preview_file",
        description="预览可预览的内容: 图片(base64 在控制板以 image 窗口渲染, 可放大)/HTML(html_preview 窗口)/文本(code 窗口)。用户说'预览/看看 XX 图片或文件'时使用。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer", "description": "文本预览行数, 默认500"},
            },
            "required": ["path"],
        },
        handler=_preview_file,
        category="filesystem",
    ))
