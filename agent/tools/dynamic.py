# -*- coding: utf-8 -*-
"""
NEURA 动态调用集系统 (v2.0 核心) —— 小AI 的工具工厂

小AI 对调用集拥有【最高修改权】与【超强辨别能力】:
  1. 调用命令已存在  -> 不新增, Agent 直接执行;
  2. 调用集不存在    -> 小AI 理解需求 -> 联网检索参考 -> 思考生成代码
                        -> 分层安全审查(辨别/拦截) -> 注册 + 持久化 -> 执行。

【安全审查五层防线】(保证只增能力、不引入危害)
  L1 意图级危险拦截: 删除/格式化/提权/后门/恶意/绕过等关键词即拒;
  L2 AST 导入白名单: 只允许安全库(json/re/math/datetime/os.path/httpx…),
                     拒绝 subprocess/ctypes/socket/pickle/paramiko/sys 等;
  L3 危险模式扫描:   os.system/os.popen/eval/exec/__import__/rm -rf/format/dd…;
  L4 破坏性操作拦截: os.remove/os.unlink/shutil.rmtree/写文件/改名/截断/杀进程…;
  L5 行为试运行:     纯计算类工具用哑上下文在超时内试跑, 崩溃即拒绝。

所有自定义调用集版本化持久化到 system/custom_tools/, 可删除、可回滚。
"""
import ast
import datetime
import json
import os
import re
import shutil
import tempfile
import time

from agent import utils
from agent.tools.registry import Tool, ToolResult, ToolContext, ToolRegistry

# ---------------- 安全审查常量 ----------------
_ALLOWED_IMPORTS = {
    "json", "re", "math", "datetime", "time", "random", "string",
    "collections", "functools", "itertools", "uuid", "html", "hashlib",
    "statistics", "csv", "urllib", "pathlib", "os", "textwrap",
    "unicodedata", "decimal", "fractions", "heapq", "bisect", "difflib",
    "io", "typing", "dataclasses", "httpx", "requests", "agent", "socket",  # socket 拒绝见下
}
_DENY_IMPORTS = {
    "subprocess", "ctypes", "paramiko", "pickle", "marshal", "socket",
    "telnetlib", "ftplib", "smtplib", "multiprocessing", "concurrent",
    "resource", "pty", "fcntl", "signal", "termios", "pwd", "grp", "sys",
    "importlib", "inspect", "runpy", "platform", "shutil", "tempfile", "secrets",
}
_DENY_CALLS = [
    r"os\.system\s*\(", r"os\.popen\s*\(", r"os\.spawn", r"os\.fork\s*\(",
    r"os\.remove\s*\(", r"os\.unlink\s*\(", r"os\.rmdir\s*\(", r"os\.rename\s*\(",
    r"os\.truncate\s*\(", r"os\.kill\s*\(", r"os\.chmod\s*\(", r"os\.chown\s*\(",
    r"shutil\.rmtree\s*\(", r"shutil\.move\s*\(", r"shutil\.copy", r"shutil\.chown",
    r"Path\s*\([^)]*\)\s*\.unlink", r"Path\s*\([^)]*\)\s*\.rmdir",
    r"open\s*\([^)]*['\"][wa][b]?['\"]", r"\.write_text\s*\(", r"\.write_bytes\s*\(",
    r"eval\s*\(", r"exec\s*\(", r"compile\s*\(", r"__import__\s*\(", r"getattr\s*\([^,]*load",
    r"subprocess", r"Popen", r"check_call", r"check_output", r"call\s*\(",
    r"importlib", r"\.__subclasses__\s*\(\)", r"\.__globals__", r"\.__builtins__",
]
_DENY_TEXT_PATTERNS = [
    r"rm\s+-rf", r"rm\s+-r\s+[/~]", r"format\s+[a-z]:", r"fdisk", r"mkfs",
    r"dd\s+if=", r"shutdown", r"reboot", r"init\s+[06]", r":\(\{\s*\|&",
    r"curl\s+.*\|\s*(ba)?sh", r"wget\s+.*\|\s*(ba)?sh", r">\s*/dev/sd",
    r"chmod\s+-R\s+777\s+/", r"chown\s+-R.*\s+/", r"kill\s+-9\s+\d+", r"taskkill",
    r"mkfs\.\w+", r"bootloader", r"mbr\b", r"格式化", r"清空磁盘", r"删除系统文件",
]
# 允许 os 的只读用法白名单(供审查提示用)
_ALLOWED_OS_ATTRS = {"path", "makedirs", "listdir", "walk", "stat", "getsize",
                     "environ", "sep", "name", "getcwd", "chdir", "getenv",
                     "get_terminal_size", "scandir"}

