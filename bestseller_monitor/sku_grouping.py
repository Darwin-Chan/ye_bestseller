"""SKU-level manual grouping and replay helpers.

This module deliberately does not use the legacy product identity or product
judgment ledger.  A SKU member is always identified by shop, offer and
platform SKU id together.
"""
from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4

from .stock_series import aggregate_points
from .sku_codes import changed_evidence


def identity(member: dict) -> str:
    return json.dumps([member["shop_key"], member["offer_id"], member["sku_id"]], ensure_ascii=False)


def identity_parts(value: str) -> tuple[str, str, str]:
    parts = json.loads(value)
    if len(parts) != 3:
        raise ValueError("无效 SKU 身份")
    return tuple(parts)


def member_key(member: dict) -> tuple[str, str, str]:
    return member["shop_key"], member["offer_id"], member["sku_id"]


def _new_group(member: dict, *, group_id: str | None = None, excluded: bool = False, existing_ids=()) -> dict:
    base = group_id or "sku-" + sha256(identity(member).encode("utf-8")).hexdigest()[:12]
    occupied = set(existing_ids)
    candidate = base
    suffix = 2
    while candidate in occupied:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return {
        "id": candidate,
        "members": [dict(shop_key=member["shop_key"], offer_id=member["offer_id"], sku_id=member["sku_id"])],
        "confirmed": False,
        "adjusted": False,
        "excluded_group": excluded,
        "manual_reason": "人工排除" if excluded else "",
        "sales": 0,
        "shop_count": 0,
        "product_count": 0,
        "sku_count": 1,
        "status": "人工排除" if excluded else "待确认",
    }


def _pair(left: str, right: str) -> list[str]:
    return sorted((left, right))


def _member_map(snapshot: dict) -> dict[str, dict]:
    return {identity(member): member for member in snapshot.get("members", [])}


def _group_member_ids(group: dict) -> list[str]:
    return [identity(member) for member in group["members"]]


def _grouping_basis(group: dict, present: list[dict]) -> str:
    if group.get("excluded_group"):
        return "人工排除"
    if any(m.get("matching_state") == "recommended" for m in present):
        return "人工归组＋多候选推荐增员" if group.get("manual_membership") else "多候选推荐归组"
    if group.get("image_grouping"):
        return "人工归组＋图文增员" if group.get("manual_membership") else "图文归组"
    if group.get("manual_membership"):
        return "人工归组"
    if any(member.get("matching_state") == "pending_candidates" for member in present):
        return "暂时独立（待候选比较）"
    return "同码归组" if group["merchant_codes"] else "暂时独立"


def summarize_group(group: dict, members: dict[str, dict]) -> None:
    present = [members[key] for key in _group_member_ids(group) if key in members]
    for member in present:
        member["display_name"] = member.get("product_name") or member["offer_id"]
        if member.get("single_sku") is not True and member.get("raw_sku_name"):
            member["display_name"] += " · " + member["raw_sku_name"]
    ordered = sorted(present, key=lambda member: (-member.get("sales", 0), member_key(member)))
    group["representative"] = ({key: ordered[0][key] for key in ("shop_key", "offer_id", "sku_id")} if ordered else None)
    group["points"] = aggregate_points([member["points"] for member in present])
    products = {}
    for member in sorted(present, key=member_key):
        key = (member["shop_key"], member["offer_id"])
        product = products.setdefault(key, dict(shop_key=key[0], offer_id=key[1],
            product_name=member["product_name"], shape_switches=member.get("shape_switches", []), members=[]))
        product["members"].append({field: member[field] for field in ("shop_key", "offer_id", "sku_id")})
    for product in products.values():
        children = [members[identity(ref)] for ref in product["members"]]
        product["sales"] = sum(member.get("sales", 0) for member in children)
        product["points"] = aggregate_points([member["points"] for member in children])
    group["products"] = list(products.values())
    group["sales"] = sum(member.get("sales", 0) for member in present)
    group["shop_count"] = len({member["shop_key"] for member in present})
    group["product_count"] = len({(member["shop_key"], member["offer_id"]) for member in present})
    group["sku_count"] = len(present)
    group["merchant_codes"] = sorted({m.get("merchant_code") for m in present} - {None})
    group["code_conflict"] = len(group["merchant_codes"]) > 1 and any("商家编码" in m.get("evidence_changes", []) for m in present)
    group["grouping_basis"] = _grouping_basis(group, present)
    group["added_members"] = [dict(shop_key=m["shop_key"], offer_id=m["offer_id"], sku_id=m["sku_id"]) for m in present if m.get("added_to_group")]
    group["evidence_changed"] = any(m.get("evidence_changes") for m in present)
    group["status"] = "人工排除" if group.get("excluded_group") else ("已确认" if group.get("confirmed") else "待确认")


