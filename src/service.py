from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def submit_measurement(self, item_id, payload, actor, role, region=None):
        """Record a monitoring-station report; duplicates merge into a batch."""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.BATCH_ROLES:
            raise DomainError("forbidden", "当前角色不能提交测量批次", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_batch(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and item["payload"].get("region") != region:
            raise DomainError("region_mismatch", "不能向其他区域的记录提交测量", 403)
        batch, updated_item, created, invalidated = self.repository.upsert_measurement(
            item_id, normalized, actor, role
        )
        return {"batch": batch, "item": updated_item, "created": created, "invalidated": invalidated}

    def merge_batch(self, item_id, batch_id, payload, actor, role, expected_version, base_payload=None, region=None):
        """Apply a concurrent batch edit; non-conflicting fields merge, conflicts are listed."""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.BATCH_ROLES:
            raise DomainError("forbidden", "当前角色不能修改测量批次", 403)
        if expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        item = self.repository.get_item(item_id)
        if region and rules.ENFORCE_REGION and role != "regulator" and item["payload"].get("region") != region:
            raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        theirs = {}
        if "strength_dbm" in payload:
            theirs["strength_dbm"] = domain.number(payload, "strength_dbm")
        if "bandwidth_mhz" in payload:
            theirs["bandwidth_mhz"] = domain.number(payload, "bandwidth_mhz", 0.001)
        if "reporter" in payload:
            reporter = payload.get("reporter")
            theirs["reporter"] = str(reporter).strip() if reporter else None
        batch, conflicts = self.repository.merge_batch(
            item_id, batch_id, theirs, base_payload, expected_version, actor, role
        )
        return {"batch": batch, "conflicts": conflicts}

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            # Cross-region: suspend/resolve must be confirmed by the target
            # jurisdiction; everything else stays in the home (original) region.
            if action in rules.TARGET_REGION_ACTIONS:
                required_region = item["payload"].get("target_region") or item["payload"].get("region")
            else:
                required_region = item["payload"].get("region")
            if required_region != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role, region)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["batches"] = self.repository.list_batches(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
