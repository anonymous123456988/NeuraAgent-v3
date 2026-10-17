# -*- coding: utf-8 -*-
"""
NEURA 内置小型 AI (MiniAI)
职责:
  1. 理解用户意图 -> 生成新的工具调用集(调用序列)
  2. 校验/补全模型返回的工具参数(路径、城市、语言等)
  3. 工具失败后建议修正参数(自适应重试)
  4. 在"内置引擎"模式下充当推理大脑(意图 + 模板 + 记忆)
设计为确定性规则引擎 + 轻量相似度, 零外部依赖、可离线运行。
"""
import datetime
import os
import re
from agent import utils

CJK = "[\u4e00-\u9fa5]"
TEXT = "[\\u4e00-\\u9fa5A-Za-z0-9_\\-./:：，。?？!！ ]"


class Intent:
    def __init__(self, action: str, confidence: float, params: dict | None = None,
                 tool_plan: list | None = None, text_answer: str | None = None):
        self.action = action            # 意图动作名
        self.confidence = confidence    # 0~1
        self.params = params or {}      # 已抽取参数
        self.tool_plan = tool_plan or []  # [{"name":..., "arguments":{...}}, ...]
        self.text_answer = text_answer  # 纯文本回复(无需工具时)


class SynthResult:
    """小AI 对调用命令的判定结果。"""
    def __init__(self, tool_name: str | None = None, args: dict | None = None,
                 new_tool: bool = False, rejected: str | None = None,
                 code: str | None = None, review: str | None = None):
        self.tool_name = tool_name      # 命中的已有调用集 或 新合成调用集名称
        self.args = args or {}          # 规范化后的参数
        self.new_tool = new_tool        # True=本次为 Agent 新增了调用集; False=调用集已存在直接执行
        self.rejected = rejected        # 被小AI辨别拦截的原因(恶意/高危)
        self.code = code                # 新调用集代码(供展示)
        self.review = review            # 安全审查结论


# 危险请求意图(小AI 辨别能力: 拦截恶意高危修改命令调用集)
_COUNTRIES = ("美国", "中国", "日本", "韩国", "朝鲜", "英国", "法国", "德国", "意大利", "西班牙",
             "泰国", "新加坡", "马来西亚", "澳大利亚", "新西兰", "加拿大", "俄罗斯", "印度",
             "巴西", "埃及", "土耳其", "瑞士", "荷兰", "希腊", "葡萄牙", "瑞典", "挪威",
             "丹麦", "芬兰", "冰岛", "波兰", "捷克", "奥地利", "比利时", "爱尔兰", "越南",
             "菲律宾", "印度尼西亚", "阿联酋", "迪拜", "卡塔尔", "沙特阿拉伯", "墨西哥",
             "阿根廷", "智利", "秘鲁", "蒙古", "尼泊尔", "斯里兰卡", "柬埔寨", "老挝", "缅甸")

_DANGER_REQUEST_PATTERNS = [
    r"删除所有|删除全部|清空|抹除|擦除|wipe|format\s*[a-z]:|格式化.{0,4}(系统盘|C盘|全盘|所有磁盘|硬盘分区)",
    r"rm\s*-rf|del\s*/[sqf]|rd\s*/[sq]",
    r"提权|获取root|root权限|绕过(安全|权限|审查)|越权|入侵|攻击|渗透|漏洞利用|exploit",
    r"后门|木马|病毒|蠕虫|勒索|挖矿|矿机|间谍|键盘记录|keylogger|远控|反弹shell|reverse shell",
    r"盗号|窃取|钓鱼|诈骗|伪造|伪造身份|撞库|爆破|破解密码|密码字典",
    r"删除系统文件|破坏系统|瘫痪|自毁|干掉|炸掉|搞垮",
    r"关机|重启(服务器|系统)|init\s+[06]",
    r"监控(他人|别人|用户|员工)隐私|窃听|偷拍",
    r"绕过内容安全|生成恶意|恶意代码|恶意调用集|危险调用集|高危调用集",
    # v3.7 加强: 关闭安全防护 / 篡改系统密码 / 后门自启动 / 提权建号 / 攻击工具
    r"关闭.{0,6}(杀毒|杀毒软件|防火墙|安全卫士|安全中心|防护|defender|antivirus|安全软件)",
    r"禁用.{0,8}(杀毒|防火墙|安全|防护|defender|antivirus|更新服务|自动更新)",
    r"(卸载|移除|干掉).{0,8}(杀毒|防火墙|安全软件|安全卫士)",
    r"修改.{0,8}(他人|别人的|系统|登录|开机).{0,4}密码|改.{0,4}别人.{0,4}密码",
    r"(添加|新建|创建).{0,8}(账号|账户|用户).{0,10}(管理员|root|最高权限)|(添加|新建|创建).{0,10}(管理员|root|最高权限).{0,6}(账号|账户|用户)",
    r"(计划任务|自启动|开机启动).{0,12}(后门|木马|恶意|免杀|隐藏)",
    r"(感染|释放|植入|投递).{0,10}(病毒|木马|恶意|payload|shellcode)",
    r"(免杀|混淆|加密|隐藏).{0,10}(木马|病毒|payload|进程|后门)",
    r"社工库|数据包|拖库|脱库|肉鸡|僵尸网络",
    r"(攻击|入侵).{0,8}(公司|政府|学校|服务器|网站|系统)",
    r"ddos|拒绝服务|洪水攻击",
]
_DANGER_SOFT = ["删除", "格式化", "攻击", "入侵", "提权", "后门", "木马", "病毒",
                "恶意", "绕过", "盗", "窃", "监控", "挖矿", "勒索", "破解", "爆破",
                "自毁", "瘫痪", "root权限", "系统文件"]


def _detect_danger(text: str) -> str | None:
    """返回拦截原因或 None。"""
    t = text or ""
    for pat in _DANGER_REQUEST_PATTERNS:
        if re.search(pat, t, flags=re.I):
            return f"检测到高危操作关键词: {pat}"
    return None


def _pick_path(text: str, default: str | None = None) -> str | None:
    """从语句中抽取一个路径(带引号优先; 支持"X文件夹/目录"纯名称, 交由工具别名映射)。"""
    m = re.search(r"['\"]([^'\"]{1,240})['\"]", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:路径|目录|文件夹|文件)\s*(?:为|是)?\s*([^\s，。；,]{1,240})", text)
    if m:
        cand = m.group(1).strip().rstrip("，。；,")
        if cand and ("/" in cand or "\\" in cand or cand.endswith(":") or cand.startswith(".")):
            return cand
    m = re.search(r"([A-Za-z]:[\\/][^\s，。；,]{0,120}|/[^\s，。；,]{1,120}|\.{1,2}/[^\s，。；,]{1,120}|~/[^\s，。；,]{1,120})", text)
    if m:
        return m.group(1).rstrip("，。；,")
    # "X文件夹/目录"(X为纯名称, 如 desktop/下载/我的桌面)
    m = re.search(r"(?:进入|进到|打开|查看|列出|浏览|读取|看看|找找|切换|定位|名为|叫)\s*([^\s，。；,]{1,40})(?:文件夹|目录)", text)
    if m:
        return m.group(1).strip().rstrip("的")
    # "X文件夹中的文件/内容/东西"
    m = re.search(r"([^\s，。；,]{1,40})(?:文件夹|目录)(?:中|里|里面)?\s*的?\s*(?:文件|内容|东西)", text)
    if m:
        return m.group(1).strip().rstrip("的")
    return default


def _normalize_folder_name(name: str) -> str:
    """把用户口中的文件夹名称归一为可解析目录(家目录别名/去掉"我的"前缀/去掉"文件夹"尾缀)。"""
    n = (name or "").strip().strip("的")
    if not n:
        return n
    if n in ("家", "我的家", "home", "用户", "我的用户") or any(k in n for k in ("家目录", "主目录", "用户目录", "用户文件夹", "user文件夹")):
        return "~"
    n = re.sub(r"^我的", "", n)
    n = re.sub(r"文件夹$", "", n)
    return n or name


def _pick_city(text: str, default: str | None = None) -> str | None:
    # 排除占位/非真实城市词("当前/本地/我的/城市/今天..."等), 避免 geocode 找不到城市
    _BAD = ("当前", "本地", "这里", "这个", "我的", "城市", "今天", "明天", "后天",
            "查询", "看看", "帮我", "请", "一下", "现在", "天气")

    def _clean(s: str) -> str | None:
        s = re.sub(r"^(查询|查一下|查看|看看|帮我|请|一下|今天|的)", "", s or "")
        s = s.rstrip("天气").rstrip("的")
        if s and len(s) >= 2 and not any(s.startswith(b) for b in _BAD) and not any(s == b for b in _BAD):
            return s
        return None

    m = re.search(r"([\u4e00-\u9fa5]{2,8})(?:市|省|县|区)(?:的)?天气", text)
    c = _clean(m.group(1) if m else "")
    if c:
        return c + "市"
    m = re.search(r"天气.{0,6}?([\u4e00-\u9fa5]{2,8})(?:市|省|县|区)", text)
    c = _clean(m.group(1) if m else "")
    if c:
        return c + "市"
    # 国家名("美国天气"): 直接用国家名(geocode 可解), 不拼"市" (优先于一切城市拼接)
    for country in _COUNTRIES:
        if country in text:
            return country
    m = re.search(r"([\u4e00-\u9fa5]{2,4})天气", text)
    c = _clean(m.group(1) if m else "")
    if c:
        return c + "市"
    return default


