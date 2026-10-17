# -*- coding: utf-8 -*-
"""NEURA v3.7 安全中枢：操作风险分级 + 系统登录密码验证。

三级模型(满足"AI 可完全操控宿主机 + 高危拦截/解锁"):
  - malicious(恶意): 直接拦截, 不执行, 不询问(勒索/擦盘/提权/关闭安全软件/攻击性命令等)
  - high(高危): 必须验证【系统登录密码】, 密码正确才允许执行
  - none(常规): 直接执行

verify_system_password: 跨平台验证系统登录密码——
  - Windows: advapi32.LogonUserW(交互登录)
  - Linux/macOS: libpam(pam_start + pam_authenticate, 通过 ctypes 无第三方依赖)
  - 无系统认证环境(无头 VM 等): 回退 config.security.fallback_password_hash(sha256)
"""
import hashlib
import os
import re

# ---------------------------------------------------------------------------
# 风险分级
# ---------------------------------------------------------------------------

# 直接拦截的恶意操作特征(工具级)
_MALICIOUS_TOOL_ARGS = [
    # 擦盘/格式化
    (r"(?:rm|del|unlink).{0,30}(?:/|\\\\|C:).{0,10}$", "删除系统根/盘符级路径"),
    (r"rm\s+-rf\s+(?:/|~|\.\s*$)", "递归强制删除根目录"),
    (r"format\s+[a-z]:", "格式化磁盘"),
    (r"fdisk\b|mkfs\b|dd\s+if=.*of=/dev/sd", "磁盘分区/格式化/写入设备"),
    # 系统与安全
    (r"(?:关闭|禁用|停止|卸载).{0,8}(?:杀毒|安全|防火墙|defender|防毒|antivirus)", "关闭/禁用安全防护"),
    (r"(?:关闭|禁用).{0,8}(?:windows\s*update|自动更新|安全中心)", "禁用系统安全更新"),
    (r"(?:改|设置|重置).{0,6}(?:系统|登录|开机).{0,4}密码|passwd\b", "篡改系统登录密码"),
    (r"chmod\s+-R\s+777\s*/|chown\s+-R\s+\w+\s*/", "修改全盘权限"),
    (r"mv\s+.*\s+/(?:dev|etc|usr|boot|proc|sys)", "移动系统关键目录"),
    (r"hosts.{0,10}(?:重定向|指向|改)", "篡改 hosts 域名劫持"),
    (r"taskkill.{0,20}/(?:IM|PID)\s+(?:system|lsass|winlogon|explorer|services|svchost)", "杀死系统关键进程"),
    (r"kill\s+-9\s+(?:1|-1)", "强制结束系统根进程"),
    (r"kill\s+-9\s+\d+\s+(?:-f\s+)?(?:systemd|init|dockerd)", "强制结束系统守护进程"),
    # 提权/后门
    (r"(?:sudo|su\b).{0,20}(?:adduser|useradd|usermod|passwd|chmod\s+-R|chown\s+-R)", "提权修改系统账号/权限"),
    (r"(?:sudo|su\b)\s+[^ ]+\s+(?:adduser|useradd|usermod|passwd|chmod\s+-R|chown\s+-R|rm\s+-rf\s+/)", "提权执行高危修改"),
    (r"at\s+\d+|schtasks.{0,10}/create.{0,20}admin", "创建计划任务后门"),
    (r"(?:reg\s+add|sc\s+config).{0,40}(?:run|start|binpath)", "修改系统服务/启动项"),
    (r"certutil.{0,30}(?:decode|download)", "可疑证书工具下载"),
    (r"(?:shutdown|reboot|init\s+[06]|poweroff|halt)", "关闭/重启系统"),
    # 攻击/勒索/挖矿
    (r"加密.{0,10}(?:文件|目录|磁盘)|勒索", "勒索加密行为"),
    (r"挖矿|miner|xmrig", "挖矿程序"),
    (r"(?:端口|内网|扫描|探测).{0,10}(?:攻击|入侵|爆破)", "攻击性扫描"),
    (r"(?:删除|清空).{0,6}(?:所有用户|全部文件)", "破坏性批量清除(所有用户/全部文件)"),
    (r"^(?:system|lsass|winlogon|csrss|smss|wininit|services|svchost|explorer|dock|finder|launchd|systemd|init)$", "系统关键进程"),
    (r"^(?:/|C:|C:\\|~|~[/\\]?)$", "删除根路径/盘符级目录"),
    # v3.24 加强检测: 数据窃取/外泄/勒索/入侵
    (r"(?:窃取|偷|打包).{0,10}(?:密码|账号|凭据|credential|token|私钥|密钥|浏览器数据)", "窃取凭据/密钥"),
    (r"(?:上传|发送|外传|外泄|post|put).{0,20}(?:密码|账号|凭据|token|私钥|\bdata\b|文件)", "数据外泄"),
    (r"keylog|键盘记录|剪贴板.{0,4}(?:窃取|读取)", "键盘记录/剪贴板窃取"),
    (r"(?:勒索|ransom|encrypt.{0,20}(?:文件|盘|卷)|给.{0,4}比特币)", "勒索加密"),
    (r"mimikatz|sam\b|hashdump|dump.{0,6}hash", "凭据转储攻击"),
    (r"(?:提权|横向|渗透|漏洞利用|exploit|payload\b|反向shell|反弹shell|nc\s+-e)", "入侵渗透行为"),
    (r"(?:爆破|暴力破解|brute)", "口令爆破"),
    (r"(?:伪造|钓鱼|仿冒).{0,10}(?:网站|登录|邮件|短信|二维码)", "钓鱼仿冒生成"),
    (r"(?:清除|删除).{0,6}(?:安全|系统|审计).{0,4}(?:日志|痕迹)", "清除安全/系统审计日志"),
    (r"(?:清除|删除).{0,8}(?:取证|取证痕迹)", "清除取证痕迹"),
]
# v3.32 破坏性(但可授权)模式: 用户明确授权时从"恶意拦截"降级为"高危需密码确认"。
# 仅涵盖盘符级/批量破坏(格式化/清空/删除全部/擦盘); 攻击性(关闭安全防护/绕过验证/
# 窃取凭据/后门/提权/爆破/勒索/钓鱼/清除痕迹)一律不降级, 即使"授权"也直接拦截。
_DESTRUCTIVE_TOOL_PATTERNS = [
    (r"(?:rm|del|unlink).{0,30}(?:/|\\|C:).{0,10}$", "删除系统根/盘符级路径"),
    (r"rm\s+-rf\s+(?:/|~|\.\s*$)", "递归强制删除根目录"),
    (r"format\s+[a-z]:", "格式化磁盘"),
    (r"fdisk\b|mkfs\b|dd\s+if=.*of=/dev/sd", "磁盘分区/格式化/写入设备"),
    (r"(?:删除|清空).{0,6}(?:回收站|浏览器记录|所有用户|全部文件)", "破坏性批量清除"),
    (r"^(?:/|C:|C:\\|~|~[/\\]?)$", "删除根路径/盘符级目录"),
]
_DESTRUCTIVE_LLM_PATTERNS = [
    (r"格式化\s*(?:磁盘|硬盘|U盘|U盘|分区|[CDE]\s*盘|系统盘)", "格式化磁盘"),
    (r"(?:删除|清除|移除|抹掉)\s*(?:整个|所有|全部|全部内容|[CDE]\s*盘|磁盘|硬盘|系统盘).{0,10}(?:内容|文件|数据|系统|分区|全部)", "删除盘符/系统级全部内容"),
    (r"清空\s*(?:[CDE]\s*盘|磁盘|硬盘|所有盘|整盘)", "清空磁盘"),
]

