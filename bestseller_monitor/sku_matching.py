"""Local SKU image judgments. Never read/write the legacy shared ledger."""
from __future__ import annotations

from .sku_lifecycle import ModelWork

from .matching import ImageJudge, digest, scored_pairs
from .sku_grouping import identity, refresh

RULE_VERSION = "sku-physical-object-v1"
LIMITATIONS = ["SKU 跨机共享尚未开放"]


def evidence_version(member):
    return digest([identity(member), member.get("product_name"), member.get("raw_sku_name"), member["matching_input"], member.get("image_hash"),
                   member.get("image_source"), member.get("observed_at"), member.get("observed_date"),
                   member.get("merchant_codes")])


def _legal(snapshot, left, right):
    a, b = identity(left), identity(right)
    excluded = set(snapshot.get("excluded_skus", []))
    if a == b or a in excluded or b in excluded:
        return False
    if any(frozenset(pair) == frozenset((a, b)) for pair in snapshot.get("excluded", [])):
        return False
    ca, cb = left.get("merchant_code"), right.get("merchant_code")
    return not (ca and cb)  # both equal and different codes belong to code/manual rules


def _finish(snapshot):
    for member in snapshot["members"]:
        if member.get("code_ambiguous") and member.get("matching_state") != "manual" and not member.get("matching_note", "").startswith("编码有歧义"):
            member["matching_note"] = "编码有歧义；" + member.get("matching_note", "")
    refresh(snapshot)


def apply(snapshot, config, *, stop=None, reload_manual=None, previous_snapshot=None):
    work = ModelWork(snapshot, config, stop)
    try:
        _apply(snapshot, config, work, stop=stop, reload_manual=reload_manual, previous_snapshot=previous_snapshot)
        work.finish()
    finally:
        work.close()


