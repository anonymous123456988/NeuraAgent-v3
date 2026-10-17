# -*- coding: utf-8 -*-
"""NEURA 内置模型一键部署(可选增强)。

自 v3.4 起, NEURA 默认已内置【独家大模型 NeuraLM】—— 项目从零自研、
纯 NumPy 训练、零外部依赖, 开箱即用(首次启动自动训练, 权重已随项目交付)。

本脚本用于【可选增强】: 若希望让内置模型使用更大规模的
互联网预训练大模型(如 qwen2.5), 可一键安装 Ollama 并拉取模型;
或直接使用 DeepSeek API(见 README)。

用法:
  python setup_model.py                    # 安装 Ollama 并拉取默认模型
  python setup_model.py --model qwen2.5:7b # 指定模型
  python setup_model.py --check            # 仅检查当前环境
"""
import json
import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")

DEFAULT_MODEL = "qwen2.5:1.5b"   # 轻量、普通电脑可跑
BIG_MODEL = "qwen2.5:7b"         # 更强, 需 8GB 内存/显存


def load_config() -> dict:
    with open(CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def has_ollama() -> bool:
    return shutil.which("ollama") is not None or os.path.exists(
        os.path.expanduser("~/.ollama/bin/ollama"))


def install_commands() -> str:
    """返回当前系统安装 Ollama 的官方命令。"""
    sysname = platform.system().lower()
    if sysname == "windows":
        return (
            "  打开 PowerShell(管理员)执行:\n"
            '    curl -fsSL https://ollama.com/install.ps1 | powershell'
        )
    if sysname == "darwin":
        return (
            "  macOS 执行:\n"
            "    curl -fsSL https://ollama.com/install.sh | sh\n"
            "  或安装 Homebrew 后:  brew install ollama"
        )
    return (
        "  Linux 执行:\n"
        "    curl -fsSL https://ollama.com/install.sh | sh\n"
        "  (安装后可能需要:  sudo systemctl start ollama 或重新登录终端)"
    )


def run(cmd: str) -> tuple:
    try:
        proc = subprocess.run(cmd, shell=True, timeout=1800,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.stdout.decode("utf-8", errors="replace")
        return proc.returncode, out
    except Exception as e:
        return -2, str(e)


def ensure_ollama(auto_install: bool = False) -> bool:
    if has_ollama():
        print("[OK] 检测到 Ollama: " + shutil.which("ollama") or "~/.ollama/bin/ollama")
        return True
    print("[!] 未检测到 Ollama —— 内置大模型需要它来运行(权重无法打包进代码)。")
    print("    官方安装命令如下:\n" + install_commands())
    if auto_install:
        print("\n[..] 正在自动执行安装命令 …")
        cmd = (install_commands().split("执行:\n")[1].strip()
               if "执行:\n" in install_commands() else install_commands())
        code, out = run(cmd)
        print(out[-2000:] if out else "")
        if code == 0 and has_ollama():
            print("[OK] Ollama 安装完成")
            return True
        print("[!] 自动安装未完成(可能需要管理员权限/手动执行)。请手动执行上面的命令后重跑本脚本。")
        return False
    return False


def pull_model(model: str) -> bool:
    print(f"\n[..] 正在拉取内置大模型 {model} (首次需下载, 视网速可能数分钟)…")
    code, out = run(f"ollama pull {model}")
    print(out[-2500:] if out else "")
    if code != 0:
        print(f"[!] 拉取失败。请检查网络后重试:  ollama pull {model}")
        return False
    print(f"[OK] 内置大模型 {model} 已就绪")
    return True


def main():
    args = [a for a in sys.argv[1:]]
    only_check = "--check" in args
    model = BIG_MODEL if "--model" in args and "7b" in " ".join(args) else DEFAULT_MODEL
    for i, a in enumerate(args):
        if a == "--model" and i + 1 < len(args):
            model = args[i + 1]

    print("=" * 60)
    print("  NEURA 内置大模型一键部署 (setup_model.py)")
    print("=" * 60)
    print("  系统:", platform.system(), platform.machine())
    print("  Python:", platform.python_version())

    ok = ensure_ollama(auto_install=not only_check)
    if only_check:
        print("\n[--check] 环境检查完成" + (" —— 可直接运行 python setup_model.py 部署" if ok else ""))
        return
    if not ok:
        print("\n部署中止: 请先安装 Ollama(命令见上方)或手动执行安装命令。")
        return

    if not pull_model(model):
        print("\n部署中止: 模型拉取失败。")
        return

    # 写回 config.json: 内置大模型后端 = ollama
    cfg = load_config()
    ai = cfg.setdefault("ai", {})
    # AI 启用开关唯一存在于 config.json 顶层(多选一互斥)
    cfg["use_builtin_model"] = True
    cfg["use_deepseek_api"] = False
    cfg["use_openai_api"] = False
    cfg["use_doubao_api"] = False
    cfg["use_yuanbao_api"] = False
    cfg["use_custom_api"] = False
    for _k in ("use_builtin_model", "use_deepseek_api", "use_openai_api", "use_doubao_api",
               "use_yuanbao_api", "use_custom_api", "api_provider", "custom_api_enabled", "mode"):
        ai.pop(_k, None)
    b = ai.setdefault("builtin", {})
    b["backend"] = "ollama"
    b["ollama_base_url"] = "http://127.0.0.1:11434"
    b["model_name"] = model
    b["auto_fallback_to_engine"] = True
    save_config(cfg)

    print("\n" + "=" * 60)
    print(f"  ✅ 内置大模型部署完成: Ollama / {model}")
    print("  config.json 已更新: use_builtin_model=true, use_deepseek_api=false")
    print("  现在运行 python run.py 并打开 http://127.0.0.1:8765 即可使用真正的内置大模型")
    print("  说明: 首次启动时模型加载需数秒; 若机器内存紧张可用 --model qwen2.5:0.5b")
    print("=" * 60)


if __name__ == "__main__":
    main()
