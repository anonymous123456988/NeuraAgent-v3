# -*- coding: utf-8 -*-
"""NeuraLM —— NEURA 独家内置大模型（零外部依赖，纯 NumPy 自研）。

本项目自带的"完整闭环独家大模型"：
  - 不使用 Ollama / DeepSeek / LM Studio 等任何第三方模型与外部端点；
  - 架构为自研 Mini Transformer（decoder-only、causal self-attention）；
  - 权重由项目内置中文语料训练而成（首次启动自动训练并持久化），
    保存在 system/nlm/model.npz，之后启动即加载，完全离线闭环。
"""
from .engine import NeuraLM, ensure_model, MODEL_PATH, TRAINED_FLAG
from .model import MiniGPT, build_params

__all__ = ["NeuraLM", "ensure_model", "MiniGPT", "build_params", "MODEL_PATH", "TRAINED_FLAG"]
