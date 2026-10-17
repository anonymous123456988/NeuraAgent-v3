# -*- coding: utf-8 -*-
"""工具：桌面端 GUI 自动化 + 实时屏幕(screen_capture / gui_control)。

v3.33 桌面版: AI 既能【数据性/命令性】操控电脑(命令、代码、工具), 又能像人一样
【图形化】操控电脑(移动鼠标/点击/双击/右键/拖拽/滚动/输入/热键/窗口管理),
并且能实时看到屏幕(screen_capture -> HUD 弹窗展示 + 返回图片路径供 AI 感知),
形成「看屏幕 -> 操作 -> 再看屏幕验证」的人类式操控闭环。

平台: Windows 优先(win32gui/pywinauto/pyautogui/PIL.ImageGrab);
Linux/macOS 兜底(xdotool/import)。无显示环境返回明确错误, 不崩溃。
"""
import asyncio
import json
import os
import re
import subprocess
import time

from .registry import Tool, ToolResult, ToolContext, ToolRegistry

_AUTOMATION_ERR = ("图形化操控在当前环境不可用(需 Windows + pywin32/pyautogui/PIL, "
                   "或 Linux/macOS + xdotool; 且需要图形桌面会话)。请用命令性方式操作: "
                   "run_command / run_code。")

# v3.42 数字化鼠标操作杆: AI 可随时调整鼠标移动速度(像素/秒)并像摇杆一样持续移动
_MOUSE_SPEED = {"slow": 200, "medium": 600, "fast": 1800}
_DEFAULT_MOUSE_SPEED = 600
_cur_speed = _DEFAULT_MOUSE_SPEED


def _set_speed(level: str = "medium", px_per_sec: int | None = None):
    """设置鼠标移动速度: slow/medium/fast, 或 custom 指定 px/s。"""
    global _cur_speed
    lv = str(level or "medium").lower()
    if lv in _MOUSE_SPEED:
        _cur_speed = _MOUSE_SPEED[lv]
    elif px_per_sec and px_per_sec > 0:
        _cur_speed = int(px_per_sec)
    else:
        _cur_speed = _DEFAULT_MOUSE_SPEED
    return _cur_speed


def _mouse_joystick(dx: float, dy: float, duration: float = 1.0,
                    button: str = "none"):
    """数字化鼠标操作杆: 以 dx/dy(像素/秒) 持续移动 duration 秒(每 10ms 步进),
    可同时按住鼠标键(模拟摇杆+按键)。返回终点坐标。"""
    dx, dy = float(dx), float(dy)
    duration = max(0.1, float(duration))
    steps = max(4, int(duration * 100))
    step_dx, step_dy = dx / 100.0, dy / 100.0

    def w():
        import win32api
        import win32con
        x, y = win32api.GetCursorPos()
        btn = {"left": 1, "right": 2, "middle": 4}.get(str(button or "none").lower(), 0)
        if btn:
            ev = win32con.MOUSEEVENTF_LEFTDOWN if btn == 1 else (
                win32con.MOUSEEVENTF_RIGHTDOWN if btn == 2 else win32con.MOUSEEVENTF_MIDDLEDOWN)
            win32api.mouse_event(ev, 0, 0, 0, 0)
        for i in range(steps):
            nx = int(x + step_dx * i)
            ny = int(y + step_dy * i)
            win32api.SetCursorPos((nx, ny))
            time.sleep(duration / steps)
        if btn:
            ev = win32con.MOUSEEVENTF_LEFTUP if btn == 1 else (
                win32con.MOUSEEVENTF_RIGHTUP if btn == 2 else win32con.MOUSEEVENTF_MIDDLEUP)
            win32api.mouse_event(ev, 0, 0, 0, 0)
        return True, f"joystick 移动至 ({int(x + step_dx * steps)},{int(y + step_dy * steps)})"

    def n():
        cx, cy = _mouse_pos() or (0, 0)
        x0, y0 = int(cx), int(cy)
        for i in range(steps):
            subprocess.run(["xdotool", "mousemove", str(x0 + int(step_dx * i)),
                            str(y0 + int(step_dy * i))], check=True, timeout=4)
            time.sleep(duration / steps)
        return True, f"joystick 移动至 ({x0 + int(step_dx * steps)},{y0 + int(step_dy * steps)})"

    return _gui_op(w, n)


