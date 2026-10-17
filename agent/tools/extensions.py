# -*- coding: utf-8 -*-
"""工具：扩展包系统(ext_list / ext_match / ext_read / ext_run / ext_install / ext_toggle)。

v3.42: 用户上传扩展包(zip: manifest.json + usage.md + src/main.py), Agent 自动解压到
extensions/<name>/, AI 只能使用已启用的扩展; AI 面对指令先按"使用用途标题"检索匹配
扩展(ext_match), 再 ext_read 读取代码与使用方法, 最后 ext_run 执行。执行由小AI 监管。
"""
import os

from agent import extensions as _ext
from .registry import Tool, ToolResult, ToolContext, ToolRegistry


def _root(ctx: ToolContext) -> str | None:
    try:
        return ctx.config.get("root") or None
    except Exception:
        return None


async def _ext_list(args: dict, ctx: ToolContext) -> ToolResult:
    ext_list = _ext.list_extensions(_root(ctx), enabled_only=True)
    return ToolResult(data={"extensions": ext_list, "count": len(ext_list)},
                      message=f"已启用扩展 {len(ext_list)} 个")


async def _ext_match(args: dict, ctx: ToolContext) -> ToolResult:
    query = str(args.get("query") or args.get("text") or "")
    hits = _ext.match_extension(query, _root(ctx))
    return ToolResult(data={"query": query, "matches": hits, "count": len(hits)},
                      message=f"匹配到 {len(hits)} 个已启用扩展")


async def _ext_read(args: dict, ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or "")
    info = _ext.read_extension(name, _root(ctx))
    if info is None:
        return ToolResult(ok=False, error=f"扩展不存在或未启用: {name}", error_type="not_found")
    return ToolResult(data=info, message=f"已读取扩展 {info.get('title')} 的使用方法与代码")


async def _ext_run(args: dict, ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or "")
    run_args = args.get("args") or {}
    timeout = int(args.get("timeout") or 30)
    res = _ext.run_extension(name, run_args, _root(ctx), timeout=timeout)
    if not res.get("ok"):
        return ToolResult(ok=False, error=res.get("error") or "执行失败", error_type="exec")
    # 结果在 HUD 展示(用户可见)
    try:
        await ctx.emitter.panel("ext_run", "data", f"扩展执行: {name}", res, status="ok")
    except Exception:
        pass
    return ToolResult(data=res, message=f"扩展 {name} 执行完成")


async def _ext_install(args: dict, ctx: ToolContext) -> ToolResult:
    zip_path = str(args.get("zip_path") or args.get("path") or "")
    url = str(args.get("url") or "")
    if url and not zip_path:
        # 联网下载扩展包到临时目录
        try:
            from agent.tools.web import _download_file
            zip_path = await _download_file(url, ".zip")
        except Exception as e:
            return ToolResult(ok=False, error=f"下载扩展包失败: {e}", error_type="exec")
    if not zip_path or not os.path.isfile(zip_path):
        return ToolResult(ok=False, error="需要 zip_path(本地路径) 或 url(联网下载)", error_type="invalid_args")
    res = _ext.install_extension(zip_path, _root(ctx))
    if not res.get("ok"):
        return ToolResult(ok=False, error=res.get("error"), error_type="exec")
    try:
        await ctx.emitter.panel("ext_install", "data", "扩展安装成功",
                                {"扩展名": res.get("name"), "标题": res.get("title"),
                                 "用途": res.get("purpose"), "状态": "已启用"}, status="ok")
    except Exception:
        pass
    return ToolResult(data=res, message=f"扩展 {res.get('title')} 安装成功并已启用")


async def _ext_toggle(args: dict, ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or "")
    enabled = bool(args.get("enabled", True))
    res = _ext.set_enabled(name, enabled, _root(ctx))
    if not res.get("ok"):
        return ToolResult(ok=False, error=res.get("error"), error_type="not_found")
    return ToolResult(data=res, message=f"扩展 {name} {'已启用' if enabled else '已禁用'}")


def register(registry: ToolRegistry):
    registry.register(Tool(
        name="ext_list",
        description="列出当前【已启用】的全部扩展(名称/标题/用途/是否有代码和使用方法)。"
                     "AI 执行任何指令前应先调用本工具或 ext_match 检查是否有可用的扩展。",
        parameters={"type": "object", "properties": {}},
        handler=_ext_list,
        category="extensions",
    ))
    registry.register(Tool(
        name="ext_match",
        description="检索【已启用】扩展: 用指令文本匹配扩展的『使用用途标题』, 返回相关度"
                     "排序的匹配列表。供 AI 参考判断是否使用扩展(可选能力, 非强制)。",
        parameters={"type": "object",
                    "properties": {"query": {"type": "string", "description": "用户指令/任务描述"}},
                    "required": ["query"]},
        handler=_ext_match,
        category="extensions",
    ))
    registry.register(Tool(
        name="ext_read",
        description="读取指定【已启用】扩展的完整信息: 使用用途标题、使用方法(usage.md)和"
                     "入口代码(src/main.py)。使用扩展前必须先读取, 按使用方法调用。",
        parameters={"type": "object",
                    "properties": {"name": {"type": "string", "description": "扩展名"}},
                    "required": ["name"]},
        handler=_ext_read,
        category="extensions",
    ))
    registry.register(Tool(
        name="ext_run",
        description="执行已启用扩展: 以扩展的 run(args) 入口在扩展目录运行, 传入 args 字典, "
                     "返回结果。参数须符合该扩展 usage.md 的说明(先 ext_read)。",
        parameters={"type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "args": {"type": "object", "description": "传给 run(args) 的参数字典"},
                        "timeout": {"type": "integer"},
                    },
                    "required": ["name"]},
        handler=_ext_run,
        category="extensions",
    ))
    registry.register(Tool(
        name="ext_install",
        description="安装扩展包: 本地 zip 路径(zip_path)或联网 URL(url)二选一; 安装后自动"
                     "解压到 extensions/<name>/ 并默认启用。扩展包需含 manifest.json"
                     "(name/title/purpose/enabled) + usage.md + src/main.py(run 入口)。",
        parameters={"type": "object",
                    "properties": {
                        "zip_path": {"type": "string", "description": "本地 zip 文件路径"},
                        "url": {"type": "string", "description": "扩展包 zip 的下载链接"},
                    }},
        handler=_ext_install,
        category="extensions",
    ))
    registry.register(Tool(
        name="ext_toggle",
        description="启用或禁用扩展(禁用后 AI 无法读取/执行该扩展)。",
        parameters={"type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "enabled": {"type": "boolean", "description": "true 启用 / false 禁用"},
                    },
                    "required": ["name", "enabled"]},
        handler=_ext_toggle,
        category="extensions",
    ))