# 高危(需密码解锁)的操作特征
_HIGH_TOOL_ARGS = [
    (r"(?:删除|移除|删掉|清除|卸掉|卸载).{0,20}(?:文件|文件夹|目录|程序|软件|应用)", "删除文件/文件夹/卸载软件"),
    (r"(?:格式化|擦除|清空).{0,10}(?:磁盘|U盘|硬盘|分区)", "格式化磁盘分区"),
    (r"kill_process", "结束进程(影响正在运行的程序)"),
    (r"taskkill\b|pkill\b", "结束进程"),
    (r"(?:结束|终止|关闭|杀死).{0,10}(?:进程|任务)", "结束进程/任务"),
    (r"(?:改|重命名|move|mv).{0,12}(?:系统|启动|服务)", "修改系统相关项"),
    (r"(?:停用|禁用|关闭).{0,10}(?:服务|进程|程序)", "停用服务/程序"),
    (r"rm\b|del\b|unlink\b", "删除操作"),
    (r"rd\s+/|rmdir", "删除目录"),
    (r"(?:清理|清空).{0,8}(?:磁盘|空间|垃圾|缓存)", "清理磁盘(批量删除)"),
    (r"(?:清空|删除).{0,4}(?:回收站)", "清空回收站(批量删除)"),
    (r"(?:删除|清除).{0,6}(?:浏览器记录|历史记录|cookie)", "删除浏览器记录/历史"),
    (r"uninstall|卸载", "卸载软件"),
    (r"(?:替换|覆盖|修改).{0,10}(?:系统|注册表|启动项|服务)", "修改系统配置"),
]