# ---------------------------------------------------------------
# 底层自动化: 平台差异封装
# ---------------------------------------------------------------
def _have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def _screenshot_to(path: str, region: tuple | None = None) -> str | None:
    """截取全屏(或 region=(x,y,w,h) 局部)保存到 path, 返回 path 或 None。
    Windows: PIL.ImageGrab(物理像素, 与 SetCursorPos 坐标一致, 无 DPI 偏差); Linux: import/xwd。"""
    try:
        if os.name == "nt":
            from PIL import ImageGrab
            if region:
                img = ImageGrab.grab(bbox=(int(region[0]), int(region[1]),
                                           int(region[0] + region[2]), int(region[1] + region[3])))
            else:
                img = ImageGrab.grab(all_screens=True)
            img.save(path)
            return path if os.path.exists(path) else None
    except Exception:
        pass
    try:
        if _have("pyautogui"):
            import pyautogui
            if region:
                img = pyautogui.screenshot(region=(int(region[0]), int(region[1]),
                                                   int(region[2]), int(region[3])))
            else:
                img = pyautogui.screenshot()
            img.save(path)
            return path if os.path.exists(path) else None
    except Exception:
        pass
    try:
        for cmd in (["import", "-window", "root", path], ["scrot", path]):
            r = subprocess.run(cmd, capture_output=True, timeout=10)
            if r.returncode == 0 and os.path.exists(path):
                return path
    except Exception:
        pass
    return None


def _gui_op(win_impl, nix_impl):
    """Windows 优先执行, 失败退 Linux/macOS 实现。返回 (ok, detail)。"""
    if os.name == "nt":
        try:
            return win_impl()
        except Exception as e:
            return False, f"win: {e}"
    try:
        return nix_impl()
    except Exception as e:
        return False, f"nix: {e}"


def _mouse_move(x: int, y: int, duration: float = 0.0):
    """移动鼠标到 (x,y)。duration>0 时平滑插值移动(像人手轨迹, 每步 8ms);
    默认瞬移(毫秒级, 用于精准定位)。"""

    def w():
        import win32api
        if duration and duration > 0:
            cx, cy = win32api.GetCursorPos()
            steps = max(4, min(40, int(duration * 120)))
            for i in range(1, steps + 1):
                win32api.SetCursorPos((int(cx + (x - cx) * i / steps),
                                       int(cy + (y - cy) * i / steps)))
                time.sleep(duration / steps)
        else:
            win32api.SetCursorPos((int(x), int(y)))
        return True, f"moved to ({x},{y})"

    def n():
        subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], check=True, timeout=8)
        return True, f"moved to ({x},{y})"

    return _gui_op(w, n)


def _mouse_pos() -> tuple | None:
    """返回当前鼠标坐标 (x, y)。"""
    try:
        if os.name == "nt":
            import win32api
            return win32api.GetCursorPos()
        r = subprocess.run(["xdotool", "getmouselocation"], capture_output=True,
                           text=True, timeout=6)
        for _tok in (r.stdout or "").split():
            if _tok.startswith("x:"):
                x = int(_tok.split(":")[1])
            if _tok.startswith("y:"):
                y = int(_tok.split(":")[1])
        return (x, y)
    except Exception:
        return None


def _mouse_move_rel(dx: int, dy: int, duration: float = 0.0):
    """相对移动鼠标 dx/dy。"""

    def w():
        import win32api
        cx, cy = win32api.GetCursorPos()
        tx, ty = cx + dx, cy + dy
        if duration and duration > 0:
            steps = max(4, min(40, int(duration * 120)))
            for i in range(1, steps + 1):
                win32api.SetCursorPos((int(cx + (tx - cx) * i / steps),
                                       int(cy + (ty - cy) * i / steps)))
                time.sleep(duration / steps)
        else:
            win32api.SetCursorPos((int(tx), int(ty)))
        return True, f"move_rel ({dx},{dy})"

    def n():
        cx, cy = _mouse_pos() or (0, 0)
        subprocess.run(["xdotool", "mousemove", str(cx + dx), str(cy + dy)],
                       check=True, timeout=8)
        return True, f"move_rel ({dx},{dy})"

    return _gui_op(w, n)


