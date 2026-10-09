"""Local full-context candidate recommendations; never export to the legacy ledger."""
from __future__ import annotations

import json

from .matching import ImageJudge, digest
from .sku_grouping import identity, refresh

RULE_VERSION = "sku-candidate-recommendation-v1"


def _member_context(member):
    fields = ("shop_key", "offer_id", "sku_id", "product_name", "raw_sku_name",
              "matching_input", "image_hash", "image_source", "image_observed_at",
              "observed_at", "observed_date", "merchant_code", "merchant_codes",
              "code_ambiguous", "single_sku", "image_judgments")
    result = {k: member.get(k) for k in fields}
    # A cache hit is not a material difference in the judgment evidence.
    judgments = {json.dumps({k: v for k, v in j.items() if k != "cached"}, sort_keys=True, ensure_ascii=False)
                 for j in member.get("image_judgments", [])}
    result["image_judgments"] = [json.loads(j) for j in sorted(judgments)]
    return result


def _legal_groups(snapshot, member, candidate_ids):
    members = {identity(m): m for m in snapshot["members"]}
    key = identity(member)
    excluded = set(snapshot.get("excluded_skus", []))
    pairs = {frozenset(p) for p in snapshot.get("excluded", [])}
    source = next(g for g in snapshot["groups"] if any(identity(r) == key for r in g["members"]))
    if key in excluded or source.get("manual_membership") or len(source["members"]) != 1:
        return []
    result = []
    for group in snapshot["groups"]:
        if group["id"] not in candidate_ids or group is source or group.get("excluded_group"):
            continue
        others = [members[identity(r)] for r in group["members"]]
        if not others or any(identity(m) in excluded or frozenset((key, identity(m))) in pairs for m in others):
            continue
        code = member.get("merchant_code")
        codes = {m.get("merchant_code") for m in others} - {None}
        if code:
            # A human mixed-code exception belongs to this group only.
            if not group.get("manual_membership") or code not in codes:
                continue
        elif any(m.get("matching_state") == "pending_candidates" for m in others):
            continue  # never recruit a different unresolved SKU as a bridge
        result.append(group)
    return sorted(result, key=lambda g: g["id"])


def _context(snapshot, member, groups):
    members = {identity(m): m for m in snapshot["members"]}
    return dict(rule=RULE_VERSION, analysis=[snapshot["start"], snapshot["end"]],
                member=_member_context(member), candidates=[dict(
        id=g["id"], manual_membership=bool(g.get("manual_membership")),
        manual_members=sorted(g.get("manual_members", [])),
        members=[_member_context(members[key]) for key in sorted(identity(r) for r in g["members"])])
        for g in groups], constraints=dict(excluded=sorted(sorted(p) for p in snapshot.get("excluded", [])),
                                          excluded_skus=sorted(snapshot.get("excluded_skus", []))))


def apply(snapshot, config, *, stop=None, reload_manual=None, work):
    judge = ImageJudge(config) if config and config.mode != "disabled" else None
    for member in snapshot["members"]:
        if member.get("matching_state") != "pending_candidates":
            continue
        candidate_ids = list(member.get("candidate_groups", []))
        if reload_manual:
            reload_manual()
        groups = _legal_groups(snapshot, member, candidate_ids)
        if len(groups) < 2:
            continue  # changed human decisions win; do not invent a new choice
        member["candidate_groups"] = [g["id"] for g in groups]
        members = {identity(m): m for m in snapshot["members"]}
        products = [member] + [members[identity(r)] for g in groups for r in g["members"]]
        if any(not m.get("image_data") for m in products):
            work.record("recommendation", _context(snapshot, member, groups), [identity(member)], "missing_evidence")
            member.update(matching_note="多个候选，缺少图片证据；无法确定归属；暂时独立")
            continue
        context = _context(snapshot, member, groups)
        key = digest(context)
        result, cached = work.execute("recommendation", context, [identity(member)], RULE_VERSION, key,
                                      lambda: judge.recommend(context, products))
        if result is None:
            member.update(matching_state="pending", matching_note="候选推荐未完成；暂时独立")
            continue
        if reload_manual:
            reload_manual()
        current = _legal_groups(snapshot, member, candidate_ids)
        if _context(snapshot, member, current) != context:
            continue  # applies equally to fresh and cached results
        selected = result["group_id"]
        target = next((g for g in current if g["id"] == selected), None)
        if selected is not None and target is None:
            continue
        member["recommendation"] = dict(result, cached=cached, context=key, rule=RULE_VERSION)
        member["multiple_candidates"] = True
        member["candidate_groups"] = [g["id"] for g in current]
        if target is None:
            member.update(matching_state="pending_candidates", matching_note="多个候选，无法确定归属；未选择归属："+result["reason"])
        else:
            source = next(g for g in snapshot["groups"] if any(identity(r) == identity(member) for r in g["members"]))
            target["members"].append(source["members"].pop())
            target["confirmed"] = False
            target["image_grouping"] = True
            member.update(matching_state="recommended", added_to_group=True, grouping_basis="多候选推荐（未人工确认）",
                          matching_note="多个候选；推荐加入 "+target["id"]+"："+result["reason"])
    refresh(snapshot)
