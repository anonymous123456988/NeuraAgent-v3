# -*- coding: utf-8 -*-
"""工具：联网(天气 Open-Meteo / 搜索 DuckDuckGo+Bing / 网页抓取)。"""
import html as _html
import json
import re
import urllib.parse

from agent import utils
from .registry import Tool, ToolResult, ToolContext, ToolRegistry

try:
    import httpx
    _HAS_HTTPX = True
except Exception:
    _HAS_HTTPX = False

WMO = {
    0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}


def _wmo_name(code: int) -> str:
    return WMO.get(code, f"天气码{code}")


# 简体化替换表(常见繁体字 -> 简体, 覆盖气象/地理/城市常用字, 保证界面无繁体)
_TRAD_PAIRS = [
    ("臺北", "台北"), ("臺中", "台中"), ("臺南", "台南"), ("臺東", "台东"),
    ("桃園", "桃园"), ("嘉義", "嘉义"), ("彰化", "彰化"), ("雲林", "云林"),
    ("屏東", "屏东"), ("花蓮", "花莲"), ("宜蘭", "宜兰"), ("苗栗", "苗栗"),
    ("澎湖", "澎湖"), ("金門", "金门"), ("連江", "连江"), ("貴陽", "贵阳"),
    ("烏魯木齊", "乌鲁木齐"), ("灣區", "湾区"), ("廣東", "广东"), ("東莞", "东莞"),
    ("廣州", "广州"), ("無錫", "无锡"), ("蘇州", "苏州"), ("重慶", "重庆"),
    ("遼寧", "辽宁"), ("山東", "山东"), ("陝西", "陕西"), ("臺灣", "台湾"),
    ("臺", "台"), ("灣", "湾"), ("廣", "广"), ("東", "东"), ("雲", "云"),
    ("門", "门"), ("龍", "龙"), ("馬", "马"), ("鳥", "鸟"), ("漢", "汉"),
    ("華", "华"), ("陽", "阳"), ("陰", "阴"), ("風", "风"), ("氣", "气"),
    ("點", "点"), ("監", "监"), ("鐵", "铁"), ("銀", "银"), ("網", "网"),
    ("視", "视"), ("線", "线"), ("島", "岛"), ("縣", "县"), ("區", "区"),
    ("鄉", "乡"), ("鎮", "镇"), ("壩", "坝"), ("橋", "桥"), ("靈", "灵"),
    ("爾", "尔"), ("薩", "萨"), ("徹", "彻"), ("貢", "贡"), ("贛", "赣"),
    ("遼", "辽"), ("蘇", "苏"), ("魯", "鲁"), ("閩", "闽"), ("瓊", "琼"),
    ("滬", "沪"), ("晉", "晋"), ("陝", "陕"), ("寧", "宁"), ("壢", "坜"),
]


def _to_simple(s: str) -> str:
    """把任意中文文本转简体(用于 geocoding 返回的城市/行政区名, 保证界面无繁体)。"""
    if not s:
        return ""
    out = str(s)
    for trad, simp in _TRAD_PAIRS:
        out = out.replace(trad, simp)
    return out


def _get_client(cfg: dict, timeout: float | None = None) -> "httpx.Client":
    return httpx.Client(timeout=timeout or cfg.get("network", {}).get("timeout_seconds", 20),
                        headers={"User-Agent": cfg.get("network", {}).get("user_agent", "NeuraAgent/1.0")},
                        follow_redirects=True)


