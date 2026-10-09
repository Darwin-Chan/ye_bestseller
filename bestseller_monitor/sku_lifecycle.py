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
from .sku_grouping import identity

LABELS = {
    "inconclusive": "图文证据不确定，尚未得到有效判断", "missing_evidence": "缺少图片证据", "needs_config": "需要模型配置",
    "model_unavailable": "模型不可用", "failed": "调用失败",
    "stopped": "用户/运行已停止", "completed_uncertain": "已完成但无法推荐唯一组",
    "completed_negative": "已完成并确定非同款", "completed_positive": "已完成并得到有效同款判断",
}
RETRYABLE = {"needs_config", "model_unavailable", "failed", "stopped", "inconclusive"}


class ModelWork:
    def __init__(self, snapshot, config, stop=None):
        self.snapshot, self.config, self.stop = snapshot, config, stop
        self.previous = copy.deepcopy(snapshot.get("model_tasks", {}))
        snapshot["model_tasks"] = self.tasks = {}
        self.conn = None
        self.requested = 0

    def close(self):
        if self.conn is not None:
            self.conn.close()

    def record(self, branch, context, members, state, **fields):
        key = digest(dict(branch=branch, context=context))
        task = dict(branch=branch, context=context, members=sorted(members), state=state,
                    completed=state.startswith("completed_"), retryable=state in RETRYABLE,
                    reason=LABELS[state], **fields)
        self.tasks[key] = task
        return key

    def execute(self, branch, context, members, rule, cache_key, call):
        cfg = self.config
        signature = digest({**signature_parts(cfg), "rule": rule}) if cfg and cfg.mode != "disabled" else None
        key = digest(dict(branch=branch, context=context))
        old = self.previous.get(key)
        result, cached = None, False
        if old and old["completed"] and (signature is None or old.get("signature") == signature):
            result, cached = old["result"], True
        if result is None and signature:
            if self.conn is None:
                cfg.cache.parent.mkdir(parents=True, exist_ok=True)
                self.conn = sqlite3.connect(cfg.cache)
                self.conn.execute("CREATE TABLE IF NOT EXISTS sku_judgments (pair TEXT, signature TEXT, result TEXT NOT NULL, PRIMARY KEY(pair, signature))")
                self.conn.execute("CREATE TABLE IF NOT EXISTS sku_recommendations (context TEXT, signature TEXT, result TEXT NOT NULL, PRIMARY KEY(context, signature))")
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
                self.conn.execute("INSERT OR REPLACE INTO sku_judgments VALUES (?,?,?)", (cache_key, signature, json.dumps(result, ensure_ascii=False)))
            else:
                self.conn.execute("INSERT OR REPLACE INTO sku_recommendations VALUES (?,?,?)", (cache_key, signature, json.dumps(result, ensure_ascii=False)))
            self.conn.commit()
        state = ("completed_uncertain" if (result.get("group_id") is None if branch == "recommendation" else result["confidence"] < .8)
                 else "completed_positive" if branch == "recommendation" or result["same"] else "completed_negative")
        self.record(branch, context, members, state, signature=signature or (old or {}).get("signature"), result=result, cached=cached)
        return result, cached

    def finish(self):
        tasks = list(self.tasks.values())
        unfinished_members = {member for t in tasks if not t["completed"] for member in t["members"]}
        self.snapshot["matching"] = dict(
            unfinished_members=len(unfinished_members),
            completed_members=len(self.snapshot["members"]) - len(unfinished_members),
            completed=sum(t["completed"] for t in tasks), unfinished=sum(not t["completed"] for t in tasks),
            retryable=sum(t["retryable"] for t in tasks), cached=sum(bool(t.get("cached")) for t in tasks),
            states=[dict(state=state, reason=reason, count=sum(t["state"] == state for t in tasks)) for state, reason in LABELS.items()],
            requested=self.requested, reasons={s: sum(t["state"] == s for t in tasks) for s in LABELS})
        protected = {identity(r) for g in self.snapshot["groups"] if g.get("manual_membership") or g.get("excluded_group") for r in g["members"]}
        for member in self.snapshot["members"]:
            own = [t for t in tasks if identity(member) in t["members"]]
            member["model_work"] = [dict(branch=t["branch"], state=t["state"], reason=t["reason"], completed=t["completed"], retryable=t["retryable"]) for t in own]
            if identity(member) not in protected:
                notes = list(dict.fromkeys(t["reason"] for t in own if not t["completed"]))
                if notes:
                    member["matching_note"] = member.get("matching_note", "") + "；" + "；".join(notes)

