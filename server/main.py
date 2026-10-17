# -*- coding: utf-8 -*-
"""NEURA 服务器: FastAPI + WebSocket + 下载接口 + 静态页面。"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent.core import AgentCore
from server.emitter import PanelEmitter


def _config_summary(cfg: dict) -> dict:
    api = cfg.get("ai", {}).get("api", {})
    key = api.get("deepseek_api_key", "")
    masked = (key[:6] + "***" + key[-4:]) if len(key) > 10 else ("已配置" if key else "未配置")
    builtin = cfg.get("ai", {}).get("builtin", {})
    use_builtin = bool(cfg.get("ai", {}).get("use_builtin_model", True))
    use_deepseek = bool(cfg.get("ai", {}).get("use_deepseek_api", not use_builtin))
    return {
        "mode": "builtin" if use_builtin else "api",
        "use_builtin_model": use_builtin,
        "use_deepseek_api": use_deepseek,
        "builtin_backend": builtin.get("backend", "nlm"),
        "builtin_model": builtin.get("model_name", ""),
        "api_model": api.get("model", "deepseek-chat"),
        "api_key_masked": masked,
        "agent_name": cfg.get("agent", {}).get("name", "NEURA"),
    }


def create_app(cfg: dict, root: str = ROOT):
    app = FastAPI(title="NEURA Agent", version="1.0.0")
    app.state.core = AgentCore(cfg, root=root)
    app.state.cfg = cfg

    @app.get("/api/health")
    async def health():
        core: AgentCore = app.state.core
        return {"ok": True, "mode": core.brain.mode_label(),
                "sessions": len(core.sessions.sessions)}

    @app.get("/api/config")
    async def get_config():
        return _config_summary(app.state.cfg)

    @app.get("/api/sessions")
    async def list_sessions():
        core: AgentCore = app.state.core
        return {"sessions": core.sessions_list()}

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        core: AgentCore = app.state.core
        ok = core.sessions.delete(sid)
        if not ok:
            raise HTTPException(404, "会话不存在")
        return {"ok": True}

    @app.post("/api/extensions/upload")
    async def upload_extension(request: Request):
        """Web 上传扩展包 zip(原始二进制) -> 自动安装并默认启用。"""
        import os as _os
        data = await request.body()
        if not data:
            return {"ok": False, "error": "空文件"}
        incoming = _os.path.join(root, "incoming_extensions")
        try:
            _os.makedirs(incoming, exist_ok=True)
        except Exception:
            pass
        path = _os.path.join(incoming, "upload_ext.zip")
        try:
            with open(path, "wb") as f:
                f.write(data)
        except Exception as e:
            return {"ok": False, "error": f"保存失败: {e}"}
        try:
            from agent import extensions as _ext
            res = _ext.install_extension(path, root)
        except Exception as e:
            return {"ok": False, "error": f"安装失败: {e}"}
        return res

    @app.get("/api/settings/model")
    async def get_model_settings():
        """当前模型配置(六开关 + 激活模式), 供手机版/网页设置面板。"""
        cfg = app.state.cfg or {}
        switches = ["use_builtin_model", "use_deepseek_api", "use_openai_api",
                    "use_doubao_api", "use_yuanbao_api", "use_custom_api"]
        mode = ""
        try:
            mode = app.state.core.brain.mode_label()
        except Exception:
            pass
        return {"ok": True, "switches": {k: bool(cfg.get(k)) for k in switches}, "mode": mode}

    @app.post("/api/settings/model")
    async def set_model_settings(request: Request):
        """设置 AI 模型(六开关互斥, 只保留一个 true), 写回 config.json 并热切换 brain。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        primary = str(body.get("primary") or "")
        switches = ["use_builtin_model", "use_deepseek_api", "use_openai_api",
                    "use_doubao_api", "use_yuanbao_api", "use_custom_api"]
        if primary not in switches:
            return {"ok": False, "error": f"无效的模型选项: {primary} (可选: {'/'.join(switches)})"}
        cfg_path = os.path.join(root, "config.json")
        try:
            data = json.load(open(cfg_path, encoding="utf-8"))
        except Exception:
            data = {}
        for k in switches:
            data[k] = (k == primary)
        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return {"ok": False, "error": f"写回 config.json 失败: {e}"}
        # 热切换 brain(无活动任务时立即生效)
        core = app.state.core
        core.cfg = data
        app.state.cfg = data
        try:
            from agent.brain import Brain
            core.brain = Brain(data, core.registry, core.mini_ai, core.memory)
            return {"ok": True, "mode": core.brain.mode_label(), "restart_required": False,
                    "message": f"已切换为: {core.brain.mode_label()}"}
        except Exception as e:
            return {"ok": True, "restart_required": True, "error": str(e),
                    "message": "配置已保存, 重启后生效"}

    @app.get("/api/extensions")
    async def list_extensions_api():
        """全部扩展列表(含已禁用), 供 Agent 页面管理。"""
        from agent import extensions as _ext
        return {"ok": True, "extensions": _ext.list_extensions(root, enabled_only=False)}

    @app.post("/api/extensions/{name}/toggle")
    async def toggle_extension_api(name: str, request: Request):
        from agent import extensions as _ext
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool(body.get("enabled", True))
        res = _ext.set_enabled(name, enabled, root)
        res["ok"] = res.get("ok", False)
        return res

    @app.delete("/api/extensions/{name}")
    async def delete_extension_api(name: str):
        from agent import extensions as _ext
        return _ext.uninstall_extension(name, root)

    @app.get("/api/extensions/{name}")
    async def extension_detail_api(name: str):
        """扩展详情: manifest + 使用方法 + 入口代码(供管理面板查看)。"""
        from agent import extensions as _ext
        info = _ext.read_extension(name, root, enabled_only=False)
        if info is None:
            return {"ok": False, "error": f"扩展不存在: {name}"}
        return {"ok": True, **info}

    @app.get("/api/tools")
    async def list_tools():
        """查看当前 Agent 的全部调用集(内置 + 小AI 动态合成)。"""
        core: AgentCore = app.state.core
        builtin = [{"name": t.name, "source": "builtin", "category": t.category}
                   for t in core.registry._tools.values() if not t.hidden and t.source == "builtin"]
        custom = core.custom_tools.list()
        return {"count": len(builtin) + len(custom), "builtin": builtin, "custom": custom}

    @app.delete("/api/tools/{name}")
    async def remove_tool(name: str):
        """删除一个小AI 动态合成的自定义调用集(内置调用集不可删)。"""
        core: AgentCore = app.state.core
        if not core.registry.is_custom(name):
            raise HTTPException(400, "只能删除自定义调用集(custom)")
        if not core.custom_tools.remove(name):
            raise HTTPException(404, "调用集不存在")
        return {"ok": True, "removed": name}

    @app.get("/api/downloads/{session_id}/{filename}")
    async def download(session_id: str, filename: str):
        core: AgentCore = app.state.core
        dl_dir = core.downloads_dir
        target = os.path.realpath(os.path.join(dl_dir, session_id, filename))
        if not target.startswith(os.path.realpath(dl_dir) + os.sep) or not os.path.exists(target):
            raise HTTPException(404, "文件不存在")
        return FileResponse(target, filename=os.path.basename(target))

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        core: AgentCore = app.state.core
        emitter = PanelEmitter(ws)
        tasks: dict = {}
        try:
            await emitter.hello(core.sessions_list(), _config_summary(app.state.cfg))
            while True:
                raw = await ws.receive_text()
                import json
                try:
                    msg = json.loads(raw)
                except Exception:
                    await emitter.error("消息格式错误")
                    continue
                mtype = msg.get("type")
                if mtype == "user_message":
                    sid = msg.get("session_id") or ""
                    text = str(msg.get("text", "")).strip()
                    if not text:
                        await emitter.error("消息为空")
                        continue
                    session = core.sessions.get(sid)
                    if session is None:
                        session = core.sessions.create()
                        sid = session.id
                        await emitter.session_created(session.to_dict())
                    if sid in tasks and not tasks[sid].done():
                        await emitter.error("该会话正在处理中，请等待完成")
                        continue
                    task = asyncio.create_task(
                        core.start_turn(sid, text, emitter),
                        name=f"turn_{sid}")
                    tasks[sid] = task

                    async def _cleanup(t, s=sid):
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass
                        except Exception as e:
                            import traceback
                            traceback.print_exc()
                            try:
                                await emitter.error(f"处理出错: {e}")
                            except Exception:
                                pass
                        finally:
                            tasks.pop(s, None)

                    asyncio.create_task(_cleanup(task))
                elif mtype == "cancel":
                    sid = msg.get("session_id") or ""
                    task = tasks.get(sid)
                    if task and not task.done():
                        task.cancel()
                        tasks.pop(sid, None)
                        await emitter.agent_status("idle", core.brain.mode_label())
                        # 广播收尾事件(空文本), 前端据此结束流式气泡, 避免半截卡住
                        await emitter.message_done(sid, "", [])
                        await emitter.notify("已取消当前任务", level="warning")
                    # 无任务在跑: 静默忽略, 不弹无意义提示
                elif mtype == "new_session":
                    session = core.sessions.create()
                    await emitter.session_created(session.to_dict())
                elif mtype == "delete_session":
                    sid = msg.get("session_id") or ""
                    if sid in tasks:
                        tasks[sid].cancel()
                    core.sessions.delete(sid)
                    await emitter.session_deleted(sid)
                elif mtype == "panel_action":
                    # 控制板用户点击交互: 打开文件/进入目录/刷新/执行命令
                    sid = msg.get("session_id") or ""
                    act = str(msg.get("action") or "")
                    params = msg.get("params") or {"path": msg.get("path") or "", "command": msg.get("command") or ""}
                    try:
                        await core.handle_panel_action(sid, act, params, emitter)
                    except Exception as e:
                        await emitter.error(f"控制板操作失败: {e}")
                elif mtype == "admin_auth":
                    # 用户提交系统登录密码 -> 验证通过则解锁挂起的高危操作
                    op_id = msg.get("operation_id") or ""
                    password = str(msg.get("password") or "")
                    if op_id:
                        await core.handle_admin_auth(op_id, password, emitter)
                elif mtype == "admin_cancel":
                    op_id = msg.get("operation_id") or ""
                    if op_id:
                        await core.handle_admin_cancel(op_id, emitter)
                elif mtype == "run_command_direct":
                    # 控制板实时命令窗口: 用户在控制板直接输入命令执行
                    sid = msg.get("session_id") or ""
                    cmd = str(msg.get("command", "")).strip()
                    if cmd:
                        asyncio.create_task(core.handle_command_direct(sid, cmd, emitter))
                elif mtype == "confirm_response":
                    # 用户对控制板确认窗口(如"是否安装依赖")的选择: yes 安装 / no 取消
                    cid = msg.get("confirm_id") or ""
                    yes = bool(msg.get("yes"))
                    if cid:
                        await core.handle_confirm(cid, yes, emitter)
        except WebSocketDisconnect:
            for t in tasks.values():
                if not t.done():
                    t.cancel()
            return

    # 静态资源
    web_dir = os.path.join(root, "web")
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(web_dir, "index.html"))

    @app.get("/mobile")
    async def mobile():
        return FileResponse(os.path.join(web_dir, "mobile.html"))

    @app.get("/{file_name}")
    async def mobile_assets(file_name: str):
        """手机版相对路径资源(兼容 Capacitor WebView 根路径): 仅允许 web 目录内的文件。"""
        import posixpath
        safe = posixpath.basename(file_name)
        p = os.path.join(web_dir, safe)
        if os.path.isfile(p):
            return FileResponse(p)
        raise HTTPException(status_code=404, detail="Not Found")

    return app