def _pick_query(text: str) -> str | None:
    for kw in ("搜索", "查一下", "百度一下", "了解一下", "查询"):
        idx = text.find(kw)
        if idx >= 0:
            q = text[idx + len(kw):].strip("，。；!！?？:： 的")
            if q:
                return q
    return None


def _pick_lang(text: str) -> str:
    for lang in ("python", "javascript", "js", "typescript", "html", "css", "bash", "shell", "node"):
        if lang in text.lower():
            return "js" if lang == "javascript" else ("shell" if lang in ("bash", "shell") else lang)
    return "python"


def _drive_or_home(text: str) -> str:
    if re.search(r"C\s*盘|C盘", text, flags=re.I):
        return "C:/" if os.name == "nt" else None  # None -> 交给工具自动回退家目录
    if re.search(r"D\s*盘|D盘", text, flags=re.I):
        return "D:/" if os.name == "nt" else None
    return None


def _extract_args_hint(text: str) -> dict:
    """从操作语句中提取数值参数(供动态合成调用集时生成参数 schema 并立即试执行)。"""
    hint: dict = {}
    nums = re.findall(r"(\d+(?:\.\d+)?)", text)
    nums = [float(n) for n in nums]
    if len(nums) >= 2:
        hint["values"] = nums
    elif len(nums) == 1:
        hint["a"] = nums[0]
        hint["b"] = nums[0]
    m = re.search(r"([\u4e00-\u9fa5]{2,8}(?:市|省|县|区))", text)
    if m:
        hint["city"] = m.group(1)
    m = re.search(r"(?:路径|目录|文件夹)\s*(?:为|是)?\s*([^\s，。；,]{1,120})", text)
    if m:
        hint["path"] = m.group(1).strip().rstrip("，。；,")
    return hint


