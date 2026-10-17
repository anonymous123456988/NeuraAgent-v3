# -*- coding: utf-8 -*-
"""NEURA 系统环境画像(v3.41): Agent 对接模型后【自动采集当前系统状态】并注入给 AI,
让 AI 知道自己运行在什么平台上、有哪些可用操控手段, 从而天然兼容所有平台。

- 每次组装系统提示词时实时采集(毫秒级), 保证 AI 永远基于真实环境决策;
- 覆盖: 平台/OS/Python/架构/屏幕与 DPI/GUI 自动化可用性/命令 shell/盘符/语言/
  内置模型可用性;
- AI 也可随时调用 system_profile 工具重新获取最新画像。
"""
import os
import platform
import sys
import subprocess

_VERSION = "v3.41"


def _try(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _screen():
    """屏幕尺寸 + DPI/缩放(尽量获取, 失败返回 None)。"""
    out = {"width": None, "height": None, "dpi": None, "scale": None}
    try:
        if os.name == "nt":
            try:
                import ctypes
                u = ctypes.windll.user32
                out["width"] = int(u.GetSystemMetrics(0))
                out["height"] = int(u.GetSystemMetrics(1))
                try:
                    dpi = ctypes.windll.shcore.GetDpiForSystem()
                    if not dpi:
                        dpi = 96
                    out["dpi"] = int(dpi)
                    out["scale"] = round(dpi / 96.0, 2)
                except Exception:
                    out["dpi"] = 96
                    out["scale"] = 1.0
            except Exception:
                pass
        elif os.environ.get("DISPLAY"):
            r = subprocess.run(["xdpyinfo"], capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                for ln in (r.stdout or "").splitlines():
                    if "dimensions:" in ln:
                        p = ln.split("dimensions:")[1].split()[0]
                        w, _, h = p.partition("x")
                        try:
                            out["width"], out["height"] = int(w), int(h)
                        except Exception:
                            pass
                        break
    except Exception:
        pass
    return out


def _shell():
    if os.name == "nt":
        return "cmd.exe (Windows 命令提示符, 亦可用 powershell.exe)"
    return os.environ.get("SHELL", "bash")


def _drives():
    if os.name != "nt":
        return None
    out = []
    try:
        import string
        for c in string.ascii_uppercase:
            if os.path.exists(f"{c}:\\"):
                out.append(f"{c}:\\")
    except Exception:
        pass
    return out or ["C:\\"]


def _os_name():
    try:
        if os.name == "nt":
            import platform as _p
            return f"Windows {_p.win32_ver()[0]} ({_p.win32_ver()[1]})"
        if sys.platform == "darwin":
            return f"macOS {platform.mac_ver()[0]}"
        # Linux 发行版
        try:
            with open("/etc/os-release", encoding="utf-8") as f:
                for ln in f:
                    if ln.startswith("PRETTY_NAME="):
                        return ln.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass
        return platform.platform()
    except Exception:
        return platform.platform()


def _gui_avail():
    """图形化操控可用性: 返回 (可用, 后端说明)。"""
    if os.name == "nt":
        has = _try("win32api") and _try("win32gui")
        extra = []
        if _try("pywinauto"):
            extra.append("pywinauto")
        if _try("pyautogui"):
            extra.append("pyautogui")
        if _try("PIL"):
            extra.append("PIL")
        if has:
            return True, "win32" + (" + " + "+".join(extra) if extra else "")
        return False, "缺少 pywin32 等依赖"
    if os.environ.get("DISPLAY"):
        if _try("pyautogui"):
            return True, "pyautogui"
        try:
            r = subprocess.run(["which", "xdotool"], capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                return True, "xdotool"
        except Exception:
            pass
    return False, "无图形桌面会话(xdotool 不可用)"


def _lang():
    try:
        import locale
        return locale.getdefaultlocale()[0] or "unknown"
    except Exception:
        return "unknown"


def collect_system_profile() -> dict:
    """采集当前系统环境画像(自动, 毫秒级)。"""
    gui_ok, gui_backend = _gui_avail()
    scr = _screen()
    profile = {
        "agent_version": _VERSION,
        "platform": sys.platform,
        "os": _os_name(),
        "python": sys.version.split()[0],
        "arch": platform.machine(),
        "hostname": platform.node(),
        "language": _lang(),
        "screen": scr,
        "gui_available": gui_ok,
        "gui_backend": gui_backend,
        "shell": _shell(),
        "drives": _drives(),
        "builtin_model": _nlm_available(),
    }
    return profile


def _nlm_available():
    try:
        from agent.nlm import engine as _e  # noqa: F401
        return True
    except Exception:
        return False


def profile_to_text(p: dict) -> str:
    """把画像转成给 AI 的人类可读中文说明。"""
    scr = p.get("screen") or {}
    scr_txt = "未知"
    if scr.get("width"):
        scr_txt = f"{scr['width']}x{scr['height']}"
        if scr.get("dpi"):
            scr_txt += f", DPI={scr['dpi']}(缩放 {scr.get('scale')})"
    drives = p.get("drives")
    drives_txt = "、".join(drives) if drives else "(非 Windows, 无盘符)"
    gui = ("可用" if p.get("gui_available") else "不可用") + \
          ("(后端: " + str(p.get("gui_backend")) + ")" if p.get("gui_available") else
           " — 请勿尝试 gui_control/screen_capture, 改用命令性操作")
    return f"""- 平台: {p.get('platform')} / {p.get('os')}
- Python: {p.get('python')}({p.get('arch')})  主机: {p.get('hostname')}
- 屏幕: {scr_txt}
- 图形化操控(GUI 鼠标/截图): {gui}
- 命令终端: {p.get('shell')}
- 磁盘/根目录: {drives_txt}
- 系统语言: {p.get('language')}   内置模型: {'可用' if p.get('builtin_model') else '不可用'}
- Agent 版本: {p.get('agent_version')}"""
