# -*- coding: utf-8 -*-
"""工具：编程工作流(写代码/运行/打包下载)。

code_task 是"一键编程"编排工具:
  1. 由模型/引擎生成代码计划
  2. 写入工作区文件
  3. 运行并流式输出到控制板
  4. 失败时由 AgentCore 的自动调试循环修复
  5. 打包(单文件直下 / 多文件 ZIP)并触发下载
"""
import datetime
import os
import re
import shutil

from agent import safety, utils
from .registry import Tool, ToolResult, ToolContext, ToolRegistry

LANG_RUNNERS = {
    "python": ["python", "{file}"],
    "py": ["python", "{file}"],
    "javascript": ["node", "{file}"],
    "js": ["node", "{file}"],
    "typescript": ["node", "{file}"],
    "shell": ["bash", "{file}"],
    "bash": ["bash", "{file}"],
    "sh": ["bash", "{file}"],
}

# 浏览器渲染类扩展名: 不经过解释器执行, 在控制板内预览(避免 python 解析 html 报 SyntaxError)
_PREVIEW_EXTS = (".html", ".htm")


def _runner_for_file(path: str):
    """按文件扩展名选择运行器。

    - .py  -> python; .js -> node; .sh -> bash
    - .html/.htm 等浏览器渲染类 -> 返回 None, 调用方走"控制板预览"分支,
      绝不把 HTML 交给 python/node 执行(否则会把 CSS 当源码解析报语法错误)。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".py":
        return ["python", "{file}"]
    if ext == ".js":
        return ["node", "{file}"]
    if ext in (".sh", ".bash"):
        return ["bash", "{file}"]
    return None


def _workspace_dir(ctx: ToolContext) -> str:
    d = os.path.join(ctx.workspace_root, "code")
    os.makedirs(d, exist_ok=True)
    return d


async def _write_code_file(args: dict, ctx: ToolContext) -> ToolResult:
    raw = str(args.get("path", ""))
    content = str(args.get("content", ""))
    language = str(args.get("language", "python"))
    if not raw:
        raw = f"main.{'py' if language in ('python', 'py') else ('js' if language in ('js', 'javascript') else 'txt')}"
    abs_path = os.path.abspath(raw)
    if not os.path.commonpath([abs_path, os.path.abspath(_workspace_dir(ctx))]) == os.path.abspath(_workspace_dir(ctx)):
        # 允许写入工作区内的相对路径
        abs_path = os.path.join(_workspace_dir(ctx), raw if os.path.isabs(raw) is False else os.path.basename(raw))
    abs_path = os.path.normpath(os.path.join(_workspace_dir(ctx), raw))
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return ToolResult(ok=False, error=f"写入失败: {e}", error_type="exec", retryable=True)
    rel = os.path.relpath(abs_path, _workspace_dir(ctx))
    await ctx.emitter.panel(
        args.get("window_id", "code_editor"),
        "code", f"代码编辑器 · {rel}",
        {"path": rel, "language": language, "code": content[:30000]},
        status="running",
    )
    return ToolResult(data={"path": abs_path, "rel": rel, "bytes": len(content.encode("utf-8"))})


async def _run_code(args: dict, ctx: ToolContext) -> ToolResult:
    cfg_exec = ctx.config.get("permissions", {}).get("code_execution", {})
    if not cfg_exec.get("enabled", True):
        return ToolResult(ok=False, error="代码执行已在配置中禁用", error_type="forbidden")
    language = str(args.get("language", "python")).lower()
    timeout = int(args.get("timeout", cfg_exec.get("timeout_seconds", 30)))
    window_id = str(args.get("window_id", "code_console"))
    path = args.get("path")
    code = args.get("code")
    ws = _workspace_dir(ctx)

    if code:
        ext = {".py": ".py", "py": ".py", "python": ".py", "js": ".js", "javascript": ".js",
               "html": ".html", "bash": ".sh", "shell": ".sh"}.get(language, ".txt")
        ts = datetime.datetime.now().strftime("%H%M%S")
        path = os.path.join(ws, f"_inline_{ts}{ext}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            return ToolResult(ok=False, error=f"代码写入失败: {e}", error_type="exec")
    elif path:
        path = os.path.normpath(os.path.join(ws, path)) if not os.path.isabs(path) else path
        if not os.path.exists(path):
            return ToolResult(ok=False, error=f"文件不存在: {path}", error_type="not_found", retryable=True)
    else:
        return ToolResult(ok=False, error="需要提供 path 或 code", error_type="invalid_args")

    if not safety.code_extension_allowed(path, ctx.config):
        return ToolResult(ok=False, error=f"不允许执行该文件类型: {path}", error_type="forbidden")

    # 浏览器渲染类(HTML): 不经过解释器执行, 控制板内预览(修复 python 解析 html 的 SyntaxError)
    if path.lower().endswith(_PREVIEW_EXTS):
        rel = os.path.relpath(path, ws)
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
        except Exception as e:
            return ToolResult(ok=False, error=f"读取文件失败: {e}", error_type="exec")
        await ctx.emitter.panel_update(window_id, content=[
            {"time": utils.now_iso(), "text": f"$ 浏览器渲染 {rel} (无需解释器执行)", "level": "cmd"},
            {"time": utils.now_iso(), "text": "已生成运行预览, 请在控制板预览窗口查看; 也可下载后直接用浏览器打开", "level": "ok"},
        ], status="ok")
        return ToolResult(ok=True, data={"path": path, "rel": rel, "preview": True,
                                         "html": html[:40000],
                                         "note": "HTML 已生成并在控制板内预览(浏览器渲染类代码不经过解释器执行)"})

    runner = _runner_for_file(path)
    if runner is None:
        return ToolResult(ok=False, error=f"不支持运行该文件类型: {path}", error_type="invalid_args")
    command = " ".join(runner).replace("{file}", f'"{path}"')

    await ctx.emitter.panel(window_id, "process", "运行输出",
                            [{"time": utils.now_iso(), "text": f"$ {command}", "level": "cmd"}])
    import asyncio
    result = await asyncio.to_thread(
        safety.sandbox_run, command, ws, timeout,
        cfg_exec.get("memory_limit_mb", 512), cfg_exec.get("firejail", False))
    stdout, stderr = result["stdout"], result["stderr"]
    lines_out = []
    for ln in (stdout.splitlines() or ["(无标准输出)"]):
        lines_out.append({"time": utils.now_iso(), "text": ln, "level": "out"})
    for ln in (stderr.splitlines() or []):
        lines_out.append({"time": utils.now_iso(), "text": ln, "level": "err"})
    await ctx.emitter.panel_update(window_id, content=lines_out, status="ok" if result["ok"] else "error")
    if not result["ok"]:
        tail = "\n".join((stderr or stdout).splitlines()[-25:])
        return ToolResult(ok=False, error=f"运行失败(退出码 {result['code']})\n{tail}",
                          error_type="exec", retryable=True)
    return ToolResult(data={"path": path, "exit_code": 0,
                            "output": safety.sanitize_output(stdout, 12000)})


async def _package_project(args: dict, ctx: ToolContext) -> ToolResult:
    ws = _workspace_dir(ctx)
    rels = args.get("paths") or []
    if isinstance(rels, str):
        rels = [rels]
    if not rels:
        # 默认打包整个工作区当前会话
        rels = [d for d in os.listdir(ws) if not d.startswith("_inline")]
        if not rels:
            return ToolResult(ok=False, error="工作区没有可打包的文件", error_type="invalid_args")
    abs_paths = [os.path.normpath(os.path.join(ws, p)) for p in rels]
    for p in abs_paths:
        if not os.path.exists(p):
            return ToolResult(ok=False, error=f"文件不存在: {p}", error_type="not_found", retryable=True)
    name = str(args.get("name", "project"))
    import uuid
    sid_safe = utils.safe_filename(ctx.session_id, "s")
    out_dir = os.path.join(ctx.downloads_dir, sid_safe)
    os.makedirs(out_dir, exist_ok=True)
    if len(abs_paths) == 1 and os.path.isfile(abs_paths[0]):
        dst_name = utils.safe_filename(os.path.basename(abs_paths[0]), "download")
        dst = os.path.join(out_dir, dst_name)
        shutil.copy2(abs_paths[0], dst)
        url = f"/api/downloads/{sid_safe}/{dst_name}"
    else:
        dst_name = utils.safe_filename(name, "project") + ".zip"
        dst = os.path.join(out_dir, dst_name)
        import zipfile
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in abs_paths:
                base = os.path.basename(p.rstrip(os.sep)) or "item"
                if os.path.isdir(p):
                    for root, dirs, files in os.walk(p):
                        for fn in files:
                            full = os.path.join(root, fn)
                            arc = os.path.join(base, os.path.relpath(full, p)).replace(os.sep, "/")
                            zf.write(full, arc)
                else:
                    zf.write(p, os.path.join(base, os.path.basename(p)))
        url = f"/api/downloads/{sid_safe}/{dst_name}"
    return ToolResult(data={"filename": os.path.basename(dst), "url": url, "multi": len(abs_paths) > 1},
                      downloads=[(os.path.basename(dst), dst)])


async def _code_task(args: dict, ctx: ToolContext) -> ToolResult:
    """一键编程编排(闭环): 生成代码->写入工作区->运行->(失败交由核心自动调试)->打包->触发下载。"""
    requirement = str(args.get("requirement", ""))
    language = str(args.get("language", "python"))
    if not requirement:
        return ToolResult(ok=False, error="缺少 requirement", error_type="invalid_args")
    ws = _workspace_dir(ctx)
    win = "code_flow_" + utils.safe_filename(ctx.session_id, "s")[:6]

    async def _log(text, level="run"):
        await ctx.emitter.panel_update(win, content=[{"time": utils.now_iso(), "text": text, "level": level}])

    await ctx.emitter.panel(win, "process", "编程工作流",
                            [{"time": utils.now_iso(), "text": f"需求: {requirement}", "level": "req"}])
    # 1. 生成代码计划
    await _log("正在生成代码计划…", "run")
    files = []
    if ctx.agent is not None:
        plan_text = await ctx.agent.generate_code_plan(requirement, language)
        files = _parse_files_from_plan(plan_text)
    if not files:
        files = _fallback_generate(requirement, language)
    if not files:
        return ToolResult(ok=False, error="无法生成代码", error_type="exec")

    # 2. 写入工作区
    written = []
    for rel, code in files:
        p = os.path.normpath(os.path.join(ws, rel))
        if not p.startswith(os.path.abspath(ws) + os.sep):
            p = os.path.join(ws, os.path.basename(rel))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(code)
        written.append(os.path.relpath(p, ws))
        await _log(f"已生成 {written[-1]}", "ok")
    await ctx.emitter.panel(win + "_ed", "code", "生成代码",
                            {"path": written[0], "language": language,
                             "code": files[0][1][:20000]})

    # 3. 运行/预览主文件(按扩展名选择: python/node/bash 进沙箱; HTML 控制板内预览)
    main_rel = next((f for f in written if f.endswith((".py", ".js", ".sh"))), written[0])
    main_abs = os.path.join(ws, main_rel)
    runner = _runner_for_file(main_abs)
    cfg_exec = ctx.config.get("permissions", {}).get("code_execution", {})
    if runner is None:
        # 浏览器渲染类(如 calculator.html): 绝不交给 python 执行, 控制板内 iframe 预览
        preview_code = next((c for (rel_, c) in files if rel_ == main_rel), "")
        if not preview_code:
            try:
                with open(main_abs, "r", encoding="utf-8") as f:
                    preview_code = f.read()
            except Exception:
                preview_code = ""
        await _log(f"$ 浏览器渲染 {main_rel} (无需解释器执行)", "cmd")
        await _log("已生成运行预览窗口(可拖拽/缩放); 下载后可直接用浏览器打开", "ok")
        await ctx.emitter.panel(win + "_pv", "html_preview", "运行预览 · " + main_rel,
                                {"path": main_rel, "html": preview_code}, status="ok")
        await ctx.emitter.panel_update(win, status="ok")
    else:
        command = " ".join(runner).replace("{file}", f'"{main_abs}"')
        await _log(f"$ {command}", "cmd")
        import asyncio
        result = await asyncio.to_thread(
            safety.sandbox_run, command, ws,
            cfg_exec.get("timeout_seconds", 30),
            cfg_exec.get("memory_limit_mb", 512), cfg_exec.get("firejail", False))
        lines = []
        for ln in (result["stdout"].splitlines() or ["(无输出)"]):
            lines.append({"time": utils.now_iso(), "text": ln, "level": "out"})
        for ln in result["stderr"].splitlines():
            lines.append({"time": utils.now_iso(), "text": ln, "level": "err"})
        await ctx.emitter.panel_update(win, content=lines,
                                       status="ok" if result["ok"] else "error")
        if not result["ok"]:
            # 交给核心循环的自动调试(retryable 触发修复流程)
            tail = "\n".join((result["stderr"] or result["stdout"]).splitlines()[-20:])
            return ToolResult(ok=False, error=f"运行失败(退出码 {result['code']})\n{tail}",
                              error_type="exec", retryable=True,
                              data={"files": written, "window_id": win})

    # 4. 打包下载(单文件直下, 多文件 ZIP)
    await _log("打包并准备下载…", "run")
    pkg = await _package_project({"paths": written,
                                  "name": utils.safe_filename(requirement[:20], "project")}, ctx)
    if not pkg.ok:
        await _log(f"打包失败: {pkg.error}", "err")
        return ToolResult(ok=True, data={"files": written, "window_id": win,
                                         "note": "代码已生成并运行, 但打包失败"})
    await _log(f"下载就绪: {pkg.data['filename']}", "ok")
    # AI 自动收尾: 关闭"编程工作流"过程窗口(代码编辑器/运行预览窗口保留供查看)
    try:
        await ctx.emitter.panel_close(win)
    except Exception:
        pass
    return ToolResult(ok=True, data={"files": written, "window_id": win,
                                     "filename": pkg.data["filename"],
                                     "url": pkg.data["url"]},
                      downloads=pkg.downloads)


def _parse_files_from_plan(plan_text: str):
    """从模型返回的代码计划文本中解析多个文件: "文件名\n```lang\ncode\n```" 或纯代码块。"""
    files = []
    blocks = utils.extract_code_blocks(plan_text)
    if blocks:
        # 尝试提取块前出现的文件名
        parts = re.split(r"```", plan_text)
        idx = 0
        for lang, code in blocks:
            # 在块之前找文件名
            before = parts[max(0, 2 * idx - 1)] if 2 * idx - 1 >= 0 else plan_text
            m = re.search(r"([\w\-./]+\.(?:py|js|ts|html|css|sh|json|txt|md))\s*[:：]?", before[-200:])
            name = m.group(1) if m else f"file_{idx}.{lang or 'txt'}"
            files.append((name, code))
            idx += 1
        if not files:
            files = [(f"main_{i}.{lang or 'py'}", code) for i, (lang, code) in enumerate(blocks)]
    else:
        m = re.search(r"```[\w]*\n(.*?)```", plan_text, flags=re.S)
        if m:
            files.append(("main.py", m.group(1)))
    return files


def _fallback_generate(requirement: str, language: str) -> list:
    """无模型可用时的兜底代码生成(内置模板)。"""
    if "计算器" in requirement or "calculator" in requirement.lower():
        # 生成 Web 界面计算器(HTML+JS 单文件), 可在浏览器直接打开 —— 项目为纯 Web 架构, 不使用桌面 GUI
        return [("calculator.html", _CALC_TPL)]
    if "爬虫" in requirement or "crawl" in requirement.lower():
        return [("crawler.py", _CRAWLER_TPL)]
    if "hello" in requirement.lower() or "你好" in requirement or "问候" in requirement:
        return [("main.py", _HELLO_TPL)]
    return [("main.py", _GENERIC_TPL.format(lang=language, req=requirement))]


_CALC_TPL = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEURA 计算器</title>
<style>
  :root { --bg:#0b1220; --panel:#101c30; --line:#1b3a5f; --cyan:#00d4ff; --text:#dceaff; }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:radial-gradient(1200px 600px at 50% -10%, #13233f 0%, var(--bg) 60%);
         font-family:"Segoe UI","Microsoft YaHei",sans-serif; }
  .calc { width:340px; background:linear-gradient(180deg,#101c30,#0b1526);
          border:1px solid var(--line); border-radius:18px; padding:22px;
          box-shadow:0 18px 50px rgba(0,0,0,.55), inset 0 0 0 1px rgba(0,212,255,.08); }
  .screen { background:#081120; border:1px solid var(--line); border-radius:12px;
            min-height:88px; display:flex; flex-direction:column; align-items:flex-end;
            justify-content:flex-end; padding:14px 18px; margin-bottom:16px;
            box-shadow:inset 0 4px 14px rgba(0,0,0,.5); }
  #expr { color:#7d93b8; font-size:16px; min-height:22px; word-break:break-all; }
  #result { color:var(--cyan); font-size:34px; font-weight:700; text-shadow:0 0 14px rgba(0,212,255,.45); }
  .keys { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
  .key { height:58px; border-radius:12px; border:1px solid var(--line); cursor:pointer;
         background:linear-gradient(180deg,#16263f,#101c30); color:var(--text);
         font-size:20px; font-weight:600; transition:.15s; user-select:none; }
  .key:hover { background:#1b3a5f; color:#fff; box-shadow:0 0 12px rgba(0,212,255,.25); }
  .key.op { color:var(--cyan); }
  .key.eq { background:linear-gradient(180deg,#00a8cc,#007ea6); color:#fff; border-color:#00d4ff; }
  .key.eq:hover { background:#00b8e0; }
  .key.clr { color:#ff8a8a; }
  @media (max-width:420px){ .calc{width:100%; margin:12px;} }
</style>
</head>
<body>
  <div class="calc">
    <div class="screen">
      <div id="expr">&nbsp;</div>
      <div id="result">0</div>
    </div>
    <div class="keys" id="keys"></div>
  </div>
<script>
  const BTNS = [["C","( )","%","/"],["7","8","9","*"],["4","5","6","-"],["1","2","3","+"],["0",".","⌫","="]];
  let expr = "", res = "0";
  const keysEl = document.getElementById("keys");
  BTNS.flat().forEach((b, i) => {
    const el = document.createElement("button");
    el.className = "key" + (["+","-","*","/","%"].includes(b) ? " op" : "")
                 + (b === "=" ? " eq" : "") + (b === "C" ? " clr" : "");
    el.textContent = b;
    el.addEventListener("click", () => press(b));
    keysEl.appendChild(el);
  });
  function render() {
    document.getElementById("expr").textContent = expr || "\\u00a0";
    document.getElementById("result").textContent = res;
  }
  function safeEval(s) {
    // 只允许数字、运算符、括号与小数点
    if (!/^[0-9.\\+\\-*\\/%() ]+$/.test(s)) return "错误";
    try {
      // 用 Function 构造仅做算术求值(输入已被严格过滤, 无法注入代码)
      const fn = new Function("return (" + s + ")");
      const v = fn();
      if (typeof v !== "number" || !isFinite(v)) return "错误";
      return String(Math.round(v * 1e10) / 1e10);
    } catch (e) { return "错误"; }
  }
  function press(b) {
    if (b === "C") { expr = ""; res = "0"; }
    else if (b === "⌫") { expr = expr.slice(0, -1); }
    else if (b === "=") { if (expr) res = safeEval(expr); }
    else {
      if (b === "(" && expr && /[0-9.)]$/.test(expr)) expr += "*";
      expr += b;
    }
    render();
  }
  document.addEventListener("keydown", (e) => {
    const k = e.key;
    if (/[0-9.\\+\\-*\\/%()]/.test(k)) press(k);
    else if (k === "Enter") press("=");
    else if (k === "Backspace") press("⌫");
    else if (k === "Escape") press("C");
  });
  render();
</script>
</body>
</html>
'''

_CRAWLER_TPL = '''# -*- coding: utf-8 -*-
"""示例爬虫: 抓取指定网页标题与正文前若干字 (仅演示, 遵守 robots.txt)"""
import urllib.request
import re
import sys

# 强制 UTF-8 输出(Windows GBK 控制台无法编码 Emoji/生僻字, 避免 UnicodeEncodeError)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def crawl(url: str, max_chars: int = 2000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "NeuraAgent/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    text = re.sub(r"<script[\\s\\S]*?</script>|<style[\\s\\S]*?</style>", "", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \\t]+", " ", text)
    text = re.sub(r"\\n{2,}", "\\n", text).strip()
    return text[:max_chars]


if __name__ == "__main__":
    print(crawl("https://www.baidu.com"))
'''

_HELLO_TPL = '''# -*- coding: utf-8 -*-
"""NEURA 生成的问候程序"""
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "朋友"
    print(f"你好, {name}!")
    print(f"现在是 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("程序运行成功 ✅")


if __name__ == "__main__":
    main()
'''

_GENERIC_TPL = '''# -*- coding: utf-8 -*-
"""NEURA 生成: {req}"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    print("项目已就绪。请告诉 NEURA 更具体的要求以完善功能。")


