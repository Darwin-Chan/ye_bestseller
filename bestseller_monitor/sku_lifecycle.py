"""Durable per-call SKU work, shared by judgments and recommendations.

Only completed results may be reused. Context keys describe current evidence;
manual legality is checked by each caller before and after the external call.
"""
from __future__ import annotations

import copy
import os
import sqlite3
import json

from .matching import ModelFailure, digest, signature_parts
from .matching_stop import stop_reason
from .sku_grouping import identity

LABELS = {
    "inconclusive": "图文证据不确定，尚未得到有效判断", "missing_evidence": "缺少图片证据", "needs_config": "需要模型配置",
    "model_unavailable": "模型不可用", "failed": "调用失败",
    "stopped": "本次匹配已停止；停止原因：未记录。", "completed_uncertain": "已完成但无法推荐唯一组",
    "completed_negative": "已完成并确定非同款", "completed_positive": "已完成并得到有效同款判断",
}
RETRYABLE = {"needs_config", "model_unavailable", "failed", "stopped", "inconclusive"}

def restore_stop_reasons(snapshot):
    """旧草稿只有停止状态，不能据此推断用户操作。保留已记录的新原因。"""
    unknown = LABELS["stopped"]
    for task in snapshot.get("model_tasks", {}).values():
        if task.get("state") == "stopped" and "停止原因：" not in task.get("reason", ""):
            task["reason"] = unknown
    for state in snapshot.get("matching", {}).get("states", []):
        if state.get("state") == "stopped" and "停止原因：" not in state.get("reason", ""):
            state["reason"] = unknown
    for member in snapshot.get("members", []):
        for task in member.get("model_work", []):
            if task.get("state") == "stopped" and "停止原因：" not in task.get("reason", ""):
                task["reason"] = unknown
        member["matching_note"] = member.get("matching_note", "").replace("用户/运行已停止", unknown)


def cache_signature(config, rule):
    """Return the durable SKU cache signature used by production work."""
    return digest({**signature_parts(config), "rule": rule}) if config and config.mode != "disabled" else None


# 两张 SKU 缓存表的生产 DDL：打包（票 08）与收取导入都照这一份口径备表。
# machine_id 只作来源与冲突说明、不参与键；一条结果记「第一次收到」的来源。
SKU_JUDGMENTS_DDL = """CREATE TABLE IF NOT EXISTS sku_judgments (
    pair TEXT, signature TEXT, machine_id TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL, PRIMARY KEY(pair, signature))"""
SKU_RECOMMENDATIONS_DDL = """CREATE TABLE IF NOT EXISTS sku_recommendations (
    context TEXT, signature TEXT, machine_id TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL, PRIMARY KEY(context, signature))"""


def ensure_tables(conn, machine_id):
    """备好两张 SKU 缓存表，并给老形状就地补来源列（一次只付条件判断）。

    补列前的行只有本机能写（那时还没有导入路径），所以老行都算本机产生的、
    来源记本机——与产品判断表升级同一口径。
    """
    conn.execute(SKU_JUDGMENTS_DDL)
    conn.execute(SKU_RECOMMENDATIONS_DDL)
    for table in ("sku_judgments", "sku_recommendations"):
        if "machine_id" in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            continue
        with conn:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN machine_id TEXT NOT NULL DEFAULT ''")
            conn.execute(f"UPDATE {table} SET machine_id=? WHERE machine_id=''", (machine_id,))


