# -*- coding: utf-8 -*-
"""NEURA 桌面端 UI: 悬浮球(点击展开对话框/再点击收起) + HUD 对话框 + 事件桥。

v3.33 桌面版: 去掉控制板, 只有一个悬浮球; Agent 的 HUD 弹窗直接渲染到电脑屏幕
(hud.HudManager)。事件桥 DesktopEmitter 把 AgentCore 事件推入队列, tkinter 主线程
轮询更新界面; 用户在对话框发消息 -> 同线程 asyncio 任务执行。
说明: 全部控件 Canvas 自绘(规避部分 Linux/Xvfb 构建对 Label/Button/Entry/Text 的
     xcb 断言), Windows / Linux 行为一致; 中文输入经 Tk 键盘事件(IEM/XIM 提交)正常。
"""
import asyncio
import os
import json
import queue
import tkinter as tk

from .hud import HudManager, HudWindow, CanvasText, HUDButton, _glass, _lerp_hex

# v3.43 macOS 玻璃风格: 与 HUD 统一(中性深灰磨砂玻璃)
_BG = "#1a1c21"
_BG2 = "#22252c"
_EDGE = "#aab4c0"
_FG = "#f2f4f7"
_DIM = "#98a1ad"
_ACCENT = "#5e9bff"
_GLASS_HI = "#ffffff"
_GLASS_L2 = "#2b2f38"
_TRAFFIC = ("#ff5f57", "#febc2e", "#28c840")
_RED = "#ff4d5e"
_FONT = ("Microsoft YaHei UI", 10)
_FONT_B = ("Microsoft YaHei UI", 11, "bold")
_FONT_T = ("Microsoft YaHei UI", 9)