if __name__ == "__main__":
    main()
'''


def register(registry: ToolRegistry):
    registry.register(Tool(
        name="write_code_file",
        description="在工作区写入一个代码/文本文件(相对路径, 自动放在会话工作区)。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区路径, 如 app/main.py"},
                "content": {"type": "string"},
                "language": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        handler=_write_code_file,
        category="code",
    ))
    registry.register(Tool(
        name="run_code",
        description="在沙箱中运行代码文件(python/js/html/shell)。输出会流式显示到控制板 process 窗口。失败时返回错误供自动调试。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "工作区相对路径"},
                "code": {"type": "string", "description": "或直接给代码内容(不传path时)"},
                "language": {"type": "string", "enum": ["python", "js", "javascript", "html", "shell", "bash"]},
                "timeout": {"type": "integer"},
                "window_id": {"type": "string"},
            },
        },
        handler=_run_code,
        category="code",
    ))
    registry.register(Tool(
        name="code_task",
        description="一键编程: 给定需求与语言, 自动生成代码 -> 写入工作区 -> 运行 -> (失败自动调试) -> 提示打包下载。",
        parameters={
            "type": "object",
            "properties": {
                "requirement": {"type": "string"},
                "language": {"type": "string", "enum": ["python", "js", "javascript", "html", "shell"]},
            },
            "required": ["requirement"],
        },
        handler=_code_task,
        category="code",
    ))
    registry.register(Tool(
        name="package_project",
        description="将工作区中的文件打包(多文件自动ZIP)并提供下载链接。用户要求下载代码时调用。",
        parameters={
            "type": "object",
            "properties": {
                "paths": {"description": "工作区相对路径列表(可省略, 默认全部)", "items": {"type": "string"}},
                "name": {"type": "string"},
            },
        },
        handler=_package_project,
        category="code",
    ))
