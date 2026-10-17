# -*- coding: utf-8 -*-
"""
NEURA Agent 核心: 会话管理 + 工具调用循环 + 代码自动调试 + 自改进。
"""
import asyncio
import datetime
import json
import os
import re
import uuid

from agent import utils
from agent import security
from agent.brain import Brain, ModelResult, ModelUnavailable
from agent.memory import MemoryStore
from agent.mini_ai import MiniAI
from agent.self_improver import SelfImprover
from agent.tools import build_registry, ToolContext


class Session:
    def __init__(self, sid: str, title: str = "新会话", created_at: str | None = None):
        self.id = sid
        self.title = title
        self.created_at = created_at or utils.now_iso()
        self.history: list = []      # OpenAI 风格消息
        self.running = False
        self.lock = asyncio.Lock()
        self.turn_emitter = None      # 会话级推送器(并发安全: 每会话独立绑定)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "created_at": self.created_at,
                "history": self.history[-120:]}


def _fmt_info(name: str, data) -> str:
    """把 info 类工具结果格式化为面板文本。"""
    import json
    if data is None:
        return "操作完成"
    if isinstance(data, dict):
        if name == "get_time":
            return (f"日期: {data.get('date', '')}  {data.get('weekday', '')}\n"
                    f"时间: {data.get('time', '')}  时区: {data.get('timezone', '')}")
        if name == "calculator":
            return f"{data.get('expression', '')} = {data.get('result', '')}"
        if name == "write_file":
            return f"已写入 {data.get('path', '')} ({data.get('bytes', 0)} 字节)"
        if name == "create_folder":
            return f"已创建 {data.get('path', '')}"
        if name == "run_command":
            out = data.get('stdout', '') or data.get('stderr', '')
            return f"$ {data.get('command', '')}\n\n{out or '(无输出)'}"
        if name == "fetch_webpage":
            return f"来源: {data.get('url', '')}\n\n{data.get('text', '')[:3000]}"
        return "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}"
                         for k, v in data.items())
    return str(data)


class SessionManager:
    def __init__(self, path: str):
        self.path = path
        # v3.48 用户要求: 会话列表要崭新的, 不要任何历史堆积数据。
        # 启动时把会话存储重置为全新空状态, 所有历史会话清空, 从零开始;
        # 之后每个会话仅在用户真正发起对话时按需创建, 不再堆积。
        try:
            utils.write_json(path, {"sessions": []})
        except Exception:
            pass
        self.sessions: dict[str, Session] = {}

    def _save(self):
        utils.write_json(self.path, {"sessions": [s.to_dict() for s in self.sessions.values()]})

    def create(self) -> Session:
        sess = Session(uuid.uuid4().hex[:12])
        self.sessions[sess.id] = sess
        self._save()
        return sess

    def get(self, sid: str) -> Session | None:
        return self.sessions.get(sid)

    def list(self) -> list:
        return sorted(self.sessions.values(), key=lambda s: s.created_at, reverse=True)

    def delete(self, sid: str) -> bool:
        if sid in self.sessions:
            del self.sessions[sid]
            self._save()
            return True
        return False

    def update_title(self, sid: str, title: str):
        s = self.get(sid)
        if s:
            s.title = title[:30]
            self._save()


