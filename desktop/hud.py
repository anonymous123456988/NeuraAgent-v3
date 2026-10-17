# -*- coding: utf-8 -*-
"""NEURA 桌面端 - 原生 HUD 弹窗渲染器(全 Canvas 自绘, 零 Label/Entry/Text/Button)。

科技幽蓝透明 HUD 面板(管理员/错误为红色): 半透明无边框、发光描边、丝滑拖动/关闭/收缩。
v3.33 桌面版: Agent 的 panel 事件直接渲染到电脑屏幕上的原生 HUD 窗口, 无控制板。
说明: 本模块刻意不用 tk.Text / tk.Entry / tk.Label / tk.Button(部分 Linux/Xvfb 构建
     的 xcb 断言问题——Label/Button 的 X 字体渲染触发), 所有文本与控件均由 Canvas
     自绘, Windows / Linux 行为一致。
"""
import os
import tkinter as tk

# v3.43 macOS 玻璃风格: 中性深灰磨砂玻璃, 柔和边框, 交通灯按钮, 无发光/无科技角标
_BG = "#1a1c21"
_BG2 = "#22252c"
_EDGE = "#aab4c0"
_EDGE_RED = "#ff5a5f"
_FG = "#f2f4f7"
_DIM = "#98a1ad"
_ACCENT = "#5e9bff"
_GLASS_HI = "#ffffff"
_GLASS_L1 = "#23262e"
_GLASS_L2 = "#2b2f38"
_TRAFFIC = ("#ff5f57", "#febc2e", "#28c840")   # macOS 交通灯: 红黄绿
_FONT = ("Microsoft YaHei UI", 10)
_FONT_B = ("Microsoft YaHei UI", 11, "bold")
_FONT_T = ("Microsoft YaHei UI", 9)
_LINE_H = 20


def _lerp_hex(c1: str, c2: str, t: float):
    """十六进制颜色线性插值(玻璃渐变用)。"""
    def _h(s):
        s = s.lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    a, b = _h(c1), _h(c2)
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _glass(cv: "tk.Canvas", w: int, h: int, red: bool = False,
           y0: int = 0, y1: int | None = None, tag: str = "glass"):
    """v3.44 立体玻璃底板: 通透渐变 + 多层高光反射 + 左侧反光条 + 底部内阴影 + 立体边缘。
    所有 HUD 面板/对话框统一调用, 保证同一立体玻璃风格。tag 用于按需重绘玻璃层。"""
    edge = _EDGE_RED if red else _EDGE
    hi = "#ffc3c5" if red else _GLASS_HI
    y1 = y1 if y1 is not None else h
    try:
        # 1) 玻璃渐变: 上深下浅(透光感, 下半部更亮更透明)
        layers = 6
        for i in range(layers):
            t = i / max(1, layers - 1)
            c0 = _lerp_hex(_BG2, _GLASS_L2, t * t) if i < layers - 1 else _GLASS_L2
            ya = y0 + (y1 - y0) * i / layers
            yb = y0 + (y1 - y0) * (i + 1) / layers
            cv.create_rectangle(0, ya, w, yb, fill=c0, outline="", tags=tag)
        # 2) 顶部高光反射(多层递减, 玻璃反光立体感)
        cv.create_rectangle(3, y0 + 1, w - 4, y0 + 3, fill=hi, outline="", tags=tag)
        cv.create_rectangle(5, y0 + 4, w - 8, y0 + 5, fill=hi, outline="", stipple="gray50")
        cv.create_rectangle(10, y0 + 6, w - 16, y0 + 7, fill=hi, outline="", stipple="gray75")
        # 3) 左侧边缘反光条(侧光, 玻璃厚度感)
        cv.create_rectangle(2, y0 + 9, 3, y1 - 10, fill=hi, outline="", stipple="gray50")
        # 4) 底部内阴影(向上渐变收拢, 玻璃厚重感)
        for i in range(5):
            _a = i / 5.0
            cv.create_rectangle(2, y1 - 10 + i * 2, w - 2, y1 - 10 + (i + 1) * 2,
                                fill=_lerp_hex(_BG2, "#0a0b0e", _a), outline="", tags=tag)
        # 5) 立体边缘: 外圈暗影 + 内圈亮边(玻璃切面)
        cv.create_rectangle(1, y0 + 1, w - 2, y1 - 2, outline="#06070a", width=1, tags=tag)
        cv.create_rectangle(2, y0 + 2, w - 3, y1 - 3, outline=edge, width=1, tags=tag)
    except Exception:
        pass


