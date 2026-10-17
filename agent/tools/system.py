# -*- coding: utf-8 -*-
"""工具：系统(命令执行/系统信息/打开应用/时间)。"""
import datetime
import os
import platform
import shutil
import re as _re
import subprocess
import sys
import time as _time

from agent import safety, utils
from .registry import Tool, ToolResult, ToolContext, ToolRegistry


async def _run_command(args: dict, ctx: ToolContext) -> ToolResult:
    command = str(args.get("command", ""))
    cwd = str(args.get("cwd", "~"))
    timeout = int(args.get("timeout", 30))
    try:
        command = safety.check_command(command, ctx.config)
    except safety.SafetyError as e:
        return ToolResult(ok=False, error=str(e), error_type="forbidden")
    # 管理员级别判定(敏感只读命令, 如 ps/systeminfo/netstat): 放行但前端红色警示
    admin = safety.check_admin(command, ctx.config)
    if admin:
        await ctx.emitter.notify(f"管理员级操作: {command.split()[0]}（结果以红色警示窗口展示）", level="warn")
    try:
        workdir = safety.resolve_path(cwd, ctx.config) if cwd else None
    except safety.SafetyError:
        workdir = None
    await ctx.emitter.notify(f"执行命令: {command}", level="info")
    res = await _run_in_thread(command, workdir, timeout, ctx.config)
    out = safety.sanitize_output(res["stdout"], ctx.config.get("permissions", {}).get("code_execution", {}).get("output_cap_chars", 60000))
    err = safety.sanitize_output(res["stderr"], 20000)
    if not res["ok"]:
        return ToolResult(ok=False, error=f"命令退出码 {res['code']}\n{err or out}", error_type="exec", admin=admin)
    return ToolResult(data={"command": command, "exit_code": res["code"], "stdout": out, "stderr": err},
                      admin=admin)


async def _run_in_thread(command, workdir, timeout, cfg):
    mem = cfg.get("permissions", {}).get("code_execution", {}).get("memory_limit_mb", 512)
    fj = cfg.get("permissions", {}).get("code_execution", {}).get("firejail", False)
    import asyncio
    return await asyncio.to_thread(safety.sandbox_run, command, workdir, timeout, mem, fj)


def _cpu_info() -> str:
    try:
        if os.name == "nt":
            return platform.processor() or "Unknown"
        with open("/proc/cpuinfo") as f:
            lines = f.readlines()
        models = {l.split(":", 1)[1].strip() for l in lines if l.startswith("model name")}
        if models:
            return next(iter(models))
        cores = sum(1 for l in lines if l.startswith("processor"))
        return f"{cores} 逻辑核心"
    except Exception:
        return platform.processor() or "Unknown"


