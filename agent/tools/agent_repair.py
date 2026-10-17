# -*- coding: utf-8 -*-
"""v3.25 Agent 自修复调用集: AI 检测到 Agent 报错后可直接修改 Agent 代码。

- read_agent_file : 读取 Agent 源码文件(控制板弹窗展示), 供 AI 定位问题。
- agent_repair    : AI 提交修复补丁 -> 小AI 实时监管审查(只允许修复性修改, 拦截恶意修改)
                    -> 备份/应用/语法验证/热重载 -> 重试原操作。

安全边界(小AI 实时监管, 拒绝简化/拒绝模拟):
1. 修改范围: 仅项目根下 agent/ server/ web/ 的源码文件; 禁止 system/(记忆/自定义调用集持久化)
   与 workspace/(沙箱) 与 server/downloads/。
2. 受保护文件: agent/security.py(检测特征/分类是安全底线) 与 config.json 禁止直接修改;
   agent/mini_ai.py(监管/拦截核心) 禁止直接修改。
3. 恶意补丁特征: 关闭安全防护/禁用杀毒/注入后门/绕过权限/窃取凭据/勒索/挖矿/清除痕迹等 -> 拦截。
4. 补丁新增代码不得引入危险调用: subprocess/os.system/eval/exec/socket/__import__ 等。
5. 应用前备份到 system/backups/agent_patches/, 语法验证失败自动回滚, 应用后热重载立即生效。
"""
import os
import re
import shutil
import importlib
import sys
import datetime

from .registry import Tool, ToolResult
from ..security import review_agent_patch

# 允许修复的文件根(项目相对路径前缀)
_ALLOW_ROOTS = ("agent/", "server/", "web/", "run.py", "setup_model.py")
# 禁止写入的目录/文件
_FORBIDDEN_PATHS = ("system/", "workspace/", "server/downloads/", "tests/", ".skills/")
_FORBIDDEN_EXTS = (".zip", ".bin", ".npz", ".db", ".log")


def _norm(rel: str) -> str:
    """规范化项目相对路径。"""
    rel = (rel or "").replace("\\", "/").strip().lstrip("./")
    rel = re.sub(r"^/+", "", rel)
    return rel


def _resolve_in_root(rel: str, root: str) -> str | None:
    """解析为项目根内绝对路径; 越界/非法返回 None。"""
    rel = _norm(rel)
    if not rel or rel in (".", ".."):
        return None
    if any(rel.startswith(p) for p in _FORBIDDEN_PATHS):
        return None
    if rel == "config.json":
        # config.json 受保护: 只读, 不可写
        return os.path.join(root, "config.json") if os.path.exists(os.path.join(root, "config.json")) else None
    if not any(rel.startswith(p) for p in _ALLOW_ROOTS):
        return None
    if rel.endswith(_FORBIDDEN_EXTS):
        return None
    full = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if not full.startswith(root_abs + os.sep):
        return None
    return full


def _is_config(rel: str) -> bool:
    return _norm(rel) == "config.json"


async def _read_agent_file(args: dict, ctx) -> ToolResult:
    """读取 Agent 源码文件(供 AI 定位修复点)。"""
    raw = str(args.get("file") or "")
    root = ctx.agent.root if getattr(ctx, "agent", None) is not None else os.getcwd()
    full = _resolve_in_root(raw, root)
    if not full:
        return ToolResult(ok=False, error=f"路径越界/非法(仅允许 agent/ server/ web/ 下源码, 禁止 system/workspace): {raw}",
                          error_type="forbidden")
    if not os.path.isfile(full):
        return ToolResult(ok=False, error=f"文件不存在: {raw}", error_type="not_found")
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return ToolResult(ok=False, error=f"读取失败: {e}", error_type="exec")
    if len(text) > 400000:
        text = text[:400000] + "\n... (已截断)"
    return ToolResult(data={
        "file": _norm(raw), "path": full,
        "language": os.path.splitext(full)[1].lstrip("."),
        "content": text, "note": "Agent 源码(只读): 定位问题后可调用 agent_repair 提交修复补丁",
    })


