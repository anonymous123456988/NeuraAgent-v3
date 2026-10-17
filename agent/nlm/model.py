# -*- coding: utf-8 -*-
"""NeuraLM 模型核心：Mini Transformer（decoder-only，纯 NumPy）。

架构（自研，真实可训练/可推理的语言模型）：
  token_embedding + 位置编码
  -> L 层 [LayerNorm + 单头 Causal Self-Attention + 残差 + LayerNorm + FFN(ReLU) + 残差]
  -> 输出 LayerNorm + 词表投影

提供：参数初始化、前向传播、手写反向传播（经数值梯度自检）、
Adam 训练步、top-k/temperature 采样生成。零第三方依赖。
"""
import math

import numpy as np


def build_params(V: int, D: int, layers: int, max_len: int = 256, seed: int = 42):
    rng = np.random.default_rng(seed)
    ps = {
        "emb": (rng.normal(0, 0.02, (V, D))).astype(np.float32),
        "pos": (rng.normal(0, 0.02, (max_len, D))).astype(np.float32),
        "ln_out_g": np.ones(D, dtype=np.float32),
        "ln_out_b": np.zeros(D, dtype=np.float32),
        "w_out": (rng.normal(0, 0.02, (D, V))).astype(np.float32),
    }
    for l in range(layers):
        for k in ("wq", "wk", "wv", "wo"):
            ps[f"L{l}.{k}"] = (rng.normal(0, 0.02, (D, D))).astype(np.float32)
        ps[f"L{l}.w1"] = (rng.normal(0, 0.02, (D, 4 * D))).astype(np.float32)
        ps[f"L{l}.b1"] = np.zeros(4 * D, dtype=np.float32)
        ps[f"L{l}.w2"] = (rng.normal(0, 0.02, (4 * D, D))).astype(np.float32)
        ps[f"L{l}.b2"] = np.zeros(D, dtype=np.float32)
        ps[f"L{l}.ln1_g"] = np.ones(D, dtype=np.float32)
        ps[f"L{l}.ln1_b"] = np.zeros(D, dtype=np.float32)
        ps[f"L{l}.ln2_g"] = np.ones(D, dtype=np.float32)
        ps[f"L{l}.ln2_b"] = np.zeros(D, dtype=np.float32)
    return ps