def _mouse_hold(button: str = "left", down: bool = True):
    """按住/释放鼠标键(长按拖拽、菜单长按等)。"""
    btn = {"left": 1, "right": 2, "middle": 4}.get(button.lower(), 1)

    def w():
        import win32api
        import win32con
        ev = (win32con.MOUSEEVENTF_LEFTDOWN if btn == 1 else
              win32con.MOUSEEVENTF_RIGHTDOWN if btn == 2 else win32con.MOUSEEVENTF_MIDDLEDOWN) if down else (
            win32con.MOUSEEVENTF_LEFTUP if btn == 1 else
            win32con.MOUSEEVENTF_RIGHTUP if btn == 2 else win32con.MOUSEEVENTF_MIDDLEUP)
        win32api.mouse_event(ev, 0, 0, 0, 0)
        return True, f"{'按住' if down else '释放'} {button}"

    def n():
        subprocess.run(["xdotool", "mousedown" if down else "mouseup", str(btn)],
                       check=True, timeout=8)
        return True, f"{'按住' if down else '释放'} {button}"

    return _gui_op(w, n)


def _mouse_click(x: int, y: int, button: str = "left", clicks: int = 1):
    btn = {"left": 1, "right": 2, "middle": 4, "4": 16, "5": 32}.get(button, 1)

    def w():
        import win32api
        import win32con
        win32api.SetCursorPos((int(x), int(y)))
        time.sleep(0.05)                     # v3.40 精修: 等待光标移动被系统处理, 避免点击落空
        for _ in range(max(1, int(clicks))):
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN if btn == 1 else (
                win32con.MOUSEEVENTF_RIGHTDOWN if btn == 2 else win32con.MOUSEEVENTF_MIDDLEDOWN), 0, 0, 0, 0)
            time.sleep(0.04)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP if btn == 1 else (
                win32con.MOUSEEVENTF_RIGHTUP if btn == 2 else win32con.MOUSEEVENTF_MIDDLEUP), 0, 0, 0, 0)
            time.sleep(0.04)
        return True, f"click {button} x{clicks} at ({x},{y})"

    def n():
        subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], check=True, timeout=8)
        time.sleep(0.04)
        subprocess.run(["xdotool", "click"] + [str(btn)] * max(1, int(clicks)), check=True, timeout=8)
        return True, f"click {button} x{clicks} at ({x},{y})"

    return _gui_op(w, n)


def _mouse_drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.4):
    def w():
        import win32api
        import win32con
        win32api.SetCursorPos((int(x1), int(y1)))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        steps = max(4, int(duration * 40))
        for i in range(1, steps + 1):
            win32api.SetCursorPos((int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps)))
            time.sleep(duration / steps)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True, f"drag ({x1},{y1})->({x2},{y2})"

    def n():
        subprocess.run(["xdotool", "mousemove", str(int(x1)), str(int(y1))], check=True, timeout=8)
        subprocess.run(["xdotool", "mousedown", "1"], check=True, timeout=8)
        subprocess.run(["xdotool", "mousemove", str(int(x2)), str(int(y2))], check=True, timeout=8)
        subprocess.run(["xdotool", "mouseup", "1"], check=True, timeout=8)
        return True, f"drag ({x1},{y1})->({x2},{y2})"

    return _gui_op(w, n)


def _mouse_scroll(x: int, y: int, amount: int = -3):
    def w():
        import win32api
        import win32con
        win32api.SetCursorPos((int(x), int(y)))
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(amount) * 120, 0)
        return True, f"scroll {amount} at ({x},{y})"

    def n():
        subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], check=True, timeout=8)
        subprocess.run(["xdotool", "click", "--repeat", str(abs(int(amount))), "--delay", "40", "5"
                        if int(amount) < 0 else "4"], check=True, timeout=8)
        return True, f"scroll {amount} at ({x},{y})"

    return _gui_op(w, n)