def _traffic_lights(cv: "tk.Canvas", x0: int, y0: int, red: bool = False):
    """macOS 交通灯: 红黄绿三圆点(标题栏左侧)。red 时中间圆点变红提示。"""
    try:
        if red:
            colors = (_TRAFFIC[0], _TRAFFIC[0], _TRAFFIC[0])
        else:
            colors = _TRAFFIC
        for i, c in enumerate(colors):
            cx = x0 + i * 16
            cv.create_oval(cx, y0, cx + 10, y0 + 10, fill=c, outline="")
    except Exception:
        pass


class HUDLabel:
    """Canvas 自绘文本标签。"""

    def __init__(self, canvas, x, y, text, fg=_FG, font=_FONT_T, anchor="nw"):
        self.cv = canvas
        self.item = canvas.create_text(x, y, text=str(text), anchor=anchor, font=font, fill=fg)

    def set(self, text, fg=None):
        try:
            self.cv.itemconfig(self.item, text=str(text))
            if fg:
                self.cv.itemconfig(self.item, fill=fg)
        except Exception:
            pass


class HUDButton:
    """Canvas 自绘按钮: 描边矩形 + 文本, 悬停发光, 点击回调。"""

    def __init__(self, canvas, x, y, w, h, text, fg=_EDGE, command=None,
                 edge=_EDGE, bg=_BG, hover_bg=_BG2):
        self.cv = canvas
        self.command = command
        self.edge = edge
        self.fg = fg
        self.bg = bg
        self.hover_bg = hover_bg
        self.hover = False
        self.rect = canvas.create_rectangle(x, y, x + w, y + h, outline=edge,
                                            fill=bg, width=1)
        self.txt = canvas.create_text(x + w // 2, y + h // 2, text=str(text),
                                      font=_FONT_B, fill=fg)
        canvas.tag_bind(self.rect, "<Enter>", lambda e: self._set_hover(True))
        canvas.tag_bind(self.rect, "<Leave>", lambda e: self._set_hover(False))
        canvas.tag_bind(self.rect, "<Button-1>", lambda e: self._click())
        canvas.tag_bind(self.txt, "<Enter>", lambda e: self._set_hover(True))
        canvas.tag_bind(self.txt, "<Leave>", lambda e: self._set_hover(False))
        canvas.tag_bind(self.txt, "<Button-1>", lambda e: self._click())

    def _set_hover(self, on):
        self.hover = on
        try:
            self.cv.itemconfig(self.rect, fill=self.hover_bg if on else self.bg,
                               outline="#7fe7ff" if on else self.edge)
        except Exception:
            pass

    def _click(self):
        if self.command:
            self.command()

    def set_text(self, text):
        try:
            self.cv.itemconfig(self.txt, text=str(text))
        except Exception:
            pass


class CanvasText:
    """Canvas 自绘文本面板: 支持 set/append/自动换行/滚动。"""

    def __init__(self, canvas: tk.Canvas, font=_FONT_T, color=_FG):
        self.cv = canvas
        self.font = font
        self.color = color
        self.lines = []
        self._items = []
        self._w = 100
        try:
            canvas.bind("<Configure>", self._on_resize)
        except Exception:
            pass

    def _on_resize(self, e):
        self._w = max(80, e.width - 16)
        self._redraw()

    def set_text(self, text):
        self.lines = self._wrap(str(text))
        self._redraw()

    def append(self, text):
        self.lines.extend(self._wrap(str(text)))
        self._redraw(scroll_to_end=True)

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

    def _redraw(self, scroll_to_end=False):
        try:
            for it in self._items:
                self.cv.delete(it)
            self._items = []
            y = 6
            for ln in self.lines:
                self._items.append(self.cv.create_text(
                    10, y, text=ln, anchor="nw", font=self.font, fill=self.color))
                y += _LINE_H
            self.cv.configure(scrollregion=(0, 0, max(self._w, 300), max(y + 6, 60)))
            if scroll_to_end:
                self.cv.yview_moveto(1.0)
        except Exception:
            pass

    def clear(self):
        self.lines = []
        self._redraw()