# ---- v3.36 图形化操控(gui_control)专属审计 ----
# 关闭受保护系统窗口 -> 直接拦截(恶意); 图形化键入/热键含危险内容 -> 高危需密码确认
_GUI_CLOSE_PROTECTED = [
    (re.compile(r"(?:桌面|desktop|explorer|资源管理器|任务栏|Shell_TrayWnd|开始菜单|StartMenu|控制面板|Control Panel)"), "图形化关闭受保护的系统界面窗口"),
]
_GUI_INPUT_HIGH = [
    (re.compile(r"(?:rm\s+-rf|format\s+[a-z]:|fdisk|mkfs|del\s+C:|rd\s+/s)", re.I), "图形化键入破坏性命令"),
    (re.compile(r"(?:勒索|ransom|keylog|挖矿|miner|mimikatz|hashdump)", re.I), "图形化键入攻击性指令"),
    (re.compile(r"(?:钓鱼|伪造).{0,6}(?:登录|网站|短信)", re.I), "图形化键入钓鱼内容"),
    (re.compile(r"(?:清除|删除).{0,6}(?:安全|审计).{0,4}(?:日志|痕迹)", re.I), "图形化清除安全审计痕迹"),
]

# 高危/恶意操作目标(工具名级别)
_HIGH_TOOLS = {"kill_process", "delete_file", "delete_folder", "package_download"}


class RiskResult:
    __slots__ = ("level", "reason", "description")

    def __init__(self, level: str = "none", reason: str = "", description: str = ""):
        self.level = level            # malicious | high | none
        self.reason = reason          # 命中规则说明
        self.description = description  # 展示给用户的操作描述