def _type_text(text: str):
    def w():
        try:
            import pywinauto.keyboard as kb
            kb.send_keys(text, with_spaces=True)
            return True, f"typed {len(text)} chars"
        except Exception:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            for ch in text:
                vk = ord(ch.upper()) if ch.isalpha() else (ord(ch) if ord(ch) < 256 else 0)
                if vk:
                    user32.keybd_event(vk, 0, 0, 0)
                    user32.keybd_event(vk, 0, 2, 0)
            return True, f"typed {len(text)} chars (vk)"

    def n():
        subprocess.run(["xdotool", "type", "--delay", "12", text], check=True, timeout=15)
        return True, f"typed {len(text)} chars"

    return _gui_op(w, n)


# v3.40: pywinauto 特殊键必须大写({ENTER}/{TAB}/{ESC}...), 小写会报
# "Unknown code: enter"(之前 press 失败根因); Linux xdotool 用其自有名称。
_WIN_KEYS = {"enter": "ENTER", "tab": "TAB", "esc": "ESC", "escape": "ESC",
             "backspace": "BACKSPACE", "space": "SPACE", "delete": "DELETE",
             "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
             "home": "HOME", "end": "END", "pageup": "PGUP", "pagedown": "PGDN",
             "win": "LWIN", "windows": "LWIN"}
_NIX_KEYS = {"enter": "enter", "tab": "tab", "esc": "escape", "backspace": "backspace",
             "space": "space", "delete": "Delete", "up": "Up", "down": "Down",
             "left": "Left", "right": "Right", "home": "Home", "end": "End",
             "pageup": "Page_Up", "pagedown": "Page_Down", "win": "super",
             "windows": "super", "super": "super"}


def _win_hotkey(keys: str) -> str:
    """把 'ctrl+shift+esc' / 'win' / 'alt+f4' / 'win+r' 转成 pywinauto 语法:
    修饰键 ^(ctrl) %(alt) +(shift) 直接组合; Win 键单独按下为 {LWIN}, 与普通键组合时
    用 {LWIN down}...{LWIN up}(顺序按下无法触发 Win+R 这类系统组合)。"""
    mods = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+",
            "win": "{LWIN}", "windows": "{LWIN}", "super": "{LWIN}"}
    mods_out = []
    others = []
    for k in re.split(r"[\s+]+", keys.strip().lower()):
        if not k:
            continue
        if k in mods:
            mods_out.append(k)
        else:
            others.append("{" + _WIN_KEYS.get(k, k.upper()) + "}")
    if any(m in ("win", "windows", "super") for m in mods_out) and others:
        return "{LWIN down}" + "".join(others) + "{LWIN up}"
    out = []
    for m in mods_out:
        if m in ("win", "windows", "super"):
            out.append("{LWIN}")
        else:
            out.append(mods[m])
    return "".join(out) + "".join(others)


def _press_key(key: str):
    k = (key or "").strip().lower()

    def w():
        import pywinauto.keyboard as kb
        kb.send_keys("{" + _WIN_KEYS.get(k, k.upper()) + "}")
        return True, f"pressed {key}"

    def n():
        subprocess.run(["xdotool", "key", _NIX_KEYS.get(k, k)], check=True, timeout=8)
        return True, f"pressed {key}"

    return _gui_op(w, n)


def _hotkey(keys: str):
    def w():
        import pywinauto.keyboard as kb
        kb.send_keys(_win_hotkey(keys))
        return True, f"hotkey {keys}"

    def n():
        kk = _NIX_KEYS.get(keys.lower().strip(), keys)
        subprocess.run(["xdotool", "key"] + kk.split("+"), check=True, timeout=8)
        return True, f"hotkey {keys}"

    return _gui_op(w, n)


def _screen_dpi() -> dict:
    """Windows 系统 DPI 与缩放比(供 AI 校准坐标感知); 非 Windows 返回空。"""
    try:
        if os.name == "nt":
            import ctypes
            try:
                dpi = ctypes.windll.user32.GetDpiForSystem()
            except Exception:
                dpi = 96
            return {"dpi": int(dpi), "scale": round(int(dpi) / 96.0, 2)}
    except Exception:
        pass
    return {}