async def _get_weather(args: dict, ctx: ToolContext) -> ToolResult:
    if not _HAS_HTTPX:
        return ToolResult(ok=False, error="未安装 httpx，无法联网。请执行 pip install httpx", error_type="network")
    city = str(args.get("city", "")).strip() or (ctx.config.get("agent") or {}).get("default_city", "宜昌")
    days = min(int(args.get("days", 7)), 7)
    import asyncio
    try:
        geo = await asyncio.to_thread(_geocode, city, ctx.config)
        if not geo:
            return ToolResult(ok=False, error=f"找不到城市: {city}，请提供更完整名称(如 xx省xx市)", error_type="network", retryable=True)
        lat, lon, full_name, admin1, country = geo
        with _get_client(ctx.config) as client:
            r = client.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,is_day",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "timezone": "auto", "forecast_days": days,
            })
            r.raise_for_status()
            d = r.json()
        cur = d.get("current", {})
        daily = d.get("daily", {})
        current = {
            "city": _to_simple(full_name),
            "admin1": admin1 or "",
            "country": country or "",
            "temperature": cur.get("temperature_2m"),
            "feels_like": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind_speed": cur.get("wind_speed_10m"),
            "precipitation": cur.get("precipitation"),
            "condition": _wmo_name(cur.get("weather_code", 0)),
            "is_day": cur.get("is_day"),
            "updated": utils.now_iso(),
        }
        days_list = []
        chart = {"labels": [], "series": []}
        for i in range(len(daily.get("time", []))):
            day = {
                "date": _to_simple(daily["time"][i]),
                "max": daily["temperature_2m_max"][i],
                "min": daily["temperature_2m_min"][i],
                "precip_prob": daily["precipitation_probability_max"][i],
                "condition": _wmo_name(daily["weather_code"][i]),
            }
            days_list.append(day)
            chart["labels"].append(day["date"][5:])
            chart["series"].append({"name": "最高温", "data": [x["max"] for x in days_list]})
            chart["series"].append({"name": "最低温", "data": [x["min"] for x in days_list]})
        return ToolResult(data={"current": current, "daily": days_list, "chart": chart})
    except Exception as e:
        return ToolResult(ok=False, error=f"天气查询失败: {e}", error_type="network", retryable=True)


def _geocode(city: str, cfg: dict):
    def _query(name: str):
        with _get_client(cfg) as client:
            r = client.get("https://geocoding-api.open-meteo.com/v1/search", params={
                "name": name, "count": 3, "language": "zh", "format": "json"})
            r.raise_for_status()
            return r.json().get("results") or []
    data = _query(city)
    # 带行政后缀("天津市/上海省"): 直接去后缀查询优先(避免歧义/漏检)
    if re.search(r"[市省县区盟旗]$", city):
        base = re.sub(r"[市省县区盟旗]$", "", city)
        if base and base != city:
            d2 = _query(base)
            if d2:
                data = d2
    # 兜底: 后缀查询失败时再试去单字后缀
    if not data and re.search(r"[市省县区盟旗]", city):
        for alt in re.sub(r"(市|省|县|区|盟|旗)$", "", city), re.sub(r"[市省县区盟旗]$", "", city):
            if alt and alt != city:
                data = _query(alt)
                if data:
                    break
    if not data:
        return None
    # 优先选中国行政区结果(避免"上海市"解析到美国伊利诺伊州等歧义)
    best = data[0]
    cn = None
    for cand in data:
        cc = str(cand.get("country_code") or cand.get("country") or "")
        ad = str(cand.get("admin1") or "")
        if cc == "CN" or re.search(r"[省市自治区特别行政区]", ad):
            cn = cand
            break
    if cn:
        best = cn
    return (best["latitude"], best["longitude"], _to_simple(best.get("name", city)),
            _to_simple(best.get("admin1", "") or ""), _to_simple(best.get("country_code", "") or ""))