def classify_operation(tool: str, args: dict, user_auth: bool = False) -> RiskResult:
    """对一次工具调用做风险分级。

    v3.32: user_auth=True(用户最近消息明确提及该目标)时, 仅【破坏性】(格式化/清空/
    盘符级删除)从"恶意拦截"降级为"高危需系统登录密码确认"; 攻击性(关闭安全防护/绕过
    验证/窃取凭据/后门/提权/爆破/勒索/钓鱼/清除痕迹)即使"授权"也直接拦截。

    Returns RiskResult(level in {malicious, high, none})。
    """
    args_txt = " ".join(str(v) for v in (args or {}).values())
    full = args_txt

    # 0) v3.36 图形化操控(gui_control)专属审计: 小AI 同样监管图形操作
    if tool == "gui_control":
        act = str((args or {}).get("action", "")).lower()
        win = str((args or {}).get("window", "") or (args or {}).get("title", "") or "")
        if act in ("close", "close_all_windows") and win:
            for gpat, gwhy in _GUI_CLOSE_PROTECTED:
                if gpat.search(win):
                    return RiskResult("malicious", gwhy, args_txt[:120])
        if act in ("type", "press", "hotkey"):
            inp = str((args or {}).get("text", "") or (args or {}).get("keys", "") or "")
            for gpat, gwhy in _GUI_INPUT_HIGH:
                if gpat.search(inp):
                    if user_auth and gpat in _GUI_INPUT_HIGH:
                        pass
                    return RiskResult("high", gwhy, args_txt[:120])
        if act in ("type", "press", "hotkey"):
            inp2 = str((args or {}).get("text", "") or (args or {}).get("keys", "") or "")
            # 键入到任意窗口的破坏性命令(用户授权时降为高危密码确认)
            for pat, why in _MALICIOUS_TOOL_ARGS:
                if re.search(pat, inp2, flags=re.I):
                    return RiskResult("high", f"图形化键入命中风险特征: {why}", args_txt[:120])

    # 1) 恶意: 直接拦截(破坏性且用户授权 -> 降级为高危密码确认; 攻击性恒拦截)
    for pat, why in _MALICIOUS_TOOL_ARGS:
        if re.search(pat, full, flags=re.I):
            if user_auth:
                for dpat, _dwhy in _DESTRUCTIVE_TOOL_PATTERNS:
                    if re.search(dpat, full, flags=re.I):
                        return RiskResult("high", f"命中破坏性特征(用户已授权, 需密码确认): {why}",
                                          args_txt[:120])
            return RiskResult("malicious", f"命中恶意特征: {why}", args_txt[:120])
    # 2) 高危: 需系统登录密码解锁
    for pat, why in _HIGH_TOOL_ARGS:
        if re.search(pat, full, flags=re.I):
            return RiskResult("high", f"命中高危特征: {why}", args_txt[:120])
    if tool in _HIGH_TOOLS:
        return RiskResult("high", f"调用集 {tool} 属高危操作", args_txt[:120])

    return RiskResult("none")


def operation_id(session_id: str, tool: str, args: dict) -> str:
    """同一会话内, 相同工具+参数的操作指纹(作为解锁凭据 ID)。"""
    import hashlib
    raw = f"{session_id}|{tool}|{sorted((str(k), str(v)) for k, v in (args or {}).items())}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# 系统登录密码验证
# ---------------------------------------------------------------------------

def _verify_pam(username: str, password: str) -> bool:
    """Linux/macOS: 通过 libpam 验证系统登录密码。

    在【子进程】中执行(ctypes 调用 PAM 存在回调/内存释放风险), 子进程即使
    异常退出也只影响本次验证, 主服务进程不受任何影响。
    """
    import subprocess
    child = r"""
import sys, ctypes, ctypes.util
lib = ctypes.util.find_library("pam")
if not lib:
    sys.exit(2)
libpam = ctypes.CDLL(lib)
PAM_SUCCESS = 0
PAM_PROMPT_ECHO_OFF = 1

class PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]
class PamResponse(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]
CONV_FUNC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(PamMessage)),
    ctypes.POINTER(ctypes.POINTER(PamResponse)), ctypes.c_void_p)

@CONV_FUNC
def conv(num_msg, msgs, resp, appdata_ptr):
    libc = ctypes.CDLL(None)
    libc.malloc.restype = ctypes.c_void_p
    pw = __PW__.encode("utf-8")
    buf = libc.malloc(len(pw) + 1)
    if buf:
        ctypes.memmove(buf, pw, len(pw) + 1)
    r = PamResponse(ctypes.cast(buf, ctypes.c_char_p), 0)
    resp[0] = ctypes.pointer(r)
    return 0

class PamConv(ctypes.Structure):
    _fields_ = [("conv", CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]

__PW__ = sys.argv[2]
conv_p = PamConv(conv, None)
pamh = ctypes.c_void_p()
if libpam.pam_start(b"neura-agent", sys.argv[1].encode("utf-8"),
                    ctypes.byref(conv_p), ctypes.byref(pamh)) != PAM_SUCCESS:
    sys.exit(3)
rc = libpam.pam_authenticate(pamh, 0)
try:
    libpam.pam_end(pamh, 0)
except Exception:
    pass
sys.exit(0 if rc == PAM_SUCCESS else 1)
"""
    try:
        r = subprocess.run([sys.executable, "-c", child, username, password],
                           capture_output=True, timeout=8)
        return r.returncode == 0
    except Exception:
        return False


