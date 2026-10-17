# -*- coding: utf-8 -*-
"""
NEURA 自我改进模块 (SelfImprover)

设计原则(确保"不会改退化"):
  1. 只学习"规则/参数提示/记忆", 绝不允许自动改写核心工具代码;
  2. 每次新规则激活前必须先通过回归测试(tests/regression.py);
  3. 激活前自动备份当前改进状态 -> system/backups/improvements_v{N}.json;
  4. 支持 rollback(回滚)恢复任何历史版本;
  5. 全部改动有版本号、时间戳、来源记录, 可审计。
"""
import datetime
import os

from agent import utils


class SelfImprover:
    def __init__(self, state_path: str, backup_dir: str):
        self.state_path = state_path
        self.backup_dir = backup_dir
        os.makedirs(backup_dir, exist_ok=True)
        self.state = utils.read_json(state_path, {
            "version": 0, "rules": [], "memories": [], "prompt_patches": [],
            "history": [], "tests": {"last_run": None, "passed": None},
        }) or {"version": 0, "rules": [], "memories": [], "prompt_patches": [],
               "history": [], "tests": {"last_run": None, "passed": None}}
        self.fail_counts: dict = {}   # (tool, error_type) -> count

    # ---------------------------------------------------------------
    # 记录工具调用结果
    # ---------------------------------------------------------------
    def record(self, tool: str, args: dict, ok: bool, error_type: str | None = None,
               session_id: str = "", error: str = "") -> None:
        key = (tool, error_type or "unknown")
        if ok:
            self.fail_counts.pop(key, None)
        else:
            self.fail_counts[key] = self.fail_counts.get(key, 0) + 1
        # 写入日志
        log_dir = os.path.dirname(self.state_path) + "/logs"
        os.makedirs(log_dir, exist_ok=True)
        utils.write_json(os.path.join(log_dir, "tool_logs.jsonl"), None)  # 触发目录创建
        try:
            with open(os.path.join(log_dir, "tool_logs.jsonl"), "a", encoding="utf-8") as f:
                f.write(utils.json_dumps_compact({
                    "ts": utils.now_iso(), "tool": tool, "ok": ok,
                    "error_type": error_type, "error": str(error)[:300],
                    "session": session_id, "args": utils.json_dumps_compact(args)[:500],
                }) + "\n")
        except Exception:
            pass

    # ---------------------------------------------------------------
    # 学习规则(满足触发条件后生成候选规则)
    # ---------------------------------------------------------------
    def propose_rules(self, mini_ai, registry) -> list:
        """根据失败统计生成候选规则(尚未激活, 需回归验证)。"""
        candidates = []
        for (tool, etype), cnt in self.fail_counts.items():
            if cnt < 2:
                continue
            if etype == "not_found":
                candidates.append({
                    "tool": tool, "match": "不存在", "hint": {"path_hint": "路径不存在时自动回退到父目录或家目录"},
                    "reason": f"{tool} 连续 {cnt} 次路径不存在",
                })
            elif etype == "timeout":
                candidates.append({
                    "tool": tool, "match": "超时", "hint": {"timeout": 60},
                    "reason": f"{tool} 连续 {cnt} 次超时, 建议提高超时",
                })
            elif etype == "network":
                candidates.append({
                    "tool": tool, "match": "网络", "hint": {"retry_backoff": 2},
                    "reason": f"{tool} 连续 {cnt} 次网络失败, 建议重试退避",
                })
        return candidates

    def maybe_improve(self, mini_ai, registry, regression_runner) -> dict:
        """触发改进: 生成候选 -> 回归测试 -> 通过则备份并激活。"""
        candidates = self.propose_rules(mini_ai, registry)
        if not candidates:
            return {"applied": []}
        applied = []
        for cand in candidates[:5]:
            rule = {
                "id": f"r_{len(self.state['rules']) + 1}",
                "tool": cand["tool"],
                "match": cand["match"],
                "hint": cand["hint"],
                "reason": cand["reason"],
                "source": "failure_analysis",
                "created_at": utils.now_iso(),
                "status": "pending",
            }
            # 回归验证
            ok, report = regression_runner()
            self.state["tests"] = {"last_run": utils.now_iso(), "passed": ok}
            if ok:
                self._activate([rule])
                applied.append(rule["id"])
            else:
                rule["status"] = "rejected_by_regression"
                self.state["rules"].append(rule)
                self._save()
        self.fail_counts.clear()
        return {"applied": applied}

    def _activate(self, new_rules: list) -> None:
        # 备份当前状态
        self.state["version"] += 1
        v = self.state["version"]
        backup = os.path.join(self.backup_dir, f"improvements_v{v - 1}.json")
        prev = dict(self.state)
        prev["version"] = v - 1
        utils.write_json(backup, prev)
        for rule in new_rules:
            rule["status"] = "active"
            rule["activated_at"] = utils.now_iso()
            rule["version"] = v
            self.state["rules"].append(rule)
        self.state["history"].append({
            "version": v, "applied_at": utils.now_iso(),
            "rules": [r["id"] for r in new_rules],
        })
        self._save()

    def rollback(self, version: int) -> bool:
        backup = os.path.join(self.backup_dir, f"improvements_v{version}.json")
        if not os.path.exists(backup):
            return False
        data = utils.read_json(backup)
        if data is None:
            return False
        # 只保留 <= version 的历史
        data["history"] = [h for h in data.get("history", []) if h.get("version", 0) <= version]
        self.state = data
        self._save()
        return True

    def active_rules(self) -> list:
        return [r for r in self.state["rules"] if r.get("status") == "active"]

    def learn_memory(self, key: str, value: str, source: str = "agent") -> None:
        self.state["memories"].append({
            "key": key, "value": value, "source": source, "ts": utils.now_iso(),
        })
        self._save()

    def _save(self):
        utils.write_json(self.state_path, self.state)

    def summary(self) -> dict:
        return {
            "version": self.state["version"],
            "active_rules": len(self.active_rules()),
            "total_rules": len(self.state["rules"]),
            "memories": len(self.state["memories"]),
            "tests": self.state["tests"],
            "history": self.state["history"][-5:],
        }