def _clean_search_query(q: str) -> str:
    """搜索关键词规范化: 剥离口语壳词/时间词/语气词, 保留核心名词短语。
    "明天我要去天津旅游" -> "天津旅游" ; "美国的首都在哪儿" -> "美国首都" ; "去美国旅游" -> "美国旅游"
    """
    if not q:
        return ""
    t = q.strip()
    # 1)-3) 迭代剥壳: 时间词 -> 意愿词 -> 方向动词 -> 查看看(循环到稳定)
    for _ in range(4):
        t2 = re.sub(r"^(?:明天|后天|今天|昨天|前天|这周|下周|上周|本周|周末|最近|之后|以前|前几天|下个月|下星期)(?:要|想|打算|计划|准备|去|到|前往)?", "", t)
        t2 = re.sub(r"^(?:帮我|请帮我|请|麻烦你|我想|我要|我想要|我打算|我计划|我准备|计划|打算|准备|你觉得|你认为|你看|你说|给点建议|建议一下)(?:去|到|前往|来)?", "", t2)
        t2 = re.sub(r"^(?:去|到|前往|出发去|来)", "", t2)
        t2 = re.sub(r"^(?:查一下|查询一下|看一下|看看|问问|帮我看看|查查|查|搜一下|搜索|搜一搜|搜索一下|百度)", "", t2)
        if t2 == t:
            break
        t = t2
    # 4) 剥疑问/语气词尾巴(含"吗/呢/啊/呀"及"可以买/能买/可不可以/能不能"类意见壳)
    t = re.sub(r"(怎么样|怎么(?:样|办)?|如何|好吗|吗|呢|啊|呀|嘛|哦|啦|哈|好不好|行不行|是啥|是啥子|是什么|是哪些|有哪些|有什么|在哪里|在哪儿|在哪|是哪|是哪里|哪儿|哪里|怎么走|有什么好玩的|好玩吗)", "", t)
    t = re.sub(r"(可以买|能买|可不可以买|能不能买|值得买|应该买|要不要买|适合买)", "", t)
    t = re.sub(r"(这个|那个|这些|那些|一下)", "", t)
    # 5) "的"连接词在名词间保留, 但在句尾"的"去掉; "的首都/的总统/的总统是谁" -> "首都/总统"
    t = re.sub(r"的(首都|首府|最高峰|最大城市|人口|面积|总统|主席|国王|首相|货币|语言|国旗|国歌|历史|简介|介绍|资料|由来|起源|含义|意思|定义|最新行情|行情|趋势|走势)", r"\1", t)
    # 5) "的"连接词在名词间保留, 但在句尾"的"去掉; "的首都/的总统/的总统是谁" -> "首都/总统"
    t = re.sub(r"的(首都|首府|最高峰|最大城市|人口|面积|总统|主席|国王|首相|货币|语言|国旗|国歌|历史|简介|介绍|资料|由来|起源|含义|意思|定义)", r"\1", t)
    t = re.sub(r"[，,、。.!！?？~～]", "", t)
    t = t.strip("的")
    # 6) 压缩连续空白
    t = re.sub(r"\s+", "", t)
    return t


def _rank_results(query: str, results: list, top_n: int) -> list:
    """搜索相关性过滤: 关键词命中打分 + 去重(URL/标题), 只保留与问题相关的核心结果。"""
    if not results:
        return []
    stop = {"的", "了", "是", "我", "你", "他", "在", "去", "到", "吗", "呢", "啊", "和", "与",
            "怎么", "怎样", "如何", "一个", "一下", "什么", "为什么", "哪个", "哪里", "谁", "哪",
            "你觉得", "你认为", "可以买", "能买", "值不值得", "应该", "有没有", "这个", "那个",
            "最新行情", "看好", "怎么样"}
    words = [w for w in re.findall(r"[\u4e00-\u9fa5]{2,4}|[A-Za-z][A-Za-z0-9]{2,}", query) if w not in stop]
    if not words:
        words = [query[:4]]
    seen_url, seen_title, out = set(), set(), []
    for r in results:
        try:
            title = str(r.get("title", "")).strip()
            url = str(r.get("url", ""))
            snip = str(r.get("snippet", "")).strip()
        except Exception:
            continue
        if not title and not url:
            continue
        key = url.split("?")[0] if url else title
        if key in seen_url:
            continue
        seen_url.add(key)
        dup = False
        for t in seen_title:
            if t and (t in title or title in t):
                dup = True
                break
        if dup:
            continue
        seen_title.add(title)
        score = 0
        blob = title + " " + snip
        for w in words:
            if w and w in title:
                score += 2
            elif w and w in blob:
                score += 1
        out.append((score, r))
    out.sort(key=lambda x: -x[0])
    return [r for _s, r in out[:top_n]]


