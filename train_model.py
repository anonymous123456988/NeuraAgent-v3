# -*- coding: utf-8 -*-
"""NEURA 独家大模型 NeuraLM 训练入口。

用法:
  python train_model.py                    # 用内置语料训练(首次)或续训
  python train_model.py --steps 500        # 指定训练步数
  python train_model.py --corpus 我的语料.txt   # 追加自定义语料(在线自我改进)
  python train_model.py --d 192 --layers 3      # 升级模型规格(重新训练)

说明: NeuraLM 是项目从零自研的 Transformer 语言模型(纯 NumPy),
权重保存在 system/nlm/model.npz, 之后启动自动加载, 完全离线。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description="NeuraLM 独家大模型训练器")
    ap.add_argument("--steps", type=int, default=None, help="训练步数(默认 90)")
    ap.add_argument("--corpus", type=str, default=None, help="追加的自定义语料文件(txt)")
    ap.add_argument("--d", type=int, default=128, help="模型宽度(默认 128)")
    ap.add_argument("--layers", type=int, default=2, help="Transformer 层数(默认 2)")
    ap.add_argument("--resume", action="store_true", help="在既有权重上续训(默认自动判断)")
    args = ap.parse_args()

    from agent.nlm import NeuraLM, MODEL_PATH
    from agent.nlm.corpus import builtin_corpus

    root = "."
    extra = ""
    if args.corpus:
        if not os.path.exists(args.corpus):
            print(f"[NeuraLM] 语料文件不存在: {args.corpus}")
            sys.exit(1)
        with open(args.corpus, "r", encoding="utf-8", errors="replace") as f:
            extra = f.read()
        print(f"[NeuraLM] 已加载自定义语料: {len(extra)} 字符")

    model_path = os.path.join(root, MODEL_PATH)
    if args.resume or (args.steps is None and os.path.exists(model_path)):
        # 续训
        nlm = NeuraLM(root)
        nlm.continue_train(extra_corpus=extra, steps=args.steps or 90)
        print("[NeuraLM] 续训完成(在线自我改进)。")
    else:
        # 全新训练(可指定规格)
        if args.d != 128 or args.layers != 2:
            import numpy as np
            from agent.nlm import vocab
            from agent.nlm.train import train, save_model
            from agent.nlm.model import MiniGPT
            text = builtin_corpus() + extra
            print(f"[NeuraLM] 按新规格重新训练 (V={vocab.vocab_size()}, D={args.d}, L={args.layers})…")
            m = MiniGPT(vocab.vocab_size(), D=args.d, layers=args.layers, max_len=96)
            loss, secs = train(m, text, steps=args.steps or 90, seq_len=96,
                               batch_size=32, lr=3e-3)
            save_model(m, model_path, corpus_len=len(text))
            print(f"[NeuraLM] 训练完成 loss={loss:.4f} ({secs:.0f}s)。")
        else:
            nlm = NeuraLM(root)
            nlm.train_now(extra_corpus=extra, steps=args.steps or 90)
            print("[NeuraLM] 训练完成，权重已保存。")

    print(f"[NeuraLM] 权重位置: {model_path}")

    # 快速抽样验证
    nlm = NeuraLM(root)
    for p in ("现在几点了", "今天天气怎么样", "你是谁"):
        out = nlm.generate(p, max_new=28, temperature=0.6, seed=1)
        print(f"  「{p}」 -> {out[:40]!r}")


if __name__ == "__main__":
    main()