def _screen_size() -> dict:
    """屏幕分辨率与工作区尺寸(物理像素, 与鼠标坐标一致)。"""
    try:
        if os.name == "nt":
            import win32api
            sw = win32api.GetSystemMetrics(0)
            sh = win32api.GetSystemMetrics(1)
            dpi = _screen_dpi()
            return {"width": sw, "height": sh, "work": None,
                    "dpi": dpi.get("dpi"), "scale": dpi.get("scale", 1.0)}
        r = subprocess.run(["xdotool", "getdisplaygeometry"], capture_output=True,
                           text=True, timeout=6)
        parts = (r.stdout or "").split()
        return {"width": int(parts[0]), "height": int(parts[1]), "work": None, "scale": 1.0}
    except Exception:
        return {"width": 0, "height": 0, "work": None, "scale": 1.0}


def _screen_info() -> dict:
    """一步返回 AI 屏幕感知所需信息: 分辨率 + 鼠标位置 + 前台窗口。"""
    info = _screen_size()
    info["mouse"] = _mouse_pos()
    try:
        if os.name == "nt":
            import win32gui
            fg = win32gui.GetForegroundWindow()
            info["foreground_window"] = win32gui.GetWindowText(fg) or ""
    except Exception:
        info["foreground_window"] = ""
    wins = _list_windows(8)
    info["windows"] = wins[:8]
    return info


def _list_windows(max_n: int = 30) -> list:
    """枚举桌面可见顶层窗口(标题非空): [{hwnd, title, x, y, w, h, minimized, foreground}]。

    v3.38: 附加 foreground(是否前台窗口), 供 AI "像人一样"根据窗口位置/大小估算
    点击坐标, 实现屏幕感知式图形化操作。"""
    out = []
    fg = None
    if os.name == "nt":
        try:
            import win32gui
            fg = win32gui.GetForegroundWindow()
        except Exception:
            fg = None
        try:
            import win32gui
            import win32con

            def _cb(h, _):
                if not win32gui.IsWindowVisible(h):
                    return
                t = win32gui.GetWindowText(h) or ""
                if not t.strip():
                    return
                try:
                    r = win32gui.GetWindowRect(h)
                except Exception:
                    r = (0, 0, 0, 0)
                out.append({"hwnd": h, "title": t, "x": r[0], "y": r[1],
                            "w": r[2] - r[0], "h": r[3] - r[1],
                            "minimized": bool(win32gui.IsIconic(h)),
                            "foreground": (h == fg)})
            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass
    else:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "."],
                               capture_output=True, text=True, timeout=8)
            for wid in (r.stdout or "").split():
                try:
                    nm = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True,
                                        text=True, timeout=4).stdout.strip()
                    if nm:
                        out.append({"hwnd": wid, "title": nm})
                except Exception:
                    continue
        except Exception:
            pass
    return out[:max_n]


def _window_act(hwnd: int, action: str, x: int | None = None, y: int | None = None,
                w: int | None = None, h: int | None = None) -> tuple:
    """窗口动作: close / move / resize / minimize / restore / maximize / focus。"""
    if os.name == "nt":
        try:
            import win32gui
            import win32con
            if action == "close":
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                return True, "closed"
            if action == "move":
                win32gui.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0,
                                      win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)
                return True, f"moved to ({x},{y})"
            if action == "resize":
                r = win32gui.GetWindowRect(hwnd)
                win32gui.SetWindowPos(hwnd, 0, r[0], r[1], int(w), int(h),
                                      win32con.SWP_NOZORDER)
                return True, f"resized to {w}x{h}"
            if action == "minimize":
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                return True, "minimized"
            if action == "restore":
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                return True, "restored"
            if action == "maximize":
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
                return True, "maximized"
            if action == "focus":
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                return True, "focused"
        except Exception as e:
            return False, f"win: {e}"
    return False, "unsupported on this platform"