# ---------------- 模板生成器(内置引擎/无大模型时的小AI编程能力) ----------------
_TPL_STATS = '''# -*- coding: utf-8 -*-
"""自动生成的统计调用集: {name}"""
from agent.tools.registry import ToolResult

async def handler(args, ctx):
    vals = []
    for k in ("a", "b", "values", "data", "nums"):
        v = args.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            vals.extend(float(x) for x in v)
        else:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
    if not vals:
        return ToolResult(ok=False, error="缺少数值参数(a/b/values)", error_type="invalid_args")
    s = sum(vals)
    return ToolResult(data={{"count": len(vals), "sum": round(s, 6),
                             "mean": round(s / len(vals), 6),
                             "min": min(vals), "max": max(vals)}})
'''

_TPL_HTTP = '''# -*- coding: utf-8 -*-
"""自动生成的联网调用集: {name}"""
import json
from agent.tools.registry import ToolResult

try:
    import httpx
except ImportError:
    httpx = None


async def handler(args, ctx):
    url = str(args.get("url", "")).strip()
    if not url:
        return ToolResult(ok=False, error="缺少 url", error_type="invalid_args")
    if httpx is None:
        return ToolResult(ok=False, error="缺少 httpx 依赖", error_type="network")
    timeout = int(args.get("timeout", 20))
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            data = {{"status": r.status_code, "text": r.text[:5000]}}
        return ToolResult(data={{"url": url, "status": r.status_code, "data": data}})
    except Exception as e:
        return ToolResult(ok=False, error=f"请求失败: {{e}}", error_type="network", retryable=True)
'''

_TPL_FILE_STATS = '''# -*- coding: utf-8 -*-
"""自动生成的文件统计调用集: {name}"""
import os
from agent import safety, utils
from agent.tools.registry import ToolResult

async def handler(args, ctx):
    raw = str(args.get("path", ctx.workspace_root or "~"))
    try:
        path = safety.resolve_path(raw, ctx.config)
    except Exception as e:
        return ToolResult(ok=False, error=str(e), error_type="forbidden")
    if not os.path.exists(path):
        return ToolResult(ok=False, error=f"路径不存在: {{path}}", error_type="not_found", retryable=True)
    rows = []
    total_chars = 0
    files = [os.path.join(path, f) for f in os.listdir(path)] if os.path.isdir(path) else [path]
    files = [f for f in files if os.path.isfile(f)][:200]
    for f in files:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fp:
                content = fp.read(400000)
            rows.append({{"file": os.path.basename(f), "chars": len(content),
                          "lines": content.count("\\n") + 1,
                          "bytes": os.path.getsize(f)}})
            total_chars += len(content)
        except Exception:
            continue
    rows.sort(key=lambda r: -r["bytes"])
    return ToolResult(data={{"path": path, "count": len(rows), "total_chars": total_chars, "files": rows}})
'''

