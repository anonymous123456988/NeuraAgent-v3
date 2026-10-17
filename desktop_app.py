# -*- coding: utf-8 -*-
"""NEURA 桌面端入口(v3.33 桌面版)。

启动: python desktop_app.py [--no-web]
 - 无控制板: 只有一个悬浮球(点击展开 HUD 对话框, 再点击收起);
 - Agent 的所有 HUD 弹窗(文件/天气/代码/进度/图片/进程/数据/密码验证)直接渲染到
   电脑屏幕上的原生透明 HUD 窗口(科技幽蓝; 管理员/错误为红色);
 - AI 可直接操控电脑: 命令性(run_command/run_code)+ 图形化(gui_control/screen_capture),
   AI 实时看到屏幕, 像人一样操作;
 - 彻底弃用调用集中转: AI 直接控制 Agent(无中间调用集)。

Web 服务器默认同端口可关: 桌面端独立内嵌 AgentCore, 不依赖 WebSocket。
"""
import asyncio
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from agent.core import AgentCore           # noqa: E402
from desktop.ui import NeuraDesktopUI      # noqa: E402


def _load_config() -> dict:
    path = os.path.join(ROOT, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    cfg = _load_config()
    print("=" * 60)
    print("  NEURA 桌面版 v3.33  (独家内置大模型 NeuraLM)")
    from agent.utils import resolve_ai_mode
    _m = resolve_ai_mode(cfg)
    _lbl = {"builtin": "内置独家大模型 NeuraLM", "deepseek": "DeepSeek API",
            "openai": "OpenAI API", "doubao": "豆包 API", "yuanbao": "元宝 API",
            "custom": "自定义 API"}.get(_m, _m)
    print(f"  模式: {_lbl}")
    print("  形态: 悬浮球 + HUD 原生弹窗(无控制板)")
    print("=" * 60)
    core = AgentCore(cfg, root=ROOT)
    ui = NeuraDesktopUI(core, cfg)
    try:
        import tkinter
    except Exception:
        print("需要 tkinter: 请安装 python3-tk 后重试")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        ui.run(loop)          # 单线程 pump: tkinter + asyncio 同线程
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