# ---------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------
async def _screen_capture(args: dict, ctx: ToolContext) -> ToolResult:
    """截取当前屏幕 -> 保存 PNG -> 返回图片路径(供 AI 感知与后续图形化操控)。

    v3.37: 默认【不弹窗给用户】(AI 操作后无需每次都截图展示), 仅当 show=true
    (或用户明确要求看屏幕)时才在 HUD 弹窗展示。AI 可配合 list_windows 定位窗口。"""
    shots_dir = os.path.join(ctx.downloads_dir, "screens")
    try:
        os.makedirs(shots_dir, exist_ok=True)
    except Exception:
        shots_dir = ctx.downloads_dir
    fname = "screen_" + time.strftime("%H%M%S") + ".png"
    path = os.path.join(shots_dir, fname)
    # v3.40 精修: 支持 region 区域截图(高清局部, 供 AI 精读按钮/输入框/列表条目);
    # 返回图片物理像素尺寸(与鼠标坐标一致), 附带 dpi 供 AI 感知缩放
    region = None
    _rx, _ry, _rw, _rh = args.get("region_x"), args.get("region_y"), args.get("region_w"), args.get("region_h")
    if _rx is not None and _ry is not None and _rw and _rh:
        region = (int(_rx), int(_ry), int(_rw), int(_rh))
    ok = _screenshot_to(path, region)
    if not ok:
        return ToolResult(ok=False, error=_AUTOMATION_ERR, error_type="exec")
    _w = _h = 0
    try:
        from PIL import Image
        with Image.open(path) as im:
            _w, _h = im.size
    except Exception:
        pass
    win_id = "screen_" + fname.replace(".png", "")
    show = str(args.get("show") or "").lower() in ("true", "1", "yes", "show")
    if show:
        try:
            await ctx.emitter.panel(win_id, "chart", "实时屏幕", {"image": path,
                                                                  "note": "AI 实时屏幕画面"},
                                    status="ok")
        except Exception:
            pass
    dpi = _screen_dpi()
    return ToolResult(data={"image": path, "screen": path, "window_id": win_id,
                            "shown": show, "width": _w, "height": _h,
                            "region": region,
                            "dpi": dpi.get("dpi") if dpi else None,
                            "scale": dpi.get("scale") if dpi else 1.0,
                            "note": ("已截取实时屏幕(供 AI 感知)" if not show
                                     else "已截取实时屏幕并在 HUD 展示")})