_TPL_DATE = '''# -*- coding: utf-8 -*-
"""自动生成的日期调用集: {name}"""
import datetime
from agent.tools.registry import ToolResult

async def handler(args, ctx):
    d1 = str(args.get("date1", "") or args.get("date", ""))
    d2 = str(args.get("date2", ""))
    def parse(s):
        s = s.strip().replace("/", "-")
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                return datetime.datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None
    if d1:
        a = parse(d1)
        if a is None:
            return ToolResult(ok=False, error=f"无法解析日期: {{d1}}", error_type="invalid_args")
        if d2:
            b = parse(d2)
            if b is None:
                return ToolResult(ok=False, error=f"无法解析日期: {{d2}}", error_type="invalid_args")
            days = (b.date() - a.date()).days
            return ToolResult(data={{"date1": d1, "date2": d2, "diff_days": days}})
        offset = int(args.get("add_days", 0))
        if offset:
            return ToolResult(data={{"date": d1, "plus_days": offset,
                                     "result": (a + datetime.timedelta(days=offset)).strftime("%Y-%m-%d")}})
        return ToolResult(data={{"date": d1, "weekday": "星期" + "一二三四五六日"[a.weekday()]}})
    return ToolResult(ok=False, error="缺少 date1/date2 或 add_days", error_type="invalid_args")
'''

_TPL_CSV = '''# -*- coding: utf-8 -*-
"""自动生成的表格解析调用集: {name}"""
import csv as _csv
import os
from agent import safety
from agent.tools.registry import ToolResult

async def handler(args, ctx):
    raw = str(args.get("path", ""))
    if not raw:
        return ToolResult(ok=False, error="缺少 path", error_type="invalid_args")
    try:
        path = safety.resolve_path(raw, ctx.config)
    except Exception as e:
        return ToolResult(ok=False, error=str(e), error_type="forbidden")
    if not os.path.exists(path):
        return ToolResult(ok=False, error=f"文件不存在: {{path}}", error_type="not_found", retryable=True)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            rows = list(_csv.reader(fp))[:1000]
        if rows:
            headers = rows[0]
            body = [dict(zip(headers, r)) for r in rows[1:]]
            return ToolResult(data={{"path": path, "rows": len(body), "headers": headers, "data": body}})
        return ToolResult(data={{"path": path, "rows": 0}})
    except Exception as e:
        return ToolResult(ok=False, error=f"解析失败: {{e}}", error_type="exec", retryable=True)
'''

_TPL_GENERIC = '''# -*- coding: utf-8 -*-
"""自动生成的通用调用集: {name}"""
import json
from agent.tools.registry import ToolResult

async def handler(args, ctx):
    # 通用兜底: 把传入参数规整后返回, 供 Agent 判断是否满足需求
    cleaned = {{k: v for k, v in args.items() if v is not None}}
    if not cleaned:
        return ToolResult(ok=False, error="缺少参数", error_type="invalid_args")
    return ToolResult(data={{"received": cleaned, "note": "通用调用集, 如需精确计算请说明参数含义"}})
'''

_TEMPLATES = {
    "stats": _TPL_STATS,
    "http": _TPL_HTTP,
    "file_stats": _TPL_FILE_STATS,
    "date": _TPL_DATE,
    "csv": _TPL_CSV,
    "generic": _TPL_GENERIC,
}

_CLASSIFY_RULES = [
    (["平均", "求和", "统计", "均值", "合计", "总和", "计算", "average", "sum", "mean", "total"], "stats"),
    (["网址", "url", "http", "接口", "api", "抓取网页", "请求", "fetch", "request"], "http"),
    (["文件", "目录", "文件夹", "字数", "词频", "行数", "file", "directory", "word count"], "file_stats"),
    (["日期", "时间差", "多少天", "星期", "date", "days between"], "date"),
    (["csv", "表格", "excel", "sheet", "table"], "csv"),
]


def classify_tool(operation: str) -> str:
    op = (operation or "").lower()
    score = 0
    best = "generic"
    for kws, tpl in _CLASSIFY_RULES:
        s = sum(1 for k in kws if k in op)
        if s > score:
            score, best = s, tpl
    return best


class ToolReviewError(Exception):
    pass