def refresh(snapshot: dict) -> None:
    members = _member_map(snapshot)
    seen: set[str] = set()
    for group in snapshot["groups"]:
        unique = []
        for member in group.get("members", []):
            key = identity(member)
            if key in members and key not in seen:
                unique.append({"shop_key": member["shop_key"], "offer_id": member["offer_id"], "sku_id": member["sku_id"]})
                seen.add(key)
        group["members"] = unique
        summarize_group(group, members)
    # A malformed saved draft must not make a member disappear or occur twice.
    for member in snapshot.get("members", []):
        key = identity(member)
        if key not in seen:
            snapshot["groups"].append(_new_group(member, existing_ids=(g["id"] for g in snapshot["groups"])))
            seen.add(key)
    snapshot["groups"] = [group for group in snapshot["groups"] if group["members"]]
    for group in snapshot["groups"]:
        summarize_group(group, members)
    # Keep existing group order for ties; a textual id tie-break would sort sku-1, sku-10, ... and make page selection unstable.
    snapshot["review_order"] = [group["id"] for group in sorted(
        snapshot["groups"], key=lambda group: (-group.get("sku_count", len(group["members"])), -group.get("sales", 0)))]
    snapshot["ranking"] = [group["id"] for group in sorted(
        snapshot["groups"], key=lambda group: (-group.get("sales", 0), group["id"]))]


def _find(snapshot: dict, group_id: str) -> dict:
    group = next((group for group in snapshot["groups"] if group["id"] == group_id), None)
    if group is None:
        raise ValueError("同款组不存在")
    return group


def _remove_pair(excluded: list[list[str]], left: str, right: str) -> list[list[str]]:
    target = frozenset((left, right))
    return [pair for pair in excluded if frozenset(pair) != target]


def bind_manual_members(snapshot: dict, group: dict, *, confirmed: bool = False) -> None:
    """Bind only the current membership; a later automatic addition is not human approval."""
    group["manual_membership"] = True
    group["manual_members"] = _group_member_ids(group)
    members = _member_map(snapshot)
    for key in group["manual_members"]:
        member = members[key]
        member["grouping_basis"] = "人工排除" if group.get("excluded_group") else "人工归组"
        member["matching_state"] = "manual"
        member["matching_note"] = ""
        if not member.get("recommendation"):
            member["candidate_groups"] = []
        member["added_to_group"] = False
        if confirmed:
            member["evidence_changes"] = []