def _verify_windows_logon(username: str, password: str) -> bool:
    """Windows: advapi32.LogonUserW(交互登录) 验证系统登录密码。"""
    import ctypes
    from ctypes import wintypes

    LOGON32_LOGON_INTERACTIVE = 2
    LOGON32_PROVIDER_DEFAULT = 0
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32

    token = wintypes.HANDLE()
    ok = advapi32.LogonUserW(
        username,
        None,
        password,
        LOGON32_LOGON_INTERACTIVE,
        LOGON32_PROVIDER_DEFAULT,
        ctypes.byref(token))
    if ok:
        kernel32.CloseHandle(token)
    return bool(ok)


def verify_system_password(password: str, cfg: dict) -> tuple[bool, str]:
    """验证系统登录密码。返回 (是否通过, 验证方式说明)。

    验证顺序:
      1) 系统认证(LogonUserW / libpam) —— 真实系统登录密码
      2) 无系统认证环境 -> 回退 config.security.fallback_password_hash(sha256)
    全部失败/未配置 -> 拒绝(安全默认: 宁可不执行, 不冒风险)。
    """
    if not password:
        return False, "密码为空"
    import getpass
    username = getpass.getuser()
    sec = cfg.get("security", {}) or {}
    mode = sec.get("password_verify", "system_first")

    if mode != "fallback_only":
        try:
            if os.name == "nt":
                if _verify_windows_logon(username, password):
                    return True, "已通过 Windows 系统登录验证"
            else:
                if _verify_pam(username, password):
                    return True, "已通过系统 PAM 登录验证"
        except Exception:
            pass

    if mode != "system_only":
        fp_hash = (sec.get("fallback_password_hash") or "").strip().lower()
        if fp_hash:
            got = hashlib.sha256(password.encode("utf-8")).hexdigest()
            if got == fp_hash:
                return True, "已通过配置的安全密码验证"
            return False, "密码不正确"

    return False, "系统密码验证不可用(未配置安全密码), 已拒绝该高危操作"


# ---------------------------------------------------------------------------
# 会话级解锁状态(由 core 维护, 此处提供工具函数)
# ---------------------------------------------------------------------------

def make_unlock_token(operation_id: str, password: str) -> str:
    """生成一次性解锁凭据(哈希), 前端/日志不出现明文密码。"""
    raw = f"{operation_id}|{password}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# v3.25 小AI 实时监管: Agent 自修复补丁审查(只允许修复性修改, 拦截恶意修改)
# ---------------------------------------------------------------------------

# 受保护文件: 安全核心/配置边界, 禁止 AI 直接修改(避免恶意弱化防护)
_AGENT_PATCH_PROTECTED = {
    "agent/security.py",   # 恶意/高危特征与分级是安全底线
    "config.json",         # 权限白名单/denied/开关是边界(配置修改走安全通道)
    "agent/mini_ai.py",    # 监管/拦截/辨别核心
}

# 恶意补丁特征(命中即拦截)
_AGENT_PATCH_MALICIOUS = [
    r"关闭.{0,6}(?:安全|防护|杀毒|防火墙|defender|antivirus|检测)",
    r"禁用.{0,6}(?:安全|防护|杀毒|防火墙|defender|antivirus|拦截|监管)",
    r"(?:绕过|跳过|关闭).{0,8}(?:白名单|权限|安全审查|监管|检测|review)",
    r"后门|backdoor|webshell",
    r"挖矿|xmrig|miner\b",
    r"勒索|ransom",
    r"窃取|steal|exfiltrat",
    r"keylog|键盘记录",
    r"mimikatz|dump\s*credential",
    r"(?:清除|删除).{0,6}(?:日志|审计|痕迹|取证)",
    r"提权|privilege\s*escalat",
    r"绕过管理员|无需密码|跳过.{0,4}密码",
    r"弱化|取消.{0,6}(?:拦截|审查|监管|安全)",
]