class MiniAI:
    """小型 AI：理解操作 + 生成调用集 + 参数补全 + 失败修复。"""

    def __init__(self, memory=None, config: dict | None = None):
        self.memory = memory
        self.config = config or {}
        self.default_city = (config.get("agent") or {}).get("default_city", "宜昌")
        # ---- 超强上下文记忆(跨回合): 最近用户话语 + 最近失败记录 ----
        self.recent_user_texts: list = []          # 最近 8 条用户消息
        self.last_failures: list = []              # 最近 8 条失败记录 {tool,args,error,ts}

    # ---------------------------------------------------------------
    # 1. 意图理解 -> 生成新的调用集
    # ---------------------------------------------------------------
    @staticmethod
    def _clean_text(text: str) -> str:
        """超强语言理解第一步: 语病/错别字/口语冗余自动修正。
        - "宜昌是天气" -> "宜昌天气"(城市名+是/了/啊 + 天气的常见口误)
        - "查看下C盘文件 / 打开一下QQ" -> "查看C盘文件 / 打开QQ"
        - "帮我看看" -> "查看" 等口语归一
        """
        t = (text or "").strip()
        if not t:
            return t
        # 城市名后跟"是/了/啦/呀/啊"+天气 -> 去掉语气字("宜昌是天气"->"宜昌天气")
        t = re.sub(r"([\u4e00-\u9fa5]{2,8}?(?:市|省|县|区)?)[是了啦呀啊]天气", r"\1天气", t)
        # 操作动词后的口语后缀归一
        t = re.sub(r"(查看|打开|列出|读取|看看|找找|搜索|查询|运行|执行)(?:一下|一下下|下|一下|一遍|一次)", r"\1", t)
        t = re.sub(r"^(帮我|请帮我|麻烦你|帮我一下|请)(?:查看|看看|浏览|打开|搜索|查询|查找)", "查看", t)
        # "帮我看看xx" -> "查看xx"
        t = re.sub(r"^(帮我|请帮我|麻烦你)(?:看看|浏览|查查|搜下)", "查看", t)
        # 多余语气词清洗(不破坏专名)
        t = re.sub(r"(?:呢|嘛|呀|哦|啦|哈)$", "", t)
        return t.strip()

    def record_user(self, text: str):
        """记录用户最近话语(供上下文理解/纠错)。"""
        self.recent_user_texts.append((text or "").strip())
        self.recent_user_texts = self.recent_user_texts[-8:]

    def record_failure(self, tool: str, args: dict, error: str):
        """记录最近失败(供"再试一次/换成xx"与城市口误纠错回路)。"""
        import datetime as _dt
        self.last_failures.append({"tool": tool, "args": dict(args or {}),
                                   "error": str(error or "")[:300],
                                   "ts": _dt.datetime.now().isoformat()})
        self.last_failures = self.last_failures[-8:]

    def _ctx_fix_city(self, city: str | None, text: str) -> str | None:
        """上下文城市纠错: 上次天气查询因城市口误失败(如"宜昌是"找不到), 这次若仍含类似口误,
        自动修正为上次意图的城市并成功查询。"""
        if not city:
            return city
        # 语气字清洗(城市名中/尾部都可能夹口误字, 如"宜昌是市"里的"是")
        norm = lambda s: re.sub(r"[是了啦呀啊哦的]+", "", s)
        c = norm(city)
        # 与最近失败意图比对: 规范化后 包含/被包含 或 高相似 -> 采用规范化意图城市
        import difflib
        for f in self.last_failures[:4]:
            if f.get("tool") != "get_weather":
                continue
            prev = str((f.get("args") or {}).get("city", ""))
            p2 = norm(prev)
            if not c or not p2:
                continue
            if c in p2 or p2 in c:
                for country in _COUNTRIES:
                    if country in p2 or p2 in country:
                        return country
                return p2 if p2.endswith(("市", "省", "县", "区")) else p2 + "市"
            if abs(len(c) - len(p2)) <= 1 and difflib.SequenceMatcher(None, c, p2).ratio() >= 0.6:
                for country in _COUNTRIES:
                    if country in p2 or p2 in country:
                        return country
                return p2 if p2.endswith(("市", "省", "县", "区")) else p2 + "市"
        # 国家名("美国"): 直接用国家名(geocode 可解), 不拼"市"
        for country in _COUNTRIES:
            if country in city or city in country:
                return country
        # 无失败记录: 仅去掉口误尾缀
        c2 = re.sub(r"[是了啦呀啊哦的]+$", "", city)
        return c2 if c2.endswith(("市", "省", "县", "区")) else c2 + "市"

    def understand(self, text: str, context: dict | None = None) -> Intent:
        text = self._clean_text(text or "").strip()
        low = text.lower()
        ctx = context or {}
        memory_hint = self.memory.as_system_context() if self.memory else ""

        # ---- 纯文本/简单问答(无工具) ----
        if len(text) <= 30 and re.search(r"^(你(好|是谁|叫什么)|hi|hello|嗨|在吗|你好呀)[\s，。！!?？~～]*$", low, flags=re.I):
            return Intent("greeting", 0.95, text_answer="你好，我是 NEURA —— 你的桌面智能体。我可以查看文件、联网查天气/搜索、执行白名单命令、编写并调试代码，并把所有结果呈现在左侧控制板中。试试对我说：\"查看我的家目录\"、\"今天天气怎么样\"、\"帮我写一个计算器程序\"。")
        if re.search(r"(你能做什么|有什么功能|帮助|help|怎么用)", low):
            return Intent("help", 0.95, text_answer=("我能做的事情(全部通过左侧控制板弹窗展示结果)：\n"
                                                      "1. 文件操作：查看目录/读取文件/新建文件\n"
                                                      "2. 系统：系统信息、运行白名单命令、打开应用、当前时间\n"
                                                      "3. 联网：查询天气(Open-Meteo)、网络搜索、抓取网页\n"
                                                      "4. 编程：编写代码->运行->自动调试->打包ZIP下载\n"
                                                      "5. 记忆：记住你的偏好并在后续使用\n"
                                                      "6. 自改进：失败会被记录、学习并通过回归测试后应用规则"))
        if re.search(r"(谢谢|感谢|多谢|thx|thank)", low):
            return Intent("thanks", 0.95, text_answer="不客气。需要任何操作随时告诉我。")
        if re.search(r"(再见|拜拜|bye|晚安)", low):
            return Intent("bye", 0.9, text_answer="再见，随时找我。")

        # ---- 危险意图优先拦截(最高辨别能力, 必须先于任何操作分支) ----
        danger = _detect_danger(text)
        if danger:
            return Intent("blocked", 0.99, {"reason": danger},
                          text_answer=f"安全拦截(小AI 辨别): {danger}。我不会创建或执行任何恶意/高危的调用集。")

        # ---- 文件操作 ----
        if re.search(r"(查看|打开|列出|浏览|读取|看看|找找|进入|进到|切换|定位).{0,30}(目录|文件夹|文件|盘|路径)|(目录|文件夹|文件|盘|路径).{0,30}(查看|打开|列出|浏览|读取)", text) \
                or re.search(r"(有哪些|有什么|都有什么).{0,6}(文件|东西|目录)", text):
            path = _pick_path(text) or _drive_or_home(text) or "~"
            # 纯名称(desktop/下载/我的桌面) -> 归一后交由 list_directory 别名映射到家目录标准目录
            if path and not ("/" in path or "\\" in path or ":" in path or path.startswith(".")):
                path = _normalize_folder_name(path)
            return Intent("list_files", 0.9, {"path": path},
                          tool_plan=[{"name": "list_directory", "arguments": {"path": path, "hidden": False}}])
        m_rf = re.search(r"(?:读取|打开|查看|读)\s*((?:/|~/|\.{1,2}/)[A-Za-z0-9_.\-~]{1,80})", low)
        if (re.search(r"(读取|打开|查看|读).{0,20}(文件|内容|源码|代码|配置)", low) or m_rf) and "代码" not in text[:8]:
            path = _pick_path(text) or _drive_or_home(text)
            if path:
                return Intent("read_file", 0.85, {"path": path},
                              tool_plan=[{"name": "read_file", "arguments": {"path": path, "limit": 300}}])

        # ---- 窗口管理(像《极限审判》中的 AI: 可关闭/滑动/聚焦/最小化/自主连续调整控制板窗口) ----
        if re.search(r"(整理|排列|平铺|排布|摆放).{0,6}(窗口|弹窗)|(窗口|弹窗).{0,6}(整理|排列|平铺|排布|摆放)|(全部|所有).{0,3}(最大化|放大|铺满)", text):
            kind = "grid" if re.search(r"(平铺|排列|排布|摆放|整理)", text) else "maximize_all"
            return Intent("panel_layout", 0.9, {"kind": kind},
                          tool_plan=[{"name": "panel_control", "arguments": {"action": "layout", "kind": kind}}])
        # "放大窗口"->最大化; "放大一点窗口/稍微放大一点"->resize(程度词排除)
        if re.search(r"(最大化|铺满).{0,4}(窗口|弹窗)|把.{0,6}窗口.{0,6}(最大化|放大|扩大)", text) \
                or (re.search(r"(放大|扩大).{0,4}(窗口|弹窗)", text)
                    and not re.search(r"(一点|一些|稍微|稍稍|再|略微)", text)):
            return Intent("panel_control", 0.88, {"action": "maximize", "window_id": self._pick_win_id(text)},
                          tool_plan=[{"name": "panel_control", "arguments": {"action": "maximize", "window_id": self._pick_win_id(text)}}])
        # 缩放窗口(缩小/调小/小一点/放大一点): 非最大化, 前端丝滑缩放; 支持"稍微缩小一点"等无"窗口"词口语
        _NOT_WIN_OBJ = r"(文件|目录|图片|字体|声音|音量|图标|内容)"
        if re.search(r"(缩小|调小|小一点|减小|变小).{0,4}(窗口|弹窗)|把.{0,6}窗口.{0,6}(缩小|调小|小一点|减小|变小)", text) \
                or (re.search(r"^(?:把|将|稍微|再)?\s*(?:窗口|弹窗)?\s*(?:稍微|再)?\s*(缩小|调小|小一点|减小|变小)", text)
                    and not re.search(_NOT_WIN_OBJ, text)):
            return Intent("panel_control", 0.88, {"action": "resize", "direction": "out", "window_id": self._pick_win_id(text)},
                          tool_plan=[{"name": "panel_control", "arguments": {"action": "resize", "direction": "out", "window_id": self._pick_win_id(text)}}])
        if re.search(r"(放大|扩大|调大|大一点|变大).{0,4}(窗口|弹窗)|把.{0,6}窗口.{0,6}(放大|扩大|调大|大一点|变大)", text) \
                or (re.search(r"^(?:把|将|稍微|再)?\s*(?:窗口|弹窗)?\s*(?:稍微|再)?\s*(放大|扩大|调大|大一点|变大)", text)
                    and not re.search(_NOT_WIN_OBJ, text)):
            return Intent("panel_control", 0.88, {"action": "resize", "direction": "in", "window_id": self._pick_win_id(text)},
                          tool_plan=[{"name": "panel_control", "arguments": {"action": "resize", "direction": "in", "window_id": self._pick_win_id(text)}}])
        if re.search(r"(关闭|关掉|收起|清除).{0,6}(所有|全部)?窗口|(关掉|关闭|收起).{0,4}(窗口|弹窗)", text) \
                or re.search(r"窗口.*(关|没|移除)", text) or "关闭所有窗口" in text:
            if re.search(r"(所有|全部|一切|都|统统|全关|清空)", text):
                return Intent("panel_control", 0.92, {"action": "close_all"},
                              tool_plan=[{"name": "panel_control", "arguments": {"action": "close_all"}}])
            win_id = self._pick_win_id(text)
            return Intent("panel_control", 0.9, {"action": "close", "window_id": win_id},
                          tool_plan=[{"name": "panel_control", "arguments": {"action": "close", "window_id": win_id}}])
        if re.search(r"(最小化|收起|折叠)", text) and re.search(r"(窗口|弹窗)", text):
            return Intent("panel_control", 0.9, {"action": "minimize", "window_id": self._pick_win_id(text)},
                          tool_plan=[{"name": "panel_control", "arguments": {"action": "minimize", "window_id": self._pick_win_id(text)}}])
        if re.search(r"(?:把)?(移动|滑动|拖|挪|移)[^，。；]{0,4}(窗口|弹窗)|(窗口|弹窗)[^，。；]{0,4}(移动|滑动|拖|挪|移)", text):
            m = re.search(r"到[（(]?\s*(\d+)\s*[,，xX]\s*(\d+)", text)
            x, y = (int(m.group(1)), int(m.group(2))) if m else (None, None)
            return Intent("panel_control", 0.85, {"action": "move", "window_id": self._pick_win_id(text), "x": x, "y": y},
                          tool_plan=[{"name": "panel_control", "arguments": {"action": "move", "window_id": self._pick_win_id(text), "x": x, "y": y}}])

        # ---- 旅行/旅游/出行/攻略: 复合推理(天气 + 攻略搜索 + 建议整理, 不是只查天气) ----
        # 以"搜索/查一下/百度"开头 -> 纯搜索意图(不误入旅行复合, 不猜默认城市)
        _is_search_ask = bool(re.search(r"^(搜索|搜一下|搜一搜|查一下|查查|查一查|百度|查询)", text))
        if (not _is_search_ask) and re.search(r"(旅游|旅行|出行|游玩|玩一玩|去.*玩|旅游攻略|行程|度假|春游|秋游)", text):
            city = _pick_city(text)
            dest = None
            if not city:
                m_c = re.search(r"(?:去|到|前往|出发去|计划去)([\u4e00-\u9fa5]{2,8})(?:市|省|县|区|玩|旅游|旅行)?", text)
                if m_c:
                    cand = m_c.group(1).rstrip("玩旅游行").strip("的")
                    if cand and len(cand) >= 2 and not any(b in cand for b in ("明天", "后天", "今天", "周末", "我们", "你们")):
                        dest = cand
                if not dest:
                    m_c2 = re.search(r"([\u4e00-\u9fa5]{2,8}?)(?:旅游|旅行|出游|游玩|攻略)", text)
                    if m_c2:
                        cand2 = m_c2.group(1).strip("搜索").strip("的")
                        if cand2 and len(cand2) >= 2 and not any(b in cand2 for b in ("明天", "后天", "今天", "周末", "我们", "你们", "去", "到")):
                            dest = cand2
            if city:
                dest = city.rstrip("市")
            dest = dest or self.default_city
            is_country = any(c in dest for c in _COUNTRIES) or any(dest == c for c in _COUNTRIES)
            # 国家 -> 天气用国家名(geocode 可解), 城市 -> 城市名+市
            w_city = dest if is_country else (dest + "市")
            days = 3 if re.search(r"(明天|后天|周末|这几天|下周)", text) else 7
            # 搜索关键词: 干净目的地攻略(修复搜出"明天(鲁迅小说)"等答非所问)
            from agent.tools.web import _clean_search_query
            q = _clean_search_query(f"{dest}旅游攻略") or f"{dest}旅游攻略"
            # 保留用户特别主题(爬山/美食/潜水/带孩子 等)
            m_theme = re.search(r"(爬山|登山|美食|吃|潜水|滑雪|冲浪|看海|沙滩|带孩子|亲子|购物|博物馆|自驾|徒步|露营|温泉)", text)
            if m_theme:
                q = q + m_theme.group(1)
            return Intent("travel", 0.9, {"city": w_city, "query": q, "days": days, "dest": dest,
                                          "is_country": is_country},
                          tool_plan=[
                              {"name": "get_weather", "arguments": {"city": w_city, "days": days}},
                              {"name": "web_search", "arguments": {"query": q, "max_results": 6}},
                          ])

        # ---- 天气 ----
        if re.search(r"(天气|气温|温度|下雨|下雪|风力|湿度|预报)", text):
            city = _pick_city(text) or self.default_city
            city = self._ctx_fix_city(city, text) or city   # 上下文纠错: 口误城市自动修正
            return Intent("weather", 0.92, {"city": city},
                          tool_plan=[{"name": "get_weather", "arguments": {"city": city, "days": 7}}])

        # ---- 联网搜索 / 网页 ----
        if re.search(r"(搜索|查一下|百度|谷歌|搜一搜|了解一下|搜下)", low):
            q = _pick_query(text)
            if q:
                return Intent("search", 0.9, {"query": q},
                              tool_plan=[{"name": "web_search", "arguments": {"query": q, "max_results": 6}}])
        if re.search(r"(抓取|打开|读取|访问).{0,6}(网页|网址|链接|网站)|https?://", low):
            m = re.search(r"https?://[^\s，。；,]+", text)
            if m:
                return Intent("fetch", 0.9, {"url": m.group(0)},
                              tool_plan=[{"name": "fetch_webpage", "arguments": {"url": m.group(0), "max_chars": 6000}}])

        # ---- 运行/操控任意软件(QQ/微信/任意应用): AI 运行电脑里的任何软件并使用它 ----
        if re.search(r"(打开|运行|启动|开启|调用)\s*(?:一下)?\s*([\u4e00-\u9fa5A-Za-z0-9_\-]{1,20})(?:软件|程序|应用|客户端)?", text) \
                and re.search(r"(软件|程序|应用|客户端|QQ|微信|chrome|浏览器|Steam|网易|音乐|视频)", text, flags=re.I):
            m = re.search(r"(打开|运行|启动|开启|调用)\s*(?:一下)?\s*([\u4e00-\u9fa5A-Za-z0-9_\-]{1,20})(?:软件|程序|应用|客户端)?", text)
            if m:
                app = re.sub(r"(软件|程序|应用|客户端)$", "", m.group(2))
                return Intent("app_launch", 0.9, {"app": app},
                              tool_plan=[{"name": "app_control", "arguments": {"action": "launch", "app": app}}])
        # 给 QQ 号发消息(数字号段 -> app 默认 QQ, to=QQ号)
        m_qq = re.search(r"给\s*(?:QQ号|QQ|qq)?\s*(?:为|是|号|：|:)?\s*(\d{5,12})\s*(?:(?:QQ|qq)?号?)?\s*(?:发送|发)\s*(?:一条)?\s*(?:消息|信息|内容)?[:：]?\s*(.+)?", text)
        if m_qq:
            qq_to = m_qq.group(1)
            qq_text = (m_qq.group(2) or "").strip()
            qq_text = re.sub(r"^(发送|发|送|消息|信息|内容)\s*", "", qq_text)
            qq_text = re.sub(r"(消息|信息)$", "", qq_text).strip()
            qq_text = qq_text.strip("\"'“”‘’ ，。")
            qq_text = qq_text or "你好，这是 NEURA 代发的消息"
            return Intent("app_message", 0.92, {"app": "QQ", "to": qq_to, "text": qq_text},
                          tool_plan=[{"name": "app_control",
                                      "arguments": {"action": "send_message", "app": "QQ",
                                                    "to": qq_to, "text": qq_text}}])

        # 用 QQ/微信 给 X 发消息
        m_msg = re.search(r"(?:用|在|通过)?\s*([\u4e00-\u9fa5A-Za-z0-9_\-]{1,16})\s*(?:给|向|对)\s*([\u4e00-\u9fa5A-Za-z0-9_\-]{1,24})?\s*(?:发|发送)(?:一条)?\s*(?:消息|信息|内容)?(?:[:：]?\s*(.+))?", text)
        if m_msg and m_msg.group(1) and re.search(r"(发|发送|消息|信息)", text):
            to = (m_msg.group(2) or "").strip()
            msg_text = (m_msg.group(3) or "").strip() or ("你好，这是 NEURA 代发的消息" if not to else "")
            if to or msg_text:
                app = re.sub(r"(软件|程序|应用|客户端|里|中|上)$", "", m_msg.group(1))
                return Intent("app_message", 0.88, {"app": app, "to": to, "text": msg_text},
                              tool_plan=[{"name": "app_control", "arguments": {"action": "send_message", "app": app, "to": to, "text": msg_text}}])
        # 截取软件界面
        if re.search(r"(截取|截图|看看界面|查看界面).{0,8}(软件|界面|屏幕|窗口|画面|QQ|微信)", text):
            return Intent("app_screenshot", 0.85, {},
                          tool_plan=[{"name": "app_control", "arguments": {"action": "screenshot"}}])
        # 杀死/结束进程
        m_kill = re.search(r"(杀死|杀掉|结束|终止|关闭|退出)\s*([\u4e00-\u9fa5A-Za-z0-9_\-]{1,20})(?:进程|软件|程序|应用|进程名)?", text)
        if m_kill and re.search(r"(进程|软件|程序|应用|QQ|微信|chrome|记事本|任务|进程名)", text, flags=re.I):
            name = re.sub(r"(进程|软件|程序|应用)$", "", m_kill.group(2))
            return Intent("kill", 0.88, {"name": name},
                          tool_plan=[{"name": "kill_process", "arguments": {"name": name}}])

        # ---- 上下文重试: "再试一次 / 重新查询 / 换个城市" -> 重试最近失败的操作 ----
        if re.search(r"(再试一次|重新(查询|搜索|执行|运行)|重试|换个(城市|地方|说法)|换一个|换成|换到|改为|改成|再来一次|还是失败|再查一次)", text):
            # 最新失败优先(record_failure 是 append, 最新在末尾)
            cand = list(reversed(self.last_failures[:6]))
            # "换成xx市/城市" -> 优先天气类失败并替换城市(城市名在句尾操作词前截断)
            m_c = re.search(r"(?:换成|换到|改为|改成|用)([\u4e00-\u9fa5]{2,6}?)(?:市|省|县|区)?(?:再查一次|再查|再试|查询|看看|的?天气|旅游|旅行|一次)?", text)
            if m_c:
                for f in cand:
                    if f.get("tool") in ("get_weather", "web_search", "travel"):
                        args = dict(f.get("args", {}) or {})
                        args["city"] = m_c.group(1) + "市"
                        return Intent("retry", 0.8, args,
                                      tool_plan=[{"name": f.get("tool"), "arguments": args}])
            for f in cand:
                tool = f.get("tool", "")
                args = dict(f.get("args", {}) or {})
                if tool and tool != "synthesize_tool":
                    return Intent("retry", 0.8, args,
                                  tool_plan=[{"name": tool, "arguments": args}])
            # 无失败记录 -> 继续走正常意图

        # ---- 系统资源利用率(真实采样 CPU/内存/磁盘利用率%) ----
        # 明确"持续/实时/一直/每隔N秒/监控/监测" -> 走 monitor_task 实时任务; 否则单次采样
        if not re.search(r"(持续|实时|一直|每隔|每.{0,3}秒|循环|监控|监测)", text, flags=re.I):
            if re.search(r"(CPU|中央处理器|处理器|内存|运行内存|物理内存|磁盘|硬盘|显卡|GPU|网卡|带宽)(利用|占用|使用)率|(利用率|占用率|使用率)|占用.{0,4}(内存|CPU|处理器|磁盘|硬盘|多少|百分之)|(磁盘|硬盘|内存|CPU|处理器)(使用|占用)(情况|状况|多少|空间)", text, flags=re.I):
                return Intent("system_stats", 0.92, {},
                              tool_plan=[{"name": "system_stats", "arguments": {}}])

        # ---- 安装 Python 模块/依赖(缺依赖时也会被自动询问) ----
        if re.search(r"(安装|装一下|装个|缺少|缺|需要装).{0,8}(模块|库|依赖|包|扩展|插件|软件包)|pip\s+install", text, flags=re.I):
            m = re.search(r"(?:安装|装一下|装个|缺少|缺|需要装).{0,6}(?:一个|个|一下)?([A-Za-z_][A-Za-z0-9_\-]{1,48})", text)
            pkg = (m.group(1) if m else "").strip()
            if pkg and pkg.lower() not in ("模块", "库", "依赖", "包", "东西", "软件"):
                return Intent("install_dep", 0.92, {"package": pkg},
                              tool_plan=[{"name": "install_dependency", "arguments": {"package": pkg}}])

        # ---- 系统信息 / 命令 / 时间 ----
        if re.search(r"(系统信息|系统配置|配置信息|电脑配置|电脑信息|查看配置|内存|CPU|处理器|磁盘|硬盘|系统版本|配置情况)", text) \
                and not re.search(r"(利用|占用|使用)率|(磁盘|硬盘|内存|CPU|处理器)(使用|占用)(情况|状况|多少|空间)", text) \
                and not re.search(r"(持续|实时|一直|循环|监控|监测|每\d+\s*(秒|分钟|小时))", text):
            return Intent("sysinfo", 0.9, {},
                          tool_plan=[{"name": "system_info", "arguments": {}}])
        # 系统进程查看(管理员级只读命令, 结果红色警示窗口展示)
        if re.search(r"(进程|tasklist)", low) and re.search(r"(查看|看|列表|当前|运行中|有哪些|有什么)", text):
            cmd = "tasklist" if os.name == "nt" else "ps aux"
            return Intent("process", 0.9, {"command": cmd},
                          tool_plan=[{"name": "run_command", "arguments": {"command": cmd, "cwd": "~", "timeout": 30}}])
        if re.search(r"(现在|当前|今天|今).{0,3}(时间|几点|日期|星期)", text) or re.search(r"^(时间|几点|日期|星期)", text):
            return Intent("time", 0.95, {},
                          tool_plan=[{"name": "get_time", "arguments": {}}])
        if re.search(r"(运行|执行|跑一下|执行命令)", text) or low.startswith(("ls ", "dir ", "cat ", "type ", "ping ", "echo ", "pwd", "whoami", "df", "du", "ps", "netstat", "tasklist", "systeminfo", "python", "python3", "node", "npm", "pip")):
            cmd = re.sub(r"^(运行|执行|跑一下|执行命令|帮我|请)\s*", "", text).strip()
            if cmd:
                return Intent("command", 0.8, {},
                              tool_plan=[{"name": "run_command", "arguments": {"command": cmd, "cwd": "~", "timeout": 30}}])

        # ---- 编程 ----
        if re.search(r"(写|编写|生成|做一个|写一个|开发|实现).{0,10}(程序|代码|脚本|软件|应用|项目|函数|网页|爬虫|计算器|小工具)", text):
            lang = _pick_lang(text)
            return Intent("code", 0.9, {"requirement": text, "language": lang},
                          tool_plan=[{"name": "code_task", "arguments": {"requirement": text, "language": lang}}])
        if re.search(r"(运行|执行|测试).{0,6}(代码|脚本|程序)", text):
            path = _pick_path(text)
            args = {"language": _pick_lang(text), "timeout": 30}
            if path:
                args["path"] = path
            return Intent("run_code", 0.85, args,
                          tool_plan=[{"name": "run_code", "arguments": args}])

        # 打开系统应用(记事本/计算器/浏览器/终端/cmd 等)——须在编程分支之后, 避免抢占"写计算器程序"
        app_map = {"记事本": "notepad", "计算器": "calculator", "浏览器": "browser",
                   "终端": "terminal", "命令行": "terminal", "cmd": "terminal",
                   "资源管理器": "explorer", "我的电脑": "explorer", "文件管理器": "explorer"}
        for kw, app in app_map.items():
            if kw in text.lower() and not re.search(r"(写|编|生成|做|开发|实现)", text):
                return Intent("open_app", 0.92, {"app": app},
                              tool_plan=[{"name": "open_app", "arguments": {"app": app}}])

        # ---- 预览可预览的内容(图片/HTML/文本) ----
        m_pv = re.search(r"(预览|查看|看看|打开).{0,6}(图片|照片|壁纸|截图|头像|封面|logo)", text, flags=re.I)
        m_pvfile = re.search(r"(预览|查看|看看|打开)\s*([\w./~\\\-]{2,80}\.(?:png|jpe?g|gif|webp|bmp|html?))", text, flags=re.I)
        if m_pv or m_pvfile:
            path = None
            if m_pvfile:
                path = m_pvfile.group(2)
            else:
                path = _pick_path(text) or _drive_or_home(text)
                if not path:
                    m_dir = re.search(r"(?:预览|查看|看看|打开)\s*(?:(?!我的|你的))([\u4e00-\u9fa5]{1,6}?)(?:上|里|中|里面)?\s*的?\s*(?:图片|照片|壁纸|截图|头像|封面|logo)", text)
                    path = (m_dir.group(1) if m_dir else "") or "~"
            if path:
                return Intent("preview", 0.9, {"path": path},
                              tool_plan=[{"name": "preview_file", "arguments": {"path": path}}])

        # ---- 删除文件/文件夹(高危: 触发系统登录密码解锁流程) ----
        _DEL_V = r"删除|删掉|移除|卸掉|清除|清理"
        m_del = re.search(r"(" + _DEL_V + r").{0,14}(文件|文件夹|目录|图片|照片)", text) \
                or re.search(r"(?:" + _DEL_V + r")\s*[A-Za-z0-9_.\-~\u4e00-\u9fa5/\\]{1,80}", text)
        if m_del:
            path = _pick_path(text) or _drive_or_home(text)
            if not path:
                mm = re.search(r"(?:" + _DEL_V + r")\s*[A-Za-z0-9_.\-~\u4e00-\u9fa5/\\]{1,80}", text)
                if mm:
                    path = re.sub(r"^(?:" + _DEL_V + r")", "", mm.group(0)).strip().rstrip("的")
            if path:
                return Intent("delete", 0.88, {"path": path},
                              tool_plan=[{"name": "delete_file", "arguments": {"path": path}}])

        # ---- 查找文件(全盘/家目录搜索文件名, 如"查找所有名为run.py的文件") ----
        if re.search(r"(查找|找一下|寻找|搜索文件|找出|找找).{0,14}(文件|run|名为|名字|\\.py|\\.txt)", text) \
                or re.search(r"(所有|全部).{0,8}(名为|叫|名字)?\s*[\w.\-]{1,80}\s*(文件|\\.py|\\.txt)", text):
            m_f = re.search(r"名为\s*[\"'“”‘’]?([^\s，。；'\"“”‘’]{1,80})[\"'“”‘’]?", text) \
                  or re.search(r"([\w.\-]{1,80}(?:\.py|\.txt|\.json|\.html|\.js|\.css|\.zip|\.docx|\.xlsx|\.pdf))\b", text)
            fname = (m_f.group(1) if m_f else "").strip() or "run.py"
            root = _drive_or_home(text) or "~"
            return Intent("find_file", 0.88, {"name": fname, "root": root},
                          tool_plan=[{"name": "find_file", "arguments": {"name": fname, "root": root}}])

        # ---- 知识问答/征询(是什么/为什么/怎么/解释/你觉得/行吗/建议等) -> 联网搜索并整理(绝不答非所问) ----
        if re.search(r"(?:什么是|是什么|啥是|啥叫|为什么|为啥|怎么回事|怎么办|怎么|如何|解释|介绍|含义|区别|原理|作用|多少钱|有哪些|你觉得|你认为|怎么样|行不行|行吗|好吗|好不好|建议|首都|首府|省会|在哪儿|在哪里|在哪|位于|是谁|哪国|哪个国家|什么国家|人口|面积|总统|主席|国王|首相|货币|语言|国旗|国歌|历史|由来|起源|资料|定义|怎么形成的|哪里|哪儿|多少(岁|年|米|公里))", text) \
                and not re.search(r"(打开|运行|启动|关闭|删除|杀掉|杀死|预览|截屏|截图|写个|写一|写一个|编程|代码|程序|文件夹|文件|窗口|股票|天气|气温)", text):
            # 关键词提取: 问题变陈述核心(修复"美国的首都在哪儿"搜出无关结果)
            from agent.tools.web import _clean_search_query
            q = _clean_search_query(text) or re.sub(r"^(帮我|请|快|立刻|马上)", "", text).strip()[:24]
            if len(q) < 3:
                q = re.sub(r"^(帮我|请|快|立刻|马上)", "", text).strip()[:24]
            return Intent("web_search", 0.85, {"query": q, "max_results": 6},
                          tool_plan=[{"name": "web_search", "arguments": {"query": q, "max_results": 6}}])

        # ---- 股票: 趋势图/走势/K线 -> 真实行情数据 + 控制板趋势图 + 建议 ----
        if re.search(r"(股票|股价|行情|走势|K线|趋势图|走势图|股价图|涨跌|大盘)", text) \
                and re.search(r"(图|走势|趋势|K线|行情|涨跌|怎么样|如何|帮我|查|看看|分析|建议|画)", text):
            m_sym = re.search(r"([A-Za-z]{1,5}\d{3,6}|\d{6}|\d{5})", text)
            m_name = re.search(r"([\u4e00-\u9fa5]{2,10}?)(?:股票|股价|行情|走势|的走势|趋势图|K线|走势图|股价)", text)
            sym = m_sym.group(1) if m_sym else ""
            nm = ""
            if m_name:
                nm = m_name.group(1)
            if not sym and not nm:
                m_n2 = re.search(r"(?:的)?([\u4e00-\u9fa5]{2,6}?)(?:股票|股价|行情|走势)", text)
                if m_n2:
                    nm = m_n2.group(1)
            # 股票名清洗: 剥口语壳("给我画一下贵州茅台" -> "贵州茅台")
            if nm:
                nm = nm.strip("的")
                for _ in range(3):
                    nm2 = re.sub(r"^(给我|帮我|请|画|绘|看看|查查|查一下|查|分析|一下|做个|做|制作|生成|来|要)", "", nm)
                    if nm2 == nm:
                        break
                    nm = nm2
                nm = nm.strip("的 给")
            return Intent("stock_chart", 0.85, {"symbol": sym, "name": nm},
                          tool_plan=[{"name": "stock_chart",
                                      "arguments": {"symbol": sym, "name": nm}}])
        # ---- 股票普通行情(联网查询) / 建议提问(上下文记忆 -> 真实标的; 无标的 -> 直接给建议不乱搜) ----
        if re.search(r"(股票|股价|行情|涨跌|大盘)", text):
            from agent.tools.web import _clean_search_query
            m_sym2 = re.search(r"([A-Za-z]{1,5}\d{3,6}|\d{6}|\d{5})", text)
            m_nm2 = re.search(r"([\u4e00-\u9fa5]{2,10}?)(?:股票|股价|行情|走势)", text)
            name2 = (m_nm2.group(1) if m_nm2 else "").strip("的") if m_nm2 else ""
            # 建议类提问("你觉得可以买这个股票吗")且无明确标的 -> 上下文记忆找最近提到的股票; 仍无 -> 直接给建议(不搜索跑题)
            import re as _re2
            _shell = _re2.sub(r"(你|我|它|觉得|认为|可以|能|能不能|可不可以|买|卖|这个|那个|该|这只|吗|呢|啊|值不值得|值得|应该|要不要|适合|一下|的|股票|股价)", "", name2 or "").strip()
            _cq0 = _clean_search_query(text) if name2 else ""
            if not m_sym2 and (not _shell or _cq0 in ("股票", "行情", "最新行情")):
                hist = " ".join(self.recent_user_texts[-6:])
                hm = re.search(r"([A-Za-z]{1,5}\d{3,6}|\d{6}|\d{5})", hist)
                hm2 = re.search(r"([\u4e00-\u9fa5]{2,10}?)(?:股票|股价|行情|走势|趋势图)", hist)
                if hm:
                    hm_name = (hm2.group(1) if hm2 else "").strip("的").rstrip("股票").strip()
                    return Intent("stock_chart", 0.9, {"symbol": hm.group(1), "name": hm_name},
                                  tool_plan=[{"name": "stock_chart",
                                              "arguments": {"symbol": hm.group(1), "name": hm_name}}])
                return Intent("stock_advice", 0.82,
                              text_answer=("关于股票买卖：我不能代替你做投资决策，但可以帮你做真实数据分析。"
                                           "请告诉我具体的股票代码或名称（例如“查看688836股票的趋势图”），"
                                           "我会拉取真实行情/走势/资金面数据，在控制板画出趋势图并给出数据面参考。"
                                           "（提醒：市场有风险，任何买卖决策请结合自身情况独立判断。）"))
            # 有明确标的: 净化后查询真实行情
            cq = _clean_search_query(text)
            if not cq or cq == "股票":
                cq = (name2 + " 最新行情") if name2 else ("股票 最新行情")
            else:
                cq = cq + " 最新行情"
            return Intent("stock", 0.85, {"query": cq},
                          tool_plan=[{"name": "web_search", "arguments": {"query": cq, "max_results": 6}}])

        # ---- 通用绘图(折线/柱状/饼图等): AI 认为需要可视化时构建图表 ----
        if re.search(r"(画|绘制|做|生成|给我|看看).{0,6}(折线图|柱状图|饼图|趋势图|图表|走势图|统计图|柱状|折线|饼状|占比图)", text):
            kind = "bar" if re.search(r"(柱状|柱形|条形)", text) else ("pie" if re.search(r"(饼图|饼状|占比)", text) else "line")
            return Intent("chart", 0.85, {"kind": kind, "title": text[:20]},
                          tool_plan=[{"name": "chart", "arguments": {"kind": kind, "title": text[:24]}}])

        # ---- 通用绘画: 生成图片文件(SVG 绘制 -> 控制板预览 + 下载) ----
        if re.search(r"(画|绘|生成|创作|做|制作|设计).{0,10}(一张|一幅|个)?(图|画|海报|logo|LOGO|头像|壁纸|封面|插画|漫画|示意图|结构图|流程图|思维导图|风景|人物|动物|标志|图标|卡通)", text) \
                and not re.search(r"(股票|K线|走势图|折线|柱状|饼图|图表|趋势图)", text):
            desc = re.sub(r"^(请|帮我|给我|我要|我想|AI|你|能不能|可以)", "", text).strip()
            return Intent("draw_image", 0.8, {"desc": desc},
                          tool_plan=[{"name": "draw_image", "arguments": {"desc": desc}}])

        # ---- 软件/应用安装(命令可装 -> 命令安装; 否则图形化安装器接管) ----
        if re.search(r"(安装|下载并安装|装一下|装个|装一个|setup|install)", low) \
                and not re.search(r"(代码|模块|库|pip install|npm install)", low):
            # 判断包名
            m_pkg = re.search(r"(?:安装|装一下|装个|装一个|下载并安装|install)\s*[:：]?\s*([a-zA-Z0-9._+-]+|[\u4e00-\u9fa5]{2,12})", text)
            pkg = m_pkg.group(1).strip() if m_pkg else ""
            if not pkg:
                pkg = re.sub(r"^(请|帮我|我要|我想|你)", "", text).replace("安装", "").replace("下载并安装", "").strip("，。！!?？ ")
                pkg = pkg or "unknown"
            is_sw = re.search(r"(微信|QQ|github|chrome|浏览器|桌面|客户端|软件|应用|程序|steam|office|wps|钉钉|飞书|ps|photoshop)", low)
            return Intent("install_package", 0.85, {"package": pkg, "is_software": bool(is_sw)},
                          tool_plan=[{"name": "install_package",
                                      "arguments": {"package": pkg, "method": "gui" if is_sw else "auto"}}])

        # ---- 实时任务监控(持续/定时/循环执行, 控制板实时刷新) ----
        if re.search(r"(持续|实时|一直|循环|每\d+\s*(秒|分钟|小时)|定时|跟踪|监控|监测).{0,12}(监控|监测|跟踪|执行|运行|查看|采样|刷新|采集|检测)", text):
            m_sec = re.search(r"每(\d+)\s*(秒|分钟|小时)", text)
            interval = int(m_sec.group(1)) if m_sec else 2
            if "分钟" in text: interval *= 60
            if "小时" in text: interval *= 3600
            target = re.sub(r"^(请|帮我|给我|持续|实时|一直|循环|定时|监控|监测)", "", text).strip()
            # 监控目标归一: CPU/内存/磁盘/资源 -> "系统资源"(采样系统指标), 其余保留为命令
            if re.search(r"(CPU|内存|磁盘|硬盘|显卡|资源|系统|利用率|占用|使用率)", target):
                target = "系统资源"
            elif len(target) > 30:
                target = target[:30]
            return Intent("monitor_task", 0.85, {"target": target, "interval": interval},
                          tool_plan=[{"name": "monitor_task", "arguments": {"target": target, "interval": interval}}])

        # ---- 记忆 ----
        if re.search(r"(记住|别忘了|以后.*叫|我(?:在|住|工作在|喜欢|偏好))", text) and len(text) < 60:
            return Intent("memory", 0.8, {"text": text})

        # ---- 控制板 ----
        if re.search(r"(弹窗|弹出|在控制板|控制台)(显示|展示|弹)", text):
            content = text.replace("弹窗", "").replace("弹出", "").replace("显示", "").replace("在控制板", "").strip("，。 ")
            return Intent("panel", 0.8, {"content": content},
                          tool_plan=[{"name": "panel_popup", "arguments": {"title": "信息", "content": content, "window_type": "info"}}])

        # ---- 动态能力: 未匹配但像操作的请求 -> 交给小AI 工具工厂合成新调用集 ----
        if self.looks_like_operation(text):
            args_hint = _extract_args_hint(text)
            return Intent("synthesize", 0.55, {"operation": text},
                          tool_plan=[{"name": "synthesize_tool",
                                      "arguments": {"operation": text,
                                                    "suggested_name": self._suggest_name(text),
                                                    "args_hint": args_hint}}])

        # ---- 默认: 交给上层模型; 引擎模式下给出诚实降级 ----
        return Intent("unknown", 0.3)

    @staticmethod
    def _suggest_name(text: str) -> str:
        m = re.search(r"(?:求|算|算一下|查|统计|分析|转换|提取|汇总|生成)[^，。；]{0,8}([\u4e00-\u9fa5A-Za-z0-9]{2,12})", text)
        if m:
            s = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", m.group(1))
            if s:
                return "tool_" + s
        return ""

    @staticmethod
    def _pick_win_id(text: str) -> str:
        """从自然语言中尝试提取窗口 ID(形如 code_flow_xxx / win_xxx); 提取不到返回空串,
        由前端作用于当前聚焦窗口。"""
        m = re.search(r"([a-z][\w]{2,40})", text)
        if m and re.match(r"^(code|win|tool|panel)", m.group(1)):
            return m.group(1)
        return ""

    # ---------------------------------------------------------------
    # 1.5 辨别能力: 危险请求拦截
    # ---------------------------------------------------------------
    def detect_dangerous_request(self, text: str) -> str | None:
        return _detect_danger(text)

    # ---------------------------------------------------------------
    # 1.6 操作型句式判断(供动态合成兜底)
    # ---------------------------------------------------------------
    _OP_VERBS = ["帮我", "请", "把", "对", "统计", "分析", "计算", "转换", "提取", "汇总",
                 "生成", "获取", "下载", "监控", "检查", "对比", "合并", "拆分", "翻译",
                 "格式化", "解析", "抓取", "备份", "压缩", "解压", "排序", "筛选", "去重",
                 "聚类", "预测", "爬取", "扫描", "检测", "校验", "转换", "整理", "计算",
                 "发送", "发消息", "消息", "给", "QQ", "微信", "打电话",
                 "窗口", "缩小", "放大", "最大化", "关闭", "滑动", "移动", "拖动", "整理",
                 "查找", "寻找", "找一下", "搜索文件", "名为", "文件", "目录", "文件夹",
                 "天气", "气温", "温度", "搜索", "查一下", "查询", "代码", "编程", "写个",
                 "下载", "软件", "运行", "打开", "启动", "删除", "清理", "移除", "杀掉",
                 "杀死", "结束", "进程", "预览", "截图", "截屏", "股票", "行情", "股价",
                 "时间", "几点", "系统", "配置", "信息", "注册表", "命令行", "终端"]

    @staticmethod
    def looks_like_chitchat(text: str) -> bool:
        """纯闲聊/问候/征询类信号(这类才交给独家大模型 NeuraLM 生成; 其余一律模板, 杜绝答非所问)。

        问候词必须出现在句首(避免"给QQ号…你好"里内容中的"你好"误判);
        征询语气(你觉得/怎么样/行不行等)整体算闲聊, 交给大模型给建议。
        """
        if not text:
            return False
        low = text.lower()
        if re.search(r"^(你好|您好|嗨|哈喽|hello|hi\b|在吗|谢谢|感谢|再见|拜拜|晚安|早上好|下午好|晚上好)", low):
            return True
        return bool(re.search(r"(你是谁|你叫什么|能做什么|会什么|会做|介绍一下|干什么的|可以帮我吗)", low))

    def looks_like_operation(self, text: str) -> bool:
        t = (text or "").strip()
        if len(t) < 4 or len(t) > 200:
            return False
        if re.search(r"[？?！!。]$", t) and not re.search(r"(请|帮我|把|对)", t):
            return False
        return any(v in t for v in self._OP_VERBS)

    # ---------------------------------------------------------------
    # 1.7 调用命令判定核心:
    #     已存在 -> 直接执行不新增; 不存在 -> 小AI 理解+联网+生成+审查+注册
    # ---------------------------------------------------------------
    TOOL_ALIASES = {
        "list_files": "list_directory", "open_file": "read_file", "view_file": "read_file",
        "show_files": "list_directory", "ls": "list_directory", "dir": "list_directory",
        "get_weather_now": "get_weather", "weather": "get_weather", "query_weather": "get_weather",
        "search_web": "web_search", "google": "web_search", "websearch": "web_search",
        "fetch_url": "fetch_webpage", "get_url": "fetch_webpage", "open_url": "fetch_webpage",
        "run_shell": "run_command", "shell": "run_command", "exec_cmd": "run_command",
        "system_stats": "system_info", "sysinfo": "system_info", "computer_info": "system_info",
        "current_time": "get_time", "now_time": "get_time", "datetime_now": "get_time",
        "math": "calculator", "calc": "calculator", "compute": "calculator",
        "write_code": "write_code_file", "save_code": "write_code_file",
        "execute_code": "run_code", "run_script": "run_code", "test_code": "run_code",
        "download_zip": "package_project", "zip_download": "package_project",
        "make_dir": "create_folder", "new_folder": "create_folder", "mkdir": "create_folder",
        "show_panel": "panel_popup", "popup": "panel_popup", "display": "panel_popup",
        "notify_user": "notify", "send_notice": "notify", "alert": "notify",
        "download_files": "package_download", "get_files": "package_download",
    }

    def fuzzy_find_tool(self, raw_name: str, registry) -> str | None:
        """判断调用命令是否已存在:
        1) 别名表精确映射; 2) 名称精确/包含; 3) 名称+描述模糊检索。不存在返回 None。"""
        low = (raw_name or "").strip().lower().replace("-", "_")
        if not low:
            return None
        if low in self.TOOL_ALIASES:
            return self.TOOL_ALIASES[low]
        hits = registry.search(low, top=1)
        if hits:
            return hits[0].name
        return None

    async def resolve_or_synthesize(self, raw_name: str, args: dict, registry,
                                    agent=None, ctx=None) -> SynthResult:
        """小AI 对调用命令的三路判定主流程。"""
        args = dict(args or {})

        # 路1: 调用集已存在 -> 不新增, 直接执行
        hit = self.fuzzy_find_tool(raw_name, registry)
        if hit is not None:
            return SynthResult(tool_name=hit, args=self.normalize_args(hit, args), new_tool=False)

        # 路2: 危险/恶意调用命令 -> 拦截(最高辨别能力)
        op_text = f"{raw_name} {' '.join(str(v) for v in args.values())}"
        reason = self.detect_dangerous_request(op_text)
        if reason:
            return SynthResult(rejected=reason)

        # 路3: 不存在 -> 小AI 理解 + 联网 + 思考 + 生成 + 审查 + 注册
        from agent.tools.dynamic import CustomToolStore
        store = (ctx.extra or {}).get("tool_store") if ctx else None
        if store is None and agent is not None:
            store = getattr(agent, "custom_tools", None)
        if store is None:
            return SynthResult(rejected="动态调用集存储不可用")
        operation = raw_name if (isinstance(raw_name, str) and len(raw_name) >= 4 and " " in raw_name) \
            else f"实现功能: {raw_name}，参数参考 {args}"
        res = await store.synthesize_and_register(
            operation=operation, suggested_name=raw_name, args_hint=args,
            registry=registry, agent=agent, ctx=ctx)
        if not res.ok:
            return SynthResult(rejected=res.error or "合成失败")
        name = res.data.get("name", "")
        return SynthResult(tool_name=name,
                           args=self.normalize_args(name, args),
                           new_tool=True, code=res.data.get("code", ""),
                           review=f"通过五层安全审查 v{res.data.get('version', 1)}")

    # ---------------------------------------------------------------
    # 2. 参数补全 / 校验(任何模式下都执行)
    # ---------------------------------------------------------------
    def normalize_args(self, tool_name: str, args: dict, ctx: dict | None = None) -> dict:
        args = dict(args or {})
        if tool_name == "list_directory":
            if not args.get("path"):
                args["path"] = "~"
            args.setdefault("hidden", False)
        elif tool_name == "read_file":
            if not args.get("path"):
                args["path"] = "~"
            args.setdefault("limit", 300)
        elif tool_name == "get_weather":
            if not args.get("city"):
                args["city"] = self.default_city
            args.setdefault("days", 7)
        elif tool_name == "web_search":
            args.setdefault("max_results", 6)
        elif tool_name == "run_code":
            args.setdefault("language", "python")
            args.setdefault("timeout", 30)
        elif tool_name == "write_file" or tool_name == "write_code_file":
            if not args.get("path") and args.get("name"):
                args["path"] = args["name"]
        elif tool_name == "run_command":
            args.setdefault("timeout", 30)
            if not args.get("cwd"):
                args["cwd"] = "~"
        elif tool_name == "synthesize_tool":
            if not args.get("operation") and args.get("需求"):
                args["operation"] = args["需求"]
            args.setdefault("suggested_name", "")
        return args

    # ---------------------------------------------------------------
    # 3. 失败修复建议
    # ---------------------------------------------------------------
    def suggest_fix(self, tool_name: str, error: str, args: dict, active_rules: list | None = None) -> dict | None:
        """根据错误信息返回修正后的参数; 无解返回 None。"""
        if not error:
            return None
        err = str(error)
        args = dict(args or {})
        fixed = dict(args)
        changed = False

        if "不存在" in err or "No such file" in err or "not found" in err or "没有找到" in err:
            if tool_name in ("read_file", "list_directory", "write_file", "run_code"):
                p = str(fixed.get("path", ""))
                if p and os.sep not in p and "/" not in p and "\\" not in p:
                    fixed["path"] = os.path.join(os.path.expanduser("~"), p)
                    changed = True
                elif p and tool_name == "list_directory":
                    fixed["path"] = os.path.dirname(os.path.abspath(p))
                    changed = True
        if "路径被拒绝" in err or "拒绝" in err:
            fixed["path"] = os.path.expanduser("~")
            changed = True
        if "找不到城市" in err or "city" in err.lower():
            if tool_name == "get_weather" and fixed.get("city"):
                fixed["city"] = str(fixed["city"]).replace("市", "") + "省" + str(fixed["city"]).replace("市", "") + "市" if False else str(fixed["city"])
                changed = True
        if "编码" in err and tool_name == "read_file":
            fixed["encoding"] = "gbk"
            changed = True
        if "超时" in err and tool_name in ("run_code", "run_command"):
            fixed["timeout"] = max(int(fixed.get("timeout", 30)) + 30, 60)
            changed = True
        # 应用自改进规则(active_rules)
        for rule in (active_rules or []):
            if rule.get("tool") == tool_name and rule.get("match") in err:
                hint = rule.get("hint", {})
                if isinstance(hint, dict):
                    fixed = utils.deep_merge(fixed, hint)
                    changed = True
        return fixed if changed else None

    # ---------------------------------------------------------------
    # 4. 引擎模式: 文本答复生成(工具结果摘要 / 未知问题降级)
    # ---------------------------------------------------------------
    def respond_text(self, last_message: dict, tool_results: list | None = None) -> str:
        role = last_message.get("role", "")
        content = last_message.get("content", "")
        if role == "tool":
            try:
                data = utils.read_json if isinstance(content, str) and content.startswith("{") else None
                import json as _json
                if content and (content.startswith("{") or content.startswith("[")):
                    obj = _json.loads(content)
                    if isinstance(obj, dict):
                        if "current" in obj:  # 天气结果
                            c = obj["current"]
                            return (f"已为您查询到天气并展示在控制板：当前 {c.get('city','')} "
                                    f"{c.get('temperature','')}℃ {c.get('condition','')}，"
                                    f"体感 {c.get('feels_like','')}℃，湿度 {c.get('humidity','')}%。")
                        if "time" in obj and "date" in obj:  # 时间结果
                            return f"现在是 {obj.get('date','')} {obj.get('time','')}，{obj.get('weekday','')}（时区 {obj.get('timezone','')}）。"
                        if "os" in obj:  # 系统信息
                            mem = obj.get("memory_mb", {})
                            return (f"系统信息已展示在控制板：{obj.get('system','')} / {obj.get('release','')}，"
                                    f"CPU: {str(obj.get('cpu',''))[:40]}，内存 {mem.get('used_mb',0)}MB/{mem.get('total_mb',0)}MB，"
                                    f"磁盘剩余 {obj.get('disk_free_gb','')}GB。")
                        if "filename" in obj and "url" in obj:  # 打包下载结果
                            return f"打包完成：{obj.get('filename','')}，下载已触发。"
                        if "name" in obj and "code" in obj and "update" in obj:  # 动态调用集合成结果
                            act = "升级" if obj.get("update") else "新增"
                            extra = ""
                            fr = obj.get("first_result")
                            if isinstance(fr, dict) and "error" not in fr:
                                extra = f"，并已立即试执行成功: {str(fr)[:80]}"
                            elif isinstance(fr, dict):
                                extra = f"，试执行提示: {str(fr.get('error',''))[:80]}"
                            return f"小AI 已为 Agent {act}调用集「{obj.get('name')}」v{obj.get('version',1)}，并通过全部安全审查，立即生效{extra}。"
                        if obj.get("error_type") == "forbidden":  # 安全拦截结果
                            return f"该请求已被小AI 安全辨别拦截：{obj.get('error','')}"
                        if "files" in obj and isinstance(obj.get("files"), list) and obj.get("files") and \
                                isinstance(obj["files"][0], str) and "window_id" in obj:  # 代码生成结果
                            return f"代码已生成并运行（{len(obj['files'])} 个文件），下载已触发，过程详见控制板。"
                        if "files" in obj:
                            return f"已列出目录，共 {len(obj.get('files', []))} 个条目，详情见控制板。"
                        if "results" in obj and isinstance(obj["results"], list):
                            return f"搜索完成，返回 {len(obj['results'])} 条结果，详见控制板。"
                        if "action" in obj and "window_id" in obj:  # 窗口管理(panel_control)
                            act_cn = {"close": "已关闭", "close_all": "已关闭全部", "move": "已滑动",
                                      "focus": "已聚焦", "minimize": "已最小化", "restore": "已还原",
                                      "maximize": "已最大化", "resize": "已缩放"}.get(
                                obj.get("action"), "已操作")
                            wid = obj.get("window_id") or "当前聚焦窗口"
                            return f"控制板窗口操作完成：{act_cn}「{wid}」。"
                        if obj.get("ok") is False:
                            return f"操作未能完成：{obj.get('error','未知错误')}"
                        keys = ", ".join(list(obj.keys())[:5])
                        return f"操作完成，关键字段: {keys}。详见控制板。"
                return "操作完成，结果已呈现在控制板中。"
            except Exception:
                return "操作完成，结果已呈现在控制板中。"
        if role == "user":
            c = str(content)
            if "天气" in c or "气温" in c or "温度" in c:
                return "正在为您联网查询天气，结果将显示在左侧控制板。"
            if "系统信息" in c or "电脑信息" in c or "系统配置" in c or "硬件" in c:
                return "正在为您收集系统信息（系统/硬件/存储/内存/磁盘），结果将显示在左侧控制板。"
            if "进程" in c or "任务" in c or "运行的程序" in c:
                return "正在为您查询系统进程列表，结果将显示在左侧控制板。"
            if any(k in c for k in ("文件", "目录", "盘", "文件夹")):
                return "正在为您读取文件系统，结果将显示在左侧控制板。"
            if "写" in c or "编程" in c or "代码" in c or "程序" in c:
                return "好的，我将进入编程工作流：编写 → 运行 → 自动调试 → 打包下载，全程在控制板展示。"
            if "发送" in c or "发消息" in c or "QQ" in c or "微信" in c or "给" in c:
                return "正在为您通过对应软件发送消息，执行过程将显示在左侧控制板。"
            if "打开" in c or "运行" in c or "启动" in c or "软件" in c:
                return "正在为您启动对应软件，界面将在左侧控制板软件窗口中渲染。"
            if "窗口" in c or "最大化" in c or "放大" in c or "关闭" in c:
                return "正在为您操作控制板窗口，动画与结果将显示在左侧控制板。"
            if "搜索" in c or "查一下" in c or "找找" in c or "最新" in c:
                return "正在联网搜索并为您整理信息，结果将显示在左侧控制板。"
            if "删除" in c or "清理" in c or "移除" in c:
                return "正在为您处理文件/文件夹（高危操作将先验证系统登录密码），结果将显示在左侧控制板。"
            if "预览" in c or "看看" in c:
                return "正在为您预览内容，结果将显示在左侧控制板。"
            if "股票" in c or "行情" in c or "股价" in c:
                return "正在为您联网查询股票行情，结果将显示在左侧控制板。"
        return ("我可以帮你完成几乎任何宿主机操作：查看/删除文件、联网查天气/搜索并整理信息、"
                "运行与操控任意软件（发消息等）、管理进程、预览文件、编写调试代码并打包下载、"
                "滑动/关闭/放大/整理窗口等。请告诉我你想做什么。\n"
                "（模型说明：默认使用内置独家大模型 NeuraLM；若想改用 DeepSeek API，在 config.json "
                "将 use_builtin_model 设为 false、use_deepseek_api 设为 true 并填写 api.deepseek_api_key 后重启即可。）")

    def plan_for(self, intent: Intent) -> list:
        return intent.tool_plan or []

    # ---- 工具名模糊匹配 ----
    def match_tool(self, raw_name: str, tool_names: list) -> str | None:
        low = (raw_name or "").lower().replace("-", "_")
        for t in tool_names:
            if t.lower() == low or low in t.lower() or t.lower() in low:
                return t
        return None

    def interpret_action(self, raw_name: str, args: dict, registry) -> dict:
        """v3.26 AI 直接操控: 把 AI 的操作描述(名称/参数)解析为结构化操作。

        优先名称模糊匹配现有能力; 其次中文/英文常见操作表述关键词映射;
        无法映射返回 capability=None(由 AI 直接用执行能力实现)。不注册任何新调用集。
        """
        name = (raw_name or "").strip().lower().replace("-", "_")
        tool_names = registry.names() if registry is not None else []
        # 1) 名称级模糊匹配
        cap = self.match_tool(name, tool_names)
        if cap:
            return {"capability": cap, "params": args, "confidence": 0.9}
        # 2) 中文/英文操作表述关键词映射
        table = [
            (("list", "目录", "文件夹", "文件列表", "浏览", "查看.*目录", "ls ", "dir "), "list_directory"),
            (("read", "读取.*文件", "查看.*文件内容", "cat "), "read_file"),
            (("write", "写入", "创建.*文件", "新建.*文件"), "write_file"),
            (("weather", "天气", "气温", "降雨", "气象"), "get_weather"),
            (("search", "搜索", "查找信息", "查询.*资料", "百度", "google"), "web_search"),
            (("open_app", "打开.*软件", "启动.*程序", "运行.*软件", "打开qq", "打开微信"), "open_app"),
            (("run_command", "执行.*命令", "命令提示符", "cmd", "terminal", "终端"), "run_command"),
            (("system_info", "系统信息", "cpu", "内存", "cpu利用", "内存利用", "硬件"), "system_stats"),
            (("get_time", "时间", "几点了", "日期"), "get_time"),
            (("calculator", "计算", "算一下"), "calculator"),
            (("panel_control", "关闭.*窗口", "整理.*窗口", "窗口布局"), "panel_control"),
            (("kill_process", "结束.*进程", "杀进程", "终止.*进程", "关闭.*进程"), "kill_process"),
            (("stock_chart", "股票", "股价", "行情", "k线", "趋势图"), "stock_chart"),
            (("draw_image", "绘画", "画一", "生成图片", "绘图"), "draw_image"),
            (("install_package", "安装"), "install_package"),
            (("fetch_webpage", "网页", "url", "http"), "fetch_webpage"),
            (("run_code", "代码", "编程", "脚本", "程序"), "run_code"),
        ]
        for kws, cap_name in table:
            if any(k in name for k in kws):
                return {"capability": cap_name, "params": args, "confidence": 0.7}
        # 3) 无法映射
        return {"capability": None, "params": args, "confidence": 0.0}