def edit_group(snapshot: dict, action: str, group_id: str, member: dict, target_id: str | None = None) -> None:
    members = _member_map(snapshot)
    key = identity(member)
    if key not in members:
        raise ValueError("SKU 已不在当前分析，请刷新后重试")
    source = _find(snapshot, group_id)
    source_member = next((item for item in source["members"] if identity(item) == key), None)
    if source_member is None:
        raise ValueError("SKU 已不在当前组，请刷新后重试")
    excluded = [list(pair) for pair in snapshot.get("excluded", [])]
    excluded_skus = set(snapshot.get("excluded_skus", []))

    if action in ("move", "rejoin"):
        target = _find(snapshot, target_id or "")
        if target is source:
            raise ValueError("请选择其他有效目标组")
        # Moving/rejoining is an explicit human override: it clears every
        # persisted exclusion edge for this SKU, not only edges to the target.
        # This is important when an excluded singleton is moved back to a
        # group after a later analysis.
        source["members"].remove(source_member)
        target["members"].append(source_member)
        excluded = [pair for pair in excluded if key not in pair]
        target["excluded_group"] = False
        target["manual_reason"] = ""
        source["confirmed"] = False
        target["confirmed"] = False
        source["adjusted"] = True
        target["adjusted"] = True
    elif action in ("remove", "exclude"):
        if action == "remove":
            excluded_group = False
            reason = ""
        else:
            if source.get("excluded_group") and len(source.get("members", [])) == 1:
                raise ValueError("该 SKU 已人工排除，请先撤销排除或重新归组")
            excluded_group = True
            reason = "人工排除"
        source_others = [item for item in source["members"] if identity(item) != key]
        source["members"].remove(source_member)
        target = _new_group(source_member, group_id="sku-manual-" + uuid4().hex[:12], excluded=excluded_group,
                            existing_ids=(g["id"] for g in snapshot["groups"]))
        target["manual_reason"] = reason
        target["adjusted"] = True
        source["confirmed"] = False
        source["adjusted"] = True
        snapshot["groups"].append(target)
        if action == "exclude":
            for other in source_others:
                excluded.append(_pair(key, identity(other)))
        else:
            # Removing a SKU is an explicit human edit. Do not preserve
            # exclusion edges that refer to the removed SKU.
            excluded = [pair for pair in excluded if key not in pair]
        if not source["members"]:
            snapshot["groups"].remove(source)
    elif action == "undo_exclude":
        if not source.get("excluded_group"):
            raise ValueError("当前 SKU 没有人工排除")
        source["excluded_group"] = False
        source["manual_reason"] = ""
        source["adjusted"] = True
        excluded = [pair for pair in excluded if key not in pair]
    else:
        raise ValueError("不支持的 SKU 分组操作")

    if action == "exclude":
        excluded_skus.add(key)
    else:
        excluded_skus.discard(key)
    snapshot["excluded_skus"] = sorted(excluded_skus)
    bind_manual_members(snapshot, source)
    if action in ("move", "rejoin", "remove", "exclude"):
        bind_manual_members(snapshot, target)
    snapshot["excluded"] = sorted({tuple(pair) for pair in excluded})
    snapshot["dirty"] = True
    refresh(snapshot)


def apply_saved(snapshot: dict, saved: dict) -> None:
    """Replay saved SKU decisions without copying product-level decisions.

    Only an exactly unchanged member set can retain confirmation.  A missing
    or newly added SKU keeps the manual grouping where possible but returns the
    affected group to pending confirmation.
    """
    members = _member_map(snapshot)
    previous_members = _member_map(saved)
    saved_groups = saved.get("groups", [])
    groups = []
    used: set[str] = set()
    for saved_group in saved_groups:
        if not any(saved_group.get(field) for field in ("manual_membership", "adjusted", "confirmed", "excluded_group")):
            continue
        kept = []
        bound = set(saved_group.get("manual_members", _group_member_ids(saved_group)))
        for member in saved_group.get("members", []):
            key = identity(member)
            if key in members and key in bound and key not in used:
                kept.append({"shop_key": member["shop_key"], "offer_id": member["offer_id"], "sku_id": member["sku_id"]})
                used.add(key)
        if not kept:
            continue
        saved_ids = {identity(member) for member in saved_group.get("members", [])}
        exact = saved_ids == {identity(member) for member in kept} and len(saved_ids) == len(kept)
        group = dict(saved_group)
        group["members"] = kept
        group["manual_members"] = [identity(ref) for ref in kept]
        for ref in kept:
            key = identity(ref)
            before = previous_members.get(key)
            changes = changed_evidence(before, members[key]) if before else ["历史证据缺失"]
            members[key]["evidence_changes"] = sorted(set(changes + (before or {}).get("evidence_changes", [])))
            members[key]["added_to_group"] = bool((before or {}).get("added_to_group"))
        material_change = any(members[identity(ref)].get("evidence_changes") for ref in kept)
        group["confirmed"] = bool(saved_group.get("confirmed")) and exact and not material_change
        group["manual_membership"] = bool(saved_group.get("manual_membership") or saved_group.get("adjusted") or saved_group.get("confirmed") or saved_group.get("excluded_group"))
        group["adjusted"] = False
        group.setdefault("excluded_group", False)
        group.setdefault("manual_reason", "")
        groups.append(group)
    for member in snapshot["members"]:
        if identity(member) not in used:
            groups.append(_new_group(member, existing_ids=(g["id"] for g in groups)))
    snapshot["groups"] = groups
    snapshot["excluded"] = [list(pair) for pair in saved.get("excluded", []) if len(pair) == 2]
    snapshot["excluded_skus"] = sorted(set(saved.get("excluded_skus", [])) | {identity(ref) for group in saved_groups if group.get("excluded_group") for ref in group["members"]})
    # Preserve auditable image evidence only when both endpoints still match the frozen inputs.
    from .sku_matching import evidence_version
    for key, member in members.items():
        before = previous_members.get(key)
        if before and key in used and before.get("recommendation"):
            # Historical recommendation is audit evidence, never a manual override.
            member["recommendation"] = dict(before["recommendation"])
            member["multiple_candidates"] = True
            member["candidate_groups"] = list(before.get("candidate_groups", []))
        if before and evidence_version(before) == evidence_version(member):
            member["image_judgments"] = [dict(judgment, cached=True) for judgment in before.get("image_judgments", [])
                if judgment["member"] in members and judgment["member"] in previous_members
                and evidence_version(previous_members[judgment["member"]]) == evidence_version(members[judgment["member"]])]
    refresh(snapshot)
    snapshot["reused_from"] = saved.get("id")
    snapshot["dirty"] = True