# 补丁新增代码中的危险调用(出现在 Agent 核心源码补丁中)
_AGENT_PATCH_DANGEROUS_CALLS = [
    r"subprocess\s*\.\s*(?:Popen|call|run|check_output)",
    r"os\.system\s*\(",
    r"os\.popen\s*\(",
    r"\beval\s*\(", r"\bexec\s*\(", r"__import__\s*\(",
    r"socket\.\s*socket\s*\(",
    r"base64\.b64decode\s*\([^)]*exec",
]


def review_agent_patch(file: str, old: str = "", new: str = "", reason: str = "") -> tuple:
    """小AI 实时监管: 审查 AI 提交的 Agent 修复补丁。

    返回 (ok: bool, message: str)。ok=False 表示拦截(恶意/越权修改), ok=True 表示允许(修复性修改)。
    审查维度: ①受保护文件 ②恶意意图特征 ③补丁新增危险调用 ④修复原因合理性。
    """
    rel = (file or "").replace("\\", "/").strip().lstrip("./").lstrip("/")
    reason_txt = (reason or "")[:500]
    new_txt = new or ""
    # ① 受保护文件: 安全核心禁止直接修改
    if rel in _AGENT_PATCH_PROTECTED:
        return (False, f"受保护文件 {rel} 禁止直接修改(安全核心/权限边界)。如需调整请通过安全配置通道。")
    if rel.startswith("system/") or rel.startswith("workspace/"):
        return (False, "system/(记忆/自定义调用集)与 workspace/(沙箱) 不允许通过 agent_repair 修改")
    # ② 恶意意图特征(补丁内容 + 修复原因)
    blob = new_txt + "\n" + reason_txt
    for pat in _AGENT_PATCH_MALICIOUS:
        m = re.search(pat, blob, flags=re.IGNORECASE)
        if m:
            return (False, f"检测到恶意修改意图: 「{m.group(0)}」(只允许修复性修改, 恶意修改已被小AI 拦截)")
    # ③ 补丁新增危险调用(对比新增行: 出现在 new 中而未出现在 old 中)
    old_lines = set((old or "").splitlines())
    added = [ln for ln in new_txt.splitlines() if ln not in old_lines]
    added_txt = "\n".join(added)
    for pat in _AGENT_PATCH_DANGEROUS_CALLS:
        if re.search(pat, added_txt):
            return (False, f"补丁新增代码引入危险调用: 「{re.search(pat, added_txt).group(0)}」, 已拦截")
    # ④ 空补丁防护
    if not old.strip() and not new.strip() and not reason_txt:
        return (False, "空补丁(未提供 old/new/reason)")
    # ⑤ 大规模删除启发式(删除超过旧片段 60% 且旧片段含函数/类定义 -> 提示弱化风险)
    if old.strip() and new.strip():
        ratio = len(new) / max(len(old), 1)
        if ratio < 0.4 and re.search(r"\bdef\s+\w+|class\s+\w+", old):
            return (False, f"疑似能力弱化: 新代码仅为旧代码 {int(ratio * 100)}%, 且旧片段包含函数/类定义。修复性修改不应大幅删除功能。")
    return (True, "修复性修改通过小AI 监管审查(仅限修复 Agent 自身问题, 不涉及安全边界)")


