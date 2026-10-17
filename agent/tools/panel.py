# -*- coding: utf-8 -*-
"""工具：控制板弹窗(panel_popup / panel_update / panel_close / panel_control / notify)。"""
import json
import uuid
from .registry import Tool, ToolResult, ToolContext, ToolRegistry


def _gen_win_id() -> str:
    return "win_" + uuid.uuid4().hex[:10]


async def _panel_popup(args: dict, ctx: ToolContext) -> ToolResult:
    title = str(args.get("title", "信息"))
    content = args.get("content", "")
    wtype = str(args.get("window_type", "info"))
    window_id = str(args.get("window_id", "") or _gen_win_id())
    status = args.get("status")
    await ctx.emitter.panel(window_id, wtype, title, content, status=status)
    return ToolResult(data={"window_id": window_id, "status": "opened"})


async def _panel_update(args: dict, ctx: ToolContext) -> ToolResult:
    window_id = str(args.get("window_id", ""))
    if not window_id:
        return ToolResult(ok=False, error="缺少 window_id", error_type="invalid_args")
    await ctx.emitter.panel_update(window_id, content=args.get("content"),
                                   status=args.get("status"), append=args.get("append"))
    return ToolResult(data={"window_id": window_id, "status": "updated"})


async def _panel_close(args: dict, ctx: ToolContext) -> ToolResult:
    window_id = str(args.get("window_id", ""))
    if not window_id:
        return ToolResult(ok=False, error="缺少 window_id", error_type="invalid_args")
    await ctx.emitter.panel_close(window_id)
    return ToolResult(data={"window_id": window_id, "status": "closed"})


async def _panel_control(args: dict, ctx: ToolContext) -> ToolResult:
    """AI 窗口管理: 像《极限审判》中的 AI 那样自由关闭/滑动/聚焦/最小化控制板窗口。"""
    action = str(args.get("action", "close"))
    window_id = str(args.get("window_id", "") or "")
    if action == "resize":
        # 缩放窗口: direction=in(放大一点) / out(缩小一点); 前端丝滑缩放动画
        direction = str(args.get("direction", "out") or "out")
        await ctx.emitter.panel_control("resize", window_id, direction=direction)
        return ToolResult(data={"action": "resize", "direction": direction, "window_id": window_id, "status": "ok"})
    if action == "layout":
        # AI 自主连续调整窗口: 一次指令完成所有窗口的最大化/整齐排列(前端串联丝滑动画)
        kind = str(args.get("kind", "maximize_all") or "maximize_all")
        await ctx.emitter.panel_control("layout", "", kind=kind)
        return ToolResult(data={"action": "layout", "kind": kind, "status": "ok"})
    if action not in ("close", "close_all", "move", "focus", "minimize", "restore", "maximize", "resize"):
        return ToolResult(ok=False, error=f"不支持的动作: {action}", error_type="invalid_args")
    # window_id 允许为空: 由前端作用于当前聚焦窗口(用户说"关闭窗口"时最自然)
    await ctx.emitter.panel_control(action, window_id,
                                    x=args.get("x"), y=args.get("y"))
    return ToolResult(data={"action": action, "window_id": window_id, "status": "ok"})


async def _notify(args: dict, ctx: ToolContext) -> ToolResult:
    await ctx.emitter.notify(str(args.get("message", "")), level=str(args.get("level", "info")))
    return ToolResult(data={"status": "notified"})


def register(registry: ToolRegistry):
    registry.register(Tool(
        name="panel_popup",
        description="在左侧控制板中以控制板风格弹窗显示信息(不是系统弹窗)。window_type: info文本 / data表格数据 / code代码 / process过程日志 / files文件列表 / chart图表(带chart字段) / download下载 / weather天气。任何需要向用户展示的结构化结果都必须用本工具。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "弹窗标题"},
                "content": {"description": "内容。字符串或对象(不同 window_type 前端有对应渲染器)"},
                "window_type": {"type": "string", "enum": ["info", "data", "code", "process", "files", "chart", "download", "weather"]},
                "window_id": {"type": "string", "description": "可选, 指定窗口ID以复用/覆盖"},
                "status": {"type": "string", "description": "可选状态: running/ok/error/closed"},
            },
            "required": ["title", "content"],
        },
        handler=_panel_popup,
        category="panel",
    ))
    registry.register(Tool(
        name="panel_update",
        description="更新/追加控制板已有弹窗的内容。append=true 时追加文本到过程窗口, status 可更新 running/ok/error。",
        parameters={
            "type": "object",
            "properties": {
                "window_id": {"type": "string"},
                "content": {"description": "新内容(字符串或对象)"},
                "status": {"type": "string", "enum": ["running", "ok", "error", "closed"]},
                "append": {"type": "boolean", "description": "是否追加(用于 process 日志窗口)"},
            },
            "required": ["window_id"],
        },
        handler=_panel_update,
        category="panel",
    ))
    registry.register(Tool(
        name="panel_close",
        description="关闭控制板中的一个弹窗。",
        parameters={"type": "object", "properties": {"window_id": {"type": "string"}}, "required": ["window_id"]},
        handler=_panel_close,
        category="panel",
    ))
    registry.register(Tool(
        name="panel_control",
        description="像《极限审判》中的 AI 一样自由管理控制板窗口: close关闭指定窗口 / close_all关闭全部窗口 / move滑动窗口到指定位置(x,y为像素,省略则回到初始位置) / focus聚焦窗口 / minimize最小化 / restore还原 / maximize最大化 / layout一次指令自主连续调整所有窗口(kind=maximize_all全部最大化/grid网格平铺)。用户要求「关闭窗口」「关闭所有窗口」「把窗口移开」「最小化窗口」「整理窗口」「窗口平铺」时调用。",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["close", "close_all", "move", "focus", "minimize", "restore", "maximize", "layout"]},
                "window_id": {"type": "string", "description": "目标窗口ID; close_all 时忽略"},
                "x": {"type": "integer", "description": "move 目标横坐标(像素, 相对控制板画布)"},
                "y": {"type": "integer", "description": "move 目标纵坐标(像素, 相对控制板画布)"},
                "kind": {"type": "string", "description": "layout 时的布局: maximize_all 全部最大化 / grid 网格平铺"},
            },
            "required": ["action"],
        },
        handler=_panel_control,
        category="panel",
    ))
    registry.register(Tool(
        name="notify",
        description="在控制板上方弹出一条短暂的提示通知。",
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "level": {"type": "string", "enum": ["info", "success", "warning", "error"]},
            },
            "required": ["message"],
        },
        handler=_notify,
        category="panel",
    ))