class ChatCanvas:
    """Canvas 自绘对话区: 每条消息可独立着色(用户蓝 / AI 青 / 拦截红 / 工具灰)。"""

    def __init__(self, canvas: tk.Canvas):
        self.cv = canvas
        self.entries = []          # [(text, color)]
        self._items = []
        self._w = 200
        try:
            canvas.bind("<Configure>", self._on_resize)
        except Exception:
            pass

    def _on_resize(self, e):
        self._w = max(120, e.width - 20)
        self._redraw()

    def add(self, text, color=_FG):
        self.entries.append((str(text), color))
        self._redraw(scroll_to_end=True)

    def replace_last_ai(self, text, color=_FG):
        """替换最后一条 AI 流式占位(合并增量)。"""
        for i in range(len(self.entries) - 1, -1, -1):
            t, c = self.entries[i]
            if c in (_FG,) and (t.startswith("NEURA") or (not t.startswith("你") and not t.startswith("⚙") and not t.startswith("ℹ"))):
                self.entries[i] = (str(text), color)
                self._redraw(scroll_to_end=True)
                return
        self.add(text, color)

    def clear(self):
        self.entries = []
        self._redraw()

    def _redraw(self, scroll_to_end=False):
        try:
            for it in self._items:
                self.cv.delete(it)
            self._items = []
            y = 8
            for text, color in self.entries:
                for ln in self._wrap(text):
                    self._items.append(self.cv.create_text(
                        12, y, text=ln, anchor="nw", font=_FONT_T, fill=color))
                    y += 20
                y += 8
            self.cv.configure(scrollregion=(0, 0, max(self._w, 300), max(y, 80)))
            if scroll_to_end:
                self.cv.yview_moveto(1.0)
        except Exception:
            pass

    def _wrap(self, text):
        out = []
        for raw in str(text).split("\n"):
            line = raw
            if not line:
                out.append("")
                continue
            while len(line) * 8 > self._w:
                cut = max(1, self._w // 8)
                out.append(line[:cut])
                line = line[cut:]
            out.append(line)
        return out


class CanvasInput:
    """Canvas 自绘输入框: 点击聚焦, 键盘输入(ASCII + IME 中文), 回车提交。"""

    def __init__(self, canvas: tk.Canvas, on_submit, placeholder="输入指令，回车发送…",
                 auto_focus: bool = True):
        self.cv = canvas
        self.on_submit = on_submit
        self.placeholder = placeholder
        self.text = ""
        self.focused = False
        self.auto_focus = auto_focus
        self._caret = None
        self._box = canvas.create_rectangle(4, 4, 600, 40, outline=_EDGE, fill=_BG)
        self._ph = canvas.create_text(14, 24, text=placeholder, anchor="w",
                                      font=_FONT_T, fill=_DIM)
        self._tx = canvas.create_text(14, 24, text="", anchor="w", font=_FONT, fill=_FG)
        try:
            canvas.configure(takefocus=1)      # 让 Canvas 可聚焦(默认 takefocus=0 点击后无输入效果)
            canvas.configure(cursor="xterm")   # v3.40 精修: 悬停/聚焦时显示 I-beam 输入光标
        except Exception:
            pass
        canvas.bind("<Button-1>", self._on_click)
        canvas.bind("<ButtonRelease-1>", lambda e: self._grab_focus())
        # 注意: 只绑定 <KeyPress>! <Key> 与 <KeyPress> 是同一事件, 双绑会让每个按键
        # 触发两次 _on_key(字符重复/显示错乱); 中文 IME 提交走 <KeyPress> 的 char 属性。
        canvas.bind("<KeyPress>", self._on_key)
        if auto_focus:
            canvas.focus_force()                # 强制获取键盘焦点(Linux 自绘输入独占)

    def _on_click(self, e):
        self.focused = True
        self._grab_focus()
        # 无边框窗口 caret 渲染依赖窗口激活: 点击时 lift+focus 顶层窗口
        try:
            win = self.cv.winfo_toplevel()
            win.lift()
            win.focus_force()
        except Exception:
            pass
        self._redraw()

    def _grab_focus(self):
        try:
            self.cv.focus_set()
            self.cv.focus_force()
        except Exception:
            try:
                self.cv.focus_set()
            except Exception:
                pass

    def _on_key(self, e):
        ks = getattr(e, "keysym", "")
        ch = getattr(e, "char", "")
        if ks == "Return":
            if self.text.strip():
                t = self.text
                self.text = ""
                self._redraw()
                self.on_submit(t)
            return "break"
        if ks == "BackSpace":
            self.text = self.text[:-1]
        elif ch and ord(ch) >= 32:
            self.text += ch
        self._redraw()
        return "break"   # 阻止冒泡: 避免对话框级兜底重复输入

    def _redraw(self):
        # 用文本内容切换而非 state 隐藏(规避个别平台 itemconfig state 异常导致
        # 输入不可见/错位的问题); 每次只更新两个文本 item 的内容
        try:
            self.cv.itemconfig(self._box, outline=_ACCENT if self.focused else _EDGE)
            if self.text:
                shown = self.text if len(self.text) <= 60 else "…" + self.text[-59:]
                self.cv.itemconfig(self._tx, text=shown)
                self.cv.itemconfig(self._ph, text="")
            else:
                self.cv.itemconfig(self._tx, text="")
                self.cv.itemconfig(self._ph, text=self.placeholder)
            # v3.40 精修: 聚焦时绘制插入光标竖线(文本末尾, 用 bbox 精确定位, 中文宽也准确)
            if self._caret is not None:
                try:
                    self.cv.delete(self._caret)
                except Exception:
                    pass
                self._caret = None
            if self.focused:
                bb = self.cv.bbox(self._tx)
                x0 = (bb[2] + 2) if bb and bb[2] > 0 else 14
                try:
                    self._caret = self.cv.create_rectangle(x0, 13, x0 + 2, 35,
                                                           fill=_ACCENT, outline="")
                except Exception:
                    pass
        except Exception:
            pass

    def set(self, text):
        self.text = text
        self._redraw()

    def get(self):
        return self.text

    def focus(self):
        self.cv.focus_set()


class DesktopEmitter:
    """AgentCore 事件发射器 -> 队列(线程安全), UI 主线程轮询消费。"""

    def __init__(self, q: "queue.Queue"):
        self.q = q

    async def _put(self, msg: dict):
        try:
            self.q.put(msg)
        except Exception:
            pass

    async def hello(self, sessions, config_summary):
        await self._put({"type": "hello", "sessions": sessions, "config": config_summary})
    async def session_created(self, session):
        await self._put({"type": "session_created", "session": session})
    async def session_updated(self, session):
        await self._put({"type": "session_updated", "session": session})
    async def session_deleted(self, session_id):
        await self._put({"type": "session_deleted", "session_id": session_id})
    async def agent_status(self, status, mode=""):
        await self._put({"type": "agent_status", "status": status, "mode": mode})
    async def message_delta(self, session_id, text, final=False):
        await self._put({"type": "message_delta", "session_id": session_id, "text": text, "final": final})
    async def message_done(self, session_id, text, tools, blocked=False):
        await self._put({"type": "message_done", "session_id": session_id,
                         "text": text, "tools": tools, "blocked": blocked})
    async def tool_event(self, tool, args, status, error=None, admin=False):
        await self._put({"type": "tool_event", "tool": tool, "args": args,
                         "status": status, "error": error, "admin": admin})
    async def panel(self, window_id, window_type, title, content, status=None, admin=False):
        await self._put({"type": "panel", "window_id": window_id, "window_type": window_type,
                         "title": title, "content": content, "status": status, "admin": admin})
    async def panel_update(self, window_id, content=None, status=None, append=None):
        await self._put({"type": "panel_update", "window_id": window_id,
                         "content": content, "status": status, "append": append})
    async def panel_close(self, window_id):
        await self._put({"type": "panel_close", "window_id": window_id})
    async def panel_control(self, action, window_id, x=None, y=None, kind=None, direction=None):
        await self._put({"type": "panel_control", "action": action, "window_id": window_id,
                         "x": x, "y": y, "kind": kind, "direction": direction})
    async def admin_challenge(self, operation_id, description, tool, args):
        await self._put({"type": "admin_challenge", "operation_id": operation_id,
                         "description": description, "tool": tool, "args": args})
    async def admin_auth_result(self, operation_id, ok, message):
        await self._put({"type": "admin_auth_result", "operation_id": operation_id,
                         "ok": ok, "message": message})
    async def notify(self, message, level="info"):
        await self._put({"type": "notify", "message": message, "level": level})
    async def download_ready(self, filename, url):
        await self._put({"type": "download_ready", "filename": filename, "url": url})
    async def command_stream(self, window_id, line, status="running"):
        await self._put({"type": "command_stream", "window_id": window_id, "line": line, "status": status})


class NeuraDesktopUI:
    """悬浮球 + HUD 对话框 + 事件消费。"""

    def __init__(self, core, cfg: dict):
        self.core = core
        self.cfg = cfg
        self.q: "queue.Queue" = queue.Queue()
        self.emitter = DesktopEmitter(self.q)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.current_sid: str | None = None
        self._ai_streaming = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("NEURA")
        self.hud = HudManager(self.root)
        self._build_ball()
        self._build_dialog()
        self.root.after(40, self._poll)
        self.root.after(200, self._refresh_sessions)

    # ================= 悬浮球 =================
    def _build_ball(self):
        self.ball = tk.Toplevel(self.root)
        self.ball.overrideredirect(True)
        try:
            self.ball.attributes("-topmost", True)
            self.ball.attributes("-alpha", 0.92)
        except Exception:
            pass
        size = 64
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.ball.geometry(f"{size}x{size}+{sw - size - 40}+{sh - size - 60}")
        cv = tk.Canvas(self.ball, width=size, height=size, bg=_BG, highlightthickness=0)
        cv.pack()
        cv.create_oval(2, 2, size - 2, size - 2, outline=_EDGE, width=2, fill=_BG2)
        cv.create_oval(12, 12, size - 12, size - 12, outline=_ACCENT, width=1, fill=_BG)
        cv.create_text(size // 2, size // 2, text="NEURA", fill=_FG,
                       font=("Segoe UI", 8, "bold"))
        cv.create_oval(size - 18, 4, size - 6, 16, outline="", fill=_ACCENT)
        self._drag = {"x": 0, "y": 0, "moved": False}
        # 仅绑定 Canvas(勿再绑 ball: 双绑定会让一次点击触发两次 toggle, 表现为"点击无反应")
        cv.bind("<Button-1>", self._ball_start)
        cv.bind("<B1-Motion>", self._ball_drag)
        cv.bind("<ButtonRelease-1>", self._ball_release)

    def _ball_start(self, e):
        self._drag.update({"x": e.x_root, "y": e.y_root, "moved": False})

    def _ball_drag(self, e):
        dx = e.x_root - self._drag["x"]
        dy = e.y_root - self._drag["y"]
        if abs(dx) + abs(dy) > 6:
            self._drag["moved"] = True
        x = self.ball.winfo_x() + dx
        y = self.ball.winfo_y() + dy
        self.ball.geometry(f"+{x}+{y}")
        self._drag["x"], self._drag["y"] = e.x_root, e.y_root

    def _ball_release(self, e):
        if not self._drag["moved"]:
            self._toggle_dialog()

    def _toggle_dialog(self):
        if self.dialog.winfo_viewable():
            self.dialog.withdraw()
        else:
            self._place_dialog()
            self.dialog.deiconify()
            self.dialog.lift()
            self._refresh_sessions()
            self.input.focus()

    # ================= 对话框 =================
    def _build_dialog(self):
        self.dialog = tk.Toplevel(self.root)
        self.dialog.title("NEURA 对话")
        self.dialog.overrideredirect(True)
        try:
            self.dialog.attributes("-topmost", True)
            self.dialog.attributes("-alpha", 0.80)   # v3.44 立体玻璃: 高透明
        except Exception:
            pass
        self._place_dialog()
        self.dialog.withdraw()
        # 对话框级键盘兜底: 点击会话/对话区导致焦点丢失后, 打字仍进输入框
        self.dialog.bind("<KeyPress>", lambda e: self._dialog_key(e))
        frame = tk.Frame(self.dialog, bg=_BG2, highlightbackground=_EDGE,
                         highlightthickness=1, bd=0)
        frame.pack(fill="both", expand=True)
        # 标题栏(Canvas 自绘)
        bar = tk.Canvas(frame, bg=_BG, height=34, highlightthickness=0)
        bar.pack(fill="x")
        bar.create_text(10, 17, text="◈ NEURA", font=("Segoe UI", 11, "bold"),
                        fill=_ACCENT, anchor="w")
        bar.create_text(120, 17, text="自主桌面智能体", font=_FONT_T, fill=_DIM, anchor="w")
        HUDButton(bar, 560, 6, 26, 22, "✕", fg=_DIM, command=self._toggle_dialog,
                  edge=_EDGE, bg=_BG)
        bar.bind("<Button-1>", lambda e: self._drag_dialog(e, 0))
        bar.bind("<B1-Motion>", lambda e: self._drag_dialog(e, 1))
        # 主体: 左会话列表 + 右对话区
        body = tk.Frame(frame, bg=_BG2)
        body.pack(fill="both", expand=True)
        self._build_session_list(body)
        self._build_chat(body)
        # 底部输入区(悬浮固定, 不随对话移动)
        self._build_input(frame)

    def _drag_dialog(self, e, mode):
        if mode == 0:
            self._dd = {"x": e.x_root - self.dialog.winfo_x(),
                        "y": e.y_root - self.dialog.winfo_y()}
        else:
            self.dialog.geometry(f"+{e.x_root - self._dd['x']}+{e.y_root - self._dd['y']}")

    def _place_dialog(self):
        w, h = 620, 660
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.dialog.geometry(f"{w}x{h}+{max(10, sw - w - 40)}+{max(10, sh - h - 80)}")

    def _build_session_list(self, body):
        left = tk.Frame(body, bg=_BG, width=150)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        cv = tk.Canvas(left, bg=_BG, height=26, highlightthickness=0)
        cv.pack(fill="x")
        cv.create_text(8, 13, text="会话", font=_FONT_T, fill=_DIM, anchor="w")
        cv.create_line(6, 22, 144, 22, fill="#123a52")
        list_cv = tk.Canvas(left, bg=_BG, highlightthickness=0)
        sb = tk.Scrollbar(left, orient="vertical", command=list_cv.yview)
        inner = tk.Frame(list_cv, bg=_BG)
        inner.bind("<Configure>", lambda e: list_cv.configure(scrollregion=list_cv.bbox("all")))
        list_cv.create_window((0, 0), window=inner, anchor="nw")
        list_cv.configure(yscrollcommand=sb.set)
        list_cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._sess_inner = inner
        self._sess_items = []

    def _build_chat(self, body):
        right = tk.Frame(body, bg=_BG2)
        right.pack(side="left", fill="both", expand=True)
        cv = tk.Canvas(right, bg=_BG, highlightthickness=0)
        sb = tk.Scrollbar(right, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._chat_canvas = cv
        self.chat = ChatCanvas(cv)
        self.chat.add("◈ NEURA 已就绪 — 点击悬浮球展开/收起", _DIM)
        # v3.39: 对话区鼠标滚轮滚动(直接滚, 无需拖滑杆)
        def _chat_wheel(e):
            try:
                if getattr(e, "num", 0) == 4:
                    cv.yview_scroll(-3, "units")
                elif getattr(e, "num", 0) == 5:
                    cv.yview_scroll(3, "units")
                else:
                    cv.yview_scroll(-3 if getattr(e, "delta", 0) > 0 else 3, "units")
                return "break"
            except Exception:
                return None
        cv.bind("<MouseWheel>", _chat_wheel)
        cv.bind("<Button-4>", _chat_wheel)
        cv.bind("<Button-5>", _chat_wheel)
        try:
            self.dialog.bind("<MouseWheel>", lambda e: _chat_wheel(e))
        except Exception:
            pass

    def _build_input(self, frame):
        # v3.47: Windows 用原生 Entry 独占输入(布局更高), Linux 用 Canvas 自绘输入
        _nt = os.name == "nt"
        inp_bar = tk.Frame(frame, bg=_BG, height=(80 if _nt else 64))
        inp_bar.pack(fill="x", side="bottom")
        inp_bar.pack_propagate(False)
        # v3.42: 工具栏(扩展管理)
        tool_bar = tk.Canvas(inp_bar, bg=_BG2, highlightthickness=0, height=26)
        tool_bar.pack(fill="x", padx=10, pady=(6, 0))
        HUDButton(tool_bar, 2, 1, 108, 22, "⬒ 扩展管理", fg=_EDGE, command=self._open_ext_manager,
                  edge=_EDGE, bg=_BG2)
        cv = tk.Canvas(inp_bar, bg=_BG2, highlightthickness=0, height=46)
        if _nt:
            # v3.47 修复: Windows 下隐藏自绘输入框, 防止其 focus_force 抢占 Entry 焦点
            # (导致"点击输入框无光标/输入不显示"), Entry 成为唯一可见输入通道
            cv.pack_forget()
        else:
            cv.pack(fill="x", padx=10, pady=8)
        cv.bind("<Button-1>", lambda e: (self.input._grab_focus(), self.input.focus()))
        self.input = CanvasInput(cv, self._send, auto_focus=not _nt)
        # v3.38: Windows 原生 Entry 输入代理 —— Canvas 自绘输入在个别 Windows 环境
        # 聚焦失败, 这里用一个隐藏 Entry 持有焦点并转发键盘事件(原生控件聚焦 100% 可靠),
        # 键入内容实时同步到 Canvas 显示。Linux(Xvfb 构建 Entry 触发 xcb 崩溃)保持纯 Canvas。
        # v3.39: Windows 直接使用【原生可见 Entry】作为输入框 —— Entry 由系统控件渲染
        # 键入文本, 100% 可靠(根治"输入内容不显示在输入框/跑到对话框上方")。
        # Linux(Xvfb 构建 Entry 触发 xcb 断言崩溃)保持 Canvas 自绘输入。
        if os.name == "nt":
            try:
                entry = tk.Entry(inp_bar, bg=_BG, fg=_FG, insertbackground=_FG,
                                 font=_FONT, relief="flat", bd=0,
                                 highlightthickness=1, highlightbackground=_EDGE,
                                 highlightcolor=_ACCENT, takefocus=1,
                                 cursor="xterm", insertwidth=2)
                entry.pack(fill="x", padx=10, pady=(9, 7), ipady=7)
                _PH = "输入指令，回车发送…"
                entry.insert(0, _PH)
                entry.configure(fg=_DIM)
                def _e_focus(_=None):
                    # v3.40 精修2: 无边框(overrideredirect)窗口在 Windows 上, Entry 插入光标
                    # 依赖窗口【真正激活】后才渲染 —— 点击时先 lift+focus_force 窗口,
                    # 再聚焦 Entry, 并延时多次强制重绘, 确保插入光标一定出现。
                    try:
                        win = entry.winfo_toplevel()
                        try:
                            win.lift()
                            win.focus_force()
                        except Exception:
                            pass
                        entry.focus_set()
                        entry.focus_force()
                        entry.configure(insertbackground=_FG, insertwidth=3)
                        entry.icursor("end")
                        entry.see("end")
                    except Exception:
                        pass
                    if entry.get() == _PH:
                        try:
                            entry.delete(0, "end")
                            entry.configure(fg=_FG)
                        except Exception:
                            pass
                    try:
                        def _re(_ms):
                            def _do():
                                try:
                                    entry.focus_force()
                                    entry.icursor("end")
                                    entry.see("end")
                                except Exception:
                                    pass
                            entry.after(_ms, _do)
                        for _ms in (40, 120, 300):
                            _re(_ms)
                    except Exception:
                        pass
                entry.bind("<Button-1>", lambda e: _e_focus())
                def _e_in(_=None):
                    try:
                        entry.icursor("end")
                        entry.see("end")
                        entry.configure(insertbackground=_FG, insertwidth=3)
                    except Exception:
                        pass
                entry.bind("<FocusIn>", _e_in)
                def _e_lost(_=None):
                    try:
                        if not entry.get().strip():
                            entry.insert(0, _PH)
                            entry.configure(fg=_DIM)
                    except Exception:
                        pass
                entry.bind("<FocusOut>", _e_lost)
                def _e_send(e):
                    t = entry.get()
                    if t and t != _PH:
                        entry.delete(0, "end")
                        entry.configure(fg=_FG)
                        try:
                            self._send(t)
                        except Exception:
                            pass
                    # v3.40 精修: 发送后重新聚焦输入框(焦点不丢, 下一条指令直接输入)
                    try:
                        entry.focus_force()
                    except Exception:
                        pass
                    return "break"
                entry.bind("<Return>", _e_send)
                cv.bind("<Button-1>", lambda e: _e_focus())
                self.input_entry = entry
                self._input_proxy = None
            except Exception:
                self.input_entry = None
                self._input_proxy = None
        else:
            self.input_entry = None
            self._input_proxy = None

    def _dialog_key(self, e):
        """对话框级键盘兜底: 仅当焦点不在输入框时转发(避免与输入框自身处理冲突)。
        点击会话/对话区导致焦点丢失后, 打字仍进输入框。"""
        if not hasattr(self, "input"):
            return
        try:
            cur = self.root.focus_get()
            # 焦点在任一输入通道(Camera 自绘输入框 / Windows 原生 Entry)时, 兜底不重复
            if cur is self.input.cv:
                return
            if getattr(self, "input_entry", None) is not None and cur is self.input_entry:
                return
        except Exception:
            pass
        ks = getattr(e, "keysym", "")
        ch = getattr(e, "char", "")
        if ch or ks in ("BackSpace", "Return"):
            self.input._on_key(e)

    # ================= 会话与消息 =================
    def _refresh_sessions(self):
        for it in getattr(self, "_sess_items", []):
            try:
                it[1].destroy()
            except Exception:
                pass
        self._sess_items = []
        try:
            sessions = self.core.sessions.list()[:20]
        except Exception:
            sessions = []
        for s in sessions:
            sid, title = s.id, s.title
            bg = _BG2 if sid == self.current_sid else _BG
            fg = _FG if sid == self.current_sid else _DIM
            b = tk.Frame(self._sess_inner, bg=bg, height=26)
            b.pack(fill="x", padx=4, pady=2)
            cv = tk.Canvas(b, bg=bg, height=26, highlightthickness=0)
            cv.pack(fill="both", expand=True)
            cv.create_text(8, 13, text=("● " if sid == self.current_sid else "· ") + title[:12],
                           font=_FONT_T, fill=fg, anchor="w")
            cv.bind("<Button-1>", lambda e, sid=sid: self._switch_session(sid))
            b.bind("<Button-1>", lambda e, sid=sid: self._switch_session(sid))
            self._sess_items.append((sid, b))

    def _switch_session(self, sid):
        self.current_sid = sid
        self._load_chat(sid)
        self._refresh_sessions()

    def _ensure_session(self):
        if not self.current_sid:
            s = self.core.sessions.create()
            self.current_sid = s.id
        return self.current_sid

    def _load_chat(self, sid):
        self.chat.clear()
        try:
            s = self.core.sessions.get(sid)
        except Exception:
            s = None
        if s:
            for m in s.history[-60:]:
                role, content = m.get("role", ""), m.get("content", "")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)[:200]
                if role == "user":
                    self.chat.add("你: " + str(text)[:200], _ACCENT)
                elif role == "assistant":
                    self.chat.add("NEURA: " + str(text)[:300], _FG)

    def _send(self, text=None):
        if text is None and getattr(self, "input_entry", None) is not None:
            try:
                text = self.input_entry.get()
            except Exception:
                text = None
        text = (text or (self.input.get() if hasattr(self, "input") else "")).strip()
        if not text or self._ai_streaming:
            return
        sid = self._ensure_session()
        self.chat.add("你: " + text, _ACCENT)
        self.chat.add("NEURA: ", _FG)
        self._ai_streaming = True
        if self.loop is None or self.loop.is_closed():
            self._ai_streaming = False
            return
        try:
            # 单线程 pump: 显式挂到 self.loop(无需 running loop, after 回调中也有效)
            self.loop.create_task(self.core.start_turn(sid, text, self.emitter))
        except Exception as e:
            self.chat.add(f"(发送失败: {e})", _DIM)
            self._ai_streaming = False

    # ================= 事件消费 =================
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    def _handle(self, m: dict):
        t = m.get("type")
        if t == "session_created":
            self._refresh_sessions()
        elif t == "message_delta":
            if m.get("session_id") == self.current_sid:
                self.chat.replace_last_ai("NEURA: " + str(m.get("text", "")), _FG)
        elif t == "message_done":
            self._ai_streaming = False
            if m.get("session_id") == self.current_sid and m.get("text"):
                color = _RED if m.get("blocked") else _FG
                self.chat.replace_last_ai("NEURA: " + str(m["text"]), color)
            self._refresh_sessions()
        elif t == "tool_event":
            tool = m.get("tool", "")
            status = m.get("status", "")
            self.chat.add(f"⚙ {tool} {status}" + (f" · {str(m.get('error'))[:60]}" if m.get("error") else ""),
                          _DIM)
        elif t == "notify":
            self.chat.add(f"ℹ {m.get('message', '')}", "#7fe07f")
        elif t == "panel":
            self.hud.open(m.get("window_id", ""), m.get("window_type", "info"),
                          m.get("title", ""), m.get("content", ""),
                          m.get("status"), admin=bool(m.get("admin")))
        elif t == "panel_update":
            self.hud.update(m.get("window_id", ""), m.get("content"),
                            m.get("status"), append=bool(m.get("append")))
        elif t == "panel_close":
            self.hud.close(m.get("window_id", ""))
        elif t == "panel_control":
            self.hud.control(m.get("action", ""), m.get("window_id", ""),
                             m.get("x"), m.get("y"), m.get("kind"), m.get("direction"))
        elif t == "admin_challenge":
            self._show_admin(m)
        elif t == "command_stream":
            self.hud.open(m.get("window_id", ""), "process", "实时命令",
                          {"text": m.get("line", "")}, "running")

    def _show_admin(self, m):
        try:
            top = tk.Toplevel(self.root)
            top.title("系统登录密码验证")
            top.overrideredirect(True)
            try:
                top.attributes("-topmost", True)
                top.attributes("-alpha", 0.95)
            except Exception:
                pass
            top.geometry("480x260+300+220")
            fr = tk.Frame(top, bg=_BG2, highlightbackground=_RED, highlightthickness=1)
            fr.pack(fill="both", expand=True)
            cv = tk.Canvas(fr, bg=_BG2, height=150, highlightthickness=0)
            cv.pack(fill="x", padx=20, pady=(12, 4))
            cv.create_text(8, 6, text="⛔ 高危操作", font=("Segoe UI", 12, "bold"),
                           fill=_RED, anchor="nw")
            cv.create_text(8, 34, text=str(m.get("description", ""))[:120],
                           font=_FONT_T, fill=_FG, anchor="nw", width=430)
            cv.create_text(8, 74, text="请输入系统登录密码以确认执行:",
                           font=_FONT_T, fill=_DIM, anchor="nw")
            box = tk.Canvas(fr, bg=_BG, highlightthickness=1, highlightbackground=_RED,
                            height=38, takefocus=1)
            box.pack(padx=20, fill="x", pady=(0, 8))
            inp = CanvasInput(box, lambda t: None, placeholder="系统登录密码")
            inp._ph = box.create_text(14, 19, text=inp.placeholder, anchor="w",
                                      font=_FONT_T, fill=_DIM)
            inp._tx = box.create_text(14, 19, text="", anchor="w", font=_FONT, fill=_FG)
            ops = m.get("operation_id", "")
            btns_cv = tk.Canvas(fr, bg=_BG2, height=40, highlightthickness=0)
            btns_cv.pack(fill="x", pady=(0, 12))

            def submit(_=None):
                pwd = inp.get()
                if self.loop and not self.loop.is_closed():
                    self.loop.create_task(
                        self.core.handle_admin_auth(ops, pwd, self.emitter))
                top.destroy()

            def cancel():
                if self.loop and not self.loop.is_closed():
                    self.loop.create_task(
                        self.core.handle_admin_cancel(ops, self.emitter))
                top.destroy()
            box.bind("<KeyPress>", lambda e: (inp._on_key(e) if e.keysym != "Return" else submit()))
            HUDButton(btns_cv, 120, 6, 100, 28, "确认", fg=_RED, command=submit,
                      edge=_RED, bg=_BG)
            HUDButton(btns_cv, 240, 6, 100, 28, "取消", fg=_DIM, command=cancel,
                      edge=_EDGE, bg=_BG)
        except Exception:
            pass

    # ================= 主循环(单线程 pump) =================
    def run(self, loop: asyncio.AbstractEventLoop):
        """tkinter 主线程驱动 asyncio: 所有协程与 UI 同线程执行,
        X/GUI 访问全部单线程, 天然线程安全。"""
        self.loop = loop
        try:
            while True:
                self.root.update()
                loop.run_until_complete(asyncio.sleep(0.05))
        except tk.TclError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            try:
                loop.run_until_complete(asyncio.sleep(0))
            except Exception:
                pass

    def _open_ext_manager(self):
        """桌面端扩展管理窗口: 列表 + 启用/禁用 + 卸载 + 导入, 立体玻璃风格。"""
        try:
            from agent import extensions as _ext
            exts = _ext.list_extensions(enabled_only=False)
        except Exception as e:
            self.notify_msg(f"读取扩展失败: {e}", level="error")
            return
        try:
            hw = HudWindow(self.root, "ext_manager", "text", "⬒ 扩展管理",
                           "", status="ok")
        except Exception as e:
            self.notify_msg(f"打开管理窗口失败: {e}", level="error")
            return
        body = hw._body
        for c in body.winfo_children():
            try:
                c.destroy()
            except Exception:
                pass
        cv = tk.Canvas(body, bg=_BG, highlightthickness=0)
        sb = tk.Scrollbar(body, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        HudWindow._bind_wheel(cv, hw.win)
        hw._scroll_cvs.append(cv)
        y = 10
        cv.create_text(14, y, text=f"已安装扩展 {len(exts)} 个 · 仅启用扩展可被 AI 使用",
                       font=("Segoe UI", 10, "bold"), fill=_FG, anchor="nw")
        y += 30
        if not exts:
            cv.create_text(14, y, text="暂无扩展 · 点击下方【⬒ 导入扩展包】添加",
                           font=("Segoe UI", 10), fill=_DIM, anchor="nw")
            y += 34
        for x in exts:
            name = str(x.get("name") or "?")
            title = str(x.get("title") or "")
            purpose = str(x.get("purpose") or "")
            en = bool(x.get("enabled"))
            ver = str(x.get("version") or "1.0")
            cv.create_rectangle(6, y, 640, y + 66, fill=_BG2, outline=_EDGE, width=1)
            cv.create_text(16, y + 9, text=f"{name}  ·  {title}  (v{ver})",
                           font=("Segoe UI", 10, "bold"), fill=_FG, anchor="nw")
            cv.create_text(16, y + 29, text=(purpose or "")[:46], font=("Segoe UI", 9),
                           fill=_DIM, anchor="nw")
            cv.create_text(16, y + 48, text=("● 已启用" if en else "○ 已禁用"),
                           font=("Segoe UI", 9, "bold"),
                           fill=(_ACCENT if en else _DIM), anchor="nw")
            # 启用/停用开关按钮(醒目彩色, 直接切换)
            if en:
                HUDButton(cv, 330, y + 18, 78, 30, "停用", fg=_RED,
                          command=lambda n=name: self._ext_toggle(n, False, hw),
                          edge=_RED, bg=_BG2)
            else:
                HUDButton(cv, 330, y + 18, 78, 30, "启用", fg=_ACCENT,
                          command=lambda n=name: self._ext_toggle(n, True, hw),
                          edge=_ACCENT, bg=_BG2)
            HUDButton(cv, 420, y + 18, 62, 30, "详情", fg=_FG,
                      command=lambda n=name: self._ext_detail(n),
                      edge=_EDGE, bg=_BG2)
            HUDButton(cv, 492, y + 18, 62, 30, "卸载", fg=_RED,
                      command=lambda n=name: self._ext_uninstall(n, hw),
                      edge=_RED, bg=_BG2)
            y += 74
        HUDButton(cv, 6, y + 8, 130, 28, "⬒ 导入扩展包", fg=_ACCENT,
                  command=self._import_extension, edge=_EDGE, bg=_BG2)
        HUDButton(cv, 148, y + 8, 80, 28, "⟳ 刷新", fg=_FG,
                  command=lambda: (self._safe_close(hw),
                                   self.root.after(120, self._open_ext_manager)),
                  edge=_EDGE, bg=_BG2)
        cv.create_text(240, y + 22, text="提示: 导入后自动启用; 停用/卸载立即生效",
                       font=("Segoe UI", 9), fill=_DIM, anchor="nw")
        cv.configure(scrollregion=(0, 0, 640, y + 60))

    def _safe_close(self, hw):
        """安全关闭 HUD 窗口(任何异常都不中断后续刷新)。"""
        try:
            hw.close()
        except Exception:
            pass

    def _ext_toggle(self, name, enabled, hw):
        """启用/停用扩展: 切换后自动刷新列表。"""
        try:
            from agent import extensions as _ext
            res = _ext.set_enabled(name, enabled)
            if res.get("ok"):
                self.notify_msg(f"扩展 {name} {'已启用' if enabled else '已停用'}")
            else:
                self.notify_msg(f"操作失败: {res.get('error')}", level="error")
        except Exception as e:
            self.notify_msg(f"操作失败: {e}", level="error")
        self._safe_close(hw)
        try:
            self.root.after(120, self._open_ext_manager)
        except Exception:
            pass

    def _ext_uninstall(self, name, hw):
        """卸载扩展(带 HUD 红色确认窗, 防误删)。"""
        try:
            top = tk.Toplevel(self.root)
            top.title("卸载确认")
            top.overrideredirect(True)
            try:
                top.attributes("-topmost", True)
                top.attributes("-alpha", 0.95)
            except Exception:
                pass
            top.geometry("440x210+320+240")
            fr = tk.Frame(top, bg=_BG2, highlightbackground=_RED, highlightthickness=1)
            fr.pack(fill="both", expand=True)
            cv = tk.Canvas(fr, bg=_BG2, height=130, highlightthickness=0)
            cv.pack(fill="x", padx=20, pady=(12, 4))
            cv.create_text(8, 6, text="⚠ 卸载扩展", font=("Segoe UI", 12, "bold"),
                           fill=_RED, anchor="nw")
            cv.create_text(8, 36, text=f"确定卸载扩展 '{name}' 吗?\n"
                                       "卸载将立即删除该扩展目录, 不可恢复。",
                           font=_FONT_T, fill=_FG, anchor="nw", width=400)
            btns = tk.Canvas(fr, bg=_BG2, height=44, highlightthickness=0)
            btns.pack(fill="x", pady=(0, 10))

            def do_confirm(_=None):
                try:
                    from agent import extensions as _ext
                    res = _ext.uninstall_extension(name)
                    if res.get("ok"):
                        self.notify_msg(f"扩展 {name} 已卸载")
                    else:
                        self.notify_msg(f"卸载失败: {res.get('error')}", level="error")
                except Exception as e:
                    self.notify_msg(f"卸载失败: {e}", level="error")
                top.destroy()
                try:
                    hw.close()
                except Exception:
                    pass
                self.root.after(120, self._open_ext_manager)

            def do_cancel(_=None):
                top.destroy()

            HUDButton(btns, 96, 8, 100, 28, "确认卸载", fg=_RED, command=do_confirm,
                      edge=_RED, bg=_BG2)
            HUDButton(btns, 220, 8, 100, 28, "取消", fg=_DIM, command=do_cancel,
                      edge=_EDGE, bg=_BG2)
            top.bind("<Return>", do_confirm)
            top.bind("<Escape>", do_cancel)
        except Exception:
            pass

    def _ext_detail(self, name):
        """查看扩展详情: 用途 + 使用方法 + 入口代码(HUD 窗口展示)。"""
        try:
            from agent import extensions as _ext
            info = _ext.read_extension(name, enabled_only=False)
            if not info:
                self.notify_msg(f"扩展不存在: {name}", level="error")
                return
            usage = str(info.get("usage") or "无使用方法")
            code = str(info.get("code") or "无入口代码")
            text = f"【{info.get('title') or name}】 v{info.get('version') or '1.0'}" \
                   f"{' · 已启用' if info.get('enabled') else ' · 已禁用'}\n\n" \
                   f"用途: {info.get('purpose') or ''}\n\n" \
                   f"使用方法:\n{usage}\n\n入口代码(src/{info.get('entry')}):\n{code[:4000]}"
            try:
                HudWindow(self.root, f"ext_detail_{name}", "text",
                          f"扩展详情: {name}", text, status="ok")
            except Exception:
                self.notify_msg("打开详情窗口失败", level="error")
        except Exception as e:
            self.notify_msg(f"读取详情失败: {e}", level="error")

    def _import_extension(self):
        """桌面端导入扩展包 zip -> 自动安装并启用, 结果 HUD 展示。"""
        try:
            import tkinter.filedialog as fd
            path = fd.askopenfilename(title="选择扩展包 zip",
                                      filetypes=[("扩展包", "*.zip"), ("所有文件", "*.*")])
            if not path:
                return
            from agent import extensions as _ext
            res = _ext.install_extension(path)
            if res.get("ok"):
                self.notify_msg(f"扩展 {res.get('title')} 安装成功并已启用")
                try:
                    self.hud.close("ext_manager")
                except Exception:
                    pass
                self.root.after(150, self._open_ext_manager)
            else:
                self.notify_msg(f"扩展安装失败: {res.get('error')}", level="error")
        except Exception as e:
            self.notify_msg(f"导入扩展失败: {e}", level="error")

    def notify_msg(self, text, level="info"):
        """同步入队通知(v3.45 修复): DesktopEmitter.notify 是 async 协程, 不能直接
        create_task 调用(NeuraDesktopUI 无 notify 属性)。改为直接同步入队,
        由主循环消费分支(t == "notify")在对话框显示。"""
        try:
            self.q.put({"type": "notify", "message": text, "level": level})
        except Exception:
            pass

    def shutdown(self):
        try:
            self.hud.close_all()
            self.root.destroy()
        except Exception:
            pass