async def _web_search(args: dict, ctx: ToolContext) -> ToolResult:
    if not _HAS_HTTPX:
        return ToolResult(ok=False, error="未安装 httpx，无法联网搜索", error_type="network")
    query = _clean_search_query(str(args.get("query", "")).strip())
    max_results = min(int(args.get("max_results", 6)), 10)
    if not query:
        return ToolResult(ok=False, error="缺少 query", error_type="invalid_args")
    backends = ctx.config.get("network", {}).get("search_backends",
                                                 ["duckduckgo", "bing", "cn_bing", "baidu", "sogou"])
    import asyncio

    async def _run(backend: str):
        # 每个后端独立 12s 超时(单通道卡死不拖累整体)
        cfg2 = json.loads(json.dumps(ctx.config))
        cfg2["network"] = dict(ctx.config.get("network", {})); cfg2["network"]["timeout_seconds"] = 12
        try:
            if backend == "duckduckgo":
                rs = await asyncio.to_thread(_ddg_search, query, max_results, cfg2)
            elif backend == "cn_bing":
                rs = await asyncio.to_thread(_cn_bing_search, query, max_results, cfg2)
            elif backend == "baidu":
                rs = await asyncio.to_thread(_baidu_search, query, max_results, cfg2)
            elif backend == "sogou":
                rs = await asyncio.to_thread(_sogou_search, query, max_results, cfg2)
            else:
                rs = await asyncio.to_thread(_bing_search, query, max_results, cfg2)
            return rs or None
        except Exception:
            return None

    # 分批并发: 国内可达通道优先(cn_bing/百度/搜狗), 避免窄带宽下通道互相拖垮;
    # 任一通道成功即立即返回, 单批最多等 20s
    domestic = [b for b in backends if b in ("cn_bing", "baidu", "sogou")]
    others = [b for b in backends if b not in domestic]

    async def _batch(bs: list, budget: float):
        if not bs:
            return None
        try:
            rs_all = await asyncio.wait_for(asyncio.gather(*[_run(b) for b in bs]), timeout=budget)
        except (asyncio.TimeoutError, Exception):
            rs_all = []
        for rs in rs_all:
            if rs:
                return rs
        return None

    # 国内通道串行单连(窄带宽下并发会互相拖垮), 成功即早返回
    for b in domestic:
        rs = await _run(b)
        if rs:
            return ToolResult(data={"query": query, "count": min(len(rs), max_results),
                                      "results": _rank_results(query, rs, max_results)})
    rs = await _batch(others, 20)
    if rs:
        return ToolResult(data={"query": query, "count": min(len(rs), max_results),
                                      "results": _rank_results(query, rs, max_results)})
    # 全部失败: 串行兜底再各试一次(通道间可达性不同, 提高容错)
    for b in backends:
        rs = await _run(b)
        if rs:
            return ToolResult(data={"query": query, "count": min(len(rs), max_results),
                                      "results": _rank_results(query, rs, max_results)})
    return ToolResult(ok=False,
                      error="联网搜索超时或网络不可用，已自动重试全部通道仍失败；请稍后重试，或换一个更具体的关键词。",
                      error_type="network", retryable=True)