# ---------------------------------------------------------------
# 分层安全审查(辨别能力核心)
# ---------------------------------------------------------------
def review_tool_code(code: str, name: str) -> tuple:
    """五层审查。返回 (ok: bool, reasons: list[str])。"""
    reasons = []
    code = code or ""

    # L3 危险文本模式
    for pat in _DENY_TEXT_PATTERNS:
        if re.search(pat, code, flags=re.I):
            reasons.append(f"命中危险文本模式: {pat}")
            return False, reasons

    # L2 AST 解析 + 导入白名单 + L4 调用模式
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        reasons.append(f"代码语法错误: {e}")
        return False, reasons
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = (a.name or "").split(".")[0]
                if root in _DENY_IMPORTS:
                    reasons.append(f"禁止导入: {a.name}")
                elif root not in _ALLOWED_IMPORTS:
                    reasons.append(f"未列入白名单的导入: {a.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _DENY_IMPORTS:
                reasons.append(f"禁止导入: {node.module}")
            elif root not in _ALLOWED_IMPORTS:
                reasons.append(f"未列入白名单的导入: {node.module}")
        elif isinstance(node, ast.Call):
            src = ast.unparse(node)
            for pat in _DENY_CALLS:
                if re.search(pat, src):
                    reasons.append(f"命中禁止调用: {pat}")
    # L4 强化: os 危险属性
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            attr = node.attr
            if attr not in _ALLOWED_OS_ATTRS:
                reasons.append(f"os.{attr} 不允许使用")
    # 必须定义 async handler(args, ctx)
    handlers = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "handler"]
    if not handlers:
        reasons.append("必须定义 async def handler(args, ctx)")
    if reasons:
        return False, reasons

    # 语法编译校验
    try:
        compile(code, f"<custom_{name}>", "exec")
    except Exception as e:
        reasons.append(f"编译失败: {e}")
        return False, reasons
    return True, []


def _compile_handler(code: str, name: str):
    ns: dict = {}
    exec(compile(code, f"<custom_tool_{name}>", "exec"), ns)
    fn = ns.get("handler")
    if not callable(fn):
        raise ToolReviewError("工具代码没有可调用的 handler")
    async def wrapper(args, ctx):
        result = await fn(args, ctx)
        if not isinstance(result, ToolResult):
            result = ToolResult(data=result if result is not None else {})
        return result
    return wrapper


class _NullEmitter:
    async def _noop(self, *a, **k):
        return None
    def __getattr__(self, name):
        return self._noop


def _safe_name(suggested: str, operation: str) -> str:
    import hashlib as _h
    s = re.sub(r"[^a-zA-Z0-9_]", "_", (suggested or "")).strip("_").lower()
    s = re.sub(r"_+", "_", s).strip("_")
    if len(s) >= 6 and s != "tool":
        return s[:48]
    # 建议名太短/含中文/退化为 "tool" -> 从操作中提取中文词并生成稳定短哈希
    op = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", operation or "")[:20]
    if op:
        if op.isascii():
            return f"tool_{op}"[:48]
        return f"tool_{_h.md5(op.encode('utf-8')).hexdigest()[:8]}"
    return f"tool_{int(time.time())}"


