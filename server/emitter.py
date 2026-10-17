# -*- coding: utf-8 -*-
"""NEURA 面板事件发射器: 把 Agent 内部事件推送到前端 WebSocket。"""
import asyncio
import json


class PanelEmitter:
    """所有方法均为 async; 内部串行发送, 保证同一连接消息有序。"""

    def __init__(self, ws):
        self.ws = ws
        self._lock = asyncio.Lock()
        self._last_notify: tuple | None = None

    async def send(self, msg: dict):
        try:
            async with self._lock:
                await self.ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def hello(self, sessions: list, config_summary: dict):
        await self.send({"type": "hello", "sessions": sessions, "config": config_summary})

    async def session_created(self, session: dict):
        await self.send({"type": "session_created", "session": session})

    async def session_updated(self, session: dict):
        await self.send({"type": "session_updated", "session": session})

    async def session_deleted(self, session_id: str):
        await self.send({"type": "session_deleted", "session_id": session_id})

    async def agent_status(self, status: str, mode: str = ""):
        await self.send({"type": "agent_status", "status": status, "mode": mode})

    async def message_delta(self, session_id: str, text: str, final: bool = False):
        await self.send({"type": "message_delta", "session_id": session_id, "text": text, "final": final})

    async def message_done(self, session_id: str, text: str, tools: list, blocked: bool = False):
        """blocked=True: 该条回复是小AI 拦截(恶意/高危)消息, 前端整条气泡显示为红色。"""
        await self.send({"type": "message_done", "session_id": session_id,
                         "text": text, "tools": tools, "blocked": blocked})

    async def tool_event(self, tool: str, args: dict, status: str, error: str | None = None,
                         admin: bool = False):
        await self.send({"type": "tool_event", "tool": tool, "args": args,
                         "status": status, "error": error, "admin": admin})

    async def panel(self, window_id: str, window_type: str, title: str, content, status: str | None = None,
                    admin: bool = False):
        await self.send({"type": "panel", "window_id": window_id, "window_type": window_type,
                         "title": title, "content": content, "status": status, "admin": admin})

    async def panel_update(self, window_id: str, content=None, status: str | None = None, append: bool | None = None):
        await self.send({"type": "panel_update", "window_id": window_id,
                         "content": content, "status": status, "append": append})

    async def panel_close(self, window_id: str):
        await self.send({"type": "panel_close", "window_id": window_id})

    async def panel_control(self, action: str, window_id: str, x=None, y=None, kind=None, direction=None):
        await self.send({"type": "panel_control", "action": action,
                         "window_id": window_id, "x": x, "y": y, "kind": kind, "direction": direction})

    async def admin_challenge(self, operation_id: str, description: str, tool: str, args):
        """高危操作验证: 前端在控制板弹【系统登录密码验证窗口】。"""
        await self.send({"type": "admin_challenge", "operation_id": operation_id,
                         "description": description, "tool": tool, "args": args})

    async def admin_auth_result(self, operation_id: str, ok: bool, message: str):
        await self.send({"type": "admin_auth_result", "operation_id": operation_id,
                         "ok": ok, "message": message})

    async def notify(self, message: str, level: str = "info"):
        # v3.40: 连续相同通知去重(避免"窗口过多"等提示反复刷屏)
        if (message, level) == self._last_notify:
            return
        self._last_notify = (message, level)
        await self.send({"type": "notify", "message": message, "level": level})

    async def download_ready(self, filename: str, url: str):
        await self.send({"type": "download_ready", "filename": filename, "url": url})

    async def command_stream(self, window_id: str, line: str, status: str = "running"):
        """实时命令窗口: 逐行追加命令输出(流式)。"""
        await self.send({"type": "command_stream", "window_id": window_id,
                         "line": line, "status": status})

    async def confirm_request(self, confirm_id: str, title: str, description: str, package: str = ""):
        """缺依赖等需要用户决定的场景: 在控制板弹【是否安装】确认窗口(非系统弹窗)。"""
        await self.send({"type": "confirm_request", "confirm_id": confirm_id,
                         "title": title, "description": description, "package": package})

    async def tool_synthesized(self, name: str, code: str, review: str):
        await self.send({"type": "tool_synthesized", "name": name, "code": code, "review": review})

    async def error(self, message: str):
        await self.send({"type": "error", "message": message})
