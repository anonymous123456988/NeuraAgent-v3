# -*- coding: utf-8 -*-
"""NeuraLM 推理引擎：加载/自动训练/文本生成，供 brain.py 作为内置大模型后端使用。"""
import os
import time

import numpy as np

from . import vocab
from .model import MiniGPT
from . import train as nlm_train
from .corpus import builtin_corpus

# 权重与训练标记位置（相对项目根）
MODEL_PATH = os.path.join("system", "nlm", "model.npz")
TRAINED_FLAG = os.path.join("system", "nlm", "trained.flag")

_DEFAULT_D = 128
_DEFAULT_L = 2
_DEFAULT_STEPS = 90
_DEFAULT_SEQ = 96
_DEFAULT_BATCH = 32


class NeuraLM:
    """独家内置大模型：首次使用自动训练，之后直接加载；支持在线续训。"""

    def __init__(self, root: str, model_path: str = None,
                 d: int = _DEFAULT_D, layers: int = _DEFAULT_L):
        self.root = root
        self.path = model_path or os.path.join(root, MODEL_PATH)
        self.d = d
        self.layers = layers
        self.model: MiniGPT | None = None
        self.trained_this_run = False
        self._load_or_train()

    # ---------------- 加载 / 训练 ----------------
    def _load_or_train(self):
        if os.path.exists(self.path):
            try:
                self.model = nlm_train.load_model(self.path)
                print(f"[NeuraLM] 已加载独家大模型权重: {self.path}")
                return
            except Exception as e:
                print(f"[NeuraLM] 权重加载失败({e})，将重新训练")
        self.train_now()

    def train_now(self, extra_corpus: str = "", steps: int = None):
        """用内置语料(+可选追加语料)训练，保存权重。"""
        text = builtin_corpus() + (extra_corpus or "")
        if len(text) < 200:
            text = "NEURA 你好 世界 时间 天气 文件 编程 搜索。\n" * 20 + text
        print(f"[NeuraLM] 首次使用，正在用内置语料训练独家大模型 "
              f"(V={vocab.vocab_size()}, D={self.d}, L={self.layers})…")
        t0 = time.time()
        m = MiniGPT(vocab.vocab_size(), D=self.d, layers=self.layers,
                    max_len=_DEFAULT_SEQ)
        steps = steps or _DEFAULT_STEPS
        loss, secs = nlm_train.train(m, text, steps=steps,
                                     seq_len=_DEFAULT_SEQ, batch_size=_DEFAULT_BATCH,
                                     out_path=self.path)
        self.model = m
        self.trained_this_run = True
        try:
            with open(os.path.join(os.path.dirname(self.path), TRAINED_FLAG),
                      "w", encoding="utf-8") as f:
                f.write(f"trained_at={int(time.time())}\nloss={loss:.4f}\n")
        except Exception:
            pass
        print(f"[NeuraLM] 训练完成 loss={loss:.4f}，耗时 {secs:.0f}s，"
              f"权重已保存: {self.path}")

    def continue_train(self, extra_corpus: str = "", steps: int = 20):
        """在线续训（自改进）：在当前权重基础上继续训练新语料。"""
        if self.model is None:
            self._load_or_train()
        text = builtin_corpus() + (extra_corpus or "")
        print(f"[NeuraLM] 在线续训 {steps} 步（自我提升）…")
        nlm_train.train(self.model, text, steps=steps,
                        seq_len=_DEFAULT_SEQ, batch_size=_DEFAULT_BATCH,
                        out_path=self.path)

    # ---------------- 生成 ----------------
    def generate(self, prompt: str, max_new: int = 48, temperature: float = 0.8,
                 top_k: int = 40, seed=None) -> str:
        if self.model is None:
            return ""
        ids = vocab.encode(prompt)
        if not ids:
            return ""
        # 预热词表（与训练一致）
        out_ids = self.model.generate(ids, max_new=max_new,
                                      temperature=temperature, top_k=top_k, seed=seed)
        return vocab.decode(out_ids[len(ids):]).strip()

    def mode_label(self) -> str:
        return f"内置独家大模型 NeuraLM (D{self.d}-L{self.layers})"


def ensure_model(root: str, force: bool = False) -> NeuraLM:
    """供 brain.py 调用：确保模型存在并返回实例。"""
    return NeuraLM(root)