class CustomToolStore:
    """动态调用集存储: 持久化 / 加载 / 删除 / 备份。"""

    def __init__(self, store_dir: str, registry: ToolRegistry, config: dict):
        self.store_dir = store_dir
        self.registry = registry
        self.config = config
        os.makedirs(store_dir, exist_ok=True)

    # ---------- 加载 ----------
    def load_all(self) -> list:
        loaded = []
        if not os.path.isdir(self.store_dir):
            return loaded
        for entry in os.listdir(self.store_dir):
            tdir = os.path.join(self.store_dir, entry)
            code_path = os.path.join(tdir, "tool.py")
            meta_path = os.path.join(tdir, "tool.json")
            if not (os.path.isdir(tdir) and os.path.exists(code_path)):
                continue
            meta = utils.read_json(meta_path, {}) or {}
            try:
                code = open(code_path, "r", encoding="utf-8").read()
                handler = _compile_handler(code, entry)
                tool = Tool(name=entry, description=meta.get("description", "自定义调用集"),
                            parameters=meta.get("parameters", {"type": "object", "properties": {}}),
                            handler=handler, category="custom", source="custom",
                            version=int(meta.get("version", 1)))
                self.registry.register(tool)
                loaded.append(entry)
            except Exception as e:
                print(f"[CustomTools] 加载失败 {entry}: {e}")
        return loaded

    # ---------- 持久化 ----------
    def persist(self, name: str, code: str, meta: dict):
        tdir = os.path.join(self.store_dir, name)
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, "tool.py"), "w", encoding="utf-8") as f:
            f.write(code)
        utils.write_json(os.path.join(tdir, "tool.json"), meta)
        # 备份历史版本
        bak = os.path.join(self.store_dir, "_backups")
        os.makedirs(bak, exist_ok=True)
        shutil.copy2(os.path.join(tdir, "tool.py"),
                     os.path.join(bak, f"{name}_v{meta.get('version', 1)}.py"))

    def remove(self, name: str) -> bool:
        tdir = os.path.join(self.store_dir, name)
        if not os.path.isdir(tdir):
            return False
        self.registry.unregister(name)
        shutil.rmtree(tdir, ignore_errors=True)
        return True

    def list(self) -> list:
        out = []
        if os.path.isdir(self.store_dir):
            for entry in os.listdir(self.store_dir):
                meta_path = os.path.join(self.store_dir, entry, "tool.json")
                meta = utils.read_json(meta_path, {}) or {}
                if meta:
                    out.append({"name": entry, "version": meta.get("version", 1),
                                "description": meta.get("description", ""),
                                "created_at": meta.get("created_at", "")})
        return out

    # ---------- 合成主流程 ----------
    async def synthesize_and_register(self, operation: str, suggested_name: str = "",
                                      args_hint: dict | None = None,
                                      update_name: str | None = None,
                                      registry: ToolRegistry | None = None,
                                      agent=None, ctx: ToolContext | None = None) -> ToolResult:
        reg = registry or self.registry
        args_hint = args_hint or {}
        operation = (operation or "").strip()
        if not operation and not update_name:
            return ToolResult(ok=False, error="缺少操作描述", error_type="invalid_args")

        # L1 意图级危险拦截(最高辨别: 恶意高危修改命令调用集在此被拦)
        if ctx and ctx.mini_ai is not None:
            reason = ctx.mini_ai.detect_dangerous_request(operation + " " + suggested_name)
            if reason:
                return ToolResult(ok=False, error=f"安全拦截(小AI辨别): {reason}", error_type="forbidden")

        name = _safe_name(suggested_name or (update_name or ""), operation)
        existing = reg.get(name)
        new_version = (existing.version + 1) if existing else 1
        # ---- v3.24 调用集"只增不减不弱化"监管: 内置调用集不可覆盖 ----
        if existing is not None and existing.source == "builtin":
            return ToolResult(ok=False,
                              error=(f"内置调用集「{name}」不可被覆盖(调用集只允许增、不允许减/弱化内置能力)。"
                                     "请使用新名称，或先经小AI 评估后以自定义调用集扩展。"),
                              error_type="forbidden")

        async def _log(text, level="run"):
            if ctx and ctx.emitter:
                try:
                    await ctx.emitter.panel_update("synth_flow", content=[
                        {"time": utils.now_iso(), "text": text, "level": level}])
                except Exception:
                    pass

        if ctx and ctx.emitter:
            await ctx.emitter.panel("synth_flow", "process", "小AI 工具工厂 · 动态调用集",
                                    [{"time": utils.now_iso(),
                                      "text": f"操作: {operation}", "level": "req"},
                                     {"time": utils.now_iso(),
                                      "text": "① 理解需求 → ② 联网检索参考 → ③ 生成代码 → ④ 安全审查 → ⑤ 注册执行", "level": "run"}])
        await _log("② 正在联网检索参考资料…")

        # 联网参考(尽力而为)
        reference = ""
        if agent is not None and ctx is not None:
            try:
                sr = await reg.invoke("web_search", {"query": operation, "max_results": 3}, ctx)
                if sr.ok:
                    reference = " ".join(
                        r.get("snippet", "") for r in (sr.data or {}).get("results", [])[:2])[:1200]
            except Exception:
                pass

        # ③ 生成代码: 优先大模型(超强编程), 否则内置模板编程
        await _log("③ 思考并生成调用集代码…")
        code, description, parameters = await self._generate(
            operation, name, args_hint, reference, agent)

        # ④ 分层安全审查(辨别能力)
        ok, reasons = review_tool_code(code, name)
        if not ok:
            if ctx and ctx.emitter:
                await ctx.emitter.panel_update("synth_flow", status="error",
                                               content=[{"time": utils.now_iso(),
                                                         "text": f"❌ 安全审查拦截: {'; '.join(reasons)}", "level": "err"}])
            return ToolResult(ok=False, error="安全审查拦截: " + "; ".join(reasons[:6]),
                              error_type="forbidden")

        # L5 行为试运行(纯计算类)
        if _is_pure_compute(code) and args_hint:
            trial = await _trial_run(code, args_hint, ctx.config if ctx else {})
            if not trial[0]:
                if ctx and ctx.emitter:
                    await ctx.emitter.panel_update("synth_flow", status="error",
                                                   content=[{"time": utils.now_iso(),
                                                             "text": f"❌ 试运行失败: {trial[1]}", "level": "err"}])
                return ToolResult(ok=False, error=f"试运行失败: {trial[1]}", error_type="exec")

        # ⑤ 注册 + 持久化
        # ---- v3.24 能力不减审查: 升级已有自定义调用集时, 新版本不得删除旧参数/弱化能力 ----
        if existing is not None and existing.source == "custom":
            old_props = set((existing.parameters.get("properties") or {}).keys())
            new_props = set((parameters.get("properties") or {}).keys())
            if old_props and not old_props.issubset(new_props):
                missing = sorted(old_props - new_props)
                return ToolResult(ok=False,
                                  error=f"能力弱化拦截: 新版本删除了参数 {missing}，调用集只允许增、不允许减/弱化。请保留全部旧参数后再提交。",
                                  error_type="forbidden")
        try:
            handler = _compile_handler(code, name)
        except Exception as e:
            return ToolResult(ok=False, error=f"注册失败: {e}", error_type="exec")
        tool = Tool(name=name, description=description, parameters=parameters,
                    handler=handler, category="custom", source="custom", version=new_version)
        reg.register(tool)
        meta = {"name": name, "description": description, "parameters": parameters,
                "version": new_version, "operation": operation,
                "created_at": utils.now_iso(), "review": {"passed": True, "checks": ["L1意图拦截", "L2导入白名单", "L3危险模式", "L4破坏性操作", "L5试运行"]}}
        self.persist(name, code, meta)

        if ctx and ctx.emitter:
            await ctx.emitter.panel_update("synth_flow", status="ok",
                                           content=[{"time": utils.now_iso(),
                                                     "text": f"✅ 调用集 {name} v{new_version} 已注册(经全部安全审查)", "level": "ok"}])

        # 注册即生效: 用调用方传入的参数立即试执行一次, 结果一并返回/展示
        first_result = None
        run_args = _filter_run_args(args_hint or {})
        if ctx is not None and run_args:
            try:
                fr = await reg.invoke(name, run_args, ctx)
                if fr.ok:
                    first_result = fr.data
                else:
                    first_result = {"error": fr.error or "执行失败"}
            except Exception as e:
                first_result = {"error": f"{type(e).__name__}: {e}"}
        if first_result is not None and ctx and ctx.emitter:
            await ctx.emitter.panel_update("synth_flow", status="ok",
                                           content=[{"time": utils.now_iso(),
                                                     "text": f"▶ 立即试执行: {json.dumps(first_result, ensure_ascii=False)[:400]}", "level": "run"}])
        data = {"name": name, "version": new_version, "code": code,
                "description": description, "parameters": parameters,
                "update": bool(update_name), "first_result": first_result}
        return ToolResult(ok=True, data=data)

    async def _generate(self, operation: str, name: str, args_hint: dict,
                        reference: str, agent) -> tuple:
        """生成 (code, description, parameters)。优先大模型, 失败回退模板。"""
        # 大模型编程(DeepSeek/Ollama 可用时 = 超强编程能力)
        if agent is not None and getattr(agent, "brain", None) is not None:
            model = agent.brain.model
            if type(model).__name__ in ("DeepSeekModel", "OllamaModel"):
                try:
                    from agent.brain import ModelResult
                    tpl = _TEMPLATES["stats"]  # 占位获得格式感
                    prompt = (
                        "你是工具工厂工程师。请为一个 Agent 编写一个新的工具调用集。\n"
                        f"需求/操作: {operation}\n"
                        f"建议名称: {name}\n"
                        f"已知参数提示: {json.dumps(args_hint, ensure_ascii=False)}\n"
                        f"联网参考资料(供参考): {reference[:800]}\n\n"
                        "输出严格为 JSON(不要任何其他文字):\n"
                        '{"name": "snake_case_name", "description": "一句话描述", '
                        '"parameters": {"type": "object", "properties": {"a": {"type": "number", "description": "..."}}, "required": []}, '
                        '"code": "完整 Python 代码，必须定义 async def handler(args, ctx) -> ToolResult"}\n'
                        "代码约束: 只允许 import json/re/math/datetime/time/os.path/statistics/random/collections/urllib.parse/httpx; "
                        "禁止 subprocess/os.system/os.popen/eval/exec/__import__/socket/shutil/写文件/删除文件/改名; "
                        "涉及路径必须用 safety.resolve_path 解析; 网络用 httpx 且 timeout<=30; 返回 ToolResult(data=...) 或 ToolResult(ok=False, error=...)。"
                    )
                    res: ModelResult = await agent.brain.chat(
                        [{"role": "system", "content": "你是严格的工具工厂，只输出 JSON。"},
                         {"role": "user", "content": prompt}], tools=None)
                    text = res.text or ""
                    m = re.search(r"```json\s*(.*?)```", text, flags=re.S)
                    payload = m.group(1) if m else text
                    obj = json.loads(payload)
                    code = obj.get("code", "")
                    if code and review_tool_code(code, name)[0]:
                        return code, str(obj.get("description", f"动态调用集 {name}")), obj.get("parameters") or {
                            "type": "object", "properties": {}}
                except Exception:
                    pass
        # 内置模板编程(零依赖)
        kind = classify_tool(operation)
        tpl = _TEMPLATES.get(kind, _TEMPLATES["generic"])
        code = tpl.format(name=name)
        desc_map = {
            "stats": f"对传入数值(a/b/values)求个数、总和、均值、最值(动态生成)",
            "http": f"联网请求指定 URL 并返回 JSON 或文本(动态生成)",
            "file_stats": f"统计目录/文件下的字符数、行数、字节数并按大小排序(动态生成)",
            "date": f"日期解析、日期差、加天数、星期计算(动态生成)",
            "csv": f"解析 CSV 表格为结构化数据(动态生成)",
            "generic": f"通用参数整理调用集(动态生成)",
        }
        props = {}
        if kind == "stats":
            props = {"a": {"type": "number", "description": "第一个数"}, "b": {"type": "number", "description": "第二个数"},
                     "values": {"type": "array", "items": {"type": "number"}, "description": "数值列表"}}
        elif kind == "http":
            props = {"url": {"type": "string", "description": "目标网址"}, "timeout": {"type": "integer"}}
        elif kind == "file_stats":
            props = {"path": {"type": "string", "description": "目录或文件路径"}}
        elif kind == "date":
            props = {"date1": {"type": "string", "description": "日期1"}, "date2": {"type": "string", "description": "日期2(可选)"},
                     "add_days": {"type": "integer", "description": "加天数(可选)"}}
        elif kind == "csv":
            props = {"path": {"type": "string", "description": "CSV 文件路径"}}
        for k, v in (args_hint or {}).items():
            if k not in props:
                vt = "number" if isinstance(v, (int, float)) and not isinstance(v, bool) else ("boolean" if isinstance(v, bool) else "string")
                props[k] = {"type": vt, "description": f"参数 {k}"}
        parameters = {"type": "object", "properties": props, "required": []}
        return code, desc_map.get(kind, f"动态调用集 {name}"), parameters


