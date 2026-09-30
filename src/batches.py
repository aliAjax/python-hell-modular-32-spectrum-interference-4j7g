import copy
import re

from . import rules
from .domain import DomainError, number, parse_timestamp, require_text

BATCH_NO_PATTERN = re.compile(r"^MB-[A-Za-z0-9_-]{1,64}$")
EDITABLE_FIELDS = ("strength_dbm", "bandwidth_mhz", "region", "note")
IDENTITY_FIELDS = ("batch_no", "station_id", "frequency_mhz", "observed_at", "reports")
INVALIDATED_KINDS = ("location", "suspend_authorization", "resolution")


def normalize_batch_no(value):
    if value is None:
        return None
    if not isinstance(value, str) or not BATCH_NO_PATTERN.match(value.strip()):
        raise DomainError("invalid_batch_no", "批次号格式应为 MB- 开头的字母数字")
    return value.strip()


def normalize_report(payload):
    """整理监测站上报；batch_no 可选（用于写入失败后的幂等重试）。"""
    batch_no = normalize_batch_no(payload.get("batch_no"))
    station_id = require_text(payload, "station_id")
    frequency_mhz = number(payload, "frequency_mhz", 0.001, 300000)
    observed_at = parse_timestamp(payload, "observed_at")
    strength_dbm = number(payload, "strength_dbm")
    bandwidth = payload.get("bandwidth_mhz", 0.1)
    bandwidth_mhz = number({"bandwidth_mhz": bandwidth}, "bandwidth_mhz", 0.001)
    reporter = require_text(payload, "reporter")
    region = payload.get("region")
    if region is not None:
        region = str(region).strip() or None
    note = payload.get("note", "")
    if not isinstance(note, str):
        raise DomainError("invalid_field", "note 必须是字符串")
    return {
        "batch_no": batch_no,
        "station_id": station_id,
        "frequency_mhz": frequency_mhz,
        "observed_at": observed_at,
        "strength_dbm": strength_dbm,
        "bandwidth_mhz": bandwidth_mhz,
        "reporter": reporter,
        "region": region,
        "note": note.strip(),
    }


def normalize_edits(body):
    fields = body.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise DomainError("fields_required", "fields 必须是非空对象")
    result = {}
    for name in fields:
        if name not in EDITABLE_FIELDS:
            raise DomainError("field_not_editable", "%s 不允许在批次上修改" % name)
    if "strength_dbm" in fields:
        result["strength_dbm"] = number(fields, "strength_dbm")
    if "bandwidth_mhz" in fields:
        result["bandwidth_mhz"] = number(fields, "bandwidth_mhz", 0.001)
    if "region" in fields:
        value = fields["region"]
        if value is not None:
            value = str(value).strip() or None
        result["region"] = value
    if "note" in fields:
        if not isinstance(fields["note"], str):
            raise DomainError("invalid_field", "note 必须是字符串")
        result["note"] = fields["note"].strip()
    base_version = body.get("base_version")
    if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
        raise DomainError("invalid_version", "base_version 必须是正整数")
    return result, base_version


def three_way_merge(base, current, changes):
    """以 base 为共同祖先，合并 current（已提交方）与 changes（当前值班员修改）。

    返回 (merged, incoming, conflicts)；conflicts 中的字段需人工选择。
    """
    incoming = dict(base)
    incoming.update(changes)
    merged = dict(current)
    conflicts = []
    keys = set(base) | set(current) | set(changes)
    for key in sorted(keys):
        if key in IDENTITY_FIELDS:
            continue
        base_value = base.get(key)
        current_value = current.get(key)
        incoming_value = incoming.get(key)
        if incoming_value == base_value:
            merged[key] = current_value
        elif current_value == base_value:
            merged[key] = incoming_value
        elif incoming_value == current_value:
            merged[key] = incoming_value
        else:
            conflicts.append(
                {
                    "field": key,
                    "base": base_value,
                    "current": current_value,
                    "incoming": incoming_value,
                }
            )
            merged[key] = None
    return merged, incoming, conflicts


def invalidate_item(row, batch, new_version, actor, at):
    """批次更新后，对关联案件生成失效/刷新结果。

    返回 (new_status, new_payload, archived)；案件已取消时返回 None。
    """
    status = row["status"]
    if status == "cancelled":
        return None
    payload = dict(row["payload"])
    archived = []
    for kind in INVALIDATED_KINDS:
        data = payload.get(kind)
        if data:
            archived.append(
                {
                    "kind": kind,
                    "data": copy.deepcopy(data),
                    "from_batch_version": payload.get("batch_version"),
                    "actor": actor,
                    "at": at,
                }
            )
            if kind == "suspend_authorization":
                payload["suspend_authorization"] = None
            else:
                payload.pop(kind, None)
    payload.pop("coordination_agreement", None)
    payload["strength_dbm"] = batch["strength_dbm"]
    payload["bandwidth_mhz"] = batch["bandwidth_mhz"]
    payload["batch_version"] = new_version
    if archived:
        payload["invalidated_artifacts"] = payload.get("invalidated_artifacts", []) + archived
        new_status = "assessed"
        payload["assessment"] = rules.assess(payload)
    else:
        new_status = status
        if status == "assessed":
            payload["assessment"] = rules.assess(payload)
    return new_status, payload, archived
