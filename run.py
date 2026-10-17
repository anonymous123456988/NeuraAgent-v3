#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NEURA Agent —— 入口脚本
用法:
  python run.py                      # 启动服务器(读取 config.json)
  python run.py --config my.json     # 指定配置文件
  python run.py --host 0.0.0.0 --port 9000
  python run.py --regression         # 只运行自改进回归测试, 不启动服务器
  python run.py --rollback 2         # 回滚自改进到版本 2
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def _write_config(path: str, cfg: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="NEURA Agent")
    parser.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--regression", action="store_true", help="运行回归测试")
    parser.add_argument("--rollback", type=int, default=None, help="回滚自改进到指定版本")
    parser.add_argument("--list-tools", action="store_true", help="列出全部调用集(内置+自定义)")
    parser.add_argument("--remove-tool", default=None, metavar="NAME", help="删除一个自定义调用集")
    args = parser.parse_args()

    if args.rollback is not None:
        from agent.self_improver import SelfImprover
        improver = SelfImprover(os.path.join(ROOT, "system", "improvements.json"),
                                os.path.join(ROOT, "system", "backups"))
        ok = improver.rollback(args.rollback)
        print("回滚成功" if ok else "回滚失败(版本不存在或备份缺失)")
        return

    if args.regression:
        import tests.regression as reg
        ok, report = reg.run_all()
        print(report)
        sys.exit(0 if ok else 1)

    if args.list_tools or args.remove_tool:
        cfg = load_config(args.config)
        from agent.tools.dynamic import CustomToolStore
        from agent.tools import build_registry
        reg = build_registry()
        store = CustomToolStore(os.path.join(ROOT, "system", "custom_tools"), reg, cfg)
        store.load_all()
        if args.remove_tool:
            if not reg.is_custom(args.remove_tool):
                print(f"错误: {args.remove_tool} 不是自定义调用集(内置调用集不可删除)")
                sys.exit(1)
            print("已删除" if store.remove(args.remove_tool) else "删除失败")
            return
        print(f"调用集总数: {len(reg.names())} (内置 {sum(1 for n in reg.names() if not reg.is_custom(n))} + 自定义 {len(store.list())})")
        for t in reg._tools.values():
            if t.hidden:
                continue
            mark = "[自定义]" if t.source == "custom" else "[内置]"
            print(f"  {mark} {t.name}: {t.description[:50]}")
        return

    cfg = load_config(args.config)
    if args.host:
        cfg["server"]["host"] = args.host
    if args.port:
        cfg["server"]["port"] = args.port

    # ---- AI 启用模式: 每个 API 单独一行开关, 收敛只选择不写回 ----
    # 顶层独立开关(唯一真源): use_builtin_model / use_deepseek_api / use_openai_api /
    # use_doubao_api / use_yuanbao_api / use_custom_api。多个开关同时为 true 时按优先级
    # (内置>DeepSeek>OpenAI>豆包>元宝>自定义)取一使用; 【不修改、不写回用户配置文件】。
    from agent.utils import resolve_ai_mode
    ai_cfg = cfg.setdefault("ai", {})
    _mode_now = resolve_ai_mode(cfg)
    _sw_keys = ("use_builtin_model", "use_deepseek_api", "use_openai_api",
                "use_doubao_api", "use_yuanbao_api", "use_custom_api")
    _active_sw = [k for k in _sw_keys if cfg.get(k)]
    if len(_active_sw) > 1:
        print(f"[提示] 检测到 {len(_active_sw)} 个 AI 启用开关同时为 true: {', '.join(_active_sw)}")
        print(f"       本次使用优先级最高的模式: {_mode_now} (配置不会被自动修改; 如需切换只保留对应开关即可)")
    _mode_lbl = {
        "builtin": "内置独家大模型 NeuraLM (自研/零外部依赖)",
        "deepseek": "DeepSeek API",
        "openai": "ChatGPT / OpenAI API",
        "doubao": "豆包 API (火山方舟)",
        "yuanbao": "腾讯元宝 API",
        "custom": "自定义 API",
    }.get(_mode_now, _mode_now)
    print("=" * 62)
    if _mode_now == "builtin":
        backend = ai_cfg.get("builtin", {}).get("backend", "nlm")
        if backend == "openai_compatible":
            print("  模型模式: 内置大模型 (OpenAI 兼容本地端点)")
        elif backend == "ollama":
            print("  模型模式: 内置大模型 (Ollama)")
        elif backend == "nlm":
            print("  模型模式: 内置独家大模型 NeuraLM (自研/零外部依赖)")
        else:
            print("  模型模式: 内置引擎 NeuraBrain")
    else:
        print(f"  模型模式: {_mode_lbl}")
    print("=" * 62)

    import uvicorn
    from server.main import create_app

    app = create_app(cfg, root=ROOT)
    print("=" * 62)
    print(f"  NEURA Agent 已启动: http://{cfg['server']['host']}:{cfg['server']['port']}")
    print(f"  模型模式: {_mode_lbl}")
    print("=" * 62)
    uvicorn.run(app, host=cfg["server"]["host"], port=cfg["server"]["port"], log_level="info")


if __name__ == "__main__":
    main()