class HudWindow:
    """一个原生 HUD 弹窗。"""

    def __init__(self, root: tk.Tk, window_id: str, window_type: str, title: str,
                 content, status: str = "ok", admin: bool = False, on_close=None):
        self.root = root
        self.window_id = window_id
        self.window_type = window_type
        self.on_close = on_close
        red = admin or status == "error"
        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.overrideredirect(True)
        try:
            # v3.37: HUD 窗口【不置顶】(只有悬浮球/对话框置顶), 保持普通层级,
            # 避免 HUD 长期压在其他应用窗口之上导致桌面混乱
            self.win.attributes("-alpha", 0.80)   # v3.44 立体玻璃: 高透明
        except Exception:
            pass
        w = min(760, max(460, 320 + len(str(title)) * 14))
        h = 380
        self.win.geometry(f"{w}x{h}+80+60")
        self._scroll_cvs = []
        self._build(title, content, status, red, w, h)
        self._wire_drag()
        try:
            self.win.bind("<MouseWheel>", self._win_wheel)
            self.win.bind("<Button-4>", self._win_wheel)
            self.win.bind("<Button-5>", self._win_wheel)
        except Exception:
            pass

    def _win_wheel(self, e):
        """窗口级滚轮路由(v3.38): 鼠标在窗口内任一位置滚动都路由到当前可滚动 Canvas,
        不再依赖鼠标精确悬停在画布上(修复 Windows 实机滚轮无效)。"""
        if not self._scroll_cvs:
            return None
        cv = self._scroll_cvs[-1]   # 最近注册的滚动区(主内容区)
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

    # ---------- 构建 ----------
    def _build(self, title, content, status, red, w, h):
        edge = _EDGE_RED if red else _EDGE
        frame = tk.Frame(self.win, bg=_BG2, highlightbackground=edge,
                         highlightthickness=1, bd=0)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        # 标题栏(Canvas 自绘: 全 Canvas 避免 Label/Button 的 xcb 字体断言)
        bar_cv = tk.Canvas(frame, bg=_BG, height=34, highlightthickness=0)
        bar_cv.pack(fill="x")
        _glass(bar_cv, w, 34, red=red, y0=0, y1=34)   # v3.43 macOS 玻璃底板
        _traffic_lights(bar_cv, 12, 12, red=red)      # v3.43 macOS 交通灯(红黄绿)
        bar_cv.create_text(64, 15, text=title[:40], font=_FONT_B, fill=_FG, anchor="w")
        stat = {"running": "● 执行中", "ok": "● 完成", "error": "● 失败",
                "closed": "● 已关闭"}.get(status, "")
        if stat:
            bar_cv.create_text(200, 15, text=stat, font=_FONT_T, fill=edge, anchor="w")
        HUDButton(bar_cv, w - 56, 4, 24, 22, "—", fg=_DIM, command=self._minimize,
                  edge=edge, bg=_BG)
        HUDButton(bar_cv, w - 30, 4, 24, 22, "✕", fg=_DIM, command=self.close,
                  edge=edge, bg=_BG)
        # 内容区
        body = tk.Frame(frame, bg=_BG2)
        body.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self._body = body
        self._render(body, content)
        # 底部状态条
        tk.Frame(frame, bg=_BG, height=2).pack(fill="x")

    def _render(self, body, content):
        wt = self.window_type
        if isinstance(content, str):
            content = {"text": content}
        if isinstance(content, dict) and content.get("image") and os.path.exists(str(content.get("image"))):
            self._render_image(body, content)
        elif wt in ("process", "code", "info", "text") or (isinstance(content, dict) and "text" in content):
            self._canvas_panel(body, content.get("text", "") if isinstance(content, dict) else str(content))
        elif wt in ("data", "weather", "files"):
            self._render_rows(body, content)
        elif wt == "progress":
            self._render_progress(body, content)
        else:
            self._canvas_panel(body, str(content))

    @staticmethod
    def _bind_wheel(cv, root):
        """鼠标滚轮直接滚动 Canvas(v3.37: 不用拖滑杆, 鼠标悬停即滚)。"""
        def _on_wheel(e):
            try:
                if getattr(e, "num", 0) == 4:
                    cv.yview_scroll(-1, "units")
                elif getattr(e, "num", 0) == 5:
                    cv.yview_scroll(1, "units")
                else:
                    cv.yview_scroll(-1 if getattr(e, "delta", 0) > 0 else 1, "units")
                return "break"
            except Exception:
                return None
        cv.bind("<MouseWheel>", _on_wheel)
        cv.bind("<Button-4>", _on_wheel)
        cv.bind("<Button-5>", _on_wheel)
        try:
            root.bind_all("<MouseWheel>", lambda e: _on_wheel(e) if cv.winfo_containing(
                e.x_root, e.y_root) is not None else None)
        except Exception:
            pass

    def _canvas_panel(self, body, text):
        cv = tk.Canvas(body, bg=_BG, highlightthickness=0)

        def _repaint_glass(e=None):
            try:
                w, h = cv.winfo_width(), cv.winfo_height()
                if w > 10 and h > 10:
                    cv.delete("glass")
                    _glass(cv, w, h, y0=0, y1=h, tag="glass")
            except Exception:
                pass
        cv.bind("<Configure>", _repaint_glass)   # v3.44 玻璃随窗口尺寸重绘
        _repaint_glass()
        sb = tk.Scrollbar(body, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._bind_wheel(cv, self.win)
        self._scroll_cvs.append(cv)
        ct = CanvasText(cv, _FONT_T, _FG)
        ct.set_text(text)
        self._ct = ct

    def _render_image(self, body, content):
        try:
            from PIL import Image, ImageTk
            img = Image.open(content["image"])
            box_w, box_h = 680, 300
            img.thumbnail((box_w, box_h))
            self._tk_img = ImageTk.PhotoImage(img)
            cv = tk.Canvas(body, bg=_BG2, highlightthickness=0)
            cv.pack(fill="both", expand=True)
            cv.create_image(box_w // 2, box_h // 2, image=self._tk_img, anchor="center")
        except Exception:
            self._canvas_panel(body, str(content.get("image", "图片渲染失败")))
        note = content.get("note")
        if note:
            cv = tk.Canvas(body, bg=_BG2, height=24, highlightthickness=0)
            cv.pack(fill="x")
            cv.create_text(4, 4, text=str(note), font=_FONT_T, fill=_DIM, anchor="nw")

    def _render_rows(self, body, content):
        rows = []
        if isinstance(content, dict):
            if "rows" in content:
                rows = content["rows"]
            elif "files" in content:
                rows = [{"文件": f.get("name", ""), "大小": f.get("size", ""),
                         "路径": f.get("path", "")} for f in content["files"]]
            elif "weather" in content:
                w = content["weather"]
                rows = [{"城市": w.get("city", ""), "天气": w.get("condition", ""),
                         "温度": w.get("temperature", ""), "湿度": w.get("humidity", "")}]
            else:
                for k, v in list(content.items())[:14]:
                    rows.append({k: str(v)[:120]})
        elif isinstance(content, list):
            rows = [{"#": i + 1, "值": str(x)[:120]} for i, x in enumerate(content[:40])]
        if not rows:
            self._canvas_panel(body, "无数据")
            return
        cv = tk.Canvas(body, bg=_BG2, highlightthickness=0)
        sb = tk.Scrollbar(body, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._bind_wheel(cv, self.win)
        self._scroll_cvs.append(cv)
        keys = list(rows[0].keys()) if rows else []
        y = 4
        x0 = 8
        widths = [max(70, (len(k) + 2) * 12) for k in keys]
        for k, wdt in zip(keys, widths):
            cv.create_text(x0, y, text=str(k), anchor="nw", font=_FONT_B, fill=_EDGE)
            x0 += wdt
        y += 24
        for i, row in enumerate(rows):
            if i % 2 == 1:
                cv.create_rectangle(4, y - 2, 2000, y + 18, fill=_BG, outline="")
            x0 = 8
            for k, wdt in zip(keys, widths):
                val = str(row.get(k, ""))
                if len(val) > 34:
                    val = val[:33] + "…"
                cv.create_text(x0, y, text=val, anchor="nw", font=_FONT_T, fill=_FG)
                x0 += wdt
            y += 20
        cv.configure(scrollregion=(0, 0, 2000, y + 8))

    def _render_progress(self, body, content):
        cv = tk.Canvas(body, bg=_BG2, highlightthickness=0)
        cv.pack(fill="both", expand=True, padx=4)
        text = str(content.get("text", "进行中…"))
        cv.create_text(10, 12, text=text, anchor="nw", font=_FONT_T, fill=_FG, width=640)
        pct = max(0.0, min(100.0, float(content.get("percent", 0) or 0)))
        cv.create_rectangle(10, 60, 690, 74, outline=_EDGE, fill=_BG)
        cv.create_rectangle(12, 62, 12 + 676 * pct / 100.0, 72, outline="", fill=_ACCENT)
        cv.create_text(690, 44, text=f"{pct:.0f}%", anchor="e", font=_FONT_B, fill=_EDGE)
        self._prog = (cv, pct)

    def _wire_drag(self):
        self._drag_data = {"x": 0, "y": 0}

        def start(e):
            self._drag_data["x"], self._drag_data["y"] = e.x_root, e.y_root

        def move(e):
            dx = e.x_root - self._drag_data["x"]
            dy = e.y_root - self._drag_data["y"]
            x = self.win.winfo_x() + dx
            y = self.win.winfo_y() + dy
            self.win.geometry(f"+{x}+{y}")
            self._drag_data["x"], self._drag_data["y"] = e.x_root, e.y_root

        self.win.bind("<Button-1>", start)
        self.win.bind("<B1-Motion>", move)

    # ---------- 更新 ----------
    def update(self, content=None, status=None, append=False):
        if content is None and status is None:
            return
        if isinstance(content, dict) and "text" in content and hasattr(self, "_ct"):
            try:
                if append:
                    self._ct.append(content["text"])
                else:
                    self._ct.set_text(content["text"])
            except Exception:
                pass
        if isinstance(content, dict) and "percent" in content and hasattr(self, "_prog"):
            try:
                cv, _ = self._prog
                pct = max(0.0, min(100.0, float(content.get("percent", 0) or 0)))
                items = cv.find_all()
                if len(items) >= 5:
                    cv.coords(items[3], 12, 62, 12 + 676 * pct / 100.0, 72)
                    cv.itemconfig(items[4], text=f"{pct:.0f}%")
            except Exception:
                pass
        if status in ("ok", "error", "closed"):
            try:
                self.win.attributes("-alpha", 0.93)
            except Exception:
                pass

    # ---------- 生命周期 ----------
    def _minimize(self):
        try:
            self.win.overrideredirect(False)
            self.win.iconify()
        except Exception:
            pass

    def restore(self):
        try:
            self.win.overrideredirect(True)
            self.win.deiconify()
            self.win.lift()
        except Exception:
            pass

    def close(self):
        try:
            if self.on_close:
                self.on_close(self.window_id)
            self.win.destroy()
        except Exception:
            pass

    def set_pos(self, x, y):
        try:
            self.win.geometry(f"+{int(x)}+{int(y)}")
        except Exception:
            pass

    def set_size(self, w, h):
        try:
            self.win.geometry(f"{int(w)}x{int(h)}")
        except Exception:
            pass


class HudManager:
    """管理所有原生 HUD 弹窗: 创建/更新/关闭/移动/布局。"""

    def __init__(self, root: tk.Tk, on_window_closed=None):
        self.root = root
        self.windows: dict[str, HudWindow] = {}
        self.on_window_closed = on_window_closed

    def _closed(self, window_id):
        self.windows.pop(window_id, None)
        if self.on_window_closed:
            try:
                self.on_window_closed(window_id)
            except Exception:
                pass

    def open(self, window_id, window_type, title, content, status=None, admin=False):
        wid = window_id or f"w{len(self.windows) + 1}"
        old = self.windows.get(wid)
        if old is not None:
            try:
                old.update(content, status, append=(window_type == "process"))
                return old
            except Exception:
                # 旧窗口已被销毁(close 未清理) → 移除并重建, 避免同名窗口永远打不开
                self.windows.pop(wid, None)
        win = HudWindow(self.root, wid, window_type, title, content, status or "ok",
                        admin=admin, on_close=self._closed)
        # 多弹窗错开位置, 避免重叠
        n = len(self.windows) % 6
        try:
            win.set_pos(80 + (n % 3) * 46, 60 + (n // 3) * 42)
        except Exception:
            pass
        self.windows[wid] = win
        return win

    def update(self, window_id, content=None, status=None, append=False):
        w = self.windows.get(window_id)
        if w:
            w.update(content, status, append)

    def close(self, window_id):
        w = self.windows.pop(window_id, None)
        if w:
            try:
                w.win.destroy()
            except Exception:
                pass

    def close_all(self):
        for wid in list(self.windows.keys()):
            self.close(wid)

    def control(self, action, window_id, x=None, y=None, kind=None, direction=None):
        if action == "close_all":
            self.close_all()
            return
        if action == "close":
            self.close(window_id or (list(self.windows.keys())[-1] if self.windows else ""))
            return
        if action == "move":
            w = self.windows.get(window_id) or (next(iter(self.windows.values()), None)
                                                if not window_id else None)
            if w and x is not None:
                w.set_pos(x, y if y is not None else 60)
            return
        if action == "resize":
            w = self.windows.get(window_id) or (next(iter(self.windows.values()), None)
                                                if not window_id else None)
            if w:
                try:
                    cur = w.win.winfo_geometry().split("+")[0].split("x")
                    cw, ch = int(cur[0]), int(cur[1])
                except Exception:
                    cw, ch = 600, 380
                d = 1.15 if direction == "in" else 0.86
                w.set_size(int(cw * d), int(ch * d))
            return
        if action == "minimize":
            w = self.windows.get(window_id)
            if w:
                w._minimize()
            return
        if action == "restore":
            w = self.windows.get(window_id)
            if w:
                w.restore()
            return
        if action == "maximize":
            w = self.windows.get(window_id)
            if w:
                try:
                    w.win.attributes("-alpha", 0.9)
                    w.set_size(self.root.winfo_screenwidth(), self.root.winfo_screenheight() - 60)
                    w.set_pos(0, 0)
                except Exception:
                    pass
            return
        if action == "layout":
            kind = kind or "maximize_all"
            wins = list(self.windows.values())
            if not wins:
                return
            n = len(wins)
            if kind == "grid" and n > 1:
                import math
                cols = math.ceil(math.sqrt(n))
                rows = math.ceil(n / cols)
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
                cw, ch = sw // cols, (sh - 60) // rows
                for i, w in enumerate(wins):
                    r, c = divmod(i, cols)
                    w.set_size(cw, ch)
                    w.set_pos(c * cw, 60 + r * ch)
            else:
                for i, w in enumerate(wins):
                    w.set_size(self.root.winfo_screenwidth(), self.root.winfo_screenheight() - 60)
                    w.set_pos(0, 60)