async def _agent_repair(args: dict, ctx) -> ToolResult:
    """AI 修改 Agent: 提交补丁 -> 小AI 监管审查 -> 备份/应用/语法验证/热重载。"""
    raw = str(args.get("file") or "")
    old = str(args.get("old") or "")
    new = str(args.get("new") or "")
    reason = str(args.get("reason") or "")
    auto = bool(args.get("auto") or False)
    root = ctx.agent.root if getattr(ctx, "agent", None) is not None else os.getcwd()
    rel = _norm(raw)
    full = _resolve_in_root(rel, root)
    if not full:
        return ToolResult(ok=False, error=f"路径越界/非法(仅允许 agent/ server/ web/ 下源码): {raw}",
                          error_type="forbidden")
    if _is_config(rel):
        return ToolResult(ok=False, error="config.json 受保护(权限/开关是安全边界), 禁止直接修改; 请通过安全配置通道调整",
                          error_type="forbidden")
    if not os.path.isfile(full):
        return ToolResult(ok=False, error=f"文件不存在: {raw}", error_type="not_found")

    # ---- 小AI 实时监管: 只允许修复性修改, 拦截恶意修改 ----
    ok_review, msg = review_agent_patch(rel, old, new, reason)
    if not ok_review:
        return ToolResult(ok=False, error=f"小AI 监管拦截: {msg}", error_type="forbidden")

    # ---- auto 模式: 规则引擎有限自动修复(仅可判定场景) ----
    if auto or (not old and not new):
        return await _auto_repair(rel, full, reason, ctx)

    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return ToolResult(ok=False, error=f"读取失败: {e}", error_type="exec")

    # 定位旧片段(必须唯一匹配)
    cnt = text.count(old)
    if cnt == 0:
        return ToolResult(ok=False, error=f"未找到待替换代码片段(请用 read_agent_file 核对原文, 保持完全一致)", error_type="invalid_args")
    if cnt > 1:
        return ToolResult(ok=False, error=f"待替换代码片段出现 {cnt} 次, 不唯一; 请扩大上下文使其唯一", error_type="invalid_args")

    # ---- 备份 ----
    backup_dir = os.path.join(root, "system", "backups", "agent_patches")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(backup_dir, f"{rel.replace('/', '__')}.{stamp}.bak")
    try:
        shutil.copy2(full, bak)
    except Exception:
        bak = ""

    # ---- 应用 ----
    new_text = text.replace(old, new, 1)
    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(new_text)
    except Exception as e:
        return ToolResult(ok=False, error=f"写入失败: {e}", error_type="exec")

    # ---- 语法验证(.py): 失败自动回滚 ----
    if full.endswith(".py"):
        try:
            compile(new_text, full, "exec")
        except SyntaxError as e:
            if bak:
                shutil.copy2(bak, full)
            return ToolResult(ok=False,
                              error=f"语法验证失败已自动回滚: 第 {e.lineno} 行 {e.msg}",
                              error_type="syntax")
    elif full.endswith(".js"):
        # 朴素括号/引号平衡检查
        for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
            if new_text.count(a) != new_text.count(b):
                if bak:
                    shutil.copy2(bak, full)
                return ToolResult(ok=False, error=f"JS 括号平衡检查失败已自动回滚({a}{b} 不配对)", error_type="syntax")

    # ---- 热重载: 让修复立即生效 ----
    reloaded = await _hot_reload(full, rel, ctx)

    if ctx and ctx.emitter:
        await ctx.emitter.panel("agent_repair_flow", "process", "Agent 自修复 · 小AI 监管",
                                [{"time": datetime.datetime.now().strftime("%H:%M:%S"),
                                  "text": f"修复文件: {rel} (小AI 监管审查通过)", "level": "ok"},
                                 {"time": "", "text": f"原因: {reason[:200]}", "level": "run"},
                                 {"time": "", "text": f"热重载: {'已生效(新代码立即可用)' if reloaded else '已写入(部分模块需重启后完全生效)'}", "level": "run"}])
    return ToolResult(data={
        "file": rel, "reason": reason[:300],
        "applied": True, "reloaded": reloaded, "backup": bak,
        "summary": f"已修复 {rel}(备份: {os.path.basename(bak) if bak else '无'}), 热重载{'已生效' if reloaded else '需重启生效'}",
    })


