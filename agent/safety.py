# -*- coding: utf-8 -*-
"""NEURA 安全层：命令白名单/黑名单、路径约束、沙箱执行、输出上限。"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


class SafetyError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# 危险命令模式(正则, 大小写不敏感)。命中即拒绝。
_DENY_PATTERNS = [
    r"^\s*rm\s+(-[a-z]*\s+)?(/|~|\.)\s",
    r"rm\s+-rf\s+[/~]",
    r"^\s*format\s+[a-z]:",
    r"^\s*fdisk",
    r"^\s*mkfs",
    r"^\s*dd\s+if=",
    r"^\s*shutdown",
    r"^\s*reboot",
    r"^\s*init\s+[06]",
    r"^\s*del\s+/[sqf]",
    r"^\s*rd\s+/[sq]",
    r"^\s*mv\s+.*\s/(dev|etc|usr|boot|proc|sys)",
    r"chmod\s+-R\s+777\s+/",
    r"chown\s+-R\s+\w+\s+/",
    r":\(\)\{\s*\|\s*&\s*\};?",
    r"mkfs\.\w+",
    r">\s*/dev/sd",
    r"curl\s+.*\|\s*(ba)?sh",
    r"wget\s+.*\|\s*(ba)?sh",
    r"^\s*sudo",
    r"^\s*su\s",
    r"^\s*kill\s+-9\s+\d+",
    r"^\s*taskkill\s+/\s*f",
]

_SHELL_META = ["|", ">", "<", "&", "`", "$(", "||", "&&", ";"]


def check_command(command: str, cfg: dict) -> str:
    """校验命令是否被允许。返回规范化后的命令, 违规抛 SafetyError。"""
    if not command or not command.strip():
        raise SafetyError("命令为空")
    cmd = command.strip()
    for pat in _DENY_PATTERNS:
        if re.search(pat, cmd, flags=re.I):
            raise SafetyError(f"命令被安全策略拒绝(命中危险模式): {pat}")
    # 禁止 shell 元字符(简单命令只允许一个程序 + 参数)
    for meta in _SHELL_META:
        if meta in cmd:
            raise SafetyError(f"命令包含不允许的 shell 元字符: {meta!r}，请直接给出单条命令")
    first_raw = cmd.split()[0]
    first = first_raw.lower()
    import os as _os
    # 绝对路径命令按 basename 判定(如 /opt/python3.12/bin/python3 -> python3), 兼容不同安装位置
    base = _os.path.basename(first_raw).lower()
    allowed = cfg.get("permissions", {}).get("allowed_commands", [])
    denied = cfg.get("permissions", {}).get("denied_commands", [])
    if first in denied or base in denied:
        raise SafetyError(f"命令被列入黑名单: {first_raw}")
    if first not in allowed and base not in allowed:
        raise SafetyError(f"命令 {first_raw} 不在白名单中，可联系管理员在 config.json permissions.allowed_commands 中添加")
    # Windows 下做大小写归一
    if os.name == "nt":
        for a in allowed:
            if a.lower() == first.lower() or a.lower() == base:
                cmd = a + cmd[len(first_raw):]
                break
    return cmd
    # Windows 下做大小写归一
    if os.name == "nt":
        first_win = cmd.split()[0]
        for a in allowed:
            if a.lower() == first.lower():
                cmd = a + cmd[len(first_win):]
                break
    return cmd


def check_admin(command: str, cfg: dict) -> bool:
    """判断命令是否属于【管理员级别】操作(敏感只读查询, 非高危)。

    管理员命令(permissions.admin_commands)会被放行, 但执行结果在前端
    以红色警示窗口展示——体现"AI 可调用任何合法操作, 敏感操作明示警示"。
    """
    if not command or not command.strip():
        return False
    first = command.strip().split()[0].lower()
    admins = [a.lower() for a in cfg.get("permissions", {}).get("admin_commands", [])]
    return first in admins


def resolve_path(raw: str, cfg: dict, cwd: str | None = None) -> str:
    """路径解析 + 黑名单检查 + 允许根约束。

    相对路径以【用户主目录】为基准(而非服务器进程目录), 这样
    "desktop"、"下载" 等纯名称会落在用户的桌面/下载目录, 不再误解析为根目录。
    """
    if not raw or raw.strip() == "":
        raw = str(Path.home())
    raw = raw.strip().strip('"').strip("'")
    raw = os.path.expanduser(raw)
    raw = os.path.expandvars(raw)
    if not os.path.isabs(raw):
        base = cwd or str(Path.home())
        raw = os.path.join(base, raw)
    real = os.path.realpath(raw)
    deny = cfg.get("permissions", {}).get("deny_paths", [])
    for d in deny:
        d = os.path.expanduser(d)
        if os.name == "nt":
            if real.lower().startswith(os.path.realpath(d).lower()):
                raise SafetyError(f"路径被安全策略拒绝: {d}")
        else:
            if real.startswith(os.path.realpath(d)):
                raise SafetyError(f"路径被安全策略拒绝: {d}")
    allow = cfg.get("permissions", {}).get("allow_paths", ["*"])
    if allow and allow != ["*"] and "*" not in allow:
        if not any(os.path.realpath(a) and real.startswith(os.path.realpath(os.path.expanduser(a))) for a in allow if a):
            raise SafetyError(f"路径不在允许范围内: {real}")
    return real


def sanitize_output(text: str, cap: int = 60000) -> str:
    text = str(text or "")
    if len(text) <= cap:
        return text
    head = text[: cap // 2]
    tail = text[- cap // 4:]
    return f"{head}\n...[输出过长已截断, 共{len(text)}字符]...\n{tail}"


def _limit_resources(mem_mb: int):
    try:
        import resource
        mem_bytes = max(int(mem_mb) * 1024 * 1024, 64 * 1024 * 1024)
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    except Exception:
        pass


def _build_sandbox_env(env: dict | None = None) -> dict:
    """构造沙箱环境变量。

    关键: 无论任何平台(含 Windows 控制台 GBK 代码页)都强制 Python 以 UTF-8 输出,
    否则脚本打印 Emoji/生僻字时 Python 按 GBK 编码会抛 UnicodeEncodeError。
    """
    base_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.path.expanduser("~"),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.name == "nt":
        base_env = dict(os.environ)
        # Windows 必须显式覆盖为 UTF-8, 否则 print(Emoji/生僻字) 会按 GBK 编码抛 UnicodeEncodeError
        base_env["PYTHONIOENCODING"] = "utf-8"
        base_env["PYTHONUTF8"] = "1"
        base_env["LANG"] = "C.UTF-8"
    if env:
        base_env.update(env)
    return base_env


def sandbox_run(command: str, cwd: str | None = None, timeout: int = 30,
                mem_mb: int = 512, use_firejail: bool = False,
                env: dict | None = None) -> dict:
    """在受限环境下执行命令。返回 {ok, code, stdout, stderr}。"""
    # 尽可能减少外部环境注入
    base_env = _build_sandbox_env(env)
    if use_firejail and shutil.which("firejail"):
        command = "firejail --net=none -- " + command
    pre = None
    if os.name != "nt":
        import functools
        pre = functools.partial(_limit_resources, mem_mb)
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd, env=base_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, preexec_fn=pre,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        return {"ok": proc.returncode == 0, "code": proc.returncode, "stdout": out, "stderr": err}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "stdout": "", "stderr": f"执行超时(>{timeout}s)，已终止"}
    except Exception as e:
        return {"ok": False, "code": -2, "stdout": "", "stderr": f"执行异常: {e}"}


def code_extension_allowed(path: str, cfg: dict) -> bool:
    ext = os.path.splitext(path)[1].lower()
    allowed = cfg.get("permissions", {}).get("code_execution", {}).get("allowed_extensions", [])
    return ext in allowed