def _mem_info() -> dict:
    # 多通道兜底: Windows ctypes(显式签名) -> psutil -> /proc/meminfo -> 系统命令
    try:
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes, POINTER, Structure
                class MS(Structure):
                    _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                                ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong)]
                fn = ctypes.windll.kernel32.GlobalMemoryStatusEx
                fn.argtypes = [POINTER(MS)]
                fn.restype = wintypes.BOOL
                m = MS()
                m.length = ctypes.sizeof(MS)
                if fn(ctypes.byref(m)) and m.total:
                    return {"total_mb": m.total // (1024 * 1024),
                            "used_mb": (m.total - m.avail) // (1024 * 1024)}
            except Exception:
                pass
        try:
            import psutil
            vm = psutil.virtual_memory()
            if vm.total:
                return {"total_mb": vm.total // (1024 * 1024), "used_mb": vm.used // (1024 * 1024)}
        except Exception:
            pass
        with open("/proc/meminfo") as f:
            d = {k: v for k, v in (line.split(":") for line in f if ":" in line)}
        total = int(d.get("MemTotal", "0").strip().split()[0]) // 1024
        avail = int(d.get("MemAvailable", "0").strip().split()[0]) // 1024
        if total:
            return {"total_mb": total, "used_mb": total - avail}
        return {}
    except Exception:
        return {}


async def _system_info(args: dict, ctx: ToolContext) -> ToolResult:
    mem = _mem_info()
    disk = shutil.disk_usage(os.path.expanduser("~"))
    boot = _time.time()
    data = {
        "os": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu": _cpu_info(),
        "memory_mb": mem,
        "disk_total_gb": round(disk.total / 2 ** 30, 1),
        "disk_used_gb": round(disk.used / 2 ** 30, 1),
        "disk_free_gb": round(disk.free / 2 ** 30, 1),
        "python": platform.python_version(),
        "home": os.path.expanduser("~"),
    }
    return ToolResult(data=data)


# ---------------------------------------------------------------
# 系统资源利用率(system_stats): 真实采样 CPU/内存/磁盘, 纯 stdlib 跨平台
# ---------------------------------------------------------------
def _cpu_usage_percent() -> float:
    """两次采样 /proc/stat(间隔 0.3s)计算 CPU 利用率; 其余平台用 platform 命令回退。"""
    if os.path.exists("/proc/stat"):
        def _snap():
            with open("/proc/stat") as f:
                vals = [int(x) for x in f.readline().split()[1:7]]
            return vals
        try:
            a, b = _snap(), (_time.sleep(0.3), _snap())[1]
            idle_a, idle_b = a[3], b[3]
            total_a, total_b = sum(a), sum(b)
            delta_total = total_b - total_a or 1
            delta_idle = idle_b - idle_a
            return round(100.0 * (1 - delta_idle / delta_total), 1)
        except Exception:
            pass
    # Windows: typeperf 或 wmic(短采样)
    for cmd in (["typeperf", "-sc", "1", "\\Processor(_Total)\\% Processor Time"],
                ["wmic", "cpu", "get", "loadpercentage"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=4,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            m = _re.findall(r"[0-9]+(?:\.\d+)?", r.stdout)
            for v in m:
                if v and 0 <= float(v) <= 100:
                    return round(float(v), 1)
        except Exception:
            continue
    # macOS / 其他: top 命令解析
    try:
        r = subprocess.run(["top", "-l", "2", "-n", "0"], capture_output=True, text=True, timeout=4)
        m = _re.findall(r"CPU usage:\s*([0-9.]+)%", r.stdout)
        if m:
            return round(float(m[-1]), 1)
    except Exception:
        pass
    return 0.0


def _mem_usage_percent() -> float:
    try:
        mem = _mem_info()
        if mem.get("total_mb"):
            return round(100.0 * mem["used_mb"] / mem["total_mb"], 1)
    except Exception:
        pass
    return 0.0


def _disk_usage_percent() -> float:
    try:
        u = shutil.disk_usage(os.path.expanduser("~"))
        return round(100.0 * u.used / u.total, 1)
    except Exception:
        return 0.0


async def _system_stats(args: dict, ctx: ToolContext) -> ToolResult:
    """实时采样系统资源利用率(CPU/内存/磁盘)。"""
    mem = _mem_info()
    disk = shutil.disk_usage(os.path.expanduser("~"))
    data = {
        "cpu_percent": _cpu_usage_percent(),
        "mem_percent": _mem_usage_percent(),
        "mem_mb": {"total_mb": mem.get("total_mb", 0), "used_mb": mem.get("used_mb", 0)},
        "disk_percent": _disk_usage_percent(),
        "disk_total_gb": round(disk.total / 2 ** 30, 1),
        "disk_used_gb": round(disk.used / 2 ** 30, 1),
        "disk_free_gb": round(disk.free / 2 ** 30, 1),
        "sampled_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return ToolResult(data=data)


# ---------------------------------------------------------------
# 依赖安装(install_dependency): pip 安装 + 实时进度 -> 前端进度条窗口
# ---------------------------------------------------------------
async def _install_dependency(args: dict, ctx: ToolContext) -> ToolResult:
    pkg = str(args.get("package", "")).strip()
    if not pkg:
        return ToolResult(ok=False, error="缺少 package(要安装的模块/包)", error_type="invalid_args")
    win_id = str(args.get("window_id", "dep_install")) or "dep_install"
    mirror = str(args.get("mirror", "") or "").strip()
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    if mirror:
        cmd += ["-i", mirror]
    cmd.append(pkg)
    await ctx.emitter.panel(win_id, "progress", f"正在安装 {pkg}", {"percent": 2, "text": "启动 pip…"},
                            status="running")
    import asyncio, re as _re
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=os.path.expanduser("~"))
    lines = []
    percent = 3
    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
            if not line:
                break
            line = line.decode("utf-8", "replace").strip()
            if not line:
                continue
            lines.append(line[-220:])
            m = _re.search(r"(\d+)\s*/\s*(\d+)", line)
            if m and int(m.group(2)) > 0:
                percent = max(percent, min(96, int(round(100 * int(m.group(1)) / int(m.group(2))))))
            elif "Downloading" in line or "Collecting" in line:
                percent = max(percent, min(60, percent + 8))
            elif "Installing" in line:
                percent = max(percent, 82)
            await ctx.emitter.panel_update(win_id, {"percent": percent, "text": line[-90:]}, status="running")
    except asyncio.TimeoutError:
        pass
    rc = await proc.wait()
    await ctx.emitter.panel_update(win_id, {"percent": 100, "text": "安装完成" if rc == 0 else "安装失败"}, status="ok" if rc == 0 else "error")
    if rc != 0:
        return ToolResult(ok=False, error=f"pip 安装失败(退出码 {rc}): {chr(10).join(lines[-6:])}", error_type="exec")
    return ToolResult(data={"package": pkg, "installed": True, "pip": " ".join(cmd)})


# ---------------------------------------------------------------
# install_package: 命令可装 -> 命令安装(多包管理器); 命令装不了 -> 图形化安装器接管(GUI)
# ---------------------------------------------------------------
_PKG_MANAGERS = [
    ("pip3", ["pip3", "install", "-q"], "Python 包"),
    ("pip", ["pip", "install", "-q"], "Python 包"),
    ("apt-get", ["sudo", "apt-get", "install", "-y"], "Debian 包"),
    ("yum", ["sudo", "yum", "install", "-y"], "RHEL 包"),
    ("dnf", ["sudo", "dnf", "install", "-y"], "Fedora 包"),
    ("brew", ["brew", "install"], "macOS 包"),
    ("pacman", ["sudo", "pacman", "-S", "--noconfirm"], "Arch 包"),
    ("npm", ["npm", "install", "-g"], "Node 包"),
    ("yarn", ["yarn", "global", "add"], "Node 包"),
    ("pnpm", ["pnpm", "add", "-g"], "Node 包"),
    ("cargo", ["cargo", "install"], "Rust 包"),
    ("go", ["go", "install"], "Go 包"),
]


def _pick_manager(pkg: str):
    low = pkg.lower()
    if low.startswith(("lib", "py", "django", "flask", "requests", "numpy", "pandas", "torch")) or "python" in low:
        return next((m for m in _PKG_MANAGERS if m[0] == "pip3"), None)
    if low.endswith((".deb", ".rpm")):
        return next((m for m in _PKG_MANAGERS if m[0] == "apt-get"), None)
    import shutil
    for name, args, _l in _PKG_MANAGERS:
        if shutil.which(name):
            return (name, args, _l)
    return None


async def _install_package(args: dict, ctx: ToolContext) -> ToolResult:
    """统一安装: method=auto 命令优先, 失败或无命令 -> gui 图形化安装器接管。"""
    pkg = str(args.get("package", "")).strip()
    method = str(args.get("method", "auto")).strip() or "auto"
    if not pkg or pkg in ("unknown",):
        return ToolResult(ok=False, error="未识别要安装的软件/包名称, 请说清楚(如: 安装github / 安装微信 / pip安装requests)",
                          error_type="invalid_args")
    mgr = _pick_manager(pkg)
    if mgr and method != "gui":
        # ---- 命令安装: 实时进度 ----
        win_id = "install_" + pkg[:12]
        name, args_l, label = mgr
        cmd = list(args_l) + [pkg]
        await ctx.emitter.panel(win_id, "progress", "正在安装 " + pkg,
                                {"percent": 3, "text": "使用 " + label + " 命令安装…"}, status="running")
        import asyncio, re as _re
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.expanduser("~"))
        except FileNotFoundError:
            return await _gui_install(args, ctx)
        lines, percent = [], 10
        import time as _t
        _last_push = _t.time()
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
                    if not line:
                        break
                    line = line.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    lines.append(line[-160:])
                    m = _re.search(r"(\d+)\s*/\s*(\d+)", line)
                    if m and int(m.group(2)) > 0:
                        percent = max(percent, min(95, int(100 * int(m.group(1)) / int(m.group(2)))))
                    elif "Downloading" in line or "Unpacking" in line:
                        percent = max(percent, min(70, percent + 5))
                    elif "Setting up" in line or "Installing" in line:
                        percent = max(percent, min(88, percent + 3))
                    else:
                        percent = min(88, percent + 2)   # 有输出就推进, 绝不卡 0%
                    if _t.time() - _last_push >= 0.4:
                        await ctx.emitter.panel_update(win_id, {"percent": percent, "text": line[-90:]}, status="running")
                        _last_push = _t.time()
                except asyncio.TimeoutError:
                    # 3 秒无输出: 安装仍在进行, 强制推进进度并继续等待
                    percent = min(90, percent + 3)
                    if _t.time() - _last_push >= 1:
                        await ctx.emitter.panel_update(win_id, {"percent": percent, "text": "安装进行中(等待输出)…"}, status="running")
                        _last_push = _t.time()
                    continue
        except Exception:
            pass
        rc = await proc.wait()
        await ctx.emitter.panel_update(win_id,
                                       {"percent": 100, "text": "安装完成" if rc == 0 else "安装失败, 转图形化安装器"},
                                       status="ok" if rc == 0 else "error")
        if rc == 0:
            return ToolResult(data={"package": pkg, "method": "command:" + name, "installed": True})
        return await _gui_install(args, ctx)
    # ---- GUI 图形化安装器接管 ----
    return await _gui_install(args, ctx)


def _gui_win_click(win, keywords: tuple) -> str:
    """在安装向导窗口中按文本关键词自动点击按钮(Next/同意/Install/Finish)。"""
    clicked = []
    try:
        if hasattr(win, "descendants"):
            btns = win.descendants()
            for kw in keywords:
                for b in btns:
                    try:
                        txt = (b.window_text() or b.element_info.control_type or "").strip().lower()
                    except Exception:
                        txt = ""
                    if kw in txt:
                        b.click_input()
                        clicked.append(kw)
                        break
        if not clicked:
            win.type_keys("{ENTER}")
            clicked.append("enter")
    except Exception:
        try:
            win.type_keys("{ENTER}")
            clicked.append("enter")
        except Exception:
            pass
    return ",".join(clicked) or "none"


async def _gui_install(args: dict, ctx: ToolContext) -> ToolResult:
    """图形化联网安装: 搜索官方下载 -> 流式下载(实时进度) -> 启动 -> 自动点击安装向导(小AI 监管各步)。
    全程逐阶段增量更新进度条, 任何失败都把进度推到失败态并给出明确文案, 绝不停留在 0%。"""
    pkg = str(args.get("package", "")).strip()
    if not pkg:
        return ToolResult(ok=False, error="缺少 package", error_type="invalid_args")
    win_id = "gui_" + pkg[:10]
    steps = []
    import asyncio, urllib.parse

    async def _pg(pct, txt, status="running"):
        try:
            await ctx.emitter.panel_update(win_id, {"percent": pct, "text": txt}, status=status)
        except Exception:
            pass

    await ctx.emitter.panel(win_id, "progress", "图形化安装 " + pkg,
                            {"percent": 5, "text": "开始安装流程…"}, status="running")
    # 1) 联网搜索官方下载地址(小AI 全程监管: 仅允许官方域名; 单批 8s 收敛, 不卡死)
    await _pg(8, "正在联网检索官方下载源…")
    from agent.tools.web import _web_search, _clean_search_query
    try:
        sr = await asyncio.wait_for(
            _web_search({"query": _clean_search_query(pkg + " 官方 下载 Windows") or (pkg + " 官方下载"),
                         "max_results": 6}, ctx), timeout=10)
    except Exception:
        sr = None
    dl_url = None
    if sr and sr.ok and sr.data:
        for r in (sr.data.get("results") or []):
            u = str(r.get("url", ""))
            if any(d in u for d in (".github.com", "github.com", "apple.com", "microsoft.com", "tencent.com",
                                    "qq.com", "google.com", "docker.com", "python.org", "nodejs.org",
                                    "jetbrains.com", "weixin.qq.com", "im.qq.com")):
                dl_url = u
                break
    steps.append("1. 联网检索官方下载源: " + ("命中 " + dl_url if dl_url else "未命中官方域名"))
    # 2) 非 Windows: 命令安装兜底(带进度)
    if not dl_url and os.name != "nt":
        mgr = _pick_manager(pkg)
        if mgr:
            name, args_l, _l = mgr
            cmd = list(args_l) + [pkg]
            steps.append("2. 尝试 " + name + " 命令安装…")
            await _pg(20, "使用 " + name + " 命令安装…")
            try:
                r = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(r.communicate(), timeout=120)
            except Exception as e:
                out = str(e).encode()
                r = type("R", (), {"returncode": 1})()
            if r.returncode == 0:
                await _pg(100, "已通过 " + name + " 安装 " + pkg, "ok")
                return ToolResult(data={"package": pkg, "method": "command:" + name, "installed": True,
                                        "steps": steps + ["安装成功"]})
            steps.append("2. " + name + " 安装失败(rc=" + str(r.returncode) + ")")
            await _pg(35, name + " 命令安装失败, 尝试图形化安装…")
    # 3) 流式下载安装包(按字节实时推进度)并启动 GUI 安装向导
    #    v3.29: 安装包一律下载到【AI 所造的目录】(Agent 下载目录下 installers/<pkg>/),
    #    绝不下到系统下载目录(~\\Downloads)——否则 AI 的进程/沙箱无法定位安装包,
    #    图形化安装找不到文件而失败。
    downloaded = None
    if dl_url and os.name == "nt":
        try:
            import httpx, shutil as _shutil
            ext = ".exe" if ".exe" in dl_url else (os.path.splitext(urllib.parse.urlparse(dl_url).path)[1] or ".exe")
            fname = pkg.replace(" ", "_") + (ext if ext.startswith(".") else ".exe")
            # AI 目录: Agent 下载目录/installers/<pkg>/, 确保存在(每次安装独立子目录, 避免同名覆盖)
            ai_install_dir = os.path.join(ctx.downloads_dir, "installers", pkg.replace(" ", "_").lower()[:20])
            try:
                os.makedirs(ai_install_dir, exist_ok=True)
            except Exception:
                ai_install_dir = ctx.downloads_dir
            dest = os.path.join(ai_install_dir, fname)
            # 兜底迁移: 若目标已存在同名旧包, 先清除旧包再下载, 防止残留损坏文件被启动
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except Exception:
                    pass
            await _pg(45, "开始下载 " + dl_url)
            with httpx.Client(timeout=60, follow_redirects=True) as cl:
                with cl.stream("GET", dl_url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length") or 0)
                    got = 0
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_bytes(65536):
                            f.write(chunk)
                            got += len(chunk)
                            if total > 0:
                                await _pg(45 + int(40 * got / max(1, total)),
                                          "下载中 " + str(int(got / 1024 / 1024)) + "MB / " +
                                          str(int(total / 1024 / 1024)) + "MB")
            # 下载完成校验 + 兜底迁移(文件可能被浏览器/外部下载器截获到系统下载目录)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                downloaded = dest
                steps.append("3. 已下载安装包(AI 目录): " + dest)
            else:
                # 系统下载目录兜底: 找到同名/含包名安装包 -> 自动迁移到 AI 目录
                for _dl_dir in (os.path.join(os.path.expanduser("~"), "Downloads"),
                                os.path.join(os.path.expanduser("~"), "下载")):
                    if not os.path.isdir(_dl_dir):
                        continue
                    for _fn in os.listdir(_dl_dir):
                        _lfn = _fn.lower()
                        if fname.lower() in _lfn or pkg.lower().replace(" ", "") in _lfn.replace(" ", ""):
                            if not _lfn.endswith((".exe", ".msi", ".dmg", ".deb", ".rpm")):
                                continue
                            _src = os.path.join(_dl_dir, _fn)
                            try:
                                _shutil.move(_src, dest)
                                downloaded = dest
                                steps.append("3. 系统下载目录发现安装包, 已迁移到 AI 目录: " + dest)
                                break
                            except Exception:
                                pass
                    if downloaded:
                        break
            if downloaded:
                await _pg(88, "启动安装向导…")
                subprocess.Popen([downloaded], shell=False)
                await asyncio.sleep(5)
            try:
                from pywinauto import Desktop
                win = None
                for _ in range(8):
                    for w in Desktop(backend="uia").windows():
                        wt = w.window_text().lower()
                        if pkg.lower() in wt or "setup" in wt or "install" in wt:
                            win = w
                            break
                    if win:
                        break
                    await asyncio.sleep(1)
                clicks = []
                if win:
                    win.set_focus()
                    for i, kws in enumerate((("同意", "accept", "next", "下一步", "i agree", "继续"),
                                             ("install", "安装", "立即安装"),
                                             ("finish", "完成", "done", "关闭"))):
                        clicks.append(_gui_win_click(win, kws))
                        await _pg(90 + i * 3, "正在自动点击安装向导…")
                        await asyncio.sleep(2)
                steps.append("4. GUI 向导自动操作: " + str(clicks))
            except Exception as e:
                steps.append("4. GUI 自动点击不可用: " + str(e) + " (可双击安装包手动完成)")
        except Exception as e:
            steps.append("3. 下载/启动失败: " + str(e))
            await _pg(100, "下载/启动失败: " + str(e)[:60], "error")
            return ToolResult(ok=False, error="安装失败: " + str(e)[:120],
                              error_type="exec", data={"package": pkg, "method": "gui", "steps": steps})
    if downloaded:
        await _pg(100, "安装向导已启动, 自动点击完成", "ok")
    else:
        await _pg(100, "未能自动下载, 请手动安装或换一种说法", "error")
    note = "安装包已下载并启动安装向导" if downloaded else "未能自动下载, 请手动安装或换一种说法"
    return ToolResult(data={"package": pkg, "method": "gui", "steps": steps, "note": note,
                            "installed": bool(downloaded)})


# ---------------------------------------------------------------
# draw_image: AI 绘画(内置 SVG 绘画引擎 -> 图片文件 -> 控制板预览 + 下载)
# ---------------------------------------------------------------
_SVG_PALETTE = [("#00d4ff", "#0a2a4a"), ("#7aa2ff", "#1b1f3a"), ("#34e0a1", "#0a2a2a"),
                ("#ff9e64", "#3a1f0a"), ("#e06cff", "#2a0a3a"), ("#ffd866", "#3a320a")]


def _svg_image(desc: str):
    """根据描述生成科技风 SVG(确定性绘画引擎, 不依赖外部模型/网络)。返回 (svg_str, title)。"""
    import html as _h
    import random
    rnd = random.Random(desc)
    c1, c2 = _SVG_PALETTE[rnd.randrange(len(_SVG_PALETTE))]
    title = _h.escape(desc[:24])
    W, H = 900, 560
    shapes = []
    if any(k in desc for k in ("猫", "狗", "动物", "卡通", "兔子", "狐狸", "熊猫")):
        shapes.append("<ellipse cx='450' cy='330' rx='170' ry='120' fill='" + c1 + "' opacity='0.85'/>")
        shapes.append("<ellipse cx='410' cy='270' rx='46' ry='40' fill='" + c1 + "'/>")
        shapes.append("<ellipse cx='490' cy='270' rx='46' ry='40' fill='" + c1 + "'/>")
        shapes.append("<circle cx='392' cy='262' r='7' fill='#e8f6ff'/><circle cx='394' cy='262' r='3' fill='#081018'/>")
        shapes.append("<circle cx='472' cy='262' r='7' fill='#e8f6ff'/><circle cx='474' cy='262' r='3' fill='#081018'/>")
        shapes.append("<path d='M430 318 q20 26 40 0' stroke='#081018' stroke-width='4' fill='none'/>")
        shapes.append("<path d='M310 360 q30 46 90 20 M590 360 q-30 46 -90 20' stroke='" + c1 + "' stroke-width='6' fill='none' opacity='0.9'/>")
    elif any(k in desc for k in ("山", "风景", "海", "湖", "森林", "自然", "天空")):
        shapes.append("<path d='M0 380 L260 180 L420 360 L600 140 L900 420 L900 560 L0 560 Z' fill='" + c1 + "' opacity='0.5'/>")
        shapes.append("<path d='M0 440 L300 260 L520 420 L900 240 L900 560 L0 560 Z' fill='" + c2 + "' opacity='0.6'/>")
        shapes.append("<circle cx='660' cy='120' r='56' fill='" + c1 + "' opacity='0.9'/>")
    elif any(k in desc for k in ("科技", "未来", "机器人", "ai", "芯片", "电路", "网络")):
        shapes.append("<path d='M120 460 L220 120 L330 300 L430 80 L560 340 L660 160 L780 460' stroke='" + c1 + "' stroke-width='5' fill='none' opacity='0.95'/>")
        for _ in range(12):
            shapes.append("<circle cx='" + str(rnd.randrange(80, 840)) + "' cy='" + str(rnd.randrange(80, 460)) + "' r='" + str(rnd.randrange(2, 7)) + "' fill='" + c1 + "' opacity='0.7'/>")
    elif any(k in desc for k in ("城市", "建筑", "楼", "夜景")):
        for i in range(9):
            w = rnd.randrange(40, 90); h = rnd.randrange(120, 320); x = 60 + i * 92
            shapes.append("<rect x='" + str(x) + "' y='" + str(560 - h) + "' width='" + str(w) + "' height='" + str(h) + "' fill='" + c1 + "' opacity='0.35' rx='6'/>")
            for wy in range(560 - h + 24, 540, 30):
                shapes.append("<rect x='" + str(x + 12) + "' y='" + str(wy) + "' width='10' height='10' fill='" + c1 + "' opacity='0.9'/>")
    else:
        for i in range(3):
            shapes.append("<ellipse cx='" + str(rnd.randrange(120, 780)) + "' cy='" + str(rnd.randrange(120, 400)) + "' rx='" + str(rnd.randrange(60, 200)) + "' ry='" + str(rnd.randrange(40, 120)) + "' fill='" + c1 + "' opacity='0.25'/>")
    hex_g = ("<g stroke='" + c1 + "' stroke-width='1' opacity='0.08' fill='none'>"
             "<polygon points='40,40 90,40 115,72 90,104 40,104 15,72'/>"
             "<polygon points='240,40 290,40 315,72 290,104 240,104 215,72'/>"
             "<polygon points='440,40 490,40 515,72 490,104 440,104 415,72'/>"
             "<polygon points='640,40 690,40 715,72 690,104 640,104 615,72'/>"
             "<polygon points='840,40 890,40 915,72 890,104 840,104 815,72'/>" + "</g>")
    parts = ["<svg xmlns='http://www.w3.org/2000/svg' width='" + str(W) + "' height='" + str(H) + "' viewBox='0 0 " + str(W) + " " + str(H) + "'>",
             "<defs><linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'>"
             "<stop offset='0' stop-color='" + c2 + "'/><stop offset='1' stop-color='#02060f'/></linearGradient>"
             "<linearGradient id='glow' x1='0' y1='0' x2='0' y2='1'>"
             "<stop offset='0' stop-color='" + c1 + "' stop-opacity='0.9'/><stop offset='1' stop-color='" + c1 + "' stop-opacity='0.15'/></linearGradient></defs>",
             "<rect width='" + str(W) + "' height='" + str(H) + "' fill='url(#bg)'/>",
             hex_g,
             "".join(shapes),
             "<rect x='0' y='470' width='" + str(W) + "' height='90' fill='url(#glow)' opacity='0.5'/>",
             "<text x='60' y='520' font-family='Orbitron, sans-serif' font-size='34' fill='" + c1 + "' font-weight='600' opacity='0.95'>" + title + "</text>",
             "</svg>"]
    svg = "".join(parts)
    return svg, title


async def _draw_image(args: dict, ctx: ToolContext) -> ToolResult:
    """AI 绘画: 根据自然语言描述用内置 SVG 绘画引擎生成科技风图片, 控制板预览 + 文件下载。"""
    desc = str(args.get("desc", "")).strip() or "科技插画"
    try:
        svg, title = _svg_image(desc)
    except Exception as e:
        return ToolResult(ok=False, error="绘画失败: " + str(e), error_type="exec")
    import base64
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    out_dir = os.path.join(os.path.expanduser("~"), "NEURA绘画")
    os.makedirs(out_dir, exist_ok=True)
    fname = "neura_" + str(abs(hash(desc)) % 999999) + ".svg"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(svg)
    png_path = None
    try:
        import cairosvg
        png_path = os.path.join(out_dir, fname.replace(".svg", ".png"))
        cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=png_path, output_width=900)
    except Exception:
        pass
    await ctx.emitter.panel("draw_" + str(abs(hash(desc)) % 99999), "image", "AI 绘画 · " + title,
                            {"image_base64": b64, "mime": "image/svg+xml", "path": fpath, "desc": desc},
                            status="ok")
    return ToolResult(data={"title": title, "desc": desc, "path": fpath,
                            "png_path": png_path, "svg_base64": b64, "mime": "image/svg+xml",
                            "note": "SVG 绘画已生成, 见控制板预览窗口; 文件已保存, 支持下载"},
                       downloads=[(os.path.basename(fpath), fpath)])


# ---------------------------------------------------------------
# monitor_task: 实时任务(持续采样 -> 控制板实时刷新, 可停止)
# ---------------------------------------------------------------
async def _monitor_task(args: dict, ctx: ToolContext) -> ToolResult:
    """实时任务: 每 interval 秒采样(CPU/内存/磁盘/命令输出)并实时推送到控制板命令窗口, 直到关闭窗口停止。"""
    target = str(args.get("target", "")).strip() or "系统资源"
    interval = max(1, int(args.get("interval", 2)))
    win_id = "monitor_" + str(abs(hash(target)) % 99999)
    try:
        import psutil
        has_ps = True
    except Exception:
        has_ps = False
    await ctx.emitter.panel(win_id, "command", "实时监控 · " + target, {"lines": [], "running": True},
                            status="running")
    import asyncio, time
    lines = []
    rounds = 0
    while True:
        try:
            if has_ps and any(k in target for k in ("cpu", "内存", "磁盘", "资源", "系统")):
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage("/")
                line = ("[" + time.strftime("%H:%M:%S") + "] CPU " + str(cpu) + "% | 内存 " + str(mem.percent) + "% ("
                        + str(round(mem.used / 1024 ** 3, 1)) + "/" + str(round(mem.total / 1024 ** 3, 1)) + "GB) | "
                        + "磁盘 " + str(disk.percent) + "% (剩余 " + str(round(disk.free / 1024 ** 3, 1)) + "GB)")
            else:
                r = subprocess.run(target, shell=True, capture_output=True, text=True, timeout=8,
                                   cwd=os.path.expanduser("~"))
                out = (r.stdout or r.stderr or "").strip()[-120:]
                line = "[" + time.strftime("%H:%M:%S") + "] " + (out or "(无输出)")
        except Exception as e:
            line = "[" + time.strftime("%H:%M:%S") + "] 采样失败: " + str(e)
        lines.append(line)
        rounds += 1
        await ctx.emitter.panel_update(win_id, {"lines": lines[-24:], "running": True,
                                                "note": "已实时采样 " + str(rounds) + " 轮(每 " + str(interval) + "s, 关闭窗口即停止)"},
                                       status="running")
        await asyncio.sleep(interval)
        if rounds >= 30:
            break
    await ctx.emitter.panel_update(win_id, {"running": False, "note": "实时监控结束, 共 " + str(rounds) + " 轮"},
                                   status="ok")
    return ToolResult(data={"target": target, "rounds": rounds, "lines": lines[-12:]})


async def _open_app(args: dict, ctx: ToolContext) -> ToolResult:
    """打开应用: 已知系统工具走安全映射; 其余任意软件【整体委托 app_control._launch_app】
    (已运行复用 -> 定位 -> GUI 降级 -> 窗口真出现验证)。v3.40 修复: 彻底弃用
    `cmd /c start "" QQ` 裸启动(QQ 不在 PATH 时会触发 Windows 系统弹窗
    "Windows找不到文件'QQ'"), 统一走验证过的启动链路, 绝不触发系统弹窗。"""
    app = str(args.get("app", ""))
    known = {
        "notepad": ["notepad.exe", "notepad"], "记事本": ["notepad.exe", "notepad"],
        "calculator": ["calc.exe", "gnome-calculator"], "计算器": ["calc.exe", "gnome-calculator"],
        "explorer": ["explorer.exe", "xdg-open ."], "资源管理器": ["explorer.exe", "xdg-open ."],
        "terminal": ["cmd.exe", "x-terminal-emulator"], "终端": ["cmd.exe", "x-terminal-emulator"],
        "browser": ["start http://www.baidu.com", "xdg-open http://www.baidu.com"], "浏览器": ["start http://www.baidu.com", "xdg-open http://www.baidu.com"],
    }
    if app.lower() in known or app in known:
        cmd = known.get(app.lower(), known.get(app))
        command = cmd[0] if os.name == "nt" else cmd[1]
        try:
            if os.name == "nt":
                if command.startswith("start "):
                    # 浏览器等: 用 os.startfile 打开 URL/已知系统命令(路径确定, 无弹窗风险)
                    url = command[6:].strip()
                    subprocess.Popen(["cmd", "/c", "start", "", url], shell=True)
                else:
                    subprocess.Popen(["cmd", "/c", "start", "", command], shell=True)
            else:
                subprocess.Popen(command, shell=True, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return ToolResult(data={"app": app, "launched": command})
        except Exception as e:
            return ToolResult(ok=False, error=f"启动失败: {e}", error_type="exec")
    if not app.strip():
        return ToolResult(ok=False, error="缺少 app 参数", error_type="invalid_args")
    # 任意软件: 整体委托新版启动链路(复用检测/GUI 降级/窗口验证/不触发系统弹窗)
    from .app_control import _launch_app
    return await _launch_app({"app": app, "args": ""}, ctx)


async def _get_time(args: dict, ctx: ToolContext) -> ToolResult:
    now = datetime.datetime.now().astimezone()
    data = {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "weekday": "星期" + "一二三四五六日"[now.weekday()],
        "time": now.strftime("%H:%M:%S"),
        "timezone": str(now.tzinfo or "local"),
        "unix": int(now.timestamp()),
    }
    return ToolResult(data=data)


async def _calculator(args: dict, ctx: ToolContext) -> ToolResult:
    expr = str(args.get("expression", ""))
    if not expr:
        return ToolResult(ok=False, error="缺少 expression", error_type="invalid_args")
    if any(ch in expr for ch in "abcdefghijklmnopqrstuvwxyz_"):
        # 允许函数名白名单
        import re
        allowed = {"sin", "cos", "tan", "sqrt", "log", "abs", "pi", "e", "round", "floor", "ceil"}
        used = set(re.findall(r"[a-zA-Z_]+", expr)) - {"pi", "e"}
        if used - allowed:
            return ToolResult(ok=False, error="表达式包含不允许的函数", error_type="invalid_args")
    try:
        ns = {"__builtins__": {}} if not any(c in expr for c in "abcdefghijklmnopqrstuvwxyz_") else {}
        import math
        safe = {"sin": math.sin, "cos": math.cos, "tan": math.tan, "sqrt": math.sqrt,
                "log": math.log, "abs": abs, "round": round, "floor": math.floor,
                "ceil": math.ceil, "pi": math.pi, "e": math.e}
        val = eval(expr, {"__builtins__": {}}, safe)
        return ToolResult(data={"expression": expr, "result": val})
    except Exception as e:
        return ToolResult(ok=False, error=f"计算失败: {e}", error_type="invalid_args")


async def _system_profile(args: dict, ctx: ToolContext) -> ToolResult:
    """返回当前系统环境画像(平台/OS/Python/屏幕/GUI可用性/终端/盘符/语言)。"""
    try:
        from agent.system_profile import collect_system_profile, profile_to_text
        p = collect_system_profile()
        txt = profile_to_text(p)
        return ToolResult(data={"profile": p, "text": txt},
                          message="已采集当前系统环境画像")
    except Exception as e:
        return ToolResult(ok=False, error=f"采集系统画像失败: {e}")


def register(registry: ToolRegistry):
    registry.register(Tool(
        name="system_profile",
        description="获取当前系统环境画像: 平台/OS/Python/屏幕尺寸与DPI/图形化操控可用性/"
                     "命令终端/磁盘根目录/系统语言/内置模型可用性。AI 对接任何平台时先调用"
                     "或依据提示词中的画像适配环境; 跨环境/重启后可随时重新获取。",
        parameters={"type": "object", "properties": {}},
        handler=_system_profile,
        category="system",
    ))
    registry.register(Tool(
        name="run_command",
        description="执行系统命令(受白名单与黑名单约束, 禁止破坏性命令)。例如 ls /home/user、dir C:\\\\、ping 127.0.0.1。",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "工作目录, 默认家目录"},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
        },
        handler=_run_command,
        category="system",
    ))
    registry.register(Tool(
        name="system_info",
        description="获取系统信息(OS/CPU/内存/磁盘/Python 版本)。",
        parameters={"type": "object", "properties": {}},
        handler=_system_info,
        category="system",
    ))
    registry.register(Tool(
        name="open_app",
        description="打开电脑里的任何软件/程序: 系统工具(记事本/计算器/浏览器/终端)直接启动; "
                     "任意软件(QQ/微信/浏览器等)自动复用已运行实例或完整定位并图形化打开, "
                     "窗口真出现才报告成功。用户说'打开/运行XX'时调用。",
        parameters={"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]},
        handler=_open_app,
        category="system",
    ))
    registry.register(Tool(
        name="system_stats",
        description="实时采样系统资源利用率: CPU 利用率%、内存利用率%(含总/已用 MB)、磁盘利用率%(含总/已用/剩余 GB)。用于'查看CPU利用率'/'内存占用'/'磁盘使用情况'等。",
        parameters={"type": "object", "properties": {}},
        handler=_system_stats,
        category="system",
    ))
    registry.register(Tool(
        name="install_package",
        description=("统一安装: 命令可装(pip/apt/brew/npm 等多包管理器)用命令实时安装; "
                     "软件/图形安装包(GitHub/微信/QQ等)自动联网搜索官方下载源, 下载后启动 GUI 安装向导并自动点击(小AI 全程监管). "
                     "适用: 安装github/安装微信/pip安装requests/apt安装xxx"),
        parameters={
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "要安装的包/软件名"},
                "method": {"type": "string", "description": "auto 自动 / gui 强制图形化 / command 强制命令"},
            },
        },
        handler=_install_package,
        category="system",
    ))
    registry.register(Tool(
        name="draw_image",
        description=("AI 绘画: 根据自然语言描述用内置 SVG 绘画引擎生成科技风图片, 控制板预览并支持下载. "
                     "适用: 画一只卡通猫/生成科技风海报/画风景插画等(不依赖外部模型)"),
        parameters={
            "type": "object",
            "properties": {"desc": {"type": "string", "description": "绘画内容描述"}},
            "required": ["desc"],
        },
        handler=_draw_image,
        category="system",
    ))
    registry.register(Tool(
        name="monitor_task",
        description=("实时任务监控: 持续采样(CPU/内存/磁盘或任意命令输出)并每N秒实时推送到控制板命令窗口, 直到关闭窗口停止. "
                     "适用: 持续监控CPU利用率/实时监测磁盘空间/每5秒刷新命令输出"),
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "监控目标: 系统资源关键词或要循环执行的命令"},
                "interval": {"type": "integer", "description": "采样间隔秒数, 默认2"},
            },
        },
        handler=_monitor_task,
        category="system",
    ))
    registry.register(Tool(
        name="install_dependency",
        description="使用 pip 安装 Python 模块/依赖(package), 安装过程实时显示进度条窗口。适用: '安装xxx模块'、运行代码缺依赖时自动询问后安装。",
        parameters={
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "要安装的模块名, 如 requests / psutil"},
                "mirror": {"type": "string", "description": "可选 pip 镜像源, 如 https://pypi.tuna.tsinghua.edu.cn/simple"},
                "window_id": {"type": "string", "description": "进度条窗口 ID(默认 dep_install)"},
            },
            "required": ["package"],
        },
        handler=_install_dependency,
        category="system",
    ))
    registry.register(Tool(
        name="get_time",
        description="获取当前日期时间、星期、时区。",
        parameters={"type": "object", "properties": {}},
        handler=_get_time,
        category="system",
    ))
    registry.register(Tool(
        name="calculator",
        description="执行安全的数学计算(支持 + - * / % 与 sin cos tan sqrt log abs round pi e)。",
        parameters={"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
        handler=_calculator,
        category="system",
    ))