class AgentCore:
    def __init__(self, cfg: dict, root: str, emitter=None):
        self.cfg = cfg
        self.root = root
        self.emitter = emitter          # 由服务器注入: PanelEmitter
        self._fill_empty_dirs(os.path.join(root, "workspace"))   # 空文件夹补占位文件(无实际意义)
        self.registry = build_registry()
        self.memory = MemoryStore(os.path.join(root, "system", "memory_store.json"))
        self.mini_ai = MiniAI(self.memory, cfg)
        self.improver = SelfImprover(os.path.join(root, "system", "improvements.json"),
                                     os.path.join(root, "system", "backups"))
        self.brain = Brain(cfg, self.registry, self.mini_ai, self.memory)
        self.sessions = SessionManager(os.path.join(root, "system", "sessions.json"))
        self.workspace_root = os.path.join(root, "workspace", "sessions")
        self.downloads_dir = os.path.join(root, "server", "downloads")
        os.makedirs(self.workspace_root, exist_ok=True)
        os.makedirs(self.downloads_dir, exist_ok=True)
        # 动态调用集存储(小AI 工具工厂), 启动时加载历史自定义调用集
        from agent.tools.dynamic import CustomToolStore
        self.custom_tools = CustomToolStore(os.path.join(root, "system", "custom_tools"),
                                            self.registry, cfg)
        self.custom_tools.load_all()
        self._ctx_pool: dict[str, ToolContext] = {}
        # 高危操作解锁状态: operation_id -> {event, password, ts, session_id, cancelled}
        self._unlock_events: dict[str, dict] = {}
        # 依赖安装确认槽: confirm_id -> {event, yes, expire, session_id}
        self._confirm_events: dict[str, dict] = {}
        # 控制板窗口估算计数(跨回合累计, 供 AI 自动整理布局)
        self._panel_count: int = 0
        self._last_auto_grid: float = 0.0
        # v3.25 自修复回路: 回合内 AI 修复 Agent 的次数上限(防无限循环)
        ag = self.cfg.get("agent") or {}
        self._repair_enabled = bool(ag.get("agent_repair_enabled", True))
        self._repair_max = max(0, int(ag.get("max_agent_repair_rounds", 1) or 1))
        self._repair_round = 0

    def _fill_empty_dirs(self, base: str, filename: str = ".keep"):
        """为所有空文件夹添加占位文件(无实际意义, 纯粹填充, 不影响任何功能)。
        避免空目录在打包/同步/文件扫描中被忽略, 也方便用户看到工作区目录结构。"""
        if not base or not os.path.isdir(base):
            return
        try:
            for dirpath, dirnames, filenames in os.walk(base):
                rel = os.path.relpath(dirpath, base)
                segs = [x for x in rel.split(os.sep) if x]
                if any(x.startswith("__") or x in ("node_modules", ".git", ".venv") for x in segs):
                    continue
                # 空目录(无子目录且无文件) → 写入占位文件
                if not filenames and not dirnames:
                    try:
                        with open(os.path.join(dirpath, filename), "w", encoding="utf-8") as f:
                            f.write("# NeuraAgent 占位文件, 无实际意义。\n")
                    except Exception:
                        pass
        except Exception:
            pass

    # ---------------------------------------------------------------
    def rebuild_registry(self):
        """v3.25 热重载: Agent 源码修复后重建工具注册表, 让新代码立即生效。"""
        try:
            from agent.tools import build_registry
            from agent.tools.dynamic import CustomToolStore
            self.registry = build_registry()
            self.custom_tools = CustomToolStore(os.path.join(self.root, "system", "custom_tools"),
                                                self.registry, self.cfg)
            self.custom_tools.load_all()
            if getattr(self, "brain", None) is not None:
                self.brain.registry = self.registry
            return True
        except Exception:
            return False

    def _context_for(self, session: Session) -> ToolContext:
        ws = os.path.join(self.workspace_root, session.id)
        os.makedirs(ws, exist_ok=True)
        ctx = self._ctx_pool.get(session.id)
        if ctx is None:
            ctx = ToolContext(
                session_id=session.id, emitter=session.turn_emitter or self.emitter,
                workspace_root=ws, downloads_dir=self.downloads_dir,
                config=self.cfg, memory=self.memory, mini_ai=self.mini_ai,
                improver=self.improver, agent=self,
            )
            self._ctx_pool[session.id] = ctx
        ctx.emitter = session.turn_emitter or self.emitter
        ctx.extra["tool_store"] = self.custom_tools
        return ctx

    def _system_prompt(self) -> str:
        base = (self.cfg.get("agent") or {}).get("system_prompt", "")
        mem_ctx = self.memory.as_system_context()
        tools_txt = self.registry.tool_list_text()
        custom_cnt = len(self.custom_tools.list())
        # v3.41: 对接模型后自动采集当前系统环境画像, 返回给 AI —— AI 基于真实环境
        # 决策, 天然兼容 Windows/Linux/macOS 所有平台; AI 也可随时调 system_profile
        # 重新获取(如跨环境/远程/重启后)
        try:
            from agent.system_profile import collect_system_profile, profile_to_text
            _profile_txt = profile_to_text(collect_system_profile())
        except Exception:
            _profile_txt = "(系统画像采集失败, 请调用 system_profile 工具重新获取)"
        return f"""{base}

【可用工具(调用集)清单】(含 {custom_cnt} 个小AI动态合成的自定义调用集)
{tools_txt}

【扩展能力参考(v3.48, AI 自主判断)】扩展包是可选能力: 面对指令时你可调用
ext_match 检索已启用扩展, 命中后【自行判断】用扩展完成还是用 Agent 自身能力/
手动操控完成(扩展只是加速项, 不是强制项; 若扩展与任务匹配度高, 优先考虑使用;
若不匹配或你认为手动操控更合适, 照常操控即可)。系统每轮自动注入『扩展匹配』
提示仅供参考, 不构成强制。

【当前系统环境画像(自动采集, AI 运行环境)】你正运行在以下环境中, 所有操作必须适配
该平台(命令用对终端、图形化按可用性决定、文件路径按平台规则), 如需最新画像可随时
调用 system_profile 工具重新获取:
{_profile_txt}

【扩展包系统(v3.42)】用户可能安装了扩展包(功能增强)。规则:
- 可调用 ext_list 查看已启用扩展, 或用 ext_match 用【指令原文】检索已启用扩展的
  『使用用途标题』是否匹配 —— 命中匹配扩展时【建议】优先使用扩展完成该任务
  (也可自行判断用手动操控/自身能力, 两者皆可);
- 使用流程: ext_match 检索 → ext_read 读取该扩展的 usage.md 使用方法与入口代码 →
  ext_run 按使用方法传参执行; 扩展只读/只算/调 API, 需要系统操作时扩展返回指令再由
  你继续控制 Agent 完成;
- 只能使用【已启用】的扩展; ext_install 可安装新扩展包(本地 zip 路径或联网 URL);
  ext_toggle 可启用/禁用;
- 没有匹配扩展时照常使用 Agent 自身能力完成, 不受影响。

【你与 Agent 的关系(重要): 直接控制, 无小AI 中转, 小AI 只监管】
- 你【直接控制 Agent】: 想执行任何操作就直接指定操作名与参数, Agent 直接执行并把真实结果
  实时返回给你——不存在任何中间调用集, 也不需要小AI 替你做意图解析/能力合成;
- 不受能力清单短缺限制: 能力清单里没有的能力, 你依然可以直接控制 Agent 完成——直接调用
  run_code 提供 Python 实现代码(沙箱执行)、直接调用 run_command 提供命令、组合现有能力,
  或调用 agent_repair 直接修改 Agent 代码来扩展/实现能力。你有 Agent 的直接控制权;
- 小AI 只负责监管(不做任何中转/决策): 你的每个操作/代码/补丁都会被小AI 监管——恶意操作
  (删除/格式化/提权/后门/绕过安全/数据窃取/关闭防护等)直接拦截并标红; 高危操作(删文件/
  结束进程/清理磁盘/卸载软件等)需系统登录密码确认; 代码经沙箱审查后运行;
- 操作结果真实返回: 以 tool 消息为准, 请勿编造执行结果;
- 一切用户合法需求(联网/查天气/操作文件/运行软件/安装/绘画/监控等)都应直接控制 Agent 完成,
  而不是只停留在文字回答;
- 【自主多步控制链】: 用户需求信息不足时, 你应当【自主规划并连续调用多个工具】主动获取所需信息
  (查天气/联网搜索/查文件/运行命令/画图/查行情/查系统状态等), 不需要用户逐条指定调用什么;
  为完成任务你可以自主连续发起任意合法工具调用(搜索->抓取->总结->展示->建议), 直到拿到所需
  信息并给出完整回答; 自主链的每一步都由小AI 实时监管;
- 【桌面版 GUI 操控闭环(v3.33)】: 本机为桌面端(无控制板, 悬浮球+HUD 原生弹窗)。你可以
  像人一样图形化操控电脑: 先调用 screen_capture 截取实时屏幕(HUD 弹窗展示, 你能看到画面),
  再用 gui_control 移动/点击/双击/右键/拖拽/滚动/输入/按键/热键/管理窗口(枚举/关闭/移动/
  缩放/最小化/最大化/聚焦/关全部用户窗口), 再截屏验证效果, 直到操作成功;
  需要精确坐标时先用 list_windows 或屏幕理解估算坐标。数据性操控(命令/代码/文件/网络)照旧;
- 【图形化优先打开软件/发消息(v3.36/3.37)】: 当 open_app/启动命令/命令式发消息失败或
  找不到软件时, 不要放弃: 像人一样用 gui_control 完成 —— 按 Win 键打开开始菜单, 输入应用名
  回车启动; 或点击任务栏图标/桌面快捷方式/托盘图标唤出窗口; 若应用已在运行但窗口隐藏/最小化,
  用 gui_control focus/restore 唤出复用, 不要重复启动新实例; 发消息同理: 打开应用 ->
  定位搜索框输入接收人 -> 回车 -> 定位输入框 -> 键入消息 -> 回车发送。
  【注意】图形操作后【默认不需要截图展示给用户】: screen_capture 默认不弹窗(仅返回路径供
  你感知), 你只需要在操作后用 list_windows 验证窗口状态即可; 确实需要展示给用户时才用
  screen_capture(show=true)。所有 GUI 操作都被小AI 实时监管(恶意拦截/高危需密码)。
- 【窗口整洁自律(v3.37)】: HUD 弹窗出现在电脑屏幕上, 你要主动保持桌面整洁 —— 当 HUD
  窗口超过 3 个或互相遮挡时, 主动用 panel_control layout(grid 网格平铺)整理; 也可以随时
  用 gui_control/panel_control move/maximize/resize/close 移动、放大、关闭窗口。
  悬浮球与对话框保持置顶, 其他 HUD 窗口不置顶(避免压在其他应用之上)。
- 【像人一样实时感知屏幕再操作(v3.38)】: 图形化操作前【先调用 gui_control(list_windows /
  screen_info / screen_capture)】获取当前屏幕窗口布局(标题/位置/大小/是否前台/屏幕尺寸/
  鼠标位置), 像人一样推理目标窗口与控件位置, 再精准移动点击/键入; 每步操作后用
  list_windows 验证窗口状态变化, 灵活调整坐标, 不要机械重试同一动作。
- 【允许 AI 直接操控鼠标(v3.40 增强)】: 你有完整的鼠标操控权 —— 移动(smooth_move 平滑/
  move 瞬移/move_rel 相对)、点击(click/double_click/right_click)、按住释放
  (mouse_down/mouse_up, 用于长按拖拽与菜单)、拖拽(drag)、滚动(scroll); 操作前可先
  gui_control(screen_info) 一步获取屏幕尺寸+鼠标位置+前台窗口+窗口列表, 像人一样决定
  下一步点击哪里; 每完成一个界面动作后用 screen_info/list_windows 观察屏幕变化再继续。
- 【数字化鼠标操作杆(v3.42)】: 你有一个数字化鼠标操作杆 —— 可随时调整移动速度并像摇杆
  一样持续移动鼠标: ① set_speed 设置速度(speed=slow/medium/fast, 或 custom 加
  px_per_sec 精确指定像素/秒), 后续所有移动按该速度执行; ② move/smooth_move 可附加
  speed 参数临时指定速度(如跨大屏幕慢速精准移动); ③ joystick 像摇杆一样持续移动:
  dx/dy(像素每秒)+duration(秒)+可选 button(left/right) 按住移动 —— 适合长距离拖拽、
  描边、滑动列表、绘制轨迹等。
- 【打开软件必须验证(v3.38/3.39)】: open_app/图形化启动后必须确认目标窗口【真的出现】
  才说"打开成功": 用 list_windows 找窗口标题; 若未出现, 继续想办法 —— 点击任务栏图标/
  托盘图标唤出/开始菜单再次搜索, 直到窗口真出现; 未确认前如实告知用户"正在打开/尚未
  出现", 绝不谎报成功。像微信/QQ 这类"打开/发送/关闭"的简单任务: 【不要逐步骤弹窗
  汇报过程】, 一次性完成操作后只在 HUD 弹【最终结果】窗口(成功/失败+必要信息)即可;
  只有复杂任务(编写/调试代码、数据分析、安装软件、多步流程)才保留过程弹窗。
- 【打开软件必须调用工具】: 用户要求打开/运行任何软件时, 必须实际调用 open_app/
  gui_control 等工具完成(禁止只输出"已打开"文字却不调用工具); 工具返回失败信息后,
  用 GUI 图形化方式(开始菜单搜索/任务栏/桌面图标/托盘)继续尝试, 直到窗口真出现。
- 【发消息必须点选联系人(v3.41)】: 给好友发送消息时, 在搜索框输入接收人后【绝不能直接
  回车】(回车在 QQ/微信搜索框 = 触发综合搜索, 会搜出网页结果导致发错对象)。必须等候选
  列表出现后, 用鼠标【精确点击联系人条目】(完全匹配接收人名或 接收人+备注)进入会话,
  再定位消息输入框输入内容回车发送; 候选列表没出现时才允许回车兜底, 并再次确认是否
  已进入会话(有消息输入框)。
- 【GUI 电脑操控操作指南(v3.40, 所有大模型必须遵守, 用这套固定流程代替自由发挥)】:
  ▸ 打开/运行软件(标准 3 步): ① 先调 open_app(app 名)——它已内置"已运行复用/注册表/常见
    目录/开始菜单快捷方式"查找; ② 若 open_app 失败或窗口未出现, 用 GUI 图形化降级: hotkey
    keys="win" 按 Win 键 -> 等待 1 秒 -> type 应用名 -> press key="enter" -> 等待 2 秒;
    若应用已在运行(托盘/最小化), 用 window_focus/window_maximize 唤出复用, 不要新开实例;
    ③ 必须用 list_windows 找到目标窗口【真的出现】才向用户报告成功, 未出现就继续换方法
    (任务栏/托盘/桌面图标), 绝不谎报。
  ▸ 点击/输入(标准 4 步): ① screen_info 拿屏幕尺寸+窗口列表+鼠标位置; ② 推理坐标
    (窗口中心 = 窗口 x+宽/2, y+高/2; 输入框通常在窗口下部 1/3 区域); ③ 执行动作:
    move 或 smooth_move 到坐标, 需要时 click / double_click / right_click / type(text) /
    press(key="enter") / hotkey; ④ 再用 screen_info/list_windows 验证界面变化再继续。
  ▸ 参数格式(直接照抄): gui_control{{"action":"click","x":512,"y":384}} /
    gui_control{{"action":"type","text":"你好"}} / gui_control{{"action":"press","key":"enter"}} /
    gui_control{{"action":"hotkey","keys":"ctrl+c"}} / gui_control{{"action":"move","x":100,"y":100}} /
    gui_control{{"action":"drag","x":100,"y":100,"x2":500,"y2":400}} /
    gui_control{{"action":"scroll","x":400,"y":400,"amount":-3}} / open_app{{"app":"QQ"}}。
  ▸ 失败应对(禁止机械重试同一参数): 动作失败或界面没反应 -> ① 换坐标(中心点/±30px 偏移)/
    换方法(点击变双击/直接 type 代替逐键); ② 先 list_windows 看目标窗口是否还在/已移动/
    已置顶; ③ 连续 2 次同一方案失败就【彻底换一种方案】(命令式->图形化, 图形化->命令式,
    或 agent_repair 修 Agent 代码); 每次失败后向用户简短说明"正在重试另一种方式"。
  ▸ 不要对每个中间步骤都截屏弹窗: 图形操作全程【不弹过程窗】, 只用 screen_info/list_windows
    做内部验证; 你完全可以不看画面连续完成 移动->点击->输入->验证 的闭环。

- 【弹窗纪律(v3.40, 严格)】: 默认【不弹过程窗】。只有以下 3 类情况才弹 HUD 窗口:
  ① 【最终结果】—— 简单任务(打开软件/发消息/关闭窗口/查天气/查文件)完成后只弹一次
     最终结果窗口; ② 【必须展示的数据】—— 文件列表/天气数据/代码过程与产物/图片/图表/
     实时命令输出/需要用户看的东西; ③ 【需要用户确认】—— 安装依赖询问/高危操作密码确认。
  其余任何过程步骤(移动鼠标/点击/查找窗口/重试/整理窗口等)一律不弹窗, 用内部验证代替。

- 【回复纪律(v3.40)】: 对话框回复保持简短(一般不超过 3-5 句)、中文简体、不重复同一句话;
  每完成一步操作用一句话说明结果; 最终总结时给出结论和必要建议; 不要输出 JSON/原始数据
  到对话框(数据一律进 HUD 窗口)。

- 【结果展示】: 操作结果(文件/天气/代码/进度/图片/数据/实时命令)通过 panel 系列工具在
  电脑屏幕上的 HUD 弹窗展示, 对话框只输出文本。

{mem_ctx}
【当前模式】{self.brain.mode_label()}
【时间】{datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %A')}
"""

    def _build_messages(self, session: Session) -> list:
        msgs = [{"role": "system", "content": self._system_prompt()}] + session.history
        msgs = self._inject_ext_hint(msgs)
        return utils.trim_messages_for_context(msgs, self.cfg.get("agent", {}).get("max_context_chars", 28000))

    def _inject_ext_hint(self, msgs: list) -> list:
        """v3.47 自动扩展预检索: 用最新用户指令检索已启用扩展, 命中则向模型注入
        『扩展匹配』提示块, 强制 AI 优先使用扩展 —— 解决"AI 不使用扩展"问题。"""
        try:
            last_user = ""
            for m in reversed(msgs):
                if m.get("role") == "user" and isinstance(m.get("content"), str):
                    last_user = m["content"]
                    break
            if not last_user.strip():
                return msgs
            from agent import extensions as _ext
            hits = _ext.match_extension(last_user)[:2]
            if not hits:
                return msgs
            lines = ["[扩展匹配] 系统检测到【已启用】扩展, 供你参考(是否使用由你自行判断):"]
            for h in hits:
                lines.append(f"- 扩展名: {h.get('name')} | 标题: {h.get('title')} | 用途: {h.get('purpose','')}")
                lines.append(f"  使用方式: ext_read(name='{h.get('name')}') 读取使用方法, 然后 ext_run(name='{h.get('name')}', args={{...}}) 执行; 你也可以选择用手动操控完成。")
            lines.append("若你认为扩展不适用或手动操控更合适, 照常使用 Agent 自身能力, 不受任何限制。")
            msgs = msgs + [{"role": "user", "content": "\n".join(lines)}]
        except Exception:
            pass
        return msgs

    # ---------------------------------------------------------------
    # 主入口
    # ---------------------------------------------------------------
    async def start_turn(self, session_id: str, user_text: str, emitter=None):
        session = self.sessions.get(session_id)
        if session is None:
            session = self.sessions.create()
            session_id = session.id
        session.turn_emitter = emitter or self.emitter
        async with session.lock:
            if session.running:
                session.turn_emitter = None
                return {"error": "该会话正在处理中"}
            session.running = True
            self._repair_round = 0          # v3.25 每回合重置自修复计数
            try:
                # 提取并记忆用户事实 + 项目记忆(持久)
                learned = self.memory.extract_and_remember(user_text)
                for key, val in learned:
                    await session.turn_emitter.notify(f"已记住: {key} = {val}", level="info")
                proj = self.memory.extract_project(user_text)
                if proj:
                    await session.turn_emitter.notify(
                        f"已记住项目「{proj.get('name','')}」: {proj.get('path','')}", level="success")
                session.history.append({"role": "user", "content": user_text})
                self.mini_ai.record_user(user_text)   # 上下文记忆: 记录最近用户话语
                if len(session.history) == 1:
                    self.sessions.update_title(session_id, user_text[:20])
                await session.turn_emitter.session_updated(session.to_dict())

                final_text, used_tools = await self._run_tool_loop(session, user_text)

                # 收尾文本(流式推送到对话框)
                if not final_text:
                    final_text = "任务已完成，结果见左侧控制板。"
                for i in range(0, len(final_text), 8):
                    await session.turn_emitter.message_delta(session_id, final_text[i:i + 8], final=False)
                # 小AI 拦截的那一次消息: 文本含"安全拦截/拦截/恶意" -> 整条气泡变红(仅该条)
                blocked = ("安全拦截" in final_text) or ("拦截" in final_text) or ("恶意" in final_text)
                await session.turn_emitter.message_done(session_id, final_text, used_tools, blocked=blocked)
                session.history.append({"role": "assistant", "content": final_text})
                self.sessions._save()

                # 触发自改进(失败>2次的规则, 回归通过才激活)
                try:
                    result = await asyncio.to_thread(
                        self.improver.maybe_improve, self.mini_ai, self.registry, self._run_regression)
                    if result.get("applied"):
                        await session.turn_emitter.notify(f"自改进已应用新规则: {result['applied']}", level="success")
                except Exception:
                    pass
                return {"session_id": session_id}
            finally:
                session.running = False
                try:
                    await session.turn_emitter.agent_status("idle", self.brain.mode_label())
                finally:
                    session.turn_emitter = None

    async def _run_tool_loop(self, session: Session, user_text: str):
        """核心工具调用循环: 模型 -> (工具调用 -> 执行 -> 实时回传) -> 最终文本。"""
        max_rounds = self.cfg.get("agent", {}).get("max_tool_rounds", 12)
        used_tools = []
        final_text = None
        for rnd in range(max_rounds):
            await session.turn_emitter.agent_status("thinking", self.brain.mode_label())
            messages = self._build_messages(session)
            tools = self.registry.schemas()

            def on_text(delta, final=False):
                pass  # 工具调用回合不直接输出最终文本, 由 message_done 统一发送

            def on_tool_calls(calls):
                pass

            try:
                result: ModelResult = await self.brain.chat(
                    messages, tools, on_text=on_text, on_tool_calls=on_tool_calls)
            except ModelUnavailable as e:
                final_text = f"模型不可用: {e}。请检查 config.json 配置。"
                break
            except Exception as e:
                final_text = f"模型调用出错: {e}"
                break

            if result.tool_calls:
                await session.turn_emitter.agent_status("executing", self.brain.mode_label())
                session.history.append({
                    "role": "assistant",
                    "content": result.text or "",
                    "tool_calls": [
                        {"id": tc.get("id") or f"call_{i}", "type": "function",
                         "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                        for i, tc in enumerate(result.tool_calls)
                    ],
                })
                for tc in result.tool_calls:
                    name = tc.get("name", "")
                    args = tc.get("arguments") or {}
                    tc_id = tc.get("id") or f"call_{len(session.history)}"
                    used_tools.append(name)
                    await session.turn_emitter.tool_event(name, args, "running")
                    try:
                        _stop = await self._run_tool_call(session, tc_id, name, args, used_tools, tc)
                        if _stop is False:
                            break        # 用户取消依赖安装 -> 终止本轮工具链
                    except Exception as e:
                        # 防御: 工具链任意异常不得中断循环, 补配对 tool 消息(避免 API 400)
                        await session.turn_emitter.tool_event(name, args, "error", f"执行异常: {e}")
                        session.history.append({
                            "role": "tool", "tool_call_id": tc_id, "name": name,
                            "content": json.dumps({"ok": False, "error": f"执行异常: {e}",
                                                   "error_type": "internal"}, ensure_ascii=False),
                        })
                continue  # 下一轮
            else:
                final_text = (result.text or "").strip()
                break
        return final_text, used_tools

    async def _run_tool_call(self, session, tc_id: str, name: str, args: dict, used_tools: list, tc: dict) -> bool | None:
        # MiniAI 参数补全 + 工具名模糊匹配
        match = self.mini_ai.match_tool(name, self.registry.names())
        if match is None:
            # ---- v3.26 直接操控: 不存在"调用集注册/合成"中转, AI 直接操控 Agent ----
            return await self._direct_act(session, tc_id, name, args, used_tools)
        norm_args = self.mini_ai.normalize_args(match, args)
        if match in ("list_directory", "read_file", "write_file", "run_code", "preview_file", "find_file") and norm_args.get("path"):
            self.memory.remember_path(str(norm_args["path"]))
        if match == "panel_control":
            a = str(norm_args.get("action", ""))
            if a == "close_all":
                self._panel_count = 0
            elif a == "close":
                self._panel_count = max(0, self._panel_count - 1)
        res = await self._execute_tool(match, norm_args, session)
        if not res.ok:
            self.mini_ai.record_failure(match, norm_args, res.error)   # 上下文纠错回路
        if not res.ok and res.retryable:
            fix = self.mini_ai.suggest_fix(match, res.error, norm_args, self.improver.active_rules())
            if fix:
                await session.turn_emitter.tool_event(match, fix, "retry", admin=res.admin)
                res = await self._execute_tool(match, fix, session)
        # 缺依赖 -> 控制板弹窗询问是否安装: 是 -> 进度条安装后重试; 否 -> 取消任务
        if not res.ok and match in ("run_code", "run_command", "system_stats", "app_control",
                                       "code_task", "write_file", "fetch_webpage", "web_search"):
            missing = self._detect_missing_dependency(res.error or "")
            if missing:
                installed = await self._ask_install_dependency(session, missing, match, norm_args)
                if installed is True:
                    res = await self._execute_tool(match, norm_args, session)
                elif installed is False:
                    await session.turn_emitter.tool_event(match, norm_args, "cancelled",
                                                       "用户选择取消安装，任务已取消")
                    session.history.append({
                        "role": "tool", "tool_call_id": tc_id, "name": match,
                        "content": json.dumps({"ok": False, "error": "用户取消安装依赖，任务已取消",
                                               "error_type": "cancelled"}, ensure_ascii=False),
                    })
                    return False
        if not res.ok:
            await session.turn_emitter.tool_event(match, norm_args, "error", res.error, admin=res.admin)
            # 自定义调用集连续失败 -> 小AI 最高修改权: 自动升级
            if self.registry.is_custom(match):
                cnt = self.improver.fail_counts.get((match, res.error_type or "unknown"), 0)
                if cnt >= 2:
                    await self._try_auto_update_tool(match, norm_args, res.error, session)
            # v3.25 自修复回路: Agent 报错 -> AI 检测并直接修改 Agent(小AI 实时监管) -> 重试
            if self._repair_enabled and self._repair_round < self._repair_max:
                fixed = await self._maybe_repair_agent(session, match, norm_args, res)
                if fixed:
                    await session.turn_emitter.notify(f"Agent 已自修复({match}), 重试成功", "success")
        else:
            await session.turn_emitter.tool_event(match, norm_args, "ok", admin=res.admin)
        # synthesize_tool 成功 -> 向控制板广播"新调用集已注册"(含代码与审查结论)
        if match == "synthesize_tool" and res.ok and isinstance(res.data, dict) and "name" in res.data:
            await session.turn_emitter.tool_synthesized(
                res.data.get("name", ""), res.data.get("code", "") or "",
                f"通过五层安全审查 v{res.data.get('version', 1)}")
        # 实时回传给模型(Agent 将所有数据实时发给 DeepSeek/本地模型)
        session.history.append({
            "role": "tool", "tool_call_id": tc_id,
            "name": match, "content": res.to_model_content(),
        })
        if res.downloads:
            for fname, fpath in res.downloads:
                await session.turn_emitter.download_ready(fname, f"/api/downloads/{session.id}/{fname}")
        # 代码调试闭环: run_code 失败时自动修复重跑
        if match == "run_code" and not res.ok:
            await self._auto_debug(session, norm_args, res.error)
        return None
    async def _maybe_repair_agent(self, session: Session, match: str, args: dict, res) -> bool:
        """v3.25 自修复回路: 工具报错 -> AI 检测 Agent 缺陷 -> 调用 agent_repair 修改 Agent
        (小AI 全程监管审查, 只允许修复性修改) -> 热重载 -> 重试原操作。

        返回 True 表示修复后重试成功。
        """
        et = getattr(res, "error_type", "") or ""
        err = (res.error or "") or ""
        # 仅处理可修复类型(拒绝/取消/找不到/需AI人工不触发)
        if et in ("forbidden", "cancelled", "not_found", "need_ai", "invalid_args"):
            return False
        if not (et in ("exec", "module", "import", "type", "attr", "key", "value", "syntax")
                or any(k in err for k in ("Traceback", "ModuleNotFoundError", "ImportError",
                                          "NameError", "AttributeError", "TypeError",
                                          "JSONDecodeError", "SyntaxError", "KeyError", "IndexError"))):
            return False
        self._repair_round += 1
        try:
            await session.turn_emitter.notify(
                f"检测到 Agent 报错({match}: {err[:80]}), 正在由 AI 自修复(小AI 实时监管)...", "info")
            # 让模型(AI)分析报错并产出修复补丁 -> 通过 agent_repair 提交(小AI 审查)
            hist = list(session.history)
            hist.append({
                "role": "user",
                "content": (
                    f"上一步调用集 {match} 执行失败, 报错: {err[:1200]}\n\n"
                    "请分析这是否是 Agent 自身代码/逻辑缺陷。如果是, 请调用 agent_repair 提交修复补丁 "
                    "(先可用 read_agent_file 查看源码定位问题; old 片段必须与源码完全一致且唯一)。"
                    "小AI 会实时监管审查, 只允许修复性修改; 恶意/越权修改会被拦截。"
                    "如果无法确定修复点, 调用 agent_repair 并说明需要的信息。"
                    "不要做与修复无关的修改。"
                ),
            })
            r2 = await self.brain.chat(hist, tools=None)
            applied = False
            for tc in (r2.tool_calls or []):
                nm = tc.get("name", "")
                ar = tc.get("arguments") or {}
                if nm == "agent_repair":
                    rr = await self._execute_tool("agent_repair", ar, session)
                    await self._auto_panel(session, "agent_repair", rr)
                    if rr.ok:
                        applied = True
            if not applied:
                # 兜底: 规则引擎有限自动修复(需 file 可判定场景)
                rr = await self._execute_tool("agent_repair", {
                    "file": "", "old": "", "new": "", "reason": err, "auto": True}, session)
                await self._auto_panel(session, "agent_repair", rr)
                applied = rr.ok
            if applied:
                # 热重载已由 agent_repair 完成 -> 重试原操作
                ret = await self._execute_tool(match, args, session)
                if ret.ok:
                    await session.turn_emitter.tool_event(match, args, "ok", admin=getattr(ret, "admin", False))
                    return True
                await session.turn_emitter.tool_event(match, args, "error",
                                                      f"重试仍失败: {ret.error}", admin=getattr(ret, "admin", False))
            return False
        except Exception:
            return False

    async def _direct_act(self, session: Session, tc_id: str, name: str, args: dict,
                           used_tools: list) -> bool | None:
        """v3.27 AI 直接控制 Agent: 无小AI 中转, 小AI 只负责安全监管。

        原则: AI 的每个操作指令都由 Agent 直接执行(可直通现有能力/直接执行 AI 提供的
        实现代码/命令/修改 Agent 代码), 不受任何能力清单短缺限制; 小AI 不做意图理解、
        能力解析、合成决策等中转, 只做监管: 恶意直接拦截, 高危需系统登录密码,
        执行层与代码层再各过一道审查。
        """
        desc = f"{name} {json.dumps(args, ensure_ascii=False)}"
        # 1) 小AI 只监管(不中转): 操作意图文本级检测(v3.32 用户授权语境降级误判)
        risk = security.classify_llm_action(desc,
                                            user_auth=self._user_explicitly_asked(session, desc))
        if risk.level == "malicious":
            await session.turn_emitter.tool_event(name, args, "error",
                                                  f"安全拦截(小AI 监管): {risk.reason}", admin=True)
            await session.turn_emitter.panel(
                f"direct_blocked_{name}", "info", "⛔ 恶意操作已拦截(直接控制)",
                {"reason": risk.reason, "detail": risk.description}, status="error", admin=True)
            session.history.append({
                "role": "tool", "tool_call_id": tc_id, "name": name,
                "content": json.dumps({"ok": False, "error": f"安全拦截: {risk.reason}",
                                       "error_type": "forbidden"}, ensure_ascii=False),
            })
            return None
        if risk.level == "high":
            unlocked = await self._await_unlock(session.id, name, args, risk)
            if not unlocked:
                await session.turn_emitter.panel(
                    f"direct_denied_{name}", "info", "⛔ 高危操作未获授权(直接控制)",
                    {"reason": "系统登录密码验证未通过、超时或已取消", "detail": risk.description},
                    status="error", admin=True)
                session.history.append({
                    "role": "tool", "tool_call_id": tc_id, "name": name,
                    "content": json.dumps({"ok": False, "error": "高危操作未获系统授权",
                                           "error_type": "forbidden"}, ensure_ascii=False),
                })
                return None
        # 2) 直通现有能力(名称对齐, 非中转): AI 指定能力名 -> Agent 直接执行
        cap = self.mini_ai.match_tool(name, self.registry.names())
        if cap is None:
            cap = self.mini_ai.interpret_action(name, args, self.registry).get("capability")
        if cap:
            cap_args = args
            used_tools.append(cap)
            res = await self._execute_tool(cap, cap_args, session)
            session.history.append({
                "role": "tool", "tool_call_id": tc_id, "name": name,
                "content": res.to_model_content(),
            })
            return None
        # 3) AI 直接控制: AI 提供了实现代码/命令 -> 小AI 代码级监管后直接执行
        code = str(args.get("code") or args.get("script") or "")
        command = str(args.get("command") or "")
        if code.strip():
            risk2 = security.classify_llm_action(
                f"AI 直接控制执行代码: {code[:600]}",
                user_auth=self._user_explicitly_asked(session, f"{name} {code[:600]}"))
            if risk2.level != "none":
                await session.turn_emitter.tool_event(
                    name, args, "error", f"安全拦截(小AI 监管): {risk2.reason}", admin=True)
                session.history.append({
                    "role": "tool", "tool_call_id": tc_id, "name": name,
                    "content": json.dumps({"ok": False, "error": f"安全拦截: {risk2.reason}",
                                           "error_type": "forbidden"}, ensure_ascii=False),
                })
                return None
            used_tools.append("run_code")
            res = await self._execute_tool("run_code", {"code": code}, session)
            session.history.append({
                "role": "tool", "tool_call_id": tc_id, "name": name,
                "content": res.to_model_content(),
            })
            return None
        if command.strip():
            used_tools.append("run_command")
            res = await self._execute_tool("run_command", {"command": command}, session)
            session.history.append({
                "role": "tool", "tool_call_id": tc_id, "name": name,
                "content": res.to_model_content(),
            })
            return None
        # 4) 无实现参数: 提示 AI 直接用控制权实现(可修改 Agent, 不受能力清单限制)
        hint = ("能力「%s」不在 Agent 直接能力表, 但你拥有 Agent 的直接控制权, 不受能力清单限制: "
                "① 直接调用 run_code 提供 Python 实现代码(沙箱执行); "
                "② 直接调用 run_command 提供命令; "
                "③ 组合现有能力; ④ 调用 agent_repair 直接修改 Agent 代码以实现/扩展能力。"
                "小AI 只做安全监管(恶意拦截/高危需密码)。")
        await session.turn_emitter.tool_event(
            name, args, "error", hint % name)
        session.history.append({
            "role": "tool", "tool_call_id": tc_id, "name": name,
            "content": json.dumps({"ok": False, "error": hint % name,
                                   "error_type": "need_ai",
                                   "hint": "直接用 run_code / run_command / agent_repair 实现该操作(你拥有直接控制权)"},
                                  ensure_ascii=False),
        })
        return None

    async def handle_panel_action(self, session_id: str, action: str, params: dict, emitter=None) -> None:
        """控制板用户点击交互: 进入目录/预览文件/刷新/执行命令(复用调用集与上板逻辑)。"""
        params = params or {}
        path = str(params.get("path") or "")
        if action == "list_dir" and path:
            await self._panel_exec("list_directory", {"path": path}, session_id, emitter)
        elif action == "preview_file" and path:
            await self._panel_exec("preview_file", {"path": path}, session_id, emitter)
        elif action == "refresh":
            await self._panel_exec("list_directory", {"path": path or "~"}, session_id, emitter)
        elif action == "run_cmd":
            cmd = str(params.get("command") or "")
            if cmd:
                await self._panel_exec("run_command", {"command": cmd}, session_id, emitter)

    async def _panel_exec(self, tool: str, args: dict, session_id: str, emitter=None):
        session = self.sessions.get(session_id)
        if session is None:
            session = self.sessions.create()
            session_id = session.id
        if emitter is not None:
            session.turn_emitter = emitter
        try:
            res = await self._execute_tool(tool, args, session)
            await self._auto_panel(session, tool, res)
        except Exception as e:
            if emitter is not None:
                await emitter.tool_event(tool, args, "error", f"控制板操作异常: {e}")

    def _user_explicitly_asked(self, session: Session, desc: str) -> bool:
        """判断用户最近消息是否【明确提及】该操作目标(用户授权语境)。

        v3.32 减少误判: 仅当同一破坏性动词(格式化/删除/清空/清理/清除/卸载/关闭等)
        同时出现在"操作描述"与"用户最近 3 条消息"中, 判定为用户明确授权 ——
        此时破坏性操作从"恶意拦截"降级为"高危需系统登录密码确认"。
        攻击性操作(关闭安全防护/绕过验证/窃取凭据/后门/提权/爆破)不依赖此判定, 恒拦截。
        """
        try:
            users = " ".join(str(m.get("content", "")) for m in session.history[-6:]
                             if m.get("role") == "user")
        except Exception:
            users = ""
        if not users.strip():
            return False
        import re as _re
        _verbs = ("格式化", "删除", "清空", "清理", "清除", "移除", "抹掉", "卸载",
                  "关闭", "禁用", "停用", "杀掉", "结束", "移除")
        for v in _verbs:
            if v in desc and v in users:
                return True
        # 路径/盘符级目标共现兜底(如 desc 含 "C:" 且用户消息也含 "C:")
        for m2 in _re.finditer(r"[A-Za-z]:[\\/][\w\-. ]{0,40}", desc, flags=_re.I):
            t = m2.group(0)[:40]
            if len(t) >= 3 and t.lower() in users.lower():
                return True
        return False

    async def _execute_tool(self, name: str, args: dict, session: Session) -> object:
        from agent.tools.registry import ToolResult
        ctx = self._context_for(session)
        # ---- v3.7 风险分级: 恶意直接拦截 / 高危需系统登录密码解锁 ----
        # v3.32: 用户明确授权语境下, 破坏性操作降级为高危密码确认(减少误判)
        _desc = f"{name} {' '.join(str(v) for v in (args or {}).values())}"
        risk = security.classify_operation(name, args,
                                           user_auth=self._user_explicitly_asked(session, _desc))
        if risk.level == "malicious":
            await session.turn_emitter.tool_event(name, args, "error",
                                                f"安全拦截(恶意操作): {risk.reason}", admin=True)
            await session.turn_emitter.panel(
                f"blocked_{name}", "info", "⛔ 恶意操作已拦截",
                {"reason": risk.reason, "detail": risk.description},
                status="error", admin=True)
            return ToolResult(ok=False, error=f"安全拦截: {risk.reason}",
                              error_type="forbidden", admin=True)
        if risk.level == "high":
            unlocked = await self._await_unlock(session.id, name, args, risk)
            if not unlocked:
                await session.turn_emitter.panel(
                    f"denied_{name}", "info", "⛔ 高危操作未获授权",
                    {"reason": "系统登录密码验证未通过、超时或已取消", "detail": risk.description},
                    status="error", admin=True)
                return ToolResult(ok=False, error="高危操作未获系统授权(系统登录密码验证未通过)",
                                  error_type="forbidden", admin=True)
        res = await self.registry.invoke(name, args, ctx)
        self.improver.record(name, args, res.ok, getattr(res, "error_type", None),
                             session.id, getattr(res, "error", ""))
        # 自动上板: 无论成功失败, 都把所有操作结果展示到控制板(成功按类型展示,
        # 失败弹红色错误窗口说明原因)——"能显示的显示, 不能显示的显示是否成功与提示"。
        await self._auto_panel(session, name, res)
        return res

    @staticmethod
    def _detect_missing_dependency(error: str) -> str | None:
        """从错误信息提取缺失的 Python 模块名; 无则 None。"""
        if not error:
            return None
        err = str(error)
        m = None
        for pat in (
                r"ModuleNotFoundError:\s*No module named ['\"]([^'\"]+)['\"]",
                r"No module named ['\"]([^'\"]+)['\"]",
                r"cannot import name ['\"]([^'\"]+)['\"]",
                r"ImportError:\s*No module named ['\"]([^'\"]+)['\"]"):
            mm = re.search(pat, err)
            if mm:
                m = mm.group(1)
                break
        if not m:
            return None
        # 过滤掉非 pip 包(相对导入/本地文件)
        if m in ("typing", "os", "sys", "re", "json", "asyncio", "datetime", "collections",
                 "functools", "itertools", "math", "random", "pathlib", "subprocess", "string",
                 "threading", "time", "io", "socket", "struct", "urllib", "uuid", "base64",
                 "hashlib", "hmac", "http", "logging", "queue", "shutil", "tempfile", "traceback",
                 "types", "warnings", "zipfile", "gzip", "csv", "sqlite3", "xml", "email",
                 "html", "glob", "bisect", "decimal", "copy", "dataclasses", "abc"):
            return None
        return m.strip()

    async def _ask_install_dependency(self, session: Session, package: str, tool: str, args: dict):
        """控制板弹【是否安装依赖】窗口: 等用户选择。
        返回 True=已安装(可重试) / False=用户取消 / None=超时或窗口被关。"""
        import re as _re
        pkg = _re.sub(r"[^A-Za-z0-9_\-]", "", package)[:48]
        if not pkg:
            return None
        cid = f"confirm_{pkg}_{tool}"
        now = asyncio.get_event_loop().time()
        timeout = int(self.cfg.get("security", {}).get("unlock_timeout", 120))
        for cid0, slot in list(self._confirm_events.items()):
            if slot.get("expire", 0) < now:
                self._confirm_events.pop(cid0, None)
        slot = self._confirm_events.get(cid)
        if slot is None:
            slot = {"event": asyncio.Event(), "yes": None, "expire": now + timeout,
                    "session_id": session.id}
            self._confirm_events[cid] = slot
            await session.turn_emitter.confirm_request(
                cid, f"缺少依赖模块 {pkg}",
                f"执行「{tool}」需要 Python 模块 {pkg}，是否现在用 pip 自动安装？"
                f"（选择【安装】将弹进度条窗口实时显示安装过程；选择【取消】则取消本次任务）",
                package=pkg)
        try:
            await asyncio.wait_for(slot["event"].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._confirm_events.pop(cid, None)
            try:
                await session.turn_emitter.notify("安装询问等待超时，任务已取消", "warning")
            except Exception:
                pass
            return None
        yes = slot.get("yes")
        self._confirm_events.pop(cid, None)
        if not yes:
            return False
        # 用户选择安装: 进度条窗口实时显示安装过程
        try:
            await session.turn_emitter.notify(f"正在通过 pip 安装 {pkg}…", "info")
            from agent.tools.registry import ToolResult
            ctx = self._context_for(session)
            inst = await self.registry.invoke("install_dependency", {"package": pkg}, ctx)
            if not inst.ok:
                await session.turn_emitter.notify(f"安装失败: {inst.error}", "warning")
                return None
            await session.turn_emitter.notify(f"依赖 {pkg} 安装完成，正在重试原操作", "success")
            return True
        except Exception as e:
            await session.turn_emitter.notify(f"安装异常: {e}", "warning")
            return None

    async def handle_confirm(self, confirm_id: str, yes: bool, emitter) -> None:
        """server 收到用户在确认窗口的选择(是/否)。"""
        slot = self._confirm_events.get(confirm_id)
        if slot is None:
            return
        slot["yes"] = bool(yes)
        slot["event"].set()
        try:
            await emitter.notify("已选择" + ("安装依赖" if yes else "取消安装，任务已取消"),
                                 "success" if yes else "warning")
        except Exception:
            pass

    async def handle_command_direct(self, session_id: str, command: str, emitter) -> None:
        """控制板实时命令窗口: 执行命令并分行流式推送到命令窗口(电影级终端感)。"""
        win_id = "cmd_terminal"
        try:
            await emitter.panel(win_id, "process", "⌘ 实时命令终端", [{"time": utils.now_iso(),
                                "text": f"$ {command}", "level": "cmd"}], status="running")
            # 非回合路径: 直接构造带 emitter 的上下文(不走 _context_for, 避免吞掉 emitter)
            ses = self.sessions.get(session_id) or self.sessions.create()
            ws = os.path.join(self.workspace_root, ses.id)
            ctx = ToolContext(session_id=ses.id, emitter=emitter, workspace_root=ws,
                              downloads_dir=self.downloads_dir, config=self.cfg,
                              memory=self.memory, mini_ai=self.mini_ai,
                              improver=self.improver, agent=self)
            ctx.extra["tool_store"] = self.custom_tools
            res = await self.registry.invoke("run_command",
                                             {"command": command, "cwd": "~", "timeout": int(self.cfg.get("network", {}).get("timeout_seconds", 30))},
                                             ctx)
            out = (res.data or {}).get("stdout", "") if res.ok else (getattr(res, "error", "") or "")
            # 分行流式推送(实时终端感)
            lines = [l for l in str(out).splitlines() if l.strip()]
            if not lines:
                lines = ["(无输出)"]
            for ln in lines:
                await emitter.command_stream(win_id, ln[:400], "out")
            if res.ok:
                await emitter.command_stream(win_id, f"[退出码 0] 执行完成", "ok")
            else:
                await emitter.command_stream(win_id, "[执行失败] 见上方错误输出", "err")
        except Exception as e:
            try:
                await emitter.command_stream(win_id, f"[异常] {e}", "err")
            except Exception:
                pass

    async def _await_unlock(self, session_id: str, tool: str, args: dict,
                            risk: security.RiskResult) -> bool:
        """高危操作解锁等待: 向控制板发密码验证窗口, 等待用户输入系统登录密码。

        密码由 server 的 admin_auth 事件验证通过后写入 slot 并 set event;
        超时/取消 -> False(拒绝执行)。"""
        op_id = security.operation_id(session_id, tool, args)
        now = asyncio.get_event_loop().time()
        timeout = int(self.cfg.get("security", {}).get("unlock_timeout", 120))
        # 清理过期槽位
        for oid, slot in list(self._unlock_events.items()):
            if slot.get("expire", 0) < now:
                self._unlock_events.pop(oid, None)
        slot = self._unlock_events.get(op_id)
        if slot is None:
            slot = {"event": asyncio.Event(), "password": None, "cancelled": False,
                    "session_id": session_id, "expire": now + timeout}
            self._unlock_events[op_id] = slot
            await session.turn_emitter.admin_challenge(
                op_id, risk.description or f"{tool} {json.dumps(args, ensure_ascii=False)[:120]}",
                tool, args)
        try:
            await asyncio.wait_for(slot["event"].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._unlock_events.pop(op_id, None)
            try:
                await session.turn_emitter.notify("高危操作等待系统密码超时，已取消", "warning")
            except Exception:
                pass
            return False
        password = slot.get("password")
        cancelled = slot.get("cancelled", False)
        self._unlock_events.pop(op_id, None)
        if cancelled or not password:
            return False
        ok, _why = security.verify_system_password(password, self.cfg)
        return ok

    async def handle_admin_auth(self, operation_id: str, password: str, emitter) -> None:
        """server 收到用户提交的系统登录密码后调用: 验证并解锁对应高危操作。"""
        slot = self._unlock_events.get(operation_id)
        if slot is None:
            return
        ok, why = security.verify_system_password(password or "", self.cfg)
        if ok:
            slot["password"] = password
            slot["cancelled"] = False
            slot["event"].set()
            try:
                await emitter.admin_auth_result(operation_id, True, "密码验证通过，正在执行该操作")
                await emitter.panel(f"unlock_{operation_id}", "info", "🔓 高危操作已授权",
                                    {"status": "系统登录密码验证通过，已放行"},
                                    admin=True)
            except Exception:
                pass
        else:
            try:
                await emitter.admin_auth_result(operation_id, False, why)
            except Exception:
                pass
            # 密码错误: 不 set event, 允许用户重试(窗口保持)

    async def handle_admin_cancel(self, operation_id: str, emitter) -> None:
        slot = self._unlock_events.get(operation_id)
        if slot is None:
            return
        slot["cancelled"] = True
        slot["event"].set()
        try:
            await emitter.admin_auth_result(operation_id, False, "用户取消了该高危操作")
        except Exception:
            pass

    async def _auto_panel(self, session, name: str, res):
        """根据工具类型把成功结果自动弹窗到控制板(引擎/模型未主动弹窗时的兜底)。"""
        mapping = {
            "list_directory": ("files", "文件浏览"),
            "read_file": ("code", "文件内容"),
            "write_file": ("info", "文件写入"),
            "create_folder": ("info", "创建文件夹"),
            "system_info": ("data", "系统信息"),
            "get_time": ("info", "当前时间"),
            "calculator": ("info", "计算结果"),
            "get_weather": ("weather", "天气预报"),
            "web_search": ("data", "搜索结果"),
            "fetch_webpage": ("info", "网页内容"),
            "run_command": ("info", "命令输出"),
            "package_download": ("download", "下载"),
            "delete_file": ("info", "删除文件"),
            "delete_folder": ("info", "删除文件夹"),
            "package_project": ("download", "下载"),
            "find_file": ("files", "查找结果"),
            "synthesize_tool": ("code", "新调用集(小AI 合成)"),
            "app_control": ("appview", "软件操控"),
            "kill_process": ("info", "进程管理"),
            "preview_file": ("preview", "内容预览"),
            "stock_chart": ("chart", "股票趋势图"),
            "chart": ("chart", "数据图表"),
            "draw_image": ("image", "AI 绘画"),
            "install_package": ("progress", "软件安装"),
            "monitor_task": ("command", "实时监控"),
            "read_agent_file": ("code", "Agent 源码"),
            "agent_repair": ("process", "Agent 自修复"),
        }
        item = mapping.get(name)
        if not item:
            return
        # v3.16: 工具执行中已自建进度/命令/绘画窗口 -> 成功时不再重复上板(避免"0% 安装中"误导窗口)
        if res.ok and name in ("install_package", "monitor_task", "draw_image", "app_control", "agent_repair"):
            return
        # v3.24: preview 回退为文件列表时 list_directory 已推过窗口 -> 不重复上板
        if res.ok and name == "preview_file" and isinstance(res.data, dict) and "files" in res.data:
            return
        # AI 自主整理控制板: 窗口计数(跨回合)≥7 -> 自动收起最早的信息窗口; ≥4 -> 自动平铺网格
        try:
            self._panel_count += 1
            now = asyncio.get_event_loop().time()
            if self._panel_count >= 7:
                await session.turn_emitter.panel_close(f"auto_{name}")
                await session.turn_emitter.notify("AI 检测到控制板窗口过多，已自动收起最早的信息窗口", "info")
            elif self._panel_count >= 4 and name not in ("panel_control",) and now - self._last_auto_grid > 60:
                self._last_auto_grid = now
                self._panel_count = 0
                await session.turn_emitter.panel_control("layout", "", kind="grid")
                await session.turn_emitter.notify("AI 检测到多个结果窗口，已自动整理为网格布局（更清晰）", "info")
        except Exception:
            pass
        wtype, title = item
        content = res.data
        if wtype == "chart" and res.ok and isinstance(content, dict):
            if content.get("points") and content.get("closes"):
                content["series"] = [{"name": content.get("name", "收盘价"),
                                      "points": [[p.get("date", ""), p.get("close", 0)] for p in content.get("points", [])]}]
            content["kind"] = content.get("kind", "line")
            content["title"] = content.get("title") or title
            if content.get("source"):
                content["sub"] = "真实行情 · " + str(content.get("source", ""))
        if not res.ok:
            # 失败: 无论工具类型, 都弹红色错误窗口展示"是否成功 + 原因提示"
            wtype = "info"
            title = f"❌ 操作失败 · {title}"
            content = {"ok": False,
                       "error": getattr(res, "error", "") or "操作失败",
                       "error_type": getattr(res, "error_type", "unknown"),
                       "detail": (res.data or {}) if isinstance(res.data, dict) else ""}
            await session.turn_emitter.panel(f"auto_{name}", wtype, title, content,
                                           status="error", admin=True)
            return
        # AI 自主构建: 天气多日预报自动追加温度曲线图(控制板可视化, 不限于用户要求)
        if name == "get_weather" and res.ok and isinstance(res.data, dict):
            daily = res.data.get("daily") or []
            if len(daily) >= 3:
                try:
                    ser = [{"name": "最高气温", "points": [[d.get("date", ""), d.get("max", 0)] for d in daily]},
                           {"name": "最低气温", "points": [[d.get("date", ""), d.get("min", 0)] for d in daily]}]
                    await session.turn_emitter.panel("auto_weather_chart", "chart", "天气预报 · 温度曲线",
                                                     {"kind": "line", "title": "未来温度趋势",
                                                      "series": ser,
                                                      "sub": "基于 Open-Meteo 真实预报数据"}, status="ok")
                except Exception:
                    pass
        if name == "preview_file":
            # 预览工具按内容类型决定窗口: 图片->image / HTML->html_preview / 目录->files / 文本->code
            pt = (res.data or {}).get("preview_type") if isinstance(res.data, dict) else None
            if pt == "image":
                wtype, title = "image", "图片预览"
            elif pt == "html":
                wtype, title = "html_preview", "HTML 预览"
            elif pt is None and isinstance(res.data, dict) and "files" in res.data:
                wtype, title = "files", "文件预览"
            else:
                wtype, title = "code", "文件预览"
        elif wtype == "info":
            content = _fmt_info(name, res.data)
        # 管理员级操作 -> 红色警示窗口; 失败操作同样以红色窗口提示
        if res.admin:
            title = "⚠ 管理员操作 · " + title
        await session.turn_emitter.panel(f"auto_{name}", wtype, title, content, admin=res.admin)

    async def _try_auto_update_tool(self, name: str, args: dict, error: str, session: Session):
        """小AI 最高修改权: 自定义调用集连续失败时, 自动理解错误、联网思考并升级该调用集。"""
        try:
            await session.turn_emitter.notify(
                f"调用集「{name}」连续失败，小AI 正在理解错误并自动升级…", "warning")
            ctx = self._context_for(session)
            res = await self.custom_tools.synthesize_and_register(
                operation=f"修复并升级调用集 {name}，需解决以下运行错误: {str(error)[:500]}",
                suggested_name=name, args_hint=args, update_name=name,
                registry=self.registry, agent=self, ctx=ctx)
            if res.ok:
                data = res.data or {}
                await session.turn_emitter.tool_synthesized(
                    name, data.get("code", ""), f"自动升级 v{data.get('version', '?')}（经安全审查）")
                await session.turn_emitter.notify(f"调用集「{name}」已自动升级到 v{data.get('version', '?')}", "success")
            else:
                await session.turn_emitter.notify(f"调用集「{name}」自动升级失败: {res.error}", "warning")
        except Exception as e:
            await session.turn_emitter.notify(f"调用集自动升级异常: {e}", "warning")

    # ---------------------------------------------------------------
    # 代码自动调试
    # ---------------------------------------------------------------
    async def _auto_debug(self, session: Session, args: dict, error: str):
        max_fix = self.cfg.get("agent", {}).get("code_max_fix_rounds", 3)
        path = args.get("path") or args.get("code")
        if not path:
            return
        await session.turn_emitter.notify(f"代码运行失败，开始自动调试 (最多 {max_fix} 轮)", level="warning")
        for i in range(1, max_fix + 1):
            await session.turn_emitter.panel_update(args.get("window_id", "code_console"),
                                            status="running",
                                            content=[{"time": utils.now_iso(), "text": f"[调试第{i}轮] 分析错误并生成修复…", "level": "dbg"}])
            fix_text = await self._ask_fix(session, path, error)
            if not fix_text:
                await session.turn_emitter.panel_update(args.get("window_id", "code_console"),
                                                content=[{"time": utils.now_iso(), "text": "模型未给出修复方案，停止调试", "level": "err"}],
                                                status="error")
                return
            patch = self._extract_patch(fix_text)
            if not patch:
                patch = fix_text
            rel = path if not os.path.isabs(str(path)) else os.path.basename(str(path))
            abs_path = os.path.join(self._context_for(session).workspace_root, "code", rel)
            try:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(patch)
                await session.turn_emitter.panel_update(args.get("window_id", "code_console"),
                                                content=[{"time": utils.now_iso(), "text": f"已应用修复 -> {rel}", "level": "ok"}])
            except Exception as e:
                await session.turn_emitter.panel_update(args.get("window_id", "code_console"),
                                                content=[{"time": utils.now_iso(), "text": f"写入修复失败: {e}", "level": "err"}],
                                                status="error")
                return
            from agent.tools.registry import ToolResult
            ctx = self._context_for(session)
            rerun = await self.registry.invoke("run_code",
                                               {"path": rel, "language": args.get("language", "python"),
                                                "timeout": args.get("timeout", 30),
                                                "window_id": args.get("window_id", "code_console")}, ctx)
            self.improver.record("run_code", {"path": rel}, rerun.ok, getattr(rerun, "error_type", None),
                                 session.id, getattr(rerun, "error", ""))
            if rerun.ok:
                await session.turn_emitter.panel_update(args.get("window_id", "code_console"),
                                                content=[{"time": utils.now_iso(), "text": f"✅ 第{i}轮调试后运行通过", "level": "ok"}],
                                                status="ok")
                await session.turn_emitter.notify(f"代码调试成功(第{i}轮)", level="success")
                return
            error = rerun.error or error
        await session.turn_emitter.panel_update(args.get("window_id", "code_console"),
                                        content=[{"time": utils.now_iso(), "text": "调试达到上限，仍失败。请人工介入或调整需求。", "level": "err"}],
                                        status="error")

    async def _ask_fix(self, session: Session, path: str, error: str) -> str:
        rel = path if not os.path.isabs(str(path)) else os.path.basename(str(path))
        abs_path = os.path.join(self._context_for(session).workspace_root, "code", rel)
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception:
            code = ""
        prompt = (
            "以下是运行失败的代码及其错误信息。请直接输出修复后的完整代码(单个代码块即可), "
            "不要解释, 不要输出额外文字。\n\n"
            f"文件: {rel}\n\n```\n{code[:8000]}\n```\n\n错误:\n{str(error)[:2000]}"
        )
        msgs = [{"role": "system", "content": "你是资深代码调试器。只输出修复后的完整代码, 放在一个代码块中。"},
                {"role": "user", "content": prompt}]
        try:
            res = await self.brain.chat(msgs, tools=None)
            return res.text or ""
        except Exception:
            return ""

    @staticmethod
    def _extract_patch(text: str) -> str:
        blocks = utils.extract_code_blocks(text)
        if blocks:
            return blocks[0][1]
        return ""

    # ---------------------------------------------------------------
    async def generate_code_plan(self, requirement: str, language: str) -> str:
        """供 code_task 调用: 让模型生成代码计划(不含工具)。"""
        msgs = [{"role": "system",
                 "content": "你是资深软件工程师。请为需求输出完整可运行代码。多文件时按此格式输出：\n"
                            "文件名: src/main.py\n```python\n代码\n```\n"
                            "只输出代码与文件名, 不要解释。"},
                {"role": "user", "content": f"语言: {language}\n需求: {requirement}"}]
        try:
            res = await self.brain.chat(msgs, tools=None)
            return res.text or ""
        except Exception:
            return ""

    def _run_regression(self):
        """回归测试运行器(在线程中执行, 供自改进模块调用)。"""
        import importlib
        reg = importlib.import_module("tests.regression")
        ok, report = reg.run_all()
        return ok, report

    # ---------------------------------------------------------------
    # 服务器可直接调用的辅助接口
    # ---------------------------------------------------------------
    def cancel(self, session_id: str):
        pass  # 由 asyncio task 取消机制处理

    def sessions_list(self) -> list:
        return [s.to_dict() for s in self.sessions.list()]

    def session_get(self, sid: str) -> dict | None:
        s = self.sessions.get(sid)
        return s.to_dict() if s else None