class ModelWork:
    def __init__(self, snapshot, config, stop=None, machine=""):
        self.snapshot, self.config, self.stop = snapshot, config, stop
        self.machine = machine
        self.previous = copy.deepcopy(snapshot.get("model_tasks", {}))
        snapshot["model_tasks"] = self.tasks = {}
        self.conn = None
        self.requested = 0

    def close(self):
        if self.conn is not None:
            self.conn.close()

    def _key(self, branch, context):
        return digest(dict(branch=branch, context=context))

    def _signature(self, rule):
        return cache_signature(self.config, rule)

    def record(self, branch, context, members, state, **fields):
        key = self._key(branch, context)
        task = dict(branch=branch, context=context, members=sorted(members), state=state,
                    completed=state.startswith("completed_"), retryable=state in RETRYABLE,
                    reason=(f"本次匹配已停止；停止原因：{stop_reason(self.stop)}。"
                            if state == "stopped" else LABELS[state]), **fields)
        self.tasks[key] = task
        return key

    def failure_reason(self, branch, context):
        task = self.tasks[self._key(branch, context)]
        return task["reason"] if task["state"] == "stopped" else task.get("error", task["reason"])

    def completed_result(self, branch, context, rule):
        signature = self._signature(rule)
        old = self.previous.get(self._key(branch, context))
        if old and old["completed"] and (signature is None or old.get("signature") == signature):
            return old["result"]
        return None

    def execute(self, branch, context, members, rule, cache_key, call):
        cfg = self.config
        signature = self._signature(rule)
        key = self._key(branch, context)
        old = self.previous.get(key)
        result = self.completed_result(branch, context, rule)
        cached = result is not None
        if result is None and signature:
            if self.conn is None:
                cfg.cache.parent.mkdir(parents=True, exist_ok=True)
                self.conn = sqlite3.connect(cfg.cache)
                ensure_tables(self.conn, self.machine)
            if branch == "judgment":
                row = self.conn.execute("SELECT result FROM sku_judgments WHERE pair=? AND signature=?", (cache_key, signature)).fetchone()
            else:
                row = self.conn.execute("SELECT result FROM sku_recommendations WHERE context=? AND signature=?", (cache_key, signature)).fetchone()
            if row:
                result, cached = json.loads(row[0]), True
        if result is None:
            state = None
            if self.stop and self.stop():
                state = "stopped"
            elif not cfg or cfg.mode == "disabled" or not os.environ.get(cfg.model.key_env):
                state = "needs_config"
            elif cfg.mode == "caption" and (cfg.vision is None or not os.environ.get(cfg.vision.key_env)):
                state = "needs_config"
            if state:
                self.record(branch, context, members, state, signature=signature)
                return None, False
            self.requested += 1
            try:
                result = call()
            except ModelFailure as exc:
                state = "model_unavailable" if "图像能力核验" in str(exc) or "视觉" in str(exc) or "模型不可用" in str(exc) else "failed"
                if self.stop and self.stop():
                    state = "stopped"
                self.record(branch, context, members, state, signature=signature, error=str(exc))
                return None, False
            # Never persist or apply a late result from a stopped run.
            if self.stop and self.stop():
                self.record(branch, context, members, "stopped", signature=signature)
                return None, False
            if branch == "judgment" and result["confidence"] < .8:
                self.record(branch, context, members, "inconclusive", signature=signature)
                return result, False
            if branch == "judgment":
                self.conn.execute("INSERT OR REPLACE INTO sku_judgments(pair,signature,machine_id,result) VALUES (?,?,?,?)",
                                  (cache_key, signature, self.machine, json.dumps(result, ensure_ascii=False)))
            else:
                self.conn.execute("INSERT OR REPLACE INTO sku_recommendations(context,signature,machine_id,result) VALUES (?,?,?,?)",
                                  (cache_key, signature, self.machine, json.dumps(result, ensure_ascii=False)))
            self.conn.commit()
        state = ("completed_uncertain" if (result.get("group_id") is None if branch == "recommendation" else result["confidence"] < .8)
                 else "completed_positive" if branch == "recommendation" or result["same"] else "completed_negative")
        self.record(branch, context, members, state, signature=signature or (old or {}).get("signature"), result=result, cached=cached)
        return result, cached

    def finish(self):
        tasks = list(self.tasks.values())
        unfinished_members = {member for t in tasks if not t["completed"] for member in t["members"]}
        self.snapshot["matching"] = dict(
            recheckable=sum(t["state"] == "missing_evidence" for t in tasks),
            unfinished_members=len(unfinished_members),
            completed_members=len(self.snapshot["members"]) - len(unfinished_members),
            completed=sum(t["completed"] for t in tasks), unfinished=sum(not t["completed"] for t in tasks),
            retryable=sum(t["retryable"] for t in tasks), cached=sum(bool(t.get("cached")) for t in tasks),
            states=[dict(state=state, reason=(f"本次匹配已停止；停止原因：{stop_reason(self.stop)}。"
                                           if state == "stopped" else reason), count=sum(t["state"] == state for t in tasks)) for state, reason in LABELS.items()],
            requested=self.requested, reasons={s: sum(t["state"] == s for t in tasks) for s in LABELS})
        protected = {identity(r) for g in self.snapshot["groups"] if g.get("manual_membership") or g.get("excluded_group") for r in g["members"]}
        for member in self.snapshot["members"]:
            own = [t for t in tasks if identity(member) in t["members"]]
            member["model_work"] = [dict(branch=t["branch"], state=t["state"], reason=t["reason"], completed=t["completed"], retryable=t["retryable"]) for t in own]
            if identity(member) not in protected:
                notes = list(dict.fromkeys(t["reason"] for t in own if not t["completed"]))
                if notes:
                    member["matching_note"] = member.get("matching_note", "") + "；" + "；".join(notes)
