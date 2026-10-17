# -*- coding: utf-8 -*-
"""
NEURA 大脑 (Brain)
四种模型后端, 通过 config.ai.use_builtin_model 与 use_deepseek_api 互斥切换:
  - DeepSeek API 模式(use_builtin_model=false): 把 Agent 的完整调用集(tool schemas)
    发送给 DeepSeek, model=deepseek-chat(非推理模型, 即"关闭深度思考"),
    流式接收文本与工具调用, 每个工具结果实时回传给 DeepSeek。
  - 内置独家大模型 NeuraLM(use_builtin_model=true, backend=nlm, 默认):
    项目从零自研、纯 NumPy 训练的 Transformer 语言模型(项目自带权重,
    不依赖任何外部模型/服务), 负责自然语言生成; 意图解析由小 AI 完成。
  - Ollama 内置大模型(use_builtin_model=true, backend=ollama): 连接本地 Ollama,
    使用互联网预训练大模型(qwen/llama 等), 支持流式与工具调用。
  - OpenAI 兼容本地大模型(use_builtin_model=true, backend=openai_compatible):
    连接 LM Studio / vLLM / 任意本地 OpenAI 兼容服务, 即"内置的大模型 AI"。
  - 内置引擎 NeuraBrain(零依赖兜底): MiniAI 理解意图 + 多轮上下文 + 多工具计划,
    离线可用, 覆盖全部功能链路。
"""
import json
import re

from agent import utils
from agent.mini_ai import MiniAI

try:
    import httpx
    _HAS_HTTPX = True
except Exception:
    _HAS_HTTPX = False


class ModelResult:
    def __init__(self, text: str | None, tool_calls: list | None, raw=None):
        self.text = text
        self.tool_calls = tool_calls or []   # [{"id","name","arguments":dict}]
        self.raw = raw


class ModelUnavailable(Exception):
    pass


class _StreamParser:
    """SSE / NDJSON 流解析: 按行喂入, 回调事件。"""

    def __init__(self):
        self.buf = ""

    def feed(self, chunk: str, on_data):
        self.buf += chunk
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                payload = line[5:].strip()
            else:
                payload = line
            if payload == "[DONE]":
                on_data(None, done=True)
                continue
            try:
                obj = json.loads(payload)
                on_data(obj, done=False)
            except Exception:
                continue


def _extract_tool_calls_from_message(msg: dict) -> list:
    """从一条 assistant 消息中提取完整 tool_calls(适配 OpenAI/DeepSeek 风格)。"""
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {"_raw": fn.get("arguments", "")}
        out.append({"id": tc.get("id") or f"call_{len(out)}",
                    "name": fn.get("name", ""), "arguments": args})
    return out


