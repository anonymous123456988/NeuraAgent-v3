# -*- coding: utf-8 -*-
"""NEURA 扩展包系统(v3.42): 用户可上传扩展包(zip), AI 读取并直接使用。

扩展包 = 一个 zip, 内含:
  manifest.json   {"name": "myext", "title": "XX工具", "purpose": "用于XX任务", "enabled": true}
  usage.md        使用方法(给 AI 读)
  src/main.py     def run(args: dict) -> dict   入口函数(给 AI 调用)

规则:
- AI 只能使用【已启用】的扩展(enabled=true);
- AI 面对指令时先按"使用用途标题"检索匹配的已启用扩展(ext_list / ext_match),
  命中后再 ext_read 读取代码与使用方法, 最后 ext_run 执行;
- 安装时 Agent 自动把 zip 安全解压到 extensions/<name>/(拒绝路径穿越), 默认启用;
- 扩展代码在独立子进程运行(30s 超时), 由小AI 监管输出。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

_EXT_DIR_NAME = "extensions"
_MANIFEST = "manifest.json"
_USAGE = "usage.md"
_ENTRY = os.path.join("src", "main.py")


def _ext_dir(root: str | None = None) -> str:
    base = root or os.getcwd()
    d = os.path.join(base, _EXT_DIR_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _safe_name(name: str) -> str:
    """扩展名只允许字母数字_- 且不能为 . .."""
    name = re.sub(r"[^A-Za-z0-9_\-]", "", str(name or "").strip())
    return name[:40]


# ---------------- 查询 ----------------
def list_extensions(root: str | None = None, enabled_only: bool = False) -> list[dict]:
    """列出全部扩展(含启用状态)。enabled_only=True 只返回已启用。"""
    d = _ext_dir(root)
    out = []
    try:
        for name in sorted(os.listdir(d)):
            mf = os.path.join(d, name, _MANIFEST)
            if not os.path.isfile(mf):
                continue
            try:
                with open(mf, encoding="utf-8") as f:
                    m = json.load(f)
                m = m if isinstance(m, dict) else {}
            except Exception:
                continue
            enabled = bool(m.get("enabled", True))
            if enabled_only and not enabled:
                continue
            has_code = os.path.isfile(os.path.join(d, name, _ENTRY))
            has_usage = os.path.isfile(os.path.join(d, name, _USAGE))
            out.append({
                "name": m.get("name") or name,
                "title": m.get("title") or name,
                "purpose": m.get("purpose") or "",
                "enabled": enabled,
                "has_code": has_code,
                "has_usage": has_usage,
                "version": m.get("version", "1.0"),
            })
    except Exception:
        pass
    return out


def match_extension(query: str, root: str | None = None) -> list[dict]:
    """按用户指令检索【已启用】扩展: 匹配 title/purpose/name 关键词, 按相关度排序。"""
    q = str(query or "").strip()
    if not q:
        return list_extensions(root, enabled_only=True)
    toks = [t for t in re.split(r"[\s,，。、;；:：!！?？]+", q) if len(t) >= 2]
    scored = []
    for ext in list_extensions(root, enabled_only=True):
        hay = f"{ext.get('title','')} {ext.get('purpose','')} {ext.get('name','')}".lower()
        score = 0
        for t in toks:
            tl = t.lower()
            if tl in hay:
                score += 2
                continue
            # 滑窗子串匹配(2..6 字), 兼容整句无标点指令
            for n in range(6, 1, -1):
                hit = False
                for i in range(len(tl) - n + 1):
                    if tl[i:i + n] in hay:
                        score += 1
                        hit = True
                        break
                if hit:
                    break
        if score:
            scored.append((score, ext))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored]


def read_extension(name: str, root: str | None = None,
                  enabled_only: bool = True) -> dict | None:
    """读取指定扩展: manifest + 使用方法 + 代码内容。
    enabled_only=True(默认): 未启用/不存在返回 None(AI 只能读已启用);
    enabled_only=False: 管理面板读取任何扩展(含已禁用)。"""
    name = _safe_name(name)
    if not name:
        return None
    d = os.path.join(_ext_dir(root), name)
    mf = os.path.join(d, _MANIFEST)
    if not os.path.isfile(mf):
        return None
    try:
        with open(mf, encoding="utf-8") as f:
            m = json.load(f)
        m = m if isinstance(m, dict) else {}
    except Exception:
        m = {}
    if enabled_only and not m.get("enabled", True):
        return None
    usage = ""
    up = os.path.join(d, _USAGE)
    if os.path.isfile(up):
        try:
            usage = open(up, encoding="utf-8").read()[:8000]
        except Exception:
            usage = ""
    code = ""
    cp = os.path.join(d, _ENTRY)
    if os.path.isfile(cp):
        try:
            code = open(cp, encoding="utf-8").read()[:20000]
        except Exception:
            code = ""
    return {
        "name": m.get("name") or name,
        "title": m.get("title") or name,
        "purpose": m.get("purpose") or "",
        "version": m.get("version", "1.0"),
        "enabled": bool(m.get("enabled", True)),
        "usage": usage,
        "code": code,
        "entry": _ENTRY,
    }


# ---------------- 安装/启停 ----------------
def install_extension(zip_path: str, root: str | None = None) -> dict:
    """安装扩展包: 安全解压到 extensions/<name>/, 校验 manifest, 默认启用。"""
    if not zip_path or not os.path.isfile(zip_path):
        return {"ok": False, "error": f"扩展包不存在: {zip_path}"}
    d = _ext_dir(root)
    tmp = tempfile.mkdtemp(prefix="neura_ext_")
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            for info in z.infolist():
                # 防路径穿越
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    return {"ok": False, "error": f"非法路径: {info.filename}"}
            z.extractall(tmp)
        mf = os.path.join(tmp, _MANIFEST)
        if not os.path.isfile(mf):
            return {"ok": False, "error": "扩展包缺少 manifest.json(含 name/title/purpose/enabled)"}
        with open(mf, encoding="utf-8") as f:
            m = json.load(f)
        m = m if isinstance(m, dict) else {}
        name = _safe_name(m.get("name"))
        if not name:
            return {"ok": False, "error": "manifest.json 缺少合法 name"}
        if not os.path.isfile(os.path.join(tmp, _ENTRY)):
            return {"ok": False, "error": f"扩展包缺少入口 {_ENTRY} (需 def run(args)->dict)"}
        # 覆盖旧版本前备份
        target = os.path.join(d, name)
        if os.path.isdir(target):
            bak = os.path.join(d, f".{name}.bak")
            shutil.rmtree(bak, ignore_errors=True)
            shutil.move(target, bak)
        shutil.move(tmp, target)
        # 默认启用(manifest 未显式 false)
        m.setdefault("enabled", True)
        with open(os.path.join(target, _MANIFEST), "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        return {"ok": True, "name": name, "title": m.get("title") or name,
                "purpose": m.get("purpose") or "", "enabled": m.get("enabled", True)}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "不是有效的 zip 文件"}
    except Exception as e:
        return {"ok": False, "error": f"安装失败: {e}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def uninstall_extension(name: str, root: str | None = None) -> dict:
    """卸载(删除)扩展: 仅允许删除已安装扩展目录, 防路径穿越。"""
    d = os.path.join(_ext_dir(root), os.path.basename(str(name)))
    if not os.path.isdir(d):
        return {"ok": False, "error": f"扩展不存在: {name}"}
    try:
        import shutil
        shutil.rmtree(d)
        return {"ok": True, "name": name, "uninstalled": True}
    except Exception as e:
        return {"ok": False, "error": f"卸载失败: {e}"}


def set_enabled(name: str, enabled: bool, root: str | None = None) -> dict:
    """启用/禁用扩展。"""
    name = _safe_name(name)
    d = os.path.join(_ext_dir(root), name)
    mf = os.path.join(d, _MANIFEST)
    if not os.path.isfile(mf):
        return {"ok": False, "error": f"扩展不存在: {name}"}
    try:
        with open(mf, encoding="utf-8") as f:
            m = json.load(f)
        m = m if isinstance(m, dict) else {}
        m["enabled"] = bool(enabled)
        with open(mf, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        return {"ok": True, "name": name, "enabled": bool(enabled)}
    except Exception as e:
        return {"ok": False, "error": f"更新失败: {e}"}


# ---------------- 执行 ----------------
def run_extension(name: str, args: dict, root: str | None = None,
                  timeout: int = 30, python: str | None = None) -> dict:
    """在扩展目录中以独立子进程运行 src/main.py 的 run(args)。小AI 监管输出。"""
    name = _safe_name(name)
    d = os.path.join(_ext_dir(root), name)
    entry = os.path.join(d, _ENTRY)
    if not os.path.isfile(entry):
        return {"ok": False, "error": f"扩展 {name} 不存在或无入口代码"}
    py = python or sys.executable
    script = os.path.join(d, "neura_runner.py")
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "import json, sys, os, traceback\n"
                "sys.path.insert(0, os.getcwd())\n"
                "sys.path.insert(0, os.path.join(os.getcwd(), 'src'))\n"
                "try:\n"
                "    import main as _m\n"
                "    if not hasattr(_m, 'run'):\n"
                "        print(json.dumps({'ok': False, 'error': 'main.py 缺少 run(args) 函数'}, ensure_ascii=False)); sys.exit(0)\n"
                "    try:\n"
                "        r = _m.run(json.loads(sys.argv[1]))\n"
                "        print(json.dumps(r if isinstance(r, dict) else {'ok': True, 'result': r}, ensure_ascii=False))\n"
                "    except Exception:\n"
                "        print(json.dumps({'ok': False, 'error': traceback.format_exc()[-2000:]}, ensure_ascii=False))\n"
                "except Exception:\n"
                "    print(json.dumps({'ok': False, 'error': traceback.format_exc()[-2000:]}, ensure_ascii=False))\n"
            )
        p = subprocess.run(
            [py, script, json.dumps(args or {}, ensure_ascii=False)],
            cwd=d, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        try:
            res = json.loads(out)
            if isinstance(res, dict):
                res["stderr"] = err[-1500:]
                return res
        except Exception:
            pass
        return {"ok": p.returncode == 0, "result": out[-4000:], "stderr": err[-1500:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"扩展执行超时(>{timeout}s)"}
    except Exception as e:
        return {"ok": False, "error": f"执行失败: {e}"}
    finally:
        try:
            os.remove(script)
        except Exception:
            pass
