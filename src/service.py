import secrets

from . import batches, domain, rules
from .audit import canonical_json
from .domain import ConflictError, DomainError, NotFoundError
from .repository import now_iso

BATCH_ROLES = {"analyst", "monitor", "coordinator"}
BATCH_EDIT_ROLES = {"analyst", "monitor", "coordinator"}


class Service:
    def __init__(self, repository):
        self.repository = repository

    # ------------------------------------------------------------------
    # Identity / region helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _require_identity(actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)

    @staticmethod
    def _governing_region(payload):
        """跨区立案后由目标辖区确认；否则以测量所在辖区为准。"""
        return payload.get("target_region") or payload.get("region")

    def _deny(self, item_id, reason, action, actor, role, region, message):
        error = DomainError(reason, message, 403)
        self.repository.append_denied_audit(
            item_id,
            "action_denied",
            actor,
            role,
            {"action": action, "reason": reason, "region": region},
        )
        raise error

    # ------------------------------------------------------------------
    # Items (interference cases)
    # ------------------------------------------------------------------
    def create_item(self, payload, actor, role, region=None):
        self._require_identity(actor, role)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        batch_no = batches.normalize_batch_no(payload.get("batch_no"))
        if batch_no:
            batch = self.repository.get_batch(batch_no)
            batch_payload = batch["payload"]
            normalized["batch_no"] = batch_no
            normalized["batch_version"] = batch["version"]
            normalized["station_id"] = batch_payload["station_id"]
            normalized["frequency_mhz"] = batch_payload["frequency_mhz"]
            normalized["strength_dbm"] = batch_payload["strength_dbm"]
            normalized["bandwidth_mhz"] = batch_payload["bandwidth_mhz"]
            detected_at = payload.get("detected_at") or batch_payload["observed_at"]
            normalized["detected_at"] = detected_at
            if not normalized.get("region") and batch_payload.get("region"):
                normalized["region"] = batch_payload["region"]
            target_region = payload.get("target_region")
            if target_region is not None:
                normalized["target_region"] = str(target_region).strip() or None
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        self._require_identity(actor, role)
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

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        self._require_identity(actor, role)
        item = self.repository.get_item(item_id)
        if role not in rules.ACTION_ROLES.get(action, set()):
            # 角色不足属于能力问题，不构成跨区尝试，无需留痕到具体记录
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and region and role != "regulator":
            item_payload = item["payload"]
            if action in rules.TARGET_REGION_ACTIONS:
                # 确认停用/结案：只能由目标辖区有权限的角色完成
                governing = self._governing_region(item_payload)
                if not governing:
                    self._deny(item_id, "region_unknown", action, actor, role, region,
                               "案件缺少管辖辖区，不能确认停用或结案")
                if region != governing:
                    self._deny(item_id, "region_mismatch", action, actor, role, region,
                               "停用和结案只能由目标辖区确认，原辖区可提交处置意见")
            elif action in rules.ORIGIN_REGION_ACTIONS:
                # 处置意见/协调/撤销由原辖区提交
                origin = item_payload.get("region")
                if origin and region != origin:
                    self._deny(item_id, "region_mismatch", action, actor, role, region,
                               "处置意见只能由原辖区提交")
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)

        # 原辖区处置意见：仅追加，不占用版本号
        if action == "propose":
            _, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
            self.repository.append_item_proposal(
                item_id, actor, role, new_payload, event_payload["proposal"]
            )
            return self.get_item(item_id)

        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    # ------------------------------------------------------------------
    # Measurement batches
    # ------------------------------------------------------------------
    def _invalidate_linked_items(self, conn, batch_payload, new_version):
        """批次更新事务内调用：失效关联案件的定位、授权和结案并重新确认。"""
        batch_no = batch_payload["batch_no"]
        affected = []
        for item in self.repository.items_for_batch(conn, batch_no):
            result = batches.invalidate_item(
                item, batch_payload, new_version, batch_payload.get("updated_by", "batch"), now_iso()
            )
            if result is None:
                continue
            new_status, new_payload, archived = result
            if not archived and new_status == item["status"] and \
                    item["payload"].get("batch_version") == new_version:
                continue
            version = int(item["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item["id"]),
            )
            self.repository.append_audit(
                conn,
                item["id"],
                "measurement_invalidated",
                "system",
                "batch",
                {
                    "batch_no": batch_no,
                    "batch_version": new_version,
                    "invalidated": [entry["kind"] for entry in archived],
                },
            )
            affected.append({"item_id": item["id"], "status": new_status, "version": version,
                             "invalidated": [entry["kind"] for entry in archived]})
        return affected

    def submit_report(self, payload, actor, role):
        self._require_identity(actor, role)
        if role not in BATCH_ROLES:
            raise DomainError("forbidden", "当前角色不能上报测量", 403)
        report = batches.normalize_report(payload)
        batch, outcome = self.repository.submit_report(
            report, actor, role, self._invalidate_linked_items
        )
        batch["outcome"] = outcome
        return batch

    def list_batches(self):
        return self.repository.list_batches()

    def get_batch(self, batch_no):
        batch = self.repository.get_batch(batch_no)
        batch["audit"] = self.repository.batch_audit_trail(batch_no)
        return batch

    def edit_batch(self, batch_no, body, actor, role):
        self._require_identity(actor, role)
        if role not in BATCH_EDIT_ROLES:
            raise DomainError("forbidden", "当前角色不能修改测量批次", 403)
        fields, base_version = batches.normalize_edits(body)
        current = self.repository.get_batch(batch_no)
        if current["batch_no"] != batch_no:
            raise NotFoundError("batch_not_found", "测量批次不存在")
        if base_version == current["version"]:
            merged = dict(current["payload"])
            merged.update(fields)
        else:
            conn = self.repository.connect()
            try:
                base = self.repository.get_batch_payload_at(conn, batch_no, base_version)
            finally:
                conn.close()
            if base is None:
                raise ConflictError("unknown_base_version", "基准版本不存在，请重新读取")
            merged, incoming, conflict_list = batches.three_way_merge(
                base, current["payload"], fields
            )
            if conflict_list:
                token = "MG-%s" % secrets.token_hex(12)
                self.repository.save_merge_session(
                    token, batch_no, base_version, current["version"],
                    conflict_list, incoming, actor,
                )
                raise _MergeConflict(token, batch_no, conflict_list, current["version"] + 1)
        merged["updated_by"] = actor
        batch, affected = self.repository.commit_batch_edit(
            batch_no, base_version, merged, actor, role, self._invalidate_linked_items
        )
        batch["invalidated_items"] = affected
        return batch

    def resolve_merge(self, token, choices, actor, role):
        self._require_identity(actor, role)
        if role not in BATCH_EDIT_ROLES:
            raise DomainError("forbidden", "当前角色不能处理合并冲突", 403)
        if not isinstance(choices, dict) or not choices:
            raise DomainError("choices_required", "choices 必须是非空对象")
        batch, affected = self.repository.resolve_merge_session(
            token, choices, actor, role, self._invalidate_linked_items
        )
        batch["invalidated_items"] = affected
        return batch


class _MergeConflict(ConflictError):
    def __init__(self, token, batch_no, conflicts, candidate_version):
        super().__init__("merge_conflict", "两个值班员修改了相同字段，需要人工选择")
        self.merge_token = token
        self.batch_no = batch_no
        self.conflicts = conflicts
        self.candidate_version = candidate_version