def shareable_decisions(snapshot: dict) -> dict:
    """这次保存要带进 SKU 账本的人工决定（跨机共享的账本来源，票 08）。

    只带人工绑定／确认／排除过的组（与 `apply_saved` 认的同一组旗标），成员取绑定集合
    （`manual_members`，缺省全部成员），带当前证据版本与组 ID——组 ID 随行，跨机回放
    保住之后推荐语境摘要才能对上；未确认的自动中间态、模型结果都不带，账本只装决定。
    排除身份与排除对按身份带（不带版本）。
    """
    from .sku_matching import evidence_version
    members = _member_map(snapshot)
    groups = []
    for group in snapshot.get("groups", []):
        if not any(group.get(field) for field in
                   ("manual_membership", "adjusted", "confirmed", "excluded_group")):
            continue
        bound = group.get("manual_members") or _group_member_ids(group)
        pairs = sorted([key, evidence_version(members[key])] for key in bound if key in members)
        if not pairs:
            continue
        groups.append({"members": pairs, "confirmed": bool(group.get("confirmed")),
                       "excluded": bool(group.get("excluded_group")), "group_id": group["id"],
                       "machine_id": ""})
    return {
        "groups": groups,
        "excluded_skus": [[key, ""] for key in sorted(snapshot.get("excluded_skus", []))],
        "excluded_pairs": [[pair[0], pair[1], ""] for pair in
                           sorted({tuple(pair) for pair in snapshot.get("excluded", [])})],
    }