class DeepSeekModel:
    """DeepSeek API 客户端 (OpenAI 兼容, 流式)。"""

    def __init__(self, cfg_api: dict, cfg_agent: dict, registry, mini_ai: MiniAI):
        self.api = cfg_api
        self.agent_cfg = cfg_agent
        self.registry = registry
        self.mini_ai = mini_ai

    @property
    def available(self) -> bool:
        return _HAS_HTTPX and bool(self.api.get("deepseek_api_key"))

    async def chat(self, messages: list, tools: list | None, on_text=None,
                   on_tool_calls=None) -> ModelResult:
        if not self.available:
            raise ModelUnavailable("DeepSeek API Key 未配置")
        self._acc_text = []
        self._acc_tool_calls = []
        from agent.utils import normalize_messages_for_api
        messages = normalize_messages_for_api(messages)
        url = self.api.get("base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        payload = {
            "model": self.api.get("model", "deepseek-chat"),
            "messages": messages,
            "stream": True,
            "max_tokens": max(1, min(int(self.api.get("max_tokens", 4096) or 4096), 4096)),
            "temperature": self.api.get("temperature", 0.6),
        }
        if self.api.get("disable_reasoning", True):
            # 使用 deepseek-chat(非推理模型)= 关闭深度思考; 并显式要求不输出推理内容
            payload["system_prompt"] = None  # 占位, 由 system message 承担
        if tools:
            # 发送前校验: 剔除不合规 schema(如小AI 动态合成缺 parameters.type), 避免整体 400
            valid_tools = []
            for _t in tools:
                try:
                    _f = _t.get("function", _t) if isinstance(_t, dict) else {}
                    _p = _f.get("parameters") if isinstance(_f, dict) else None
                    if not isinstance(_p, dict) or _p.get("type") != "object":
                        continue
                    valid_tools.append(_t)
                except Exception:
                    continue
            payload["tools"] = valid_tools or None
            payload["tool_choice"] = "auto"
        payload = {k: v for k, v in payload.items() if v is not None}
        headers = {"Authorization": f"Bearer {self.api['deepseek_api_key']}",
                   "Content-Type": "application/json"}
        try:
            transport = httpx.AsyncHTTPTransport(retries=1)
            async with httpx.AsyncClient(transport=transport, timeout=self.api.get("timeout", 120)) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        # 先读 body(httpx stream 模式下异常后流即关闭, 无法再读; 手动保真错误信息)
                        try:
                            raw = await resp.aread()
                            _body = raw.decode("utf-8", "replace")[:400]
                        except Exception:
                            _body = f"HTTP {resp.status_code}"
                        raise ModelUnavailable(f"DeepSeek API 错误: HTTP {resp.status_code}: {_body} "
                                               f"(请检查 config.json 的 ai.api 配置 model/max_tokens/api_key)")
                    parser = _StreamParser()
                    async for chunk in resp.aiter_text():
                        parser.feed(chunk, lambda obj, done: self._handle(obj, done,
                                                                          on_text, on_tool_calls))
        except httpx.TimeoutException:
            raise ModelUnavailable("DeepSeek API 请求超时")
        except ModelUnavailable:
            raise
        except Exception as e:
            raise ModelUnavailable(f"DeepSeek API 连接失败: {e}")
        text = "".join(self._acc_text)
        calls = []
        for tc in self._acc_tool_calls:
            try:
                args = json.loads(tc.get("arguments") or "{}")
            except Exception:
                args = {"_raw": tc.get("arguments", "")}
            if tc.get("name"):
                calls.append({"id": tc.get("id") or f"call_{len(calls)}",
                              "name": tc["name"], "arguments": args})
        if calls and on_tool_calls:
            on_tool_calls(calls)
        if text and on_text:
            on_text(text, final=True)
        return ModelResult(text=text or None, tool_calls=calls)

    def _handle(self, obj, done, on_text, on_tool_calls):
        if obj is None:
            return
        choices = obj.get("choices") or []
        if not choices:
            return
        delta = choices[0].get("delta") or {}
        if delta.get("reasoning_content") and not delta.get("content"):
            return  # 丢弃推理内容(关闭深度思考)
        if delta.get("content"):
            self._acc_text.append(delta["content"])
            if on_text:
                on_text(delta["content"], final=False)
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            while len(self._acc_tool_calls) <= idx:
                self._acc_tool_calls.append({"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                self._acc_tool_calls[idx]["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                self._acc_tool_calls[idx]["name"] = fn["name"]
            if fn.get("arguments"):
                self._acc_tool_calls[idx]["arguments"] += fn["arguments"]


class CustomAPIModel:
    """通用外部 AI 模型 API 接入(任意厂商):
    - format="openai": OpenAI 兼容端点(流式, 自动带工具)
    - format="template": 用户自定义请求模板(用户自己编写调用格式, 见 config 说明):
        request_template 为 JSON 模板字符串, 占位符:
          {messages} 对话消息数组(JSON)   {tools} 工具 schema 数组(JSON, 可省略)
          {model} 模型名                 {api_key} API Key
        response_text_path 点路径提取回答文本(如 "choices.0.message.content")
        response_tools_path 可选, 点路径提取 tool_calls 数组
    不做任何输出格式强约束: AI 按自己的方式回答; Agent 仅在 system 提示中说明如何调用工具。
    """

    def __init__(self, cfg_api: dict, cfg_agent: dict, registry, mini_ai: MiniAI):
        self.api = cfg_api
        self.agent_cfg = cfg_agent
        self.registry = registry
        self.mini_ai = mini_ai
        custom = (self.api or {}).get("custom") or {}
        self.fmt = str(custom.get("format") or "openai")
        self.base = str(custom.get("base_url") or "").strip()
        self.model_name = str(custom.get("model") or "").strip() or "custom-model"
        self.key = str(custom.get("api_key") or self.api.get("deepseek_api_key") or "").strip()
        self.template = str(custom.get("request_template") or "").strip()
        self.text_path = str(custom.get("response_text_path") or "choices.0.message.content")
        self.tools_path = str(custom.get("response_tools_path") or "").strip()
        self.stream = bool(custom.get("response_stream", True)) if self.fmt == "openai" else False

    @property
    def available(self) -> bool:
        if not _HAS_HTTPX:
            return False
        if self.fmt == "template":
            return bool(self.template and self.base)
        return bool(self.base and self.key)

    def _dget(self, obj, path: str):
        try:
            cur = obj
            for k in path.split("."):
                if isinstance(cur, dict):
                    cur = cur[k]
                elif isinstance(cur, list) and k.isdigit():
                    cur = cur[int(k)]
                else:
                    return None
            return cur
        except Exception:
            return None

    async def chat(self, messages: list, tools: list | None, on_text=None,
                   on_tool_calls=None) -> ModelResult:
        if not self.available:
            raise ModelUnavailable("自定义 API 未配置完整(base_url/model/api_key 或 request_template)")
        from agent.utils import normalize_messages_for_api
        messages = normalize_messages_for_api(messages)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if self.fmt == "template":
            # ---- 用户自定义模板模式(调用格式由用户编写) ----
            try:
                tpl = self.template
                body = (tpl.replace("{messages}", json.dumps(messages, ensure_ascii=False))
                           .replace("{tools}", json.dumps(tools or [], ensure_ascii=False))
                           .replace("{model}", json.dumps(self.model_name))
                           .replace("{api_key}", json.dumps(self.key)))
                payload = json.loads(body)
            except Exception as e:
                raise ModelUnavailable(f"自定义模板解析失败: {e}")
            try:
                transport = httpx.AsyncHTTPTransport(retries=1)
                async with httpx.AsyncClient(transport=transport,
                                             timeout=int(self.api.get("timeout", 120))) as client:
                    resp = await client.post(self.base, json=payload, headers=headers)
                    if resp.status_code >= 400:
                        raise ModelUnavailable(f"自定义 API 错误: HTTP {resp.status_code}: "
                                               f"{resp.text[:300]} (请检查 config 的 ai.api.custom)")
                    data = resp.json()
            except ModelUnavailable:
                raise
            except Exception as e:
                raise ModelUnavailable(f"自定义 API 请求失败: {e}")
            text = self._dget(data, self.text_path)
            calls = []
            if self.tools_path:
                raw_calls = self._dget(data, self.tools_path) or []
                if isinstance(raw_calls, list):
                    for i, c in enumerate(raw_calls):
                        if not isinstance(c, dict):
                            continue
                        fn = c.get("function") or {}
                        name = fn.get("name") or c.get("name") or ""
                        args_raw = fn.get("arguments") or c.get("arguments") or "{}"
                        try:
                            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                        except Exception:
                            args = {"_raw": str(args_raw)}
                        if name:
                            calls.append({"id": c.get("id") or f"call_{i}", "name": name, "arguments": args})
            if text:
                text = str(text)
                if on_text:
                    on_text(text, final=True)
            if calls and on_tool_calls:
                on_tool_calls(calls)
            return ModelResult(text=str(text) if text is not None else None, tool_calls=calls)
        # ---- OpenAI 兼容模式(流式) ----
        url = self.base.rstrip("/")
        if "/chat/completions" not in url:
            url += "/chat/completions"
        custom_cfg = self.api.get("custom") or {}
        try:
            _tmp = float(custom_cfg.get("temperature", self.api.get("temperature", 0.6)) or 0.6)
        except (TypeError, ValueError):
            _tmp = 0.6
        payload = {"model": self.model_name, "messages": messages, "stream": True,
                   "max_tokens": max(1, min(int(custom_cfg.get("max_tokens", self.api.get("max_tokens", 4096)) or 4096), 8192)),
                   "temperature": max(0.0, min(_tmp, 2.0))}
        _extra_headers = custom_cfg.get("headers") or {}
        if isinstance(_extra_headers, dict):
            for _hk, _hv in _extra_headers.items():
                headers[_hk] = str(_hv)
        if tools:
            valid = []
            for _t in tools:
                try:
                    _f = _t.get("function", _t) if isinstance(_t, dict) else {}
                    _p = _f.get("parameters") if isinstance(_f, dict) else None
                    if isinstance(_p, dict) and _p.get("type") == "object":
                        valid.append(_t)
                except Exception:
                    continue
            payload["tools"] = valid or None
            payload["tool_choice"] = "auto"
        payload = {k: v for k, v in payload.items() if v is not None}
        acc_text, acc_calls = [], []
        try:
            transport = httpx.AsyncHTTPTransport(retries=1)
            async with httpx.AsyncClient(transport=transport,
                                         timeout=int(self.api.get("timeout", 120))) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        try:
                            raw = await resp.aread()
                            _body = raw.decode("utf-8", "replace")[:300]
                        except Exception:
                            _body = f"HTTP {resp.status_code}"
                        raise ModelUnavailable(f"自定义 API 错误: HTTP {resp.status_code}: {_body}")
                    parser = _StreamParser()
                    async for chunk in resp.aiter_text():
                        parser.feed(chunk, lambda obj, done: self._handle(obj, done, acc_text, acc_calls))
        except ModelUnavailable:
            raise
        except Exception as e:
            raise ModelUnavailable(f"自定义 API 连接失败: {e}")
        text = "".join(acc_text)
        calls = []
        for tc in acc_calls:
            try:
                args = json.loads(tc.get("arguments") or "{}")
            except Exception:
                args = {"_raw": tc.get("arguments", "")}
            if tc.get("name"):
                calls.append({"id": tc.get("id") or f"call_{len(calls)}", "name": tc["name"], "arguments": args})
        if calls and on_tool_calls:
            on_tool_calls(calls)
        if text and on_text:
            on_text(text, final=True)
        return ModelResult(text=text or None, tool_calls=calls)

    def _handle(self, obj, done, acc_text, acc_calls):
        if obj is None:
            return
        choices = obj.get("choices") or []
        if not choices:
            return
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            acc_text.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            while len(acc_calls) <= idx:
                acc_calls.append({"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                acc_calls[idx]["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                acc_calls[idx]["name"] = fn["name"]
            if fn.get("arguments"):
                acc_calls[idx]["arguments"] += fn["arguments"]


class OllamaModel:
    """内置模型: 本地 Ollama (预训练大模型, 支持工具调用)。"""

    def __init__(self, cfg_builtin: dict, registry, mini_ai: MiniAI):
        self.cfg = cfg_builtin
        self.registry = registry
        self.mini_ai = mini_ai
        self._ready = None
        self._client = None

    @property
    def available(self) -> bool:
        if self._ready is None:
            self._ready = self._probe()
        return self._ready

    def _probe(self) -> bool:
        if not _HAS_HTTPX:
            return False
        try:
            base = self.cfg.get("ollama_base_url", "http://127.0.0.1:11434").rstrip("/")
            r = httpx.get(base + "/api/tags", timeout=3)
            if r.status_code != 200:
                return False
            names = [m.get("name", "") for m in r.json().get("models", [])]
            model = self.cfg.get("model_name", "qwen2.5:7b")
            if not any(model in n or n in model for n in names):
                print(f"[Ollama] 未找到模型 {model!r}, 已安装: {names[:5]}")
                return False
            return True
        except Exception:
            return False

    async def chat(self, messages: list, tools: list | None, on_text=None,
                   on_tool_calls=None) -> ModelResult:
        if not self.available:
            raise ModelUnavailable("Ollama 不可用")
        base = self.cfg.get("ollama_base_url", "http://127.0.0.1:11434").rstrip("/")
        from agent.utils import normalize_messages_for_api
        messages = normalize_messages_for_api(messages)
        payload = {
            "model": self.cfg.get("model_name"),
            "messages": messages,
            "stream": True,
            "options": {"temperature": 0.6, "num_predict": 4096},
        }
        if tools:
            payload["tools"] = tools
        import asyncio
        acc_text = []
        acc_calls = []
        try:
            transport = httpx.AsyncHTTPTransport(retries=1)
            async with httpx.AsyncClient(transport=transport, timeout=180) as client:
                async with client.stream("POST", base + "/api/chat", json=payload) as resp:
                    if resp.status_code >= 400:
                        try:
                            raw = await resp.aread()
                            _body = raw.decode("utf-8", "replace")[:300]
                        except Exception:
                            _body = f"HTTP {resp.status_code}"
                        raise ModelUnavailable(f"Ollama 错误: HTTP {resp.status_code}: {_body}")
                    parser = _StreamParser()
                    async for chunk in resp.aiter_text():
                        parser.feed(chunk, lambda obj, done: self._handle(obj, done, acc_text, acc_calls))
        except ModelUnavailable:
            raise
        except Exception as e:
            raise ModelUnavailable(f"Ollama 连接失败: {e}")
        calls = []
        for tc in acc_calls:
            fn = tc.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            calls.append({"id": tc.get("id") or f"call_{len(calls)}",
                          "name": fn.get("name", ""), "arguments": args or {}})
        text = "".join(acc_text)
        if text and on_text:
            on_text(text, final=True)
        if calls and on_tool_calls:
            on_tool_calls(calls)
        return ModelResult(text=text or None, tool_calls=calls)

    @staticmethod
    def _handle(obj, done, acc_text, acc_calls):
        if obj is None:
            return
        if obj.get("done"):
            return
        msg = obj.get("message") or {}
        if msg.get("content"):
            acc_text.append(msg["content"])
        if msg.get("tool_calls"):
            acc_calls.extend(msg["tool_calls"])


class OpenAICompatibleModel:
    """内置大模型: 任意本地 OpenAI 兼容服务(LM Studio / vLLM / OpenAI 等)。
    使"内置 AI 就是大模型 AI"——只要本机有兼容端点(如 LM Studio 加载的模型)。"""

    def __init__(self, cfg_builtin: dict, registry, mini_ai: MiniAI):
        self.cfg = cfg_builtin
        self.registry = registry
        self.mini_ai = mini_ai
        self._ready = None

    @property
    def available(self) -> bool:
        if self._ready is None:
            self._ready = self._probe()
        return self._ready

    def _probe(self) -> bool:
        if not _HAS_HTTPX:
            return False
        base = (self.cfg.get("openai_base_url") or self.cfg.get("ollama_base_url")
                or "http://127.0.0.1:1234/v1").rstrip("/")
        url = base + "/models"
        try:
            headers = {}
            if self.cfg.get("api_key"):
                headers["Authorization"] = f"Bearer {self.cfg['api_key']}"
            r = httpx.get(url, timeout=4, headers=headers)
            return r.status_code == 200
        except Exception:
            return False

    async def chat(self, messages: list, tools: list | None, on_text=None,
                   on_tool_calls=None) -> ModelResult:
        if not self.available:
            raise ModelUnavailable("本地 OpenAI 兼容服务不可用")
        base = (self.cfg.get("openai_base_url") or self.cfg.get("ollama_base_url")
                or "http://127.0.0.1:1234/v1").rstrip("/")
        payload = {
            "model": self.cfg.get("model_name", "local-model"),
            "messages": messages,
            "stream": True,
            "max_tokens": int(self.cfg.get("max_tokens", 4096)),
            "temperature": float(self.cfg.get("temperature", 0.6)),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {self.cfg['api_key']}"
        acc_text, acc_calls = [], []
        from agent.utils import normalize_messages_for_api
        messages = normalize_messages_for_api(messages)
        try:
            transport = httpx.AsyncHTTPTransport(retries=1)
            async with httpx.AsyncClient(transport=transport,
                                         timeout=int(self.cfg.get("timeout", 180))) as client:
                async with client.stream("POST", base + "/chat/completions",
                                         json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        try:
                            raw = await resp.aread()
                            _body = raw.decode("utf-8", "replace")[:300]
                        except Exception:
                            _body = f"HTTP {resp.status_code}"
                        raise ModelUnavailable(f"本地大模型错误: HTTP {resp.status_code}: {_body}")
                    parser = _StreamParser()
                    async for chunk in resp.aiter_text():
                        parser.feed(chunk, lambda obj, done: self._handle(
                            obj, done, acc_text, acc_calls))
        except ModelUnavailable:
            raise
        except Exception as e:
            raise ModelUnavailable(f"本地大模型连接失败: {e}")
        calls = []
        for tc in acc_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {"_raw": fn.get("arguments", "")}
            calls.append({"id": tc.get("id") or f"call_{len(calls)}",
                          "name": fn.get("name", ""), "arguments": args})
        text = "".join(acc_text)
        if text and on_text:
            on_text(text, final=True)
        if calls and on_tool_calls:
            on_tool_calls(calls)
        return ModelResult(text=text or None, tool_calls=calls)

    @staticmethod
    def _handle(obj, done, acc_text, acc_calls):
        if obj is None:
            return
        choices = obj.get("choices") or []
        if not choices:
            return
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            acc_text.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            while len(acc_calls) <= idx:
                acc_calls.append({"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                acc_calls[idx]["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                acc_calls[idx]["name"] = fn["name"]
            if fn.get("arguments"):
                acc_calls[idx]["arguments"] += fn["arguments"]


class EngineModel:
    """内置引擎 NeuraBrain(零依赖兜底):
    MiniAI 意图理解 + 多轮上下文 + 多工具计划 + 智能降级答复。"""

    def __init__(self, mini_ai: MiniAI, memory=None):
        self.mini_ai = mini_ai
        self.memory = memory
        # 将长期记忆注入小 AI(供 understand 参考用户偏好)
        if self.memory is not None:
            self.mini_ai.memory = self.memory

    @property
    def available(self) -> bool:
        return True

    # 复合操作连接词: "查看文件然后统计字数" -> 拆成两个工具计划
    _SPLIT = re.compile(r"(?:然后|接着|再|并且|同时|并且顺便|顺便)")
    _MAX_PLAN = 3

    @staticmethod
    def _recent_tools(messages: list, n: int = 6) -> list:
        names = []
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        names.append(fn["name"])
            if len(names) >= n:
                break
        return names

    async def chat(self, messages: list, tools: list | None, on_text=None,
                   on_tool_calls=None) -> ModelResult:
        if not messages:
            raise ModelUnavailable("空消息")
        last = messages[-1]

        # ---------- 用户消息回合: 意图 -> 调用集计划 ----------
        if last.get("role") == "user":
            text = str(last.get("content", "")).strip()
            recent = self._recent_tools(messages)
            intent = self.mini_ai.understand(text, {"recent_tools": recent})
            if intent.action == "blocked":
                answer = intent.text_answer or "该请求已被安全拦截。"
                self._emit_text(answer, on_text)
                return ModelResult(text=answer, tool_calls=[])
            if intent.tool_plan:
                plan = list(intent.tool_plan)
                # 多工具计划: "A 然后 B" -> 尽量补齐第二个操作
                if len(plan) == 1 and self._SPLIT.search(text):
                    rest = self._SPLIT.split(text)
                    if len(rest) >= 2:
                        for part in rest[1:]:
                            if len(plan) >= self._MAX_PLAN:
                                break
                            sub = self.mini_ai.understand(part.strip("，。 "), {"recent_tools": recent})
                            if sub.tool_plan and sub.action not in ("unknown", "blocked"):
                                for t in sub.tool_plan[:2]:
                                    plan.append(t)
                if on_tool_calls:
                    on_tool_calls(plan)
                return ModelResult(text=None, tool_calls=plan)
            # 对话连贯: 用户引用上一操作(如"再算一次")
            if recent and re.search(r"(再|又|接着|同样|还是|重复).{0,6}(来一次|一次|算|查|看一下|运行|执行)", text):
                name = recent[0]
                args = {}
                plan = [{"name": name, "arguments": args}]
                if on_tool_calls:
                    on_tool_calls(plan)
                return ModelResult(text=None, tool_calls=plan)
            answer = intent.text_answer
            if not answer:
                answer = self.mini_ai.respond_text(last)
            if answer:
                self._emit_text(answer, on_text)
            return ModelResult(text=answer, tool_calls=[])

        # ---------- 工具结果回合: 汇总为文本 ----------
        answer = self.mini_ai.respond_text(last)
        if answer:
            self._emit_text(answer, on_text)
        return ModelResult(text=answer, tool_calls=[])

    @staticmethod
    def _emit_text(answer: str, on_text):
        if not answer or not on_text:
            return
        # 流式模拟(按 6 字符片段, 带轻微呼吸感)
        for i in range(0, len(answer), 6):
            on_text(answer[i:i + 6], final=False)
        on_text(answer, final=True)


class NLMNeuraModel:
    """内置独家大模型后端(backend=nlm, 默认):
    项目从零自研的 NeuraLM —— 纯 NumPy 训练的 Transformer 语言模型,
    权重由内置中文语料训练而成, 不依赖任何外部模型与服务。

    职责划分(完整闭环):
      - 用户意图解析 / 调用集生成: 小 AI(MiniAI, 超强语言理解 + 最高修改权)
      - 自然语言生成: NeuraLM(独家大模型, 生成连贯中文回复)
      - 工具执行: Agent 调用集
      - 多轮上下文: 模型参考最近对话继续生成
    """

    def __init__(self, cfg_builtin: dict, registry, mini_ai: MiniAI, memory=None,
                 root: str = "."):
        self.cfg = cfg_builtin
        self.registry = registry
        self.mini_ai = mini_ai
        self.memory = memory
        self.nlm = None
        self._loaded = False
        self._load(root)

    @property
    def available(self) -> bool:
        return True  # 权重缺失时自动训练, 永远可用

    def _load(self, root: str):
        from agent.nlm import NeuraLM, ensure_model
        try:
            path = self.cfg.get("nlm_model_path") or None
            self.nlm = NeuraLM(root, model_path=path)
            self._loaded = True
        except Exception as e:
            print(f"[NeuraLM] 加载失败: {e} -> 降级到内置引擎(NeuraBrain)")
            self.nlm = None

    def _build_prompt(self, messages: list) -> str:
        """把最近对话组装成 NeuraLM 的续写提示(语料同分布)，以回答前缀结尾。"""
        parts = []
        for m in messages[-5:]:
            role = m.get("role", "")
            content = str(m.get("content", ""))
            if not content:
                continue
            if role == "user":
                parts.append(f"用户说：{content}")
            elif role == "assistant":
                parts.append(f"NEURA 回答：{content}")
            elif role == "tool":
                parts.append(f"工具结果：{content[:120]}")
            else:
                parts.append(content)
        prompt = "。".join(parts).strip("。")
        if not prompt:
            prompt = "用户说：你好"
        # 以回答前缀结尾, 引导模型续写 NEURA 的答复
        return prompt + "。NEURA 回答："

    async def chat(self, messages: list, tools: list | None, on_text=None,
                   on_tool_calls=None) -> ModelResult:
        if not messages:
            raise ModelUnavailable("空消息")
        last = messages[-1]

        # ---------- 用户消息回合: 小AI 意图 -> 调用集计划 ----------
        if last.get("role") == "user":
            text = str(last.get("content", "")).strip()
            recent = EngineModel._recent_tools(messages)
            intent = self.mini_ai.understand(text, {"recent_tools": recent})
            # 安全第一: 高危请求由小AI 拦截
            if intent.action == "blocked":
                answer = intent.text_answer or "该请求已被安全拦截。"
                self._emit(answer, on_text)
                return ModelResult(text=answer, tool_calls=[])
            # 有明确工具意图 -> 直接交给调用集执行(小AI 生成调用集)
            if intent.tool_plan:
                plan = list(intent.tool_plan)
                if len(plan) == 1 and EngineModel._SPLIT.search(text):
                    rest = EngineModel._SPLIT.split(text)
                    if len(rest) >= 2:
                        for part in rest[1:]:
                            if len(plan) >= EngineModel._MAX_PLAN:
                                break
                            sub = self.mini_ai.understand(part.strip("，。 "), {"recent_tools": recent})
                            if sub.tool_plan and sub.action not in ("unknown", "blocked"):
                                for t in sub.tool_plan[:2]:
                                    plan.append(t)
                if on_tool_calls:
                    on_tool_calls(plan)
                return ModelResult(text=None, tool_calls=plan)
            # "再查一次"等复用最近工具
            if recent and re.search(r"(再|又|接着|同样|还是|重复).{0,6}(来一次|一次|算|查|看一下|运行|执行)", text):
                plan = [{"name": recent[0], "arguments": {}}]
                if on_tool_calls:
                    on_tool_calls(plan)
                return ModelResult(text=None, tool_calls=plan)
            # 无工具意图 -> 文本答复(双层策略 + 质量门):
            #   操作类请求(哪怕意图落空)一律用确定性模板, 避免 NeuraLM 续写残缺语料造成答非所问;
            #   纯闲聊(问好/无实词)才交给独家大模型 NeuraLM 生成, 并过质量门兜底
            answer = intent.text_answer
            if not answer:
                last_text = str(last.get("content", ""))
                if self.mini_ai.looks_like_operation(last_text):
                    # 操作类(含"给QQ号…你好"这类内容里带闲聊词的): 确定性模板
                    answer = self.mini_ai.respond_text(last)
                elif self.mini_ai.looks_like_chitchat(last_text):
                    # 纯闲聊: 交给独家大模型 NeuraLM 生成(过质量门)
                    answer = self._generate_reply(messages)
                else:
                    # 未知意图: 确定性模板, 绝不交给模型续写残缺语料
                    answer = self.mini_ai.respond_text(last)
            if answer:
                self._emit(answer, on_text)
            return ModelResult(text=answer or "", tool_calls=[])

        # ---------- 工具结果回合: 确定性模板总结(不再由模型续写, 杜绝答非所问) ----------
        answer = self._summarize_tool_result(messages)
        if answer:
            self._emit(answer, on_text)
        return ModelResult(text=answer or "", tool_calls=[])

    @staticmethod
    def _safe_json(s: str):
        import json as _j
        s = str(s or "").strip()
        if not s:
            return None
        try:
            return _j.loads(s)
        except Exception:
            pass
        try:
            dec = _j.JSONDecoder()
            obj, _ = dec.raw_decode(s)
            return obj
        except Exception:
            return None

    # 工具结果 → 中文总结的确定性模板(按最近一次执行的工具名生成, 回答必与所问相关)
    def _summarize_tool_result(self, messages: list) -> str:
        # ---- 复合推理: 旅行类(天气 + 攻略搜索 两条工具结果) -> 整理信息 + 语言组织 + 建议 ----
        # 只取本轮工具链: 从末尾反向收集 tool 消息, 遇到 user/assistant 即停(避免复用旧轮搜索结果)
        recent_tools = []
        for m in reversed(messages):
            if m.get("role") in ("user", "assistant"):
                break
            if m.get("role") == "tool":
                recent_tools.append(m)
                if len(recent_tools) >= 6:
                    break
        t_names = [str(m.get("name", "")) for m in recent_tools]
        if "get_weather" in t_names and "web_search" in t_names:
            wdata = sdata = None
            for m in recent_tools:
                try:
                    d = json.loads(str(m.get("content", "")))
                except Exception:
                    continue
                if m.get("name") == "get_weather" and wdata is None and isinstance(d, dict):
                    wdata = d
                if m.get("name") == "web_search" and sdata is None and isinstance(d, dict):
                    sdata = d
            if wdata and sdata:
                cur = wdata.get("current") or {}
                daily = wdata.get("daily") or []
                city = str(cur.get("city") or "目的地")
                cond = str(cur.get("condition") or "多云")
                temp = cur.get("temperature")
                parts = [f"已为你整理 {city} 出行参考：当前 {cond}"
                         + (f"、{temp}℃" if temp is not None else "")]
                if cur.get("humidity") is not None:
                    parts.append(f"湿度{cur['humidity']}%")
                if cur.get("wind_speed") is not None:
                    parts.append(f"风速{cur['wind_speed']}km/h")
                if daily:
                    d0 = daily[0]
                    parts.append(f"今天 {d0.get('min','--')}~{d0.get('max','--')}℃（{d0.get('condition','')}）")
                    if len(daily) > 1:
                        d1 = daily[1]
                        parts.append(f"明天 {d1.get('min','--')}~{d1.get('max','--')}℃（{d1.get('condition','')}）")
                # 攻略要点(搜索结果标题前3条)
                tips = []
                for r in (sdata.get("results") or [])[:3]:
                    t = str(r.get("title", "")).strip()
                    if t:
                        tips.append(t)
                if tips:
                    parts.append("攻略要点：" + "；".join(tips[:3])[:180])
                # 智能建议(由天气数据推导, 不是硬编码)
                sugg = []
                try:
                    t0 = float(d0.get("min") or temp or 0) if daily else float(temp or 0)
                    t1 = float(d0.get("max") or temp or 0) if daily else float(temp or 0)
                    if "雨" in (d0.get("condition") or "") + (daily[1].get("condition", "") if len(daily) > 1 else ""):
                        sugg.append("建议携带雨具")
                    if t0 <= 12:
                        sugg.append("早晚偏凉，记得加件外套")
                    if t1 >= 30:
                        sugg.append("午后炎热，注意防晒补水")
                    if cur.get("wind_speed") is not None and float(cur.get("wind_speed")) >= 40:
                        sugg.append("风力较大，户外注意防风")
                except Exception:
                    pass
                if sugg:
                    parts.append("出行建议：" + "、".join(sugg))
                parts.append("详细预报与攻略已显示在左侧控制板窗口。")
                return "，".join(parts)
        # ---- 股票趋势: 真实行情数据 -> 控制板趋势图 + 数据推导建议(非硬编码) ----
        if "stock_chart" in t_names:
            d0 = None
            for m in recent_tools:
                d = self._safe_json(str(m.get("content", "")))
                if m.get("name") == "stock_chart" and isinstance(d, dict) and "closes" in d:
                    d0 = d
                    break
            if d0:
                name_s = str(d0.get("name") or "")
                last = d0.get("last"); chg = d0.get("change_pct")
                ma5 = d0.get("ma5"); ma20 = d0.get("ma20")
                high = d0.get("high"); low = d0.get("low")
                pts = d0.get("points") or []
                parts = [f"已为你绘制 {name_s} 真实行情趋势图（{d0.get('source','')}，近 {d0.get('count',0)} 个交易日，详见左侧控制板趋势图窗口）。"]
                if last is not None:
                    parts.append(f"最新收盘 {last}")
                if chg is not None:
                    parts.append(f"区间涨跌 {chg}%")
                if high is not None and low is not None:
                    parts.append(f"区间最高 {high} / 最低 {low}")
                # 趋势建议(由均线/涨跌数据推导)
                sugg = []
                if ma5 is not None and ma20 is not None:
                    if ma5 > ma20:
                        sugg.append("短期均线(MA5)位于长期均线(MA20)之上，近期走势偏强")
                    elif ma5 < ma20:
                        sugg.append("短期均线(MA5)位于长期均线(MA20)之下，近期走势偏弱")
                    else:
                        sugg.append("短期与长期均线接近，处于整理阶段")
                if chg is not None:
                    if chg >= 5:
                        sugg.append("区间涨幅明显，注意高位波动风险")
                    elif chg <= -5:
                        sugg.append("区间跌幅明显，关注低位企稳信号")
                    else:
                        sugg.append("区间波动温和")
                if len(pts) >= 5:
                    s5 = [p.get("close") for p in pts[-5:]]
                    if s5 and s5[-1] >= s5[0]:
                        sugg.append("近5日收盘整体上行")
                    else:
                        sugg.append("近5日收盘整体下行")
                if sugg:
                    parts.append("趋势解读：" + "；".join(sugg) + "。以上为数据面参考，不构成投资建议。")
                return "，".join(parts)
        # ---- 知识问答/联网检索: 搜索结果 -> 整理为与问题相关的回答 ----
        if "web_search" in t_names and "get_weather" not in t_names:
            sdata = None
            for m in recent_tools:
                d = self._safe_json(str(m.get("content", "")))
                if m.get("name") == "web_search" and isinstance(d, dict) and "results" in d:
                    sdata = d
                    break
            if sdata:
                qq = str(sdata.get("query", ""))
                res = (sdata.get("results") or [])[:4]
                if res:
                    parts = [f"已为你检索「{qq}」并整理如下（详见左侧控制板搜索结果窗口）："]
                    for r in res:
                        t = str(r.get("title", "")).strip()
                        sn = str(r.get("snippet", "")).strip()
                        u = str(r.get("url", ""))
                        if sn:
                            parts.append(f"· {t}：{sn[:110]}")
                        elif t:
                            parts.append(f"· {t}（{u[:60]}）")
                    return "\n".join(parts[:6])
        tool_name, content = None, ""
        for m in recent_tools:
            if m.get("role") == "tool":
                tool_name = str(m.get("name", ""))
                content = str(m.get("content", ""))
                break
        if not tool_name:
            return "操作已完成，结果已显示在左侧控制板。"
        try:
            data = json.loads(content) if content else {}
        except Exception:
            data = {}
        ok = data.get("ok", True) if isinstance(data, dict) else True
        if not ok:
            err = str(data.get("error", "") if isinstance(data, dict) else "").strip()
            err = re.sub(r"[。．.]$", "", err).strip()
            return f"「{tool_name}」未能完成：{err}。可换个说法再试一次。"
        if tool_name == "get_time":
            return (f"现在时间是 {data.get('date','')} {data.get('weekday','')} "
                    f"{data.get('time','')}（{data.get('timezone','')}）。")
        if tool_name == "get_weather":
            # 工具数据结构: {"current": {city,temperature,condition,...}, "daily": [...]}
            cur = data.get("current") or {}
            city = str(cur.get("city") or data.get("city") or "本地")
            weather = cur.get("condition") or "多云"
            temp = cur.get("temperature")
            feels = cur.get("feels_like")
            wind = cur.get("wind_speed")

            def _num(v):
                """None / "None" / 空 -> 占位符, 其余 -> 数值文本。"""
                if v is None or str(v).strip().lower() in ("", "none", "null"):
                    return None
                return str(v).strip()

            temp_n = _num(temp); feels_n = _num(feels); wind_n = _num(wind)
            temp_txt = f"{temp_n}℃" if temp_n is not None else "--℃"
            feels_txt = f"体感 {feels_n}℃" if feels_n is not None else ""
            wind_txt = f"风速 {wind_n}km/h" if wind_n is not None else ""
            parts = [f"已为你查询 {city} 今日天气：{weather} 温度 {temp_txt}"]
            if feels_txt: parts.append(feels_txt)
            if wind_txt: parts.append(wind_txt)
            return "，".join(parts) + "，详情见左侧控制板天气预报窗口。"
        if tool_name == "list_directory":
            path = data.get("path", "")
            count = data.get("count", 0)
            return f"已列出 {path} 下的 {count} 个文件与文件夹，详见左侧控制板文件浏览窗口。"
        if tool_name == "read_file":
            path = data.get("path", "")
            total = data.get("total_lines", 0)
            return f"已读取 {path}（共 {total} 行），内容预览见左侧控制板。"
        if tool_name == "calculator":
            return f"计算结果：{data.get('expression','')} = {data.get('result','')}。"
        if tool_name == "system_info":
            return "系统信息已查询完成，详见左侧控制板系统信息窗口。"
        if tool_name == "system_stats":
            return (f"CPU 利用率 {data.get('cpu_percent','--')}%，内存 {data.get('mem_percent','--')}%"
                    f"（{data.get('mem_mb',{}).get('used_mb','--')}/{data.get('mem_mb',{}).get('total_mb','--')}MB），"
                    f"磁盘 {data.get('disk_percent','--')}%（剩余 {data.get('disk_free_gb','--')}GB），"
                    f"采样于 {data.get('sampled_at','')}，详见左侧控制板。")
        if tool_name == "install_dependency":
            return f"依赖 {data.get('package','')} 已通过 pip 安装完成（进度过程见左侧控制板进度窗口）。"
        if tool_name == "install_package":
            st = data.get("steps") or []
            if data.get("installed"):
                return ("「" + str(data.get("package", "")) + "」安装成功" +
                        ("（已通过图形化安装向导联网下载并安装，步骤见左侧控制板）" if data.get("method") == "gui"
                         else "（命令安装完成，过程见左侧控制板进度窗口）"))
            return ("「" + str(data.get("package", "")) + "」未能自动完成安装：" + str(data.get("note", "")) +
                    "。步骤：" + "；".join(str(s)[:60] for s in st[-3:]))
        if tool_name == "draw_image":
            return ("已为你绘画「" + str(data.get("title", "")) + "」（SVG 图片已生成并显示在左侧控制板，文件已保存可下载）")
        if tool_name == "monitor_task":
            return ("实时监控已完成：共 " + str(data.get("rounds", 0)) + " 轮采样（目标：" + str(data.get("target", "")) +
                    "），实时数据已全程显示在左侧控制板命令窗口")
        if tool_name == "run_command":
            return "命令执行完成，输出已显示在左侧控制板命令窗口。"
        if tool_name in ("web_search", "search"):
            return "联网搜索完成，结果已显示在左侧控制板搜索窗口。"
        if tool_name == "fetch_webpage":
            return "网页内容已获取，详见左侧控制板。"
        if tool_name == "open_app":
            return f"已为你打开 {data.get('app','应用')}。"
        if tool_name == "panel_control":
            action = {"close": "关闭窗口", "close_all": "关闭所有窗口", "move": "移动窗口",
                      "focus": "聚焦窗口", "minimize": "最小化窗口", "restore": "还原窗口",
                      "maximize": "最大化窗口", "resize": "缩放窗口"}.get(str(data.get("action", "")), "窗口操作")
            return f"已执行：{action}。"
        if tool_name in ("package_download", "package_project"):
            return f"文件已打包并自动下载：{data.get('filename','')}。"
        if tool_name == "app_control":
            action = str(data.get("action", "") or "")
            app = data.get("app", "") or ""
            if data.get("launched") or action == "launch":
                return f"已启动软件 {app}，其界面已在左侧控制板软件窗口中渲染/展示。"
            if data.get("status") == "sent" or data.get("steps"):
                return f"已通过 {app} 发送消息（{data.get('to','') or '当前会话'}），操作步骤见左侧控制板。"
            if data.get("image_base64"):
                return f"已截取软件界面画面，显示在左侧控制板软件窗口中，可双击放大。"
            if action == "focus":
                return f"已聚焦软件 {app} 的窗口。"
            if action == "type_text":
                return f"已向软件输入文本：{str(data.get('typed','')).strip()[:40]}。"
            return f"软件操控已完成（{app or action}），见左侧控制板。"
        if tool_name == "kill_process":
            return f"已结束{data.get('killed','进程')}（管理员级操作，红色警示窗口）。"
        if tool_name in ("delete_file", "delete_folder"):
            return f"已删除 {data.get('deleted','')}（管理员级操作，红色警示窗口）。"
        if tool_name == "preview_file":
            pt = data.get("preview_type", "")
            p = data.get("path", "")
            if pt == "image":
                return f"已预览图片 {p}，显示在左侧控制板图片窗口，可双击放大。"
            if pt == "html":
                return f"已预览 HTML 页面 {p}，显示在左侧控制板。"
            return f"已预览文件 {p}，内容见左侧控制板。"
        if tool_name == "synthesize_tool":
            return f"小AI 已为你新增调用集「{data.get('name','')}」并通过安全审查，立即可用。"
        return f"「{tool_name}」执行完成，结果已显示在左侧控制板。"

    def _generate_reply(self, messages: list) -> str:
        if self.nlm is None:
            return self.mini_ai.respond_text(messages[-1])
        prompt = self._build_prompt(messages)
        reply = self.nlm.generate(prompt, max_new=34, temperature=0.5, top_k=40)
        # 提取回答段: 取最后一个"NEURA 回答/回"之后的部分
        last_marker = max(reply.rfind("NEURA 回答："), reply.rfind("NEURA 回："))
        if last_marker >= 0:
            reply = reply[last_marker:]
            for marker in ("NEURA 回答：", "NEURA 回："):
                reply = reply.replace(marker, "")
        # 鲁棒截断: 生成中若续写了下一轮"用户说/NEURA 回答/工具结果", 只保留回答本身
        m_cut = re.search(r"(?:用?户?说|NEURA\s*回?答?：|工具结果)", reply)
        if m_cut:
            reply = reply[:m_cut.start()]
        reply = reply.strip("。， ")
        # 质量门: 输出过短 / 无中文字符 / 出现明显残缺续写(截断语料片段) -> 回退模板
        if len(reply) < 4 or not re.search(r"[\u4e00-\u9fa5]{2,}", reply) \
                or re.search(r"(该目|大型的最新信|正在大型|目下的所有|先生成都是|联查的天气|最新信)", reply):
            reply = self.mini_ai.respond_text(messages[-1])
        return reply

    @staticmethod
    def _emit(answer: str, on_text):
        if not answer or not on_text:
            return
        for i in range(0, len(answer), 6):
            on_text(answer[i:i + 6], final=False)
        on_text(answer, final=True)


class Brain:
    """统一入口: 按配置选择后端(内置大模型 / DeepSeek API), 失败自动降级。"""

    def __init__(self, cfg: dict, registry, mini_ai: MiniAI, memory=None):
        self.cfg = cfg
        self.registry = registry
        self.mini_ai = mini_ai
        self.memory = memory
        from agent.utils import resolve_ai_mode
        # 独立开关收敛: 每个 API 单独一行开关(use_builtin_model / use_deepseek_api /
        # use_openai_api / use_doubao_api / use_yuanbao_api / use_custom_api),
        # 返回唯一模式; 【不修改用户显式开关】(仅收敛选择层)。
        self.mode = resolve_ai_mode(cfg)
        self.use_builtin = (self.mode == "builtin")
        self.use_deepseek = (self.mode == "deepseek")
        self.provider = "deepseek"
        self.custom_enabled = (self.mode == "custom")
        self.model = None
        self._fallbacks = []     # 连接失败自动切换的候选模型链
        self._init()

    def _init(self):
        """按收敛模式构建模型(含候选回退链): 选中 API 不可用/连接失败时
        自动按 启用开关顺序 回退到下一候选, 最后兜底内置独家大模型 NeuraLM。"""
        ai = self.cfg.get("ai", {})
        api_cfg = dict(ai.get("api", {}))
        providers = ai.get("providers") or {}
        custom = dict(api_cfg.get("custom") or {})
        agent_cfg = ai.get("agent", {})

        def make_deepseek():
            return DeepSeekModel(dict(api_cfg), agent_cfg, self.registry, self.mini_ai)

        def make_provider(p):
            pp = providers.get(p) or {}
            c = dict(api_cfg)
            cc = dict(custom)
            cc.update({
                "enabled": True, "provider_name": pp.get("name", p),
                "format": "openai", "base_url": pp.get("base_url", ""),
                "api_key": pp.get("api_key", ""), "model": pp.get("model", ""),
                "max_tokens": pp.get("max_tokens", 4096),
                "temperature": pp.get("temperature", 0.6),
            })
            c["custom"] = cc
            return CustomAPIModel(c, agent_cfg, self.registry, self.mini_ai)

        def make_custom():
            c = dict(api_cfg)
            cc = dict(custom)
            cc["enabled"] = True
            c["custom"] = cc
            return CustomAPIModel(c, agent_cfg, self.registry, self.mini_ai)

        def make_nlm():
            b = ai.get("builtin", {})
            root = self.cfg.get("paths", {}).get("root") or "."
            m = NLMNeuraModel(b, self.registry, self.mini_ai, self.memory, root=root)
            if m.nlm is None and b.get("auto_fallback_to_engine", True):
                m = EngineModel(self.mini_ai, self.memory)
            return m

        if self.use_builtin:
            b = ai.get("builtin", {})
            backend = b.get("backend", "nlm")
            if backend == "openai_compatible":
                self.model = OpenAICompatibleModel(b, self.registry, self.mini_ai)
                if not self.model.available and b.get("auto_fallback_to_engine", True):
                    print("[Brain] 本地大模型端点不可用 -> 降级到内置引擎(NeuraBrain)")
                    self.model = EngineModel(self.mini_ai, self.memory)
            elif backend == "ollama":
                self.model = OllamaModel(b, self.registry, self.mini_ai)
                if not self.model.available and b.get("auto_fallback_to_engine", True):
                    print("[Brain] Ollama 不可用 -> 降级到内置引擎(NeuraBrain)")
                    self.model = EngineModel(self.mini_ai, self.memory)
            elif backend == "nlm":
                self.model = make_nlm()
            else:
                self.model = EngineModel(self.mini_ai, self.memory)
            return

        # ---- 外部 API 模式: 按启用开关顺序构建候选链 ----
        # 注意: candidates 存"延迟工厂函数"(lambda), 实例化后才真正构建模型
        candidates = []
        mode = self.mode
        if mode in ("openai", "doubao", "yuanbao"):
            candidates.append((mode, lambda p=mode: make_provider(p)))
        elif mode == "custom":
            candidates.append(("custom", lambda: make_custom()))
        elif mode == "deepseek":
            candidates.append(("deepseek", lambda: make_deepseek()))
        # 用户还启用了其他 API 开关 -> 一并纳入回退候选(优先级按 resolve 顺序)
        order = ["deepseek", "openai", "doubao", "yuanbao", "custom"]
        for p in order:
            key = "use_custom_api" if p == "custom" else f"use_{p}_api"
            if p != mode and bool(self.cfg.get(key)):
                if p == "custom":
                    candidates.append(("custom", lambda: make_custom()))
                elif p == "deepseek":
                    candidates.append(("deepseek", lambda: make_deepseek()))
                else:
                    candidates.append((p, lambda p=p: make_provider(p)))
        # 兜底: 内置独家大模型 NeuraLM(始终可用)
        candidates.append(("builtin", lambda: make_nlm()))

        self.model = None
        self._fallbacks = []
        for name, factory in candidates:
            try:
                m = factory()
            except Exception as e:
                print(f"[Brain] 构建 {name} 模型失败: {e}")
                continue
            if getattr(m, "available", True):
                if self.model is None:
                    self.model = m
                    print(f"[Brain] 使用模型: {name} -> {m.__class__.__name__}")
                else:
                    self._fallbacks.append((name, m))
            else:
                print(f"[Brain] {name} 未完整配置/不可用 -> 跳过(加入回退候选)")
                self._fallbacks.append((name, m))
        if self.model is None:
            # 理论上不会发生(builtin 兜底始终可用)
            print("[Brain] 警告: 无可用模型, 使用内置引擎")
            self.model = EngineModel(self.mini_ai, self.memory)

    def mode_label(self) -> str:
        if isinstance(self.model, CustomAPIModel):
            c = self.cfg['ai']['api'].get('custom') or {}
            return f"自定义API({c.get('provider_name') or self.model.model_name})"
        if isinstance(self.model, DeepSeekModel):
            return f"DeepSeek({self.cfg['ai']['api'].get('model')})"
        if isinstance(self.model, OllamaModel):
            return f"Ollama({self.cfg['ai']['builtin'].get('model_name')})"
        if isinstance(self.model, OpenAICompatibleModel):
            return f"本地大模型({self.cfg['ai']['builtin'].get('model_name')})"
        if isinstance(self.model, NLMNeuraModel):
            return "内置独家大模型 NeuraLM"
        return "内置引擎 NeuraBrain"

    @staticmethod
    def _is_conn_error(e: Exception) -> bool:
        """连接类错误(端点不可达/超时/代理失败) -> 允许自动切换回退模型;
        400/401 等业务错误不切换(那是消息结构或密钥问题, 切换无益)。"""
        s = f"{type(e).__name__}: {e}".lower()
        conn_mark = ("connect", "connection", "connectionrefused", "timed out", "timeout",
                     "all connection attempts", "name resolution", "unreachable",
                     "network is unreachable", "failed to establish", "proxyerror",
                     "http2", "remote server", "errno 110", "ssl", "readerror",
                     "connectionreset")
        return any(mk in s for mk in conn_mark) and ("400" not in s and "401" not in s and "403" not in s)

    async def chat(self, messages: list, tools: list | None = None,
                   on_text=None, on_tool_calls=None) -> ModelResult:
        try:
            return await self.model.chat(messages, tools, on_text, on_tool_calls)
        except Exception as e:
            if self._is_conn_error(e) and self._fallbacks:
                name, fb = self._fallbacks.pop(0)
                print(f"[Brain] {self.model.__class__.__name__} 连接失败({str(e)[:80]}) "
                      f"-> 自动切换 {name}: {fb.__class__.__name__}")
                self.model = fb
                return await fb.chat(messages, tools, on_text, on_tool_calls)
            raise