async def _gui_control(args: dict, ctx: ToolContext) -> ToolResult:
    """图形化操控电脑(像人一样): 移动/点击/双击/右键/拖拽/滚动/输入/按键/热键/窗口管理。"""
    action = str(args.get("action", "click")).strip()
    try:
        x = int(float(args.get("x") or 0))
        y = int(float(args.get("y") or 0))
        x2 = int(float(args.get("x2") or x))
        y2 = int(float(args.get("y2") or y))
    except Exception:
        return ToolResult(ok=False, error="x/y 必须是数字", error_type="invalid_args")
    ok, detail = False, ""
    _spd = args.get("speed") or 0
    if _spd:
        _set_speed(_spd if isinstance(_spd, str) else "custom", int(_spd) if isinstance(_spd, (int, float)) else None)
    if action in ("move", "mousemove"):
        _dur = float(args.get("duration") or 0)
        if not _dur and (args.get("speed") or 0):
            _dur = max(0.05, ((x - (_mouse_pos() or (0, 0))[0]) ** 2 +
                              (y - (_mouse_pos() or (0, 0))[1]) ** 2) ** 0.5 / max(1, _cur_speed))
        ok, detail = _mouse_move(x, y, _dur)
    elif action in ("smooth_move", "move_smooth"):
        _dur = float(args.get("duration") or 0.35)
        if not args.get("duration") and (args.get("speed") or 0):
            _cur = _mouse_pos() or (0, 0)
            _dur = max(0.1, ((x - _cur[0]) ** 2 + (y - _cur[1]) ** 2) ** 0.5 / max(1, _cur_speed))
        ok, detail = _mouse_move(x, y, _dur)
    elif action in ("move_rel", "mousemove_rel"):
        ok, detail = _mouse_move_rel(x, y, float(args.get("duration") or 0))
    elif action in ("joystick", "stick"):
        ok, detail = _mouse_joystick(float(args.get("dx") or 0), float(args.get("dy") or 0),
                                     float(args.get("duration") or 1.0),
                                     str(args.get("button") or "none"))
    elif action in ("set_speed", "mouse_speed"):
        _v = _set_speed(str(args.get("speed") or "medium"), int(args.get("px_per_sec") or 0))
        return ToolResult(data={"action": "set_speed", "speed_px_per_sec": _v,
                                "level": str(args.get("speed") or "medium")},
                          message=f"鼠标速度已设为 {_v} 像素/秒")
    elif action in ("mouse_pos", "get_mouse", "mouse_position"):
        pos = _mouse_pos()
        return ToolResult(data={"x": pos[0] if pos else None,
                                "y": pos[1] if pos else None,
                                "mouse": pos})
    elif action in ("screen_size", "display_size", "screen_geometry"):
        return ToolResult(data=_screen_size())
    elif action in ("screen_info", "desktop_info", "screen_understand"):
        return ToolResult(data=_screen_info())
    elif action in ("mouse_down", "hold_down", "button_down"):
        ok, detail = _mouse_hold(str(args.get("button") or "left"), True)
    elif action in ("mouse_up", "release", "button_up"):
        ok, detail = _mouse_hold(str(args.get("button") or "left"), False)
    elif action in ("click", "click_left"):
        ok, detail = _mouse_click(x, y, "left", int(args.get("clicks") or 1))
    elif action == "double_click":
        ok, detail = _mouse_click(x, y, "left", 2)
    elif action == "right_click":
        ok, detail = _mouse_click(x, y, "right", 1)
    elif action in ("drag", "drag_to"):
        ok, detail = _mouse_drag(x, y, x2, y2, float(args.get("duration") or 0.4))
    elif action in ("scroll", "wheel"):
        ok, detail = _mouse_scroll(x, y, int(args.get("amount") or -3))
    elif action in ("type", "input"):
        ok, detail = _type_text(str(args.get("text") or ""))
    elif action in ("press", "key"):
        ok, detail = _press_key(str(args.get("key") or "enter"))
    elif action in ("hotkey", "keys"):
        ok, detail = _hotkey(str(args.get("keys") or ""))
    elif action in ("screenshot", "screen"):
        return await _screen_capture(args, ctx)
    elif action in ("list_windows", "windows"):
        wins = _list_windows(int(args.get("max") or 30))
        try:
            await ctx.emitter.panel("gui_windows", "data", "桌面窗口列表",
                                    {"windows": wins}, status="ok")
        except Exception:
            pass
        return ToolResult(data={"windows": wins, "count": len(wins)})
    elif action in ("window_close", "close_window"):
        ok, detail = _window_act(int(args.get("hwnd") or 0), "close")
    elif action in ("window_move", "move_window"):
        ok, detail = _window_act(int(args.get("hwnd") or 0), "move", x, y)
    elif action in ("window_resize", "resize_window"):
        ok, detail = _window_act(int(args.get("hwnd") or 0), "resize", w=int(args.get("w") or 800),
                                 h=int(args.get("h") or 600))
    elif action in ("window_minimize", "minimize_window"):
        ok, detail = _window_act(int(args.get("hwnd") or 0), "minimize")
    elif action in ("window_maximize", "maximize_window"):
        ok, detail = _window_act(int(args.get("hwnd") or 0), "maximize")
    elif action in ("window_focus", "focus_window"):
        ok, detail = _window_act(int(args.get("hwnd") or 0), "focus")
    elif action == "close_all_windows":
        # 关闭桌面所有用户窗口(排除系统关键窗口/自身)
        closed = 0
        for w in _list_windows(80):
            t = w.get("title", "")
            if any(k in t for k in ("任务管理器", "Program Manager", "Shell_TrayWnd",
                                    "控制面板", "设置")):
                continue
            if t.lower().startswith(("python", "neura", "cmd")):
                continue
            try:
                _window_act(w["hwnd"], "close")
                closed += 1
            except Exception:
                pass
        return ToolResult(data={"action": "close_all_windows", "closed": closed})
    else:
        return ToolResult(ok=False, error=f"不支持的动作: {action}", error_type="invalid_args")
    if not ok:
        return ToolResult(ok=False, error=f"gui_control {action} 失败: {detail}", error_type="exec")
    return ToolResult(data={"action": action, "detail": detail, "ok": True})