async def _auto_repair(rel: str, full: str, reason: str, ctx) -> ToolResult:
    """auto 模式: 仅处理可判定的常见修复; 否则返回 need_ai 提示由 AI 提供补丁。"""
    err = reason or ""
    m = re.search(r"No module named ['\"]([^'\"]+)['\"]", err)
    if m:
        mod = m.group(1)
        if mod.startswith("agent.") or mod in ("agent", "server", "web"):
            return ToolResult(ok=False, error=f"内部模块 {mod} 导入失败(疑似 Agent 代码缺陷), 请提供具体修复补丁(file/old/new)",
                              error_type="need_ai")
        return ToolResult(ok=False, error=f"缺少第三方依赖 {mod}: 请安装依赖后重试(或提供修复补丁)",
                          error_type="need_ai")
    m = re.search(r"NameError: name '(\w+)' is not defined", err)
    if m and full.endswith(".py"):
        return ToolResult(ok=False, error=f"疑似未定义名称 {m.group(1)}: 请提供修复补丁(补充定义/导入, 保持 Agent 语义不变)",
                          error_type="need_ai")
    return ToolResult(ok=False, error="规则引擎无法确定修复点: 请由 AI 基于 read_agent_file 提供补丁(file/old/new, 小AI 会监管审查)",
                      error_type="need_ai")


async def _hot_reload(full: str, rel: str, ctx) -> bool:
    """热重载: reload 已加载模块 + 重建工具注册表(让修复立即生效)。"""
    try:
        if full.endswith(".py"):
            # 计算模块路径: agent/tools/x.py -> agent.tools.x
            modpath = rel[:-3].replace("/", ".")
            if modpath in sys.modules:
                try:
                    importlib.reload(sys.modules[modpath])
                except Exception:
                    pass
        agent = getattr(ctx, "agent", None)
        if agent is not None and hasattr(agent, "rebuild_registry"):
            try:
                agent.rebuild_registry()
                return True
            except Exception:
                pass
        # 无 agent 引用时尽力 reload
        return full.endswith(".py") and modpath in sys.modules
    except Exception:
        return False


def register(registry):
    registry.register(Tool(
        name="read_agent_file",
        description=("读取 Agent 源码文件(agent/ server/ web/ 下)到控制板, 供 AI 定位 Agent 自身问题。"
                     "当工具执行报错、怀疑是 Agent 代码缺陷时调用。只读, 不修改。"),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "项目相对路径, 如 agent/tools/filesystem.py"},
            },
            "required": ["file"],
        },
        handler=_read_agent_file,
        category="agent_repair",
    ))
    registry.register(Tool(
        name="agent_repair",
        description=("AI 自修复 Agent: 当某个工具/命令执行报错且判定为 Agent 自身代码问题时调用。"
                     "提交修复补丁(file/old/new), 小AI 会实时监管审查——只允许修复性修改, "
                     "恶意修改(注入后门/窃取/绕过安全/弱化权限/清除痕迹等)会被拦截; "
                     "应用前自动备份, 语法验证失败自动回滚, 成功后热重载立即生效并可重试原操作。"
                     "禁止修改 agent/security.py 与 config.json(安全边界受保护)。"),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "待修复文件(项目相对路径), 如 agent/tools/filesystem.py"},
                "old": {"type": "string", "description": "需替换的旧代码片段(必须与 read_agent_file 内容完全一致且唯一)"},
                "new": {"type": "string", "description": "新代码片段(修复后的代码)"},
                "reason": {"type": "string", "description": "修复原因(对应的报错信息)"},
                "auto": {"type": "boolean", "description": "true 时由规则引擎尝试自动修复(仅可判定场景)"},
            },
            "required": ["file", "reason"],
        },
        handler=_agent_repair,
        category="agent_repair",
    ))
