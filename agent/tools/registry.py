# -*- coding: utf-8 -*-
"""NEURA 工具调用集(调用集)注册器。

每个工具 = name + description + JSON Schema 参数 + 异步 handler。
registry.schemas() 生成 OpenAI/DeepSeek 兼容的 function calling 定义,
在 API 模式下完整发送给 DeepSeek; 在 Ollama/引擎模式下同样使用。

v2.0 新增:
  - search(): 语义/关键字检索调用集(供小AI判断"调用是否已存在")
  - source/version: 区分内置(builtin)与动态合成(custom)调用集
  - unregister(): 删除自定义调用集
"""
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
import difflib


@dataclass
class ToolResult:
    ok: bool = True
    data: Any = None
    error: str | None = None
    error_type: str | None = None   # not_found | forbidden | timeout | exec | network | invalid_args | unknown
    retryable: bool = False
    downloads: list = field(default_factory=list)  # [(filename, path)]
    admin: bool = False              # True=管理员级别操作(前端以红色警示窗口展示)
    message: str | None = None       # v3.48: 面向用户/模型的结果摘要(部分工具以 message= 传入)

    def to_model_content(self, max_len: int = 8000) -> str:
        import json
        if not self.ok:
            return json.dumps({"ok": False, "error": self.error, "error_type": self.error_type},
                              ensure_ascii=False)[:max_len]
        if self.message:
            try:
                payload = {"message": self.message, "data": self.data}
                return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:max_len]
            except Exception:
                pass
        try:
            return json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))[:max_len]
        except Exception:
            return str(self.data)[:max_len]


@dataclass
class ToolContext:
    session_id: str
    emitter: Any                 # PanelEmitter(server/emitter.py)
    workspace_root: str          # 会话代码工作区根目录
    downloads_dir: str           # 下载目录
    config: dict
    memory: Any = None
    mini_ai: Any = None
    improver: Any = None
    agent: Any = None            # 反向引用 AgentCore(供需要模型推理/动态合成的工具使用)
    extra: dict = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], Awaitable[ToolResult]]
    category: str = "general"
    hidden: bool = False
    source: str = "builtin"      # builtin | custom(小AI动态合成)
    version: int = 1

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def unregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def is_custom(self, name: str) -> bool:
        t = self._tools.get(name)
        return bool(t and t.source == "custom")

    def names(self) -> list:
        return [t.name for t in self._tools.values() if not t.hidden]

    def schemas(self) -> list:
        return [t.schema() for t in self._tools.values() if not t.hidden]

    def tool_list_text(self) -> str:
        lines = []
        for t in self._tools.values():
            if t.hidden:
                continue
            mark = "[自定义]" if t.source == "custom" else ""
            lines.append(f"- {t.name} {mark}: {t.description}")
        return "\n".join(lines)

    # ---------------------------------------------------------------
    # 检索(小AI 判断"调用集是否已存在"的核心)
    # ---------------------------------------------------------------
    def search(self, query: str, top: int = 3) -> list:
        """按名称/描述模糊检索调用集, 返回按相关度排序的工具列表。"""
        q = (query or "").lower().replace("_", " ").replace("-", " ")
        scored = []
        for t in self._tools.values():
            if t.hidden:
                continue
            name_l = t.name.lower().replace("_", " ").replace("-", " ")
            score = 0.0
            if q.strip() and q.strip() == name_l:
                score = 1.0
            elif q.strip() and (q.strip() in name_l or name_l in q.strip()):
                score = 0.85
            else:
                score = difflib.SequenceMatcher(None, q.strip(), name_l).ratio() * 0.5
                desc = t.description.lower().replace(" ", "")
                qk = q.replace(" ", "")
                for ch in set(qk):
                    if len(ch) >= 2 and ch in desc:
                        score += 0.12
                for w in q.split():
                    if len(w) >= 3 and w in t.description.lower():
                        score += 0.1
            if score >= 0.42:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:top]]

    async def invoke(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(ok=False, error=f"未知工具: {name}", error_type="invalid_args")
        try:
            return await tool.handler(args, ctx)
        except Exception as e:
            return ToolResult(ok=False, error=f"工具执行异常: {type(e).__name__}: {e}",
                              error_type="exec", retryable=True)