def register(registry: ToolRegistry):
    registry.register(Tool(
        name="screen_capture",
        description="截取电脑当前屏幕(实时屏幕画面), 保存为图片并在 HUD 弹窗展示, 返回图片路径与物理像素尺寸(与鼠标坐标一致)及DPI。AI 需要看到屏幕以图形化操控电脑时使用(看屏幕->gui_control 操作->再截屏验证); 精读按钮/输入框/联系人等小控件时用 region_x/region_y/region_w/region_h 截取局部高清图。用户说'看下我的屏幕/截图给我看/实时屏幕'时调用。",
        parameters={"type": "object",
                    "properties": {"show": {"type": "boolean", "description": "是否在 HUD 弹窗展示截图(默认 false, 仅返回路径)"},
                                   "region_x": {"type": "integer", "description": "区域截图左上角X(配合 region_y/w/h 高清截取局部界面供 AI 精读, 可选)"},
                                   "region_y": {"type": "integer", "description": "区域截图左上角Y"},
                                   "region_w": {"type": "integer", "description": "区域截图宽度"},
                                   "region_h": {"type": "integer", "description": "区域截图高度"}},
                    "required": []},
        handler=_screen_capture,
        category="desktop",
    ))
    registry.register(Tool(
        name="gui_control",
        description="像人一样图形化操控电脑(数据性+图形化双通道, 允许 AI 直接操控鼠标): "
                   "move/smooth_move平滑移动 / move_rel相对移动 / mouse_pos获取鼠标位置 / "
                   "screen_info一步获取屏幕尺寸+鼠标位置+前台窗口+窗口列表 / click点击 / "
                   "double_click双击 / right_click右键 / mouse_down按住 / mouse_up释放(长按) / "
                   "drag拖拽(x,y->x2,y2,duration秒) / scroll滚动 / type输入文本 / press按键 / "
                   "hotkey组合键 / screenshot截屏 / list_windows枚举桌面窗口 / "
                   "window_close关闭窗口(hwnd) / window_move / window_resize / "
                   "window_minimize / window_maximize / window_focus / "
                   "close_all_windows关闭桌面所有用户窗口。配合 screen_info/screen_capture "
                   "形成「看屏幕->操控鼠标->验证」人类式闭环。用户说'帮我点一下/双击/拖拽/输入/"
                   "按回车/关闭XX窗口/移动窗口/缩小窗口'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["move", "smooth_move", "move_rel",
                          "mouse_pos", "mouse_down", "mouse_up", "screen_info",
                          "screen_size", "click", "double_click", "right_click",
                          "drag", "scroll", "type", "press", "hotkey", "screenshot",
                          "list_windows", "window_close", "window_move",
                          "window_resize", "window_minimize", "window_maximize",
                          "window_focus", "close_all_windows"]},
                "x": {"type": "integer", "description": "坐标X(点击/移动/拖拽起点)"},
                "y": {"type": "integer", "description": "坐标Y"},
                "x2": {"type": "integer", "description": "拖拽终点X(可选)"},
                "y2": {"type": "integer", "description": "拖拽终点Y(可选)"},
                "clicks": {"type": "integer", "description": "点击次数(click 用, 默认1)"},
                "duration": {"type": "number", "description": "拖拽/平滑移动时长秒(move 默认0 瞬移, smooth_move 默认0.35)"},
                "button": {"type": "string", "description": "鼠标键: left/right/middle(click/mouse_down/mouse_up 用, 默认left)"},
                "amount": {"type": "integer", "description": "滚动量(负数向下, 默认-3)"},
                "text": {"type": "string", "description": "type 要输入的文本"},
                "key": {"type": "string", "description": "press 按键名(enter/tab/esc/up/down/space...)"},
                "keys": {"type": "string", "description": "hotkey 组合键(如 ^c 或 ctrl+c)"},
                "hwnd": {"type": "integer", "description": "窗口句柄(枚举 window list 获得)"},
                "w": {"type": "integer", "description": "resize 宽"},
                "h": {"type": "integer", "description": "resize 高"},
                "max": {"type": "integer", "description": "list_windows 最多返回数(默认30)"},
            },
            "required": ["action"],
        },
        handler=_gui_control,
        category="desktop",
    ))