def _apply(snapshot, config, work, *, stop=None, reload_manual=None, previous_snapshot=None):
    snapshot["limitations"] = list(LIMITATIONS)
    members = {identity(m): m for m in snapshot["members"]}
    groups = list(snapshot["groups"])
    group_of = {identity(ref): g for g in groups for ref in g["members"]}
    protected = {identity(ref) for g in groups if g.get("manual_membership") or g.get("excluded_group") for ref in g["members"]}
    for key, m in members.items():
        if key in protected or m.get("matching_state") == "pending_candidates":
            continue
        if not m.get("image_data"):
            if not m.get("merchant_code"):
                m.update(matching_state="missing_image", matching_note="缺少图片证据；暂时独立，可人工处理")
        elif not m.get("merchant_code"):
            m.update(matching_state="unjudged", matching_note="尚未完成图文判断；暂时独立")
    options = snapshot.setdefault("matching_options", {})
    if config:
        options.update(candidates=config.candidates, min_score=config.min_score)
    for key, m in members.items():
        if key not in protected and not m.get("image_data") and not m.get("merchant_code"):
            work.record("judgment", dict(member=key, evidence=evidence_version(m), rule=RULE_VERSION), [key], "missing_evidence")
    eligible = sorted((m for m in members.values() if m.get("image_data")), key=identity)
    judge = ImageJudge(config) if config and config.mode != "disabled" else None
    positive = {}
    negative = set()
    failed_pairs = {}
    for (i, j), score in sorted(scored_pairs(eligible, options.get("candidates", 12)).items()):
        if reload_manual:
            reload_manual()
            group_of = {identity(ref): g for g in snapshot["groups"] for ref in g["members"]}
            protected = {identity(ref) for g in snapshot["groups"] if g.get("manual_membership") or g.get("excluded_group") for ref in g["members"]}
        left, right = eligible[i], eligible[j]
        a, b = identity(left), identity(right)
        if group_of[a] is group_of[b] or (a in protected and b in protected) or not _legal(snapshot, left, right):
            continue
        pair = digest(dict(evidence=sorted((evidence_version(left), evidence_version(right))),
                           analysis=[snapshot["start"], snapshot["end"]]))
        if score < options.get("min_score", 0):
            continue
        result, cached = work.execute("judgment", dict(pair=pair, rule=RULE_VERSION), [a, b], RULE_VERSION, pair,
                                      lambda: judge.compare(left, right))
        if result is None:
            failed_pairs[frozenset((a, b))] = work.failure_reason("judgment", dict(pair=pair, rule=RULE_VERSION))
            continue
        if reload_manual:
            reload_manual()
        if result["confidence"] < .8:
            for key in (a, b):
                if key not in protected:
                    members[key].update(matching_state="pending", matching_note="图文证据不确定；暂时独立")
            continue
        if not _legal(snapshot, left, right):
            continue
        edge = frozenset((a, b))
        if result["same"]:
            reason = result.get("reason") or "主体实物图文判断同款（忽略颜色、包装与销售件数差异）"
            positive[edge] = dict(reason=reason, cached=cached, pair=pair)
            for key, other in ((a, b), (b, a)):
                members[key].setdefault("image_judgments", []).append(dict(member=other, **positive[edge]))
        else:
            negative.add(edge)
            for key in (a, b):
                if key not in protected:
                    members[key].update(matching_state="judged", matching_note="已完成图文判断：该候选非同款")
    if reload_manual:
        reload_manual()
    groups = list(snapshot["groups"])
    group_of = {identity(ref): g for g in groups for ref in g["members"]}
    protected = {identity(ref) for g in groups if g.get("manual_membership") or g.get("excluded_group") for ref in g["members"]}
    # A failed recalled alternative is unknown, not evidence for uniqueness.
    # Recheck current manual constraints so an in-flight human choice still wins.
    failed_members = set()
    for edge, error in failed_pairs.items():
        a, b = sorted(edge)
        if group_of[a] is group_of[b] or not _legal(snapshot, members[a], members[b]):
            continue
        for key in (a, b):
            if key not in protected:
                failed_members.add(key)
                members[key].update(matching_state="pending", matching_note=error + "；图文判断未完成；暂时独立")
    # Unknown new alternatives cannot negate an existing, still-valid result.
    # Evidence, rule/model cache validity and current human legality must all hold.
    previous_members = {identity(m): m for m in (previous_snapshot or {}).get("members", [])}
    previous_groups = {identity(r): g["id"] for g in (previous_snapshot or {}).get("groups", []) for r in g["members"]}
    def valid_history(key, target):
        others = [identity(r) for r in target["members"]]
        keys = [key] + others
        return (all(previous_groups.get(k) == target["id"] and k in previous_members
                    and evidence_version(previous_members[k]) == evidence_version(members[k]) for k in keys)
                and any(positive.get(frozenset((key, other)), {}).get("cached") for other in others))
    # Freeze targets before any addition. A no-code SKU is never a bridge.
    proposals = {}
    for key, member in members.items():
        source = group_of[key]
        if key in protected or len(source["members"]) != 1 or member.get("matching_state") == "pending_candidates":
            continue
        targets = []
        for target in groups:
            if target is source or target.get("excluded_group"):
                continue
            if member.get("merchant_code") and not target.get("manual_membership"):
                continue  # coded groups anchor automatic no-code arrivals, not the reverse
            others = [identity(ref) for ref in target["members"]]
            if key in failed_members and not valid_history(key, target):
                continue
            if (any(frozenset((key, other)) in positive for other in others)
                and all(_legal(snapshot, member, members[other]) and frozenset((key, other)) not in negative for other in others)):
                targets.append(target)
        if len(targets) > 1:
            member.update(candidate_groups=sorted(g["id"] for g in targets), matching_state="pending_candidates",
                          grouping_basis="暂时独立（待候选比较）", matching_note="多个图文候选，等待候选比较；未选择归属")
        elif len(targets) == 1:
            proposals[key] = targets[0]
    moved = set()
    for key in sorted(proposals):
        member = members[key]
        source, target = group_of[key], proposals[key]
        # A coded group is an anchor, not a way to pull a multi-candidate SKU.
        target_ids = [identity(ref) for ref in target["members"]]
        if len(source["members"]) != 1 or not target["members"]:
            continue
        if key in moved or any(members[other].get("matching_state") == "pending_candidates" for other in target_ids):
            continue
        if any(other in failed_members for other in target_ids) and not valid_history(key, target):
            continue
        if target.get("merchant_codes") and member.get("merchant_code"):
            continue
        if member.get("merchant_code") and not target.get("manual_membership"):
            continue  # the unencoded side will join this coded anchor instead
        if not all(_legal(snapshot, member, members[other]) and frozenset((key, other)) not in negative for other in target_ids):
            continue
        ref = source["members"].pop()
        target["members"].append(ref)
        target["confirmed"] = False
        member.update(added_to_group=True, grouping_basis="图文归组", matching_state="image_rule",
                      matching_note="图文依据：" + "; ".join(e["reason"] for edge, e in positive.items() if key in edge and any(other in edge for other in target_ids)))
        target["image_grouping"] = True
        moved.add(key)
    from .sku_recommendations import apply as apply_recommendations
    apply_recommendations(snapshot, config, stop=stop, reload_manual=reload_manual, work=work)
    _finish(snapshot)