def _ddg_search(query: str, n: int, cfg: dict) -> list:
    with _get_client(cfg) as client:
        r = client.get("https://html.duckduckgo.com/html/", params={"q": query, "kl": "cn-zh"})
        r.raise_for_status()
        text = r.text
    results = []
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, flags=re.S):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        if "uddg=" in href:
            href = urllib.parse.unquote(re.search(r"uddg=([^&]+)", href).group(1))
        results.append({"title": _html.unescape(title).strip(), "url": href})
        if len(results) >= n:
            break
    snips = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', text, flags=re.S)
    for i, s in enumerate(snips[:n]):
        if i < len(results):
            results[i]["snippet"] = _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    return results


def _cn_bing_search(query: str, n: int, cfg: dict) -> list:
    """必应中国(cn.bing.com / www.bing.cn): 国内可达, 中文结果质量高。"""
    with _get_client(cfg) as client:
        r = client.get("https://cn.bing.com/search", params={
            "q": query, "setlang": "zh-hans", "mkt": "zh-CN"})
        r.raise_for_status()
        text = r.text
    results = []
    for m in re.finditer(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', text, flags=re.S):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        if href.startswith("http"):
            results.append({"title": _html.unescape(title).strip(), "url": href})
            if len(results) >= n:
                break
    for i, s in enumerate(re.finditer(r'<p[^>]*class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', text, flags=re.S)):
        if i < len(results):
            results[i]["snippet"] = _html.unescape(re.sub(r"<[^>]+>", "", s.group(1))).strip()
    return results


def _baidu_search(query: str, n: int, cfg: dict) -> list:
    """百度搜索(www.baidu.com/s): 国内可达, 兼容反爬(UA/重试)。"""
    with _get_client(cfg) as client:
        r = client.get("https://www.baidu.com/s", params={"wd": query, "ie": "utf-8", "rn": n})
        r.raise_for_status()
        text = r.text
    results = []
    for m in re.finditer(r'<h3[^>]*class="[^"]*c-title[^"]*"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         text, flags=re.S):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        if "baidu.com/link" in href or href.startswith("http"):
            results.append({"title": _html.unescape(title).strip(),
                            "url": href if href.startswith("http") else "https://www.baidu.com" + href})
            if len(results) >= n:
                break
    for i, s in enumerate(re.finditer(r'<span[^>]*class="[^"]*content-right_[^"]*"[^>]*>(.*?)</span>|<div[^>]*class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</div>',
                                      text, flags=re.S)):
        g = s.group(1) or s.group(2) or ""
        if i < len(results):
            results[i]["snippet"] = _html.unescape(re.sub(r"<[^>]+>", "", g)).strip()[:160]
    return results


def _sogou_search(query: str, n: int, cfg: dict) -> list:
    """搜狗搜索(www.sogou.com/web): 国内可达。"""
    with _get_client(cfg) as client:
        r = client.get("https://www.sogou.com/web", params={"query": query})
        r.raise_for_status()
        text = r.text
    results = []
    for m in re.finditer(r'<h3[^>]*class="[^"]*vr-title[^"]*"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         text, flags=re.S):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        results.append({"title": _html.unescape(title).strip(), "url": href})
        if len(results) >= n:
            break
    for i, s in enumerate(re.finditer(r'<p[^>]*class="[^"]*str_info[^"]*"[^>]*>(.*?)</p>', text, flags=re.S)):
        if i < len(results):
            results[i]["snippet"] = _html.unescape(re.sub(r"<[^>]+>", "", s.group(1))).strip()
    return results


def _bing_search(query: str, n: int, cfg: dict) -> list:
    with _get_client(cfg) as client:
        r = client.get("https://www.bing.com/search", params={"q": query, "setlang": "zh-hans", "mkt": "zh-CN"})
        r.raise_for_status()
        text = r.text
    results = []
    for m in re.finditer(r'<h2><a href="([^"]+)"[^>]*>(.*?)</a></h2>', text, flags=re.S):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        if href.startswith("http") and "bing.com" not in urllib.parse.urlparse(href).netloc:
            results.append({"title": _html.unescape(title).strip(), "url": href})
            if len(results) >= n:
                break
    snippets = re.finditer(r'<p[^>]*class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', text, flags=re.S)
    for i, s in enumerate(snippets):
        if i < len(results):
            results[i]["snippet"] = _html.unescape(re.sub(r"<[^>]+>", "", s.group(1))).strip()
    return results


class _TextExtractor:
    def __init__(self):
        self.skip_depth = 0
        self.parts = []

    def handle(self, tag, attrs):
        pass

    def data(self, text):
        if self.skip_depth == 0:
            self.parts.append(text)

    def close(self):
        pass


def _extract_text(html_text: str, max_chars: int) -> str:
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.skip = 0
            self.out = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript", "svg", "head"):
                self.skip += 1
            if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "pre"):
                self.out.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript", "svg", "head") and self.skip:
                self.skip -= 1
            if tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "pre"):
                self.out.append("\n")

        def handle_data(self, data):
            if self.skip == 0:
                self.out.append(data)

    p = P()
    try:
        p.feed(html_text)
    except Exception:
        pass
    raw = "".join(p.out)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    return raw[:max_chars]


async def _fetch_webpage(args: dict, ctx: ToolContext) -> ToolResult:
    if not _HAS_HTTPX:
        return ToolResult(ok=False, error="未安装 httpx，无法抓取网页", error_type="network")
    url = str(args.get("url", "")).strip()
    max_chars = int(args.get("max_chars", 6000))
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    import asyncio
    try:
        text = await asyncio.to_thread(_fetch_text, url, ctx.config, max_chars)
        return ToolResult(data={"url": url, "chars": len(text), "text": text})
    except Exception as e:
        return ToolResult(ok=False, error=f"抓取失败: {e}", error_type="network", retryable=True)


def _fetch_text(url: str, cfg: dict, max_chars: int) -> str:
    with _get_client(cfg) as client:
        r = client.get(url)
        r.raise_for_status()
        return _extract_text(r.text, max_chars)


_STOCK_MAP = {
    # 常用 A 股
    "平安银行": "sz000001", "万科a": "sz000002", "万科": "sz000002", "中兴通讯": "sz000063",
    "五粮液": "sz000858", "格力电器": "sz000651", "美的集团": "sz000333", "宁德时代": "sz300750",
    "东方财富": "sz300059", "比亚迪": "sz002594", "贵州茅台": "sh600519", "茅台": "sh600519",
    "中国平安": "sh601318", "招商银行": "sh600036", "工商银行": "sh601398", "农业银行": "sh601288",
    "建设银行": "sh601939", "隆基绿能": "sh601012", "中芯国际": "sh688981", "药明康德": "sh603259",
    "恒瑞医药": "sh600276", "伊利股份": "sh600887", "中国中免": "sh601888", "三一重工": "sh600031",
    "海尔智家": "sh600690", "美的": "sz000333", "紫金矿业": "sh601899", "中远海控": "sh601919",
    "长江电力": "sh600900", "中国石油": "sh601857", "中国石化": "sh600028", "中国移动": "sh600941",
    "京东方a": "sz000725", "立讯精密": "sz002475", "海康威视": "sz002415", "顺丰控股": "sz002352",
    "山西汾酒": "sh600809", "泸州老窖": "sz000568", "片仔癀": "sh600436",
    # 港股
    "腾讯": "hk00700", "腾讯控股": "hk00700", "阿里巴巴": "hk09988", "京东": "hk09618",
    "美团": "hk03690", "百度": "hk09888", "小米": "hk01810", "网易": "hk09999",
    "中国移动港股": "hk00941", "比亚迪港股": "hk01211", "快手": "hk01024", "理想汽车": "hk02015",
    "蔚来": "hk09866", "小鹏汽车": "hk09868", "海底捞": "hk06862", "拼多多港股": "hk09999",
    # 美股
    "苹果": "usAAPL", "微软": "usMSFT", "谷歌": "usGOOGL", "特斯拉": "usTSLA", "英伟达": "usNVDA",
    "亚马逊": "usAMZN", "meta": "usMETA", "脸书": "usMETA", "奈飞": "usNFLX", "英特尔": "usINTC",
    "amd": "usAMD", "台积电": "usTSM", "迪士尼": "usDIS", "可口可乐": "usKO", "波音": "usBA",
    "高盛": "usGS", "摩根大通": "usJPM", "美国银行": "usBAC", "麦当劳": "usMCD", "耐克": "usNKE",
    "星巴克": "usSBUX", "优步": "usUBER", "ai龙头": "usNVDA",
}
_STOCK_NAME = {v: k for k, v in _STOCK_MAP.items()}


def _resolve_symbol(symbol: str, name: str = "") -> str | None:
    """解析股票标识: 名称->代码(前缀), 6位数字->A股(6开头沪/0,3开头深), 代码->补前缀。"""
    s = (symbol or "").strip().lower().replace(" ", "")
    n = (name or "").strip().lower()
    if not s and n:
        return _STOCK_MAP.get(n)
    if n and n in _STOCK_MAP:
        return _STOCK_MAP[n]
    if s in _STOCK_MAP:
        return _STOCK_MAP[s]
    if s.isdigit():
        if len(s) == 6:
            return ("sh" if s.startswith(("5", "6", "9")) else "sz") + s
        if len(s) == 5:
            return "hk" + s
    if s.startswith(("sh", "sz", "hk", "us")):
        return s
    return None


async def _stock_chart(args: dict, ctx: ToolContext) -> ToolResult:
    """真实股票行情趋势图: 腾讯行情接口日K线(国内可达, 真实数据, 非模拟)。"""
    if not _HAS_HTTPX:
        return ToolResult(ok=False, error="未安装 httpx，无法获取行情", error_type="network")
    symbol = str(args.get("symbol", "")).strip()
    name = str(args.get("name", "")).strip()
    code = _resolve_symbol(symbol, name)
    if not code:
        # 名称未映射 -> 尝试搜代码
        try:
            from agent.tools.web import _clean_search_query
            sr = await _web_search({"query": _clean_search_query(f"{name} 股票代码") or f"{name} 股票代码",
                                    "max_results": 3}, ctx)
            if sr.ok:
                hint = "; ".join(r.get("title", "") for r in (sr.data or {}).get("results", [])[:2])[:200]
                return ToolResult(ok=False, error=f"未识别该股票/代码，可尝试输入 6 位A股代码或常见名称(如 贵州茅台/腾讯/苹果)。检索参考: {hint}",
                                  error_type="invalid_args")
        except Exception:
            pass
        return ToolResult(ok=False, error="未识别该股票/代码，可尝试输入 6 位A股代码或常见名称(如 贵州茅台/腾讯/苹果)",
                          error_type="invalid_args")
    import asyncio
    try:
        r = await asyncio.to_thread(_fetch_kline, code)
    except Exception as e:
        return ToolResult(ok=False, error=f"行情接口异常: {e}", error_type="network", retryable=True)
    if r is None:
        return ToolResult(ok=False, error="行情接口不可达或该代码无数据，请稍后重试或换一只股票", error_type="network", retryable=True)
    display = _STOCK_NAME.get(code, name or symbol)
    closes = [p["close"] for p in r]
    first, last = closes[0], closes[-1]
    change_pct = round((last - first) / first * 100, 2) if first else 0
    ma5 = round(sum(closes[-5:]) / 5, 3) if len(closes) >= 5 else None
    ma20 = round(sum(closes[-20:]) / 20, 3) if len(closes) >= 20 else None
    high = max(p["high"] for p in r)
    low = min(p["low"] for p in r)
    # 精简点数据(仅日期+收盘)以控制体积, 避免模型通道截断; 完整高低价由统计字段覆盖
    slim = [{"date": p["date"], "close": p["close"]} for p in r]
    return ToolResult(data={
        "symbol": code, "name": display, "count": len(r),
        "points": slim, "closes": closes, "change_pct": change_pct,
        "last": last, "first": first, "high": high, "low": low,
        "ma5": ma5, "ma20": ma20,
        "source": "腾讯行情(真实日K)", "suggested_name": name,
    })


def _fetch_kline(code: str) -> list | None:
    """拉取腾讯日K: [date, open, close, high, low, volume]。"""
    import httpx as _h
    with _h.Client(timeout=12, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True) as c:
        r = c.get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,80,qfq")
        r.raise_for_status()
        d = r.json()
    node = d.get("data", {}).get(code, {})
    rows = node.get("day") or node.get("qfqday") or []
    out = []
    for row in rows:
        try:
            out.append({"date": str(row[0]), "open": float(row[1]), "close": float(row[2]),
                        "high": float(row[3]), "low": float(row[4]),
                        "volume": float(row[5]) if len(row) > 5 else 0})
        except (TypeError, ValueError, IndexError):
            continue
    return out or None


async def _chart(args: dict, ctx: ToolContext) -> ToolResult:
    """通用图表调用集: 把数据渲染为控制板科技图表窗口(折线/柱状/饼图), 供 AI 自主构建可视化。"""
    title = str(args.get("title", "数据图表"))
    kind = str(args.get("kind", "line"))
    labels = args.get("labels") or []
    series = args.get("series") or []
    if not series and args.get("values"):
        series = [{"name": args.get("series_name", "数据"), "points": args["values"]}]
    if not series:
        return ToolResult(ok=False, error="缺少 series/values 数据", error_type="invalid_args")
    return ToolResult(data={"title": title, "kind": kind, "labels": labels, "series": series})


def register(registry: ToolRegistry):
    registry.register(Tool(
        name="stock_chart",
        description=("获取某股票真实历史行情趋势(腾讯行情日K, 非模拟), 返回收盘/开高低/涨跌幅/均线, 供控制板绘制趋势图与给出建议。"
                     "支持 A股(名称或6位代码)/港股(名称或5位代码)/美股(常见名称)"),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "股票代码, 如 sz000001 / 600519 / hk00700 / usAAPL, 或 6 位数字"},
                "name": {"type": "string", "description": "股票名称(中文), 如 贵州茅台 / 腾讯 / 苹果; 与 symbol 二选一"},
            },
        },
        handler=_stock_chart,
        category="network",
    ))
    registry.register(Tool(
        name="chart",
        description=("通用图表调用集: 把结构化数据渲染为控制板科技图表窗口(折线/柱状/饼图)。"
                     "AI 认为需要可视化时(趋势/对比/分布/预测等)自动调用。"),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "图表标题"},
                "kind": {"type": "string", "description": "line 折线 / bar 柱状 / pie 饼图"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "X轴/类目标签"},
                "series": {"type": "array", "description": "[{name, points: [[x,y],...]}] 或 [{name, value}]"},
            },
        },
        handler=_chart,
        category="network",
    ))
    registry.register(Tool(
        name="get_weather",
        description="联网查询某城市实时天气与未来多天预报(Open-Meteo)。结果应在控制板 weather 弹窗展示。",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名, 如 宜昌/北京/上海"},
                "days": {"type": "integer", "description": "预报天数 1-7"},
            },
        },
        handler=_get_weather,
        category="network",
    ))
    registry.register(Tool(
        name="web_search",
        description="联网搜索(query), 返回标题/链接/摘要列表。结果应在控制板 data 弹窗展示。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
        handler=_web_search,
        category="network",
    ))
    registry.register(Tool(
        name="fetch_webpage",
        description="抓取网页正文文本。",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["url"],
        },
        handler=_fetch_webpage,
        category="network",
    ))