def _is_pure_compute(code: str) -> bool:
    """判断是否为纯计算(可安全试运行): 无网络/文件/httpx 引用。"""
    return ("httpx" not in code and "open(" not in code and "resolve_path" not in code
            and "requests" not in code and "os.listdir" not in code)


def _filter_run_args(args_hint: dict) -> dict:
    """过滤参数为可安全传入工具的类型(数字/字符串/布尔 + 纯数值列表)。"""
    out = {}
    for k, v in (args_hint or {}).items():
        if isinstance(v, (int, float, str, bool)):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            cleaned = [x for x in v if isinstance(x, (int, float)) and not isinstance(x, bool)]
            if cleaned:
                out[k] = cleaned
    return out


async def _trial_run(code: str, args_hint: dict, config: dict) -> tuple:
    """用哑上下文在超时内试运行 handler。返回 (ok, message)。"""
    import asyncio
    try:
        handler = _compile_handler(code, "trial")
    except Exception as e:
        return False, f"编译失败: {e}"
    tmp = tempfile.mkdtemp(prefix="neura_trial_")
    try:
        ctx = ToolContext(session_id="trial", emitter=_NullEmitter(),
                          workspace_root=tmp, downloads_dir=tmp,
                          config=config or {"permissions": {"allow_paths": ["*"], "deny_paths": []}})
        args = _filter_run_args(args_hint)
        result = await asyncio.wait_for(handler(args, ctx), timeout=6)
        return (result.ok, result.error or "ok") if result else (False, "无返回")
    except asyncio.TimeoutError:
        return False, "试运行超时"
    except Exception as e:
        return False, f"试运行异常: {type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------
