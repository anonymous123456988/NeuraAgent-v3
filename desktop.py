# -*- coding: utf-8 -*-
"""NEURA 桌面端(desktop.py): 将 Web 界面包装为原生桌面应用。

特性:
  - 自动拉起内置 HTTP/WS 服务(同 run.py), 用 pywebview 原生窗口加载界面;
  - 无边框 HUD 窗口(frameless=True, 幽蓝科技风), 前端标题栏拖动(经 js_api 移动窗口);
  - js_api 系统桥: 桌面端可查询 CPU/内存/磁盘/电池/网络等完整系统状态并回显控制板;
  - 对话框悬浮球: 前端聊天面板可折叠为悬浮球(点击展开/缩回), 桌面端体验更佳;
  - 无 pywebview 环境时自动回退: 直接用系统浏览器打开 Web 版(功能不变)。

用法:
  python desktop.py            # 桌面端(推荐)
  python desktop.py --web      # 强制 Web 模式(浏览器打开)
  python desktop.py --port 8765
"""
import argparse
import json
import os
import platform
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_config(path: str, cfg: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def resolve_ai_mode(cfg: dict) -> str:
    """AI 启用选项独立开关(与 agent/utils.resolve_ai_mode 同语义, 桌面端独立实现)。

    每个 API 单独一行开关(全部在 config.json 最顶部): use_builtin_model /
    use_deepseek_api / use_openai_api / use_doubao_api / use_yuanbao_api / use_custom_api。
    多选一收敛按优先级(内置>DeepSeek>OpenAI>豆包>元宝>自定义)取一使用,
    【不修改用户其余开关, 不写回配置文件】; 兼容迁移旧 api_provider / custom_api_enabled。
    """
    old_provider = str(cfg.get("api_provider", ""))
    if old_provider in ("openai", "doubao", "yuanbao") and not cfg.get(f"use_{old_provider}_api"):
        cfg[f"use_{old_provider}_api"] = True
    if cfg.get("custom_api_enabled") and not cfg.get("use_custom_api"):
        cfg["use_custom_api"] = True
    cfg.pop("api_provider", None)
    cfg.pop("custom_api_enabled", None)
    order = [("builtin", "use_builtin_model"),
             ("deepseek", "use_deepseek_api"),
             ("openai", "use_openai_api"),
             ("doubao", "use_doubao_api"),
             ("yuanbao", "use_yuanbao_api"),
             ("custom", "use_custom_api")]
    active = [m for m, k in order if bool(cfg.get(k))]
    mode = active[0] if active else "builtin"
    providers = cfg.get("ai", {}).get("providers") or {}
    custom_cfg = (cfg.get("ai", {}).get("api") or {}).setdefault("custom", {})
    for p in providers:
        providers[p]["enabled"] = (p == mode)
    custom_cfg["enabled"] = (mode == "custom")
    return mode



def start_server(cfg: dict, root: str, log_cb=None):
    """后台线程启动 uvicorn; 返回是否成功。"""
    from server.main import create_app
    import uvicorn

    app = create_app(cfg, root=root)
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(cfg.get("server", {}).get("port", 8765))

    def _run():
        uvicorn.run(app, host=host, port=port, log_level="error")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # 等待端口就绪
    import socket
    for _ in range(60):
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except Exception:
            time.sleep(0.25)
    return False


def _open_browser(url: str) -> None:
    import webbrowser
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="NEURA 桌面端")
    parser.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--web", action="store_true", help="强制 Web 模式(浏览器打开)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.port:
        cfg["server"]["port"] = args.port
    mode = resolve_ai_mode(cfg)      # 只收敛选择, 不写回配置文件
    mode_lbl = {
        "builtin": "内置独家大模型 NeuraLM",
        "deepseek": "DeepSeek API",
        "openai": "ChatGPT / OpenAI API",
        "doubao": "豆包 API",
        "yuanbao": "腾讯元宝 API",
        "custom": "自定义 API",
    }.get(mode, mode)
    print("=" * 62)
    print("  NEURA 桌面端")
    print(f"  模型模式: {mode_lbl}")
    print("=" * 62)

    if not start_server(cfg, ROOT):
        print("[错误] 内置服务启动失败, 请检查端口占用: ", cfg["server"])
        sys.exit(1)

    host = cfg["server"]["host"]
    port = cfg["server"]["port"]
    url = f"http://{host}:{port}/"

    # Web 模式(强制或缺少 pywebview 环境): 浏览器打开
    try:
        import webview
    except Exception:
        print("[提示] 未安装 pywebview, 回退到浏览器 Web 模式。")
        print("       安装桌面端支持: pip install pywebview  (Windows 需 WebView2 Runtime, Win10/11 自带)")
        _open_browser(url)
        print(f"  NEURA 已在浏览器打开: {url}")
        return

    if args.web:
        _open_browser(url)
        return

    dcfg = cfg.get("desktop") or {}
    w, h = int(dcfg.get("width", 1280)), int(dcfg.get("height", 800))
    frameless = bool(dcfg.get("frameless", True))

    bridge = DesktopBridge()
    try:
        window = webview.create_window(
            "NEURA Agent — 桌面端",
            url=url,
            width=w,
            height=h,
            frameless=frameless,
            easy_drag=not frameless,   # frameless 时由前端 js_api 拖动
            js_api=bridge,
        )
        bridge._win = window
        webview.start()
    except Exception as e:
        print(f"[错误] 桌面窗口启动失败: {e}")
        print("       回退到浏览器 Web 模式…")
        _open_browser(url)


if __name__ == "__main__":
    main()