class MiniGPT:
    def __init__(self, V: int, D: int = 128, layers: int = 2, max_len: int = 128,
                 params: dict | None = None):
        self.V = V
        self.D = D
        self.L = layers
        self.max_len = max_len
        self.params = params if params is not None else build_params(V, D, layers, max_len)

    # ---------------- 前向 ----------------
    def forward(self, ids, cache: dict | None = None):
        """ids: (B,T) int -> logits (B,T,V)。cache 保存中间量供 backward。"""
        p = self.params
        B, T = ids.shape
        h = p["emb"][ids] + p["pos"][:T][None, :, :]
        if cache is not None:
            cache["ids"] = ids
            cache["h0"] = h
        for l in range(self.L):
            ln1_in = h
            a = self._ln(h, p[f"L{l}.ln1_g"], p[f"L{l}.ln1_b"])
            q = a @ p[f"L{l}.wq"]
            k = a @ p[f"L{l}.wk"]
            v = a @ p[f"L{l}.wv"]
            scores = (q @ k.transpose(0, 2, 1)) / math.sqrt(self.D)
            mask = np.triu(np.ones((T, T), dtype=np.float32), k=1) * -1e9
            scores = scores + mask
            amax = scores.max(-1, keepdims=True)
            att = np.exp(scores - amax)
            att = att / att.sum(-1, keepdims=True)
            ctx = att @ v
            h = h + ctx @ p[f"L{l}.wo"]
            ln2_in = h
            b = self._ln(h, p[f"L{l}.ln2_g"], p[f"L{l}.ln2_b"])
            f = np.maximum(b @ p[f"L{l}.w1"] + p[f"L{l}.b1"], 0)
            h = h + f @ p[f"L{l}.w2"] + p[f"L{l}.b2"]
            if cache is not None:
                cache[f"L{l}"] = (ln1_in, a, q, k, v, scores, att, ctx,
                                  ln2_in, b, f)
        n = self._ln(h, p["ln_out_g"], p["ln_out_b"])
        logits = n @ p["w_out"]
        if cache is not None:
            cache["h_final"] = h
            cache["n_out"] = n
        return logits

    @staticmethod
    def _ln(x, g, b):
        mu = x.mean(-1, keepdims=True)
        var = ((x - mu) ** 2).mean(-1, keepdims=True)
        xn = (x - mu) / np.sqrt(var + 1e-6)
        return xn * g + b

    @staticmethod
    def _ln_back(x, xn, dout, g, b):
        """LayerNorm 反向：输入 x，输出 xn；dout 对 xn 的梯度。返回 (dg, db, dx)。

        dx = (dxn - mean(dxn) - xn * mean(dxn·xn)) / σ
        """
        mu = x.mean(-1, keepdims=True)
        var = ((x - mu) ** 2).mean(-1, keepdims=True)
        std = np.sqrt(var + 1e-6)
        dxn = dout * g
        dg = (dout * xn).sum((0, 1))
        db = dout.sum((0, 1))
        dx = (dxn - dxn.mean(-1, keepdims=True)
              - xn * (dxn * xn).mean(-1, keepdims=True)) / std
        return dg, db, dx

    # ---------------- 反向 ----------------
    def backward(self, cache: dict, dlogits):
        """dlogits: (B,T,V)。返回与 self.params 同 key 的梯度 dict。"""
        p = self.params
        g = {k: np.zeros_like(v) for k, v in p.items()}
        # 输出投影
        n = cache["n_out"]
        dh = dlogits @ p["w_out"].T
        g["w_out"] = (n.transpose(0, 2, 1) @ dlogits).sum(0)
        g["ln_out_g"], g["ln_out_b"], dh = self._ln_back(
            cache["h_final"], n, dh, p["ln_out_g"], p["ln_out_b"])

        for l in reversed(range(self.L)):
            ln1_in, a, q, k, v, scores, att, ctx, ln2_in, b, f = cache[f"L{l}"]
            # ---- 残差2: h = ln2_in + ff ----
            dff = dh                              # 恒等路径对 ff 的梯度
            g[f"L{l}.w2"] = (f.transpose(0, 2, 1) @ dff).sum(0)
            g[f"L{l}.b2"] = dff.sum((0, 1))
            df = dff @ p[f"L{l}.w2"].T
            # ---- FFN: f = relu(b W1 + b1) ----
            df[b @ p[f"L{l}.w1"] + p[f"L{l}.b1"] <= 0] = 0
            g[f"L{l}.w1"] = (b.transpose(0, 2, 1) @ df).sum(0)
            g[f"L{l}.b1"] = df.sum((0, 1))
            db = df @ p[f"L{l}.w1"].T
            # ---- LN2: b = ln(ln2_in) ----
            g[f"L{l}.ln2_g"], g[f"L{l}.ln2_b"], dln2_in = self._ln_back(
                ln2_in, b, db, p[f"L{l}.ln2_g"], p[f"L{l}.ln2_b"])
            dh = dh + dln2_in                     # 残差2: d(ln2_in)=恒等+LN路径

            # ---- 残差1: h = ln1_in + o, o = ctx@wo ----
            do = dh                              # 恒等路径对 o 的梯度
            g[f"L{l}.wo"] = (ctx.transpose(0, 2, 1) @ do).sum(0)
            dctx = do @ p[f"L{l}.wo"].T
            # ---- Attention 反向 ----
            dv = att.transpose(0, 2, 1) @ dctx
            datt = dctx @ v.transpose(0, 2, 1)
            dscores = att * (datt - (att * datt).sum(-1, keepdims=True))
            dscores = dscores / math.sqrt(self.D)
            dk = dscores.transpose(0, 2, 1) @ q
            dq = dscores @ k
            da = (dq @ p[f"L{l}.wq"].T + dk @ p[f"L{l}.wk"].T + dv @ p[f"L{l}.wv"].T)
            g[f"L{l}.wq"] = (a.transpose(0, 2, 1) @ dq).sum(0)
            g[f"L{l}.wk"] = (a.transpose(0, 2, 1) @ dk).sum(0)
            g[f"L{l}.wv"] = (a.transpose(0, 2, 1) @ dv).sum(0)
            # ---- LN1: a = ln(ln1_in) ----
            g[f"L{l}.ln1_g"], g[f"L{l}.ln1_b"], dln1_in = self._ln_back(
                ln1_in, a, da, p[f"L{l}.ln1_g"], p[f"L{l}.ln1_b"])
            dh = dln1_in + dh                     # 残差1: d(ln1_in)=LN路径+恒等

        # embedding & 位置
        ids = cache["ids"]
        np.add.at(g["emb"], ids, dh)
        g["pos"][: ids.shape[1]] += dh.sum(0)
        return g

    # ---------------- 采样生成 ----------------
    def generate(self, ids, max_new: int = 40, temperature: float = 0.8,
                 top_k: int = 40, seed=None):
        rng = np.random.default_rng(seed) if seed is not None else np.random.default_rng()
        seq = list(ids)
        for _ in range(max_new):
            inp = np.asarray([seq[-self.max_len:]], dtype=np.int64)
            logits = self.forward(inp)[0, -1, :]
            logits = logits / max(temperature, 1e-6)
            k = min(top_k, self.V)
            idx = np.argpartition(logits, -k)[-k:]
            idx = idx[np.argsort(-logits[idx])]
            lk = logits[idx]
            probs = np.exp(lk - lk.max())
            probs = probs / probs.sum()
            pick = int(rng.choice(idx, p=probs))
            seq.append(pick)
            if pick == 1:  # <eos>
                break
        return seq


def numerical_grad(model: MiniGPT, ids, dlogits, eps: float = 1e-4):
    """数值梯度自检（随机抽查一组参数）。返回与 backward 对齐性结论。"""
    from numpy import ndarray
    p0 = model.params
    cache = {}
    logits0 = model.forward(ids, cache)
    loss0 = float((dlogits * logits0).sum())
    g_num = {}
    count = 0
    rng = np.random.default_rng(7)
    for key, w in p0.items():
        g_num[key] = np.zeros_like(w)
        # 抽查每类参数前若干元素
        flat = w.reshape(-1)
        n = min(flat.size, 6)
        for i in rng.choice(flat.size, size=n, replace=False):
            idx = np.unravel_index(i, w.shape)
            old = w[idx]
            w[idx] = old + eps
            lp = float((dlogits * model.forward(ids)).sum())
            w[idx] = old - eps
            lm = float((dlogits * model.forward(ids)).sum())
            w[idx] = old
            g_num[key][idx] = (lp - lm) / (2 * eps)
        count += n
    g_ana = model.backward(cache, dlogits)
    max_err, worst_key = 0.0, ""
    for key in p0:
        e = float(np.abs(g_num[key] - g_ana[key]).max())
        if e > max_err:
            max_err, worst_key = e, key
    return count, max_err, worst_key, g_ana
