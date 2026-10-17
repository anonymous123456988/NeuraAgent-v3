# -*- coding: utf-8 -*-
"""NeuraLM 训练器：内置语料 -> MiniGPT 预训练（Adam + 交叉熵），保存权重。

纯 NumPy，无任何第三方机器学习框架。支持：
  - 首次训练（从内置语料）
  - 续训（--resume 加载既有权重继续提升 = 模型的在线自我改进）
"""
import os
import time

import numpy as np

from . import vocab
from .model import MiniGPT


class Adam:
    def __init__(self, params: dict, lr: float = 3e-3, beta1: float = 0.9,
                 beta2: float = 0.999, eps: float = 1e-8):
        self.lr = lr
        self.b1, self.b2, self.eps = beta1, beta2, eps
        self.m = {k: np.zeros_like(v) for k, v in params.items()}
        self.v = {k: np.zeros_like(v) for k, v in params.items()}
        self.t = 0

    def step(self, params: dict, grads: dict):
        self.t += 1
        for k in params:
            self.m[k] = self.b1 * self.m[k] + (1 - self.b1) * grads[k]
            self.v[k] = self.b2 * self.v[k] + (1 - self.b2) * (grads[k] ** 2)
            mhat = self.m[k] / (1 - self.b1 ** self.t)
            vhat = self.v[k] / (1 - self.b2 ** self.t)
            params[k] -= self.lr * mhat / (np.sqrt(vhat) + self.eps)


def _make_batches(ids: np.ndarray, seq_len: int, batch_size: int, rng):
    """语料 ids -> (B, seq_len) 批次迭代器（随机起点）。"""
    n = len(ids)
    max_start = n - seq_len - 1
    while True:
        starts = rng.integers(0, max(1, max_start), size=batch_size)
        xs = np.stack([ids[s:s + seq_len] for s in starts]).astype(np.int64)
        ys = np.stack([ids[s + 1:s + seq_len + 1] for s in starts]).astype(np.int64)
        yield xs, ys


def train(model: MiniGPT, corpus_text: str, steps: int = 60, seq_len: int = 96,
          batch_size: int = 32, lr: float = 3e-3, log_every: int = 6,
          out_path: str = None, rng_seed: int = 0):
    """训练模型。返回 (平均loss, 训练秒数)。"""
    ids = np.asarray(vocab.encode(corpus_text), dtype=np.int64)
    if len(ids) < seq_len + 2:
        raise ValueError("语料过短，无法训练")
    opt = Adam(model.params, lr=lr)
    rng = np.random.default_rng(rng_seed)
    batches = _make_batches(ids, seq_len, batch_size, rng)
    t0 = time.time()
    losses = []
    for step in range(1, steps + 1):
        xs, ys = next(batches)
        cache = {}
        logits = model.forward(xs, cache)
        # 交叉熵(带标签平滑 0.0)
        logp = logits - logits.max(-1, keepdims=True)
        logp = logp - np.log(np.exp(logp).sum(-1, keepdims=True))
        loss = -np.mean(logp[np.arange(xs.shape[0])[:, None], np.arange(xs.shape[1])[None, :], ys])
        # softmax 概率用于梯度
        probs = np.exp(logp)
        dlogits = probs.copy()
        dlogits[np.arange(xs.shape[0])[:, None], np.arange(xs.shape[1])[None, :], ys] -= 1
        dlogits = dlogits / (xs.shape[0] * xs.shape[1])
        grads = model.backward(cache, dlogits)
        # 梯度裁剪
        total = 0.0
        for g in grads.values():
            total += float((g ** 2).sum())
        scale = min(1.0, 1.0 / (np.sqrt(total) + 1e-6))
        if scale < 1.0:
            for g in grads.values():
                g *= scale
        opt.step(model.params, grads)
        losses.append(float(loss))
        if step % log_every == 0 or step == steps:
            print(f"  [train] step {step}/{steps}  loss={loss:.4f}  "
                  f"({time.time() - t0:.0f}s)", flush=True)
    if out_path:
        save_model(model, out_path, corpus_len=len(ids))
    return float(np.mean(losses)), time.time() - t0


def save_model(model: MiniGPT, path: str, corpus_len: int = 0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(
        path,
        V=np.asarray(model.V, dtype=np.int32),
        D=np.asarray(model.D, dtype=np.int32),
        L=np.asarray(model.L, dtype=np.int32),
        max_len=np.asarray(model.max_len, dtype=np.int32),
        corpus_len=np.asarray(corpus_len, dtype=np.int64),
        trained_at=np.asarray(int(time.time()), dtype=np.int64),
        **model.params,
    )
    # 同时保存词表
    vocab_path = os.path.join(os.path.dirname(path), "vocab.json")
    vocab.save_vocab(vocab_path)


def load_model(path: str) -> MiniGPT:
    data = np.load(path, allow_pickle=False)
    m = MiniGPT(
        V=int(data["V"]), D=int(data["D"]), layers=int(data["L"]),
        max_len=int(data["max_len"]),
        params={k: data[k] for k in data.files if k not in
                ("V", "D", "L", "max_len", "corpus_len", "trained_at")},
    )
    return m