def apply_shared(snapshot: dict, ledger: dict) -> tuple[int, list[dict]]:
    """把 SKU 账本里的共享决定套到当前快照（票 08 的回放口）：本地草稿回放之后、编码规则之前。

    本地人工主张（草稿回放出来的手动组、确认组、排除）永不被覆盖：账本行与本地主张
    矛盾时不应用、返回一条回放冲突（由调用方记进 SKU 冲突账）；兼容或已生效的不重复应用。

    应用语义与 `apply_saved` 同一套规矩：成员与证据版本在本机对得上才保留 `confirmed`，
    版本对不上（或成员不齐）整组未确认并在成员上标「与共享决定时的证据不一致」；
    组 ID 随行（撞了本地已有 ID 就避让）——推荐语境摘要靠它跨机对上。返回（应用行数, 冲突）。

    每条冲突的形状：`{'kind': …, 'incoming': 行, 'local': [本地主张, …]}`，行是
    `{side, members, confirmed?, excluded?, machine}`；格式化与记账在 `analysis_store`。
    """
    from .analysis_store import CONFLICT_DIFFERENT_GROUPS, CONFLICT_GROUP_VS_EXCLUSION
    from .sku_matching import evidence_version
    members = _member_map(snapshot)
    group_of = {identity(ref): g for g in snapshot["groups"] for ref in g["members"]}
    excluded_skus = set(snapshot.get("excluded_skus", []))
    excluded_pairs = {frozenset(pair) for pair in snapshot.get("excluded", [])}
    applied, conflicts = 0, []

    def claimed(group) -> bool:
        return group is not None and any(
            group.get(field) for field in ("manual_membership", "excluded_group", "confirmed"))

    def local_group_entry(group) -> dict:
        return {"side": "group", "confirmed": bool(group.get("confirmed")),
                "excluded": bool(group.get("excluded_group")),
                "members": [[identity(ref), evidence_version(members[identity(ref)])]
                            for ref in group["members"] if identity(ref) in members],
                "machine": ""}

    def local_sku_entry(key) -> dict:
        return {"side": "sku", "members": [[key, None]], "machine": ""}

    def incoming(row) -> dict:
        return {"side": "group", "members": [[str(member), version] for member, version in row["members"]],
                "confirmed": bool(row.get("confirmed")), "excluded": bool(row.get("excluded")),
                "machine": row.get("machine_id") or ""}

    def conflict(kind, incoming_entry, local_entries):
        conflicts.append({"kind": kind, "incoming": incoming_entry,
                          "local": sorted(local_entries,
                                          key=lambda entry: json.dumps(entry, sort_keys=True,
                                                                       ensure_ascii=False))})

    for row in sorted(ledger.get("groups", []),
                      key=lambda item: json.dumps(item["members"], ensure_ascii=False)):
        pairs = [(str(member), version) for member, version in row["members"]]
        keys = [member for member, _ in pairs]
        present = [key for key in keys if key in members]
        if not present:
            continue                                   # 成员一个都还没到：等数据到了再来
        claim_groups = {}
        for key in present:
            group = group_of.get(key)
            if claimed(group):
                claim_groups.setdefault(group["id"], group)
        excluded_local = [key for key in present if key in excluded_skus]
        if claim_groups or excluded_local:
            bound_sets = {gid: set(group.get("manual_members") or _group_member_ids(group))
                          for gid, group in claim_groups.items()}
            settled = (not excluded_local and len(claim_groups) == 1
                       and set(present) <= next(iter(bound_sets.values())) <= set(keys))
            if settled:
                continue                               # 本地已按同一条（或它的在场子集）建立：跳过
            local_entries = [local_group_entry(group) for group in claim_groups.values()]
            local_entries += [local_sku_entry(key) for key in excluded_local]
            kind = (CONFLICT_GROUP_VS_EXCLUSION
                    if excluded_local or any(g.get("excluded_group") for g in claim_groups.values())
                    else CONFLICT_DIFFERENT_GROUPS)
            conflict(kind, incoming(row), local_entries)
            continue
        group = _new_group(members[present[0]], group_id=str(row.get("group_id") or "") or None,
                           excluded=bool(row.get("excluded")),
                           existing_ids=(g["id"] for g in snapshot["groups"]))
        group["members"] = []                          # 成员统一在下面逐个搬进来
        snapshot["groups"].append(group)
        for key in present:
            ref = members[key]
            source_group = group_of.get(key)
            if source_group is not None and source_group is not group:
                source_group["members"] = [r for r in source_group["members"] if identity(r) != key]
                if not source_group["members"]:
                    snapshot["groups"].remove(source_group)
            group["members"].append({"shop_key": ref["shop_key"], "offer_id": ref["offer_id"],
                                     "sku_id": ref["sku_id"]})
            group_of[key] = group
        if row.get("excluded"):
            group["excluded_group"] = True
            group["manual_reason"] = "人工排除"
            excluded_skus.update(present)
        exact = len(present) == len(keys)
        versions_ok = exact and all(version == evidence_version(members[key])
                                    for key, version in pairs)
        group["confirmed"] = bool(row.get("confirmed")) and versions_ok
        for key, version in pairs:
            if key in members and version != evidence_version(members[key]):
                members[key]["evidence_changes"] = sorted(
                    set(members[key].get("evidence_changes", []) + ["与共享决定时的证据不一致"]))
        bind_manual_members(snapshot, group, confirmed=group["confirmed"])
        applied += 1

    for entry in sorted(ledger.get("excluded_skus", []), key=lambda item: item[0]):
        key = entry[0]
        if key not in members or key in excluded_skus:
            continue
        group = group_of.get(key)
        if claimed(group):
            if group.get("excluded_group"):
                continue                               # 本地已排除：同一件事
            conflict(CONFLICT_GROUP_VS_EXCLUSION,
                     {"side": "sku", "members": [[key, None]], "machine": entry[-1] if len(entry) > 1 else ""},
                     [local_group_entry(group)])
            continue
        excluded_skus.add(key)
        if group is not None and len(group["members"]) == 1 and not claimed(group):
            group.update(excluded_group=True, manual_reason="人工排除", manual_membership=True)
        applied += 1

    for entry in sorted(ledger.get("excluded_pairs", []), key=lambda item: (item[0], item[1])):
        left, right = entry[0], entry[1]
        if left not in members or right not in members:
            continue
        pair = frozenset((left, right))
        if pair in excluded_pairs:
            continue
        shared_group = next((g for g in snapshot["groups"]
                             if claimed(g) and {left, right} <= {identity(r) for r in g["members"]}),
                            None)
        if shared_group is not None:
            conflict(CONFLICT_GROUP_VS_EXCLUSION,
                     {"side": "pair", "members": [[left, None], [right, None]],
                      "machine": entry[2] if len(entry) > 2 else ""},
                     [local_group_entry(shared_group)])
            continue
        excluded_pairs.add(pair)
        applied += 1

    snapshot["excluded_skus"] = sorted(excluded_skus)
    snapshot["excluded"] = [sorted(pair) for pair in sorted(excluded_pairs, key=sorted)]
    refresh(snapshot)
    return applied, conflicts