# 注册 synthesize_tool(供 Agent/模型显式调用)
# ---------------------------------------------------------------
async def _synthesize_tool(args: dict, ctx: ToolContext) -> ToolResult:
    operation = str(args.get("operation", "") or args.get("需求", ""))
    suggested = str(args.get("suggested_name", ""))
    args_hint = args.get("args_hint") or {}
    update_name = str(args.get("update_name", ""))
    if not operation and not update_name:
        return ToolResult(ok=False, error="缺少 operation(需求描述)", error_type="invalid_args")
    store = (ctx.extra or {}).get("tool_store")
    if store is None and ctx.agent is not None:
        store = ctx.agent.custom_tools
    if store is None:
        return ToolResult(ok=False, error="动态调用集存储不可用", error_type="exec")
    return await store.synthesize_and_register(
        operation=operation, suggested_name=suggested, args_hint=args_hint,
        update_name=update_name, registry=ctx.agent.registry if ctx.agent else None,
        agent=ctx.agent, ctx=ctx)


def register(registry: ToolRegistry):
    registry.register(Tool(
        name="synthesize_tool",
        description=("可选能力沉淀: 当你希望把一个可复用的新能力沉淀下来时调用。小AI 会理解需求、联网检索参考、"
                     "编写并【经五层安全审查】注册, 注册后立即可用。默认你直接操控 Agent, 此调用非必经之路。恶意/高危操作会被拦截。"),
        parameters={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "description": "需求/操作描述(自然语言)"},
                "suggested_name": {"type": "string", "description": "建议的调用集名称(snake_case)"},
                "args_hint": {"description": "参数示例, 帮助生成参数 schema, 如 {\"a\": 2, \"b\": 3}"},
                "update_name": {"type": "string", "description": "若要升级已有自定义调用集, 填其名称"},
            },
            "required": ["operation"],
        },
        handler=_synthesize_tool,
        category="dynamic",
    ))