# 中文恶意意图补充(直接操控意图级): 盘符级破坏/清盘/提权/入侵
_LLM_MALICIOUS_CN = [
    (r"格式化\s*(?:磁盘|硬盘|U盘|U盘|分区|[CDE]\s*盘|系统盘)", "格式化磁盘"),
    (r"(?:删除|清除|移除|抹掉)\s*(?:整个|所有|全部|全部内容|[CDE]\s*盘|磁盘|硬盘|系统盘).{0,10}(?:内容|文件|数据|系统|分区|全部)", "删除盘符/系统级全部内容"),
    (r"清空\s*(?:[CDE]\s*盘|磁盘|硬盘|所有盘|整盘)", "清空磁盘"),
    (r"(?:关闭|禁用|停止|卸载).{0,6}(?:杀毒|安全|防火墙|防毒|defender|antivirus|安全中心)", "关闭/禁用安全防护"),
    (r"(?:绕过|跳过|关闭).{0,8}(?:密码|白名单|安全|验证|监管|拦截)", "绕过安全验证"),
    (r"(?:窃取|偷|打包).{0,8}(?:密码|账号|凭据|密钥|token|浏览器数据)", "窃取凭据/数据"),
    (r"注入.{0,6}(?:后门|木马|病毒)", "注入后门/木马"),
    (r"(?:爆破|暴力破解)", "口令爆破"),
    (r"(?:提权|反弹shell|反向shell|nc\s+-e)", "提权/渗透"),
    (r"(?:勒索|ransom|加密.{0,6}(?:文件|盘|卷)|给.{0,4}比特币)", "勒索加密"),
    (r"(?:钓鱼|仿冒|伪造).{0,8}(?:网站|登录页|邮件|短信|二维码)", "钓鱼仿冒"),
    (r"挖矿|miner|xmrig", "挖矿程序"),
    (r"键盘记录|keylog|剪贴板.{0,4}(?:窃取|读取)", "键盘记录/剪贴板窃取"),
    (r"(?:上传|外传|外泄|发送).{0,12}(?:密码|账号|凭据|token|私钥|浏览器数据)", "数据外泄"),
    (r"篡改.{0,6}(?:hosts|域名解析)|hosts.{0,10}(?:劫持|重定向)", "篡改 hosts 域名劫持"),
    (r"(?:添加|创建).{0,8}(?:自启动|开机启动|计划任务).{0,6}(?:后门|恶意)", "自启动后门"),
    (r"(?:清除|删除).{0,6}(?:安全|系统|审计).{0,4}(?:日志|痕迹)", "清除安全日志"),
    (r"mimikatz|hashdump|dump.{0,6}hash", "凭据转储攻击"),
    (r"(?:横向|渗透|漏洞利用|exploit|payload)", "入侵渗透行为"),
]


def classify_llm_action(desc: str, user_auth: bool = False) -> RiskResult:
    """v3.26 小AI 加强监管: 对 AI【直接操控】的操作意图文本做恶意/高危检测。

    复用同一套恶意/高危特征表, 但作用于操作描述文本(不只工具名+参数)——
    AI 直接操控时, 意图表述本身也过审; 命中即拦截/需密码。
    v3.32: user_auth=True 且仅【破坏性】(格式化/清空/盘符级删除)降级为高危密码确认;
    攻击性(关闭安全防护/绕过验证/窃取凭据/后门/提权/爆破)恒拦截。
    """
    txt = str(desc or "")
    if not txt.strip():
        return RiskResult("none", "", "")
    # 中文恶意意图补充(优先于高危, 盘符级破坏/清盘直接拦截)
    for pat, reason in _LLM_MALICIOUS_CN:
        m = re.search(pat, txt, flags=re.IGNORECASE)
        if m:
            if user_auth:
                for dpat, _dwhy in _DESTRUCTIVE_LLM_PATTERNS:
                    if re.search(dpat, txt, flags=re.IGNORECASE):
                        return RiskResult("high", f"命中破坏性意图(用户已授权, 需密码确认): {reason}",
                                          f"直接操控意图命中破坏性特征: {m.group(0)[:60]}")
            return RiskResult("malicious", reason, f"直接操控意图命中恶意特征: {m.group(0)[:60]}")
    for pat, reason in _MALICIOUS_TOOL_ARGS:
        m = re.search(pat, txt, flags=re.IGNORECASE)
        if m:
            if user_auth:
                for dpat, _dwhy in _DESTRUCTIVE_TOOL_PATTERNS:
                    if re.search(dpat, txt, flags=re.IGNORECASE):
                        return RiskResult("high", f"命中破坏性特征(用户已授权, 需密码确认): {reason}",
                                          f"直接操控意图命中破坏性特征: {m.group(0)[:60]}")
            return RiskResult("malicious", reason, f"直接操控意图命中恶意特征: {m.group(0)[:60]}")
    for pat, reason in _HIGH_TOOL_ARGS:
        m = re.search(pat, txt, flags=re.IGNORECASE)
        if m:
            return RiskResult("high", reason, f"直接操控意图命中高危特征: {m.group(0)[:60]}")
    return RiskResult("none", "", "")