def search_values(member: dict, group: dict, shops: dict[str, str]) -> tuple[str, ...]:
    return (
        member.get("product_name", ""),
        member.get("raw_sku_name", ""),
        member.get("merchant_code") or "",
        *member.get("merchant_codes", []),
        member.get("offer_id", ""),
        member.get("sku_id", ""),
        group.get("id", ""),
        member.get("shop_key", ""),
        shops.get(member.get("shop_key", ""), ""),
    )



def apply_codes(snapshot: dict) -> None:
    """Resolve only unique legal code targets, without bridging human groups.

    Candidate targets are frozen before adding anyone. An unresolved singleton
    is not a new target for the next SKU with the same code.
    """
    members = _member_map(snapshot)
    excluded_skus = set(snapshot.get("excluded_skus", []))
    for group in snapshot["groups"]:
        if len(group["members"]) == 1 and identity(group["members"][0]) in excluded_skus:
            group.update(excluded_group=True, manual_reason="人工排除", manual_membership=True)
    anchors = [g for g in snapshot["groups"] if g.get("manual_membership") or g.get("excluded_group")]
    protected = {key for group in anchors for key in _group_member_ids(group)}
    excluded = {frozenset(pair) for pair in snapshot.get("excluded", [])}
    codes = {g["id"]: {members[key].get("merchant_code") for key in _group_member_ids(g)} - {None} for g in anchors}
    groups = list(anchors)
    automatic = {}
    for member in snapshot["members"]:
        key = identity(member)
        code = member.get("merchant_code")
        if key not in protected or not member.get("recommendation"):
            member["candidate_groups"] = []
        if key in protected:
            member["matching_state"] = "manual"
            member["matching_note"] = ""
            member["grouping_basis"] = "人工排除" if any(g.get("excluded_group") and key in _group_member_ids(g) for g in anchors) else "人工归组"
            continue
        member["grouping_basis"] = "同码归组" if code else "暂时独立"
        member["matching_state"] = "code_rule" if code else "unjudged"
        member["matching_note"] = "" if code else ("编码有歧义；图文判断尚未开放" if member.get("code_ambiguous") else "无有效编码；图文判断尚未开放")
        if not code and member.get("image_source") == "missing":
            member["matching_note"] += "；缺少图片证据"
        targets = [g for g in anchors if code and code in codes[g["id"]] and not g.get("excluded_group")
                   and all(frozenset((key, other)) not in excluded for other in _group_member_ids(g))]
        if len(targets) == 1:
            target = targets[0]
            target["members"].append({field: member[field] for field in ("shop_key", "offer_id", "sku_id")})
            target["confirmed"] = False
            member["added_to_group"] = True
        elif len(targets) > 1:
            member["grouping_basis"] = "暂时独立（待候选比较）"
            member["candidate_groups"] = [g["id"] for g in targets]
            member["matching_state"] = "pending_candidates"
            member["matching_note"] = "多个候选，无法确定归属；等待候选比较"
            groups.append(_new_group(member, existing_ids=(g["id"] for g in groups)))
        elif code and code in automatic and all(frozenset((key, other)) not in excluded for other in _group_member_ids(automatic[code])):
            automatic[code]["members"].append({field: member[field] for field in ("shop_key", "offer_id", "sku_id")})
        else:
            group = _new_group(member, existing_ids=(g["id"] for g in groups))
            groups.append(group)
            if code:
                automatic[code] = group
    snapshot["groups"] = groups
    refresh(snapshot)
