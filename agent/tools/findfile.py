"""find_file 调用集: 在指定根目录(默认主目录)内按文件名查找文件。

跨平台 os.walk 实现, 自动跳过系统/缓存/隐藏大目录, 限时 20 秒、最多 60 条,
结果在控制板以文件浏览窗口展示。
"""
import os
import time

from .registry import Tool, ToolResult, ToolContext

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".cache", ".npm", ".venv",
    "AppData", "Windows", "System32", "Program Files", "Program Files (x86)",
    "$RECYCLE.BIN", "System Volume Information", "Library", ".Trash",
}


async def _find_file(args: dict, ctx: ToolContext) -> ToolResult:
    name = str(args.get("name", "")).strip()
    root = os.path.expanduser(str(args.get("root", "~")).strip() or "~")
    if not name:
        return ToolResult(ok=False, error="缺少 name(要查找的文件名)", error_type="invalid_args")
    if not os.path.isdir(root):
        root = os.path.expanduser("~")
    hits: list = []
    scanned = 0
    limit = int(args.get("limit", 60))
    start = time.time()
    for dirpath, dirnames, filenames in os.walk(root):
        if time.time() - start > 20:
            break
        scanned += 1
        for d in list(dirnames):
            if d in _SKIP_DIRS or d.startswith("."):
                dirnames.remove(d)
        for f in filenames:
            if name.lower() in f.lower():
                fp = os.path.join(dirpath, f)
                try:
                    size = os.path.getsize(fp)
                except OSError:
                    size = None
                hits.append({"path": fp, "name": f, "size": size})
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break
    limited = len(hits) >= limit
    return ToolResult(data={
        "name": name,
        "root": root,
        "files": hits,
        "count": len(hits),
        "scanned": scanned,
        "limited": limited,
        "timeout": time.time() - start >= 20,
    })


def register(r: "ToolRegistry") -> None:
    r.register(Tool(
        name="find_file",
        description=(
            "在宿主机的某个目录(默认主目录)内按文件名查找文件, 返回匹配的文件路径列表。"
            "适用: '查找所有名为run.py的文件'、'找出桌面的图片' 等。"
            "参数: name=文件名关键字(必填), root=起始目录(可选, 默认主目录), limit=最大结果数(可选, 默认60)。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要查找的文件名或关键字, 如 run.py / 报告"},
                "root": {"type": "string", "description": "起始目录, 如 ~ / C:\\\\ / /home, 默认主目录"},
                "limit": {"type": "integer", "description": "最多返回条数, 默认 60"},
            },
            "required": ["name"],
        },
        handler=_find_file,
    ))
