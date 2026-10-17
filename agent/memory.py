# -*- coding: utf-8 -*-
"""NEURA 长期记忆：事实存储、提取、召回。"""
import datetime
import re
from agent import utils


class MemoryStore:
    def __init__(self, path: str):
        self.path = path
        data = utils.read_json(path, {"facts": [], "projects": [], "recent_paths": []}) or {}
        self.facts = data.get("facts", []) or []
        self.projects = data.get("projects", []) or []       # 项目/工作目录记忆(持久)
        self.recent_paths = data.get("recent_paths", []) or []  # 最近访问路径(持久, 最多20条)

    def _save(self):
        utils.write_json(self.path, {"facts": self.facts, "projects": self.projects,
                                     "recent_paths": self.recent_paths})

    def remember(self, key: str, value: str, source: str = "agent", confidence: float = 0.8):
        now = utils.now_iso()
        for f in self.facts["facts"]:
            if f.get("key", "").lower() == key.lower():
                f.update({"value": value, "ts": now, "source": source, "confidence": confidence})
                self._save()
                return f
        fact = {"key": key, "value": value, "ts": now, "source": source, "confidence": confidence}
        self.facts["facts"].append(fact)
        self._save()
        return fact

    def recall(self, keyword: str = "", top: int = 10) -> list:
        kw = (keyword or "").strip().lower()
        hits = self.facts["facts"]
        if kw:
            hits = [f for f in hits
                    if kw in str(f.get("key", "")).lower() or kw in str(f.get("value", "")).lower()]
        hits.sort(key=lambda f: (f.get("confidence", 0), f.get("ts", "")), reverse=True)
        return hits[:top]

    def recall_all(self) -> list:
        return self.facts

    # ---- 持久项目记忆: AI 记住用户的工作项目/目录 ----
    def remember_project(self, name: str, path: str, detail: str = "") -> dict:
        now = utils.now_iso()
        for p in self.projects:
            if p.get("name", "").lower() == name.lower():
                p.update({"path": path, "detail": detail or p.get("detail", ""), "ts": now})
                self._save()
                return p
        proj = {"name": name, "path": path, "detail": detail, "ts": now}
        self.projects.append(proj)
        self._save()
        return proj

    def remember_path(self, path: str, label: str = ""):
        """记录最近访问路径(跨会话持久), 供上下文召回。"""
        path = (path or "").strip()
        if not path:
            return
        for p in self.recent_paths:
            if p.get("path") == path:
                p["ts"] = utils.now_iso()
                self._save()
                return
        self.recent_paths.insert(0, {"path": path, "label": label, "ts": utils.now_iso()})
        self.recent_paths = self.recent_paths[:20]
        self._save()

    def extract_project(self, user_text: str) -> dict | None:
        """从语句提取项目信息: '我在D:/work写个项目叫XX' / '这个项目叫XX放在YY目录'。"""
        m = re.search(r"(?:项目|工程|课题|作品集)[^，。；]{0,6}(?:叫|名为|是|:)?\s*([\u4e00-\u9fa5A-Za-z0-9_\-]{2,30})", user_text)
        name = m.group(1) if m else None
        p = re.search(r"(?:在|放在|位于)\s*([^\s，。；]{1,120})", user_text)
        path = p.group(1).strip() if p else None
        if name:
            return self.remember_project(name.strip().rstrip("的"), path or "", user_text[:60])
        return None

    # ---- 从用户语句中提取可记忆事实 ----
    _PATTERNS = [
        (re.compile(r"(?:我叫|我是|我的名字是|you can call me)\s*([\u4e00-\u9fa5A-Za-z0-9_·]{2,20})"),
         lambda m: ("user_name", m.group(1))),
        (re.compile(r"(?:我的|我常|我主要|平时在)[^，。；]{0,12}(?:目录|文件夹|路径|项目)是\s*([^\s，。；]{1,120})"),
         lambda m: ("user_dir", m.group(1))),
        (re.compile(r"我(?:现在|目前)?(?:在|住在|工作于|坐标)[^，。；]{0,6}([\u4e00-\u9fa5]{2,12})(?:市|省)?"),
         lambda m: ("user_city", m.group(1))),
        (re.compile(r"(?:我喜欢|我偏好|我爱用)\s*([\u4e00-\u9fa5A-Za-z0-9 ]{2,24})"),
         lambda m: ("user_preference", m.group(1))),
    ]

    def extract_and_remember(self, user_text: str, source: str = "user"):
        learned = []
        for pat, keyfn in self._PATTERNS:
            m = pat.search(user_text)
            if m:
                try:
                    key, value = keyfn(m)
                    self.remember(key, value, source=source, confidence=0.7)
                    learned.append((key, value))
                except Exception:
                    continue
        return learned

    def as_system_context(self, max_items: int = 12) -> str:
        parts = []
        if self.facts:
            parts.append("【长期记忆】\n" + "\n".join(
                f"- {f['key']}: {f['value']} (来源:{f.get('source','')})" for f in self.facts[-max_items:]))
        if self.projects:
            parts.append("【项目记忆】\n" + "\n".join(
                f"- 项目「{p['name']}」: {p.get('path','')} {p.get('detail','')}" for p in self.projects[-8:]))
        if self.recent_paths:
            parts.append("【最近访问路径】\n" + "\n".join(
                f"- {p.get('path','')}" for p in self.recent_paths[:8]))
        return "\n\n".join(parts)
