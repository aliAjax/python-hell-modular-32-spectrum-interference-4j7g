import math

from .domain import DomainError

ENTITY_TYPE = "spectrum_interference"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst", "monitor"}
SOURCE_ROLES = {"analyst", "monitor", "field_operator"}
BATCH_ROLES = {"analyst", "monitor"}
ACTION_ROLES = {
    "assess": {"analyst", "monitor"},
    "locate": {"field_operator", "analyst"},
    "suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "cancel": {"coordinator"},
    "submit_opinion": {"coordinator", "regulator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"suspend", "coordinate", "resolve", "cancel", "submit_opinion"}
ACTION_REQUIRES_VERSION = {"suspend", "coordinate", "resolve", "cancel", "submit_opinion"}
# Actions whose confirmation must be performed by the *target* jurisdiction in
# cross-region coordination; all other region-sensitive acts stay in the home
# (original) jurisdiction.
TARGET_REGION_ACTIONS = {"suspend", "resolve"}

# Derived state that rests on a measurement. When a measurement batch is
# updated these are invalidated and must be re-confirmed.
_DERIVED_FIELDS = (
    "location",
    "suspend_authorization",
    "coordination_agreement",
    "coordination_note",
    "resolution",
)


def assess(payload):
    strength = float(payload.get("strength_dbm", -120))
    bandwidth = max(float(payload.get("bandwidth_mhz", 0.1)), 0.001)
    impact = strength + 10.0 * math.log10(bandwidth * 1000.0)
    if impact >= -37:
        level = "critical"
    elif impact >= -50:
        level = "high"
    elif impact >= -65:
        level = "medium"
    else:
        level = "low"
    score = round(max(0.0, min(100.0, 100.0 + impact)), 2)
    return {"score": score, "level": level, "impact_value": round(impact, 2)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role, region=None):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        current["assessment"] = assess(current)
        return "assessed", current, {"assessment": current["assessment"]}

    if action == "correct_measurement":
        _need_status(item, {"pending", "assessed", "located"})
        try:
            strength = float(payload["strength_dbm"])
        except (KeyError, TypeError, ValueError):
            raise DomainError("field_required", "strength_dbm 不能为空")
        revision = {
            "old_strength_dbm": current.get("strength_dbm"),
            "new_strength_dbm": strength,
            "reason": _text(payload, "reason"),
            "actor": actor,
        }
        current.setdefault("measurement_revisions", []).append(revision)
        current["strength_dbm"] = strength
        current["assessment"] = assess(current)
        return status, current, {"revision": revision}

    if action == "locate":
        _need_status(item, {"assessed", "located"})
        location = _text(payload, "location")
        confidence = float(payload.get("confidence", 0))
        if confidence < 0.6:
            raise DomainError("low_location_confidence", "定位置信度低于0.6，不能进入处置", 409)
        current["location"] = {"label": location, "confidence": confidence}
        return "located", current, {"location": current["location"]}

    if action == "suspend":
        _need_status(item, {"located", "suspended", "coordinating"})
        authorization = _text(payload, "authorization_code")
        if not authorization.startswith("REG-"):
            raise DomainError("invalid_authorization", "停用授权编号无效", 403)
        current["suspend_authorization"] = authorization
        return "suspended", current, {"authorization_code": authorization}

    if action == "coordinate":
        _need_status(item, {"suspended"})
        agreement = _text(payload, "coordination_agreement")
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        return "coordinating", current, {"coordination_agreement": agreement}

    if action == "submit_opinion":
        _need_status(item, {"located", "coordinating"})
        opinion = _text(payload, "opinion")
        target_region = _text(payload, "target_region")
        current["target_region"] = target_region
        current["coordination_opinion"] = {
            "opinion": opinion,
            "actor": actor,
            "region": region,
        }
        new_status = "coordinating" if status == "located" else status
        return new_status, current, {"opinion": opinion, "target_region": target_region}

    if action == "resolve":
        _need_status(item, {"suspended", "coordinating"})
        if not payload.get("measurement_cleared"):
            raise DomainError("interference_present", "干扰尚未消除，不能结案", 409)
        current["resolution"] = {"evidence": _text(payload, "evidence"), "cleared": True}
        return "resolved", current, {"evidence": current["resolution"]["evidence"]}

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")


def invalidate_on_measurement(payload, measurement):
    """Sync a measurement batch into the item and invalidate derived state.

    The assessment is recomputed from the new measurement; location,
    authorization and case closure (plus the coordination that depended on
    them) are cleared so they can be re-confirmed. Returns
    ``(new_payload, invalidated_fields)``.
    """
    current = dict(payload)
    if measurement.get("strength_dbm") is not None:
        current["strength_dbm"] = measurement["strength_dbm"]
    if measurement.get("bandwidth_mhz") is not None:
        current["bandwidth_mhz"] = measurement["bandwidth_mhz"]
    current["assessment"] = assess(current)
    invalidated = []
    for field in _DERIVED_FIELDS:
        if field in current:
            del current[field]
            invalidated.append(field)
    return current, invalidated


def regress_on_measurement(status):
    """Status to fall back to after a measurement batch is updated.

    Any post-assessment state loses the conclusions that rested on the stale
    measurement and goes back to ``assessed`` for re-confirmation; a brand-new
    item is assessed automatically; a cancelled item stays cancelled.
    """
    if status == "cancelled":
        return "cancelled"
    if status in {"located", "suspended", "coordinating", "resolved"}:
        return "assessed"
    if status == "pending":
        return "assessed"
    return status


def three_way_merge(base, theirs, current):
    """Field-level 3-way merge for concurrent batch edits.

    ``base`` is the payload the editor started from, ``theirs`` is the editor's
    desired payload and ``current`` is the latest committed payload. Fields
    changed by only one side are kept; fields changed to the same value by both
    sides are kept; fields changed to different values are reported as conflicts
    for a person to choose. Returns ``(merged_payload, conflicts)``.
    """
    merged = dict(base)
    conflicts = []
    fields = set(base) | set(theirs) | set(current)
    for field in fields:
        b = base.get(field)
        t = theirs.get(field)
        c = current.get(field)
        t_changed = field in theirs and t != b
        c_changed = field in current and c != b
        if t_changed and c_changed:
            if t == c:
                merged[field] = t
            else:
                conflicts.append({"field": field, "base": b, "theirs": t, "current": c})
        elif t_changed:
            merged[field] = t
        elif c_changed:
            merged[field] = c
    return merged, conflicts
