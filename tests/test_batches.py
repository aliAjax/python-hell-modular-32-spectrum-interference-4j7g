import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError

REPORT = {
    "station_id": "ST-10",
    "frequency_mhz": 925.0,
    "observed_at": "2026-09-30T02:00:00+00:00",
    "strength_dbm": -50.0,
    "bandwidth_mhz": 5.0,
    "reporter": "duty-a",
    "region": "north",
}


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def submit(self, **overrides):
        payload = dict(REPORT)
        payload.update(overrides)
        return self.service.submit_report(payload, "duty-a", "monitor")

    def full_case(self, batch, origin="north", target="south"):
        item = self.service.create_item({
            "frequency_mhz": 925.0,
            "bandwidth_mhz": 5.0,
            "station_id": "ST-10",
            "region": origin,
            "target_region": target,
            "strength_dbm": -50.0,
            "detected_at": "2026-09-30T02:00:00+00:00",
            "reporter": "duty-a",
            "batch_no": batch["payload"]["batch_no"],
        }, "duty-a", "monitor")
        item = self.service.act(item["id"], "assess", {}, "duty-a", "monitor", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "tower-3", "confidence": 0.9},
                                "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-SOUTH-9"},
                                "coord-s", "coordinator", item["version"], target)
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGR-9"},
                                "coord-n", "coordinator", item["version"], origin)
        item = self.service.act(item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-9"},
                                "coord-s", "coordinator", item["version"], target)
        return item

    # 1. 归并 + 幂等重试 ------------------------------------------------
    def test_duplicate_reports_merge_by_station_frequency_time(self):
        first = self.submit()
        second = self.submit(strength_dbm=-48.0)  # 同一站点/频点/时刻
        self.assertEqual(first["payload"]["batch_no"], second["payload"]["batch_no"])
        self.assertEqual(second["outcome"], "merged")
        self.assertEqual(len(second["payload"]["reports"]), 2)
        self.assertEqual(second["version"], 2)
        # 完全相同的上报再次提交，不追加、不升版本、不触发失效
        third = self.submit(strength_dbm=-48.0)
        self.assertEqual(len(third["payload"]["reports"]), 2)
        self.assertEqual(third["outcome"], "duplicate")
        self.assertEqual(third["version"], 2)

        other = self.submit(station_id="ST-11")
        self.assertNotEqual(first["payload"]["batch_no"], other["payload"]["batch_no"])

    def test_retry_with_same_batch_no_does_not_duplicate(self):
        first = self.submit()
        batch_no = first["payload"]["batch_no"]
        retry_payload = dict(REPORT)
        retry_payload["batch_no"] = batch_no
        retry_payload["strength_dbm"] = -99.0  # 即使内容不同，重试也原样返回
        retried = self.service.submit_report(retry_payload, "duty-a", "monitor")
        self.assertEqual(retried["outcome"], "retried")
        self.assertEqual(retried["payload"]["batch_no"], batch_no)
        self.assertEqual(retried["payload"]["strength_dbm"], -50.0)
        self.assertEqual(len(self.service.list_batches()), 1)

    # 2. 更新即失效 ------------------------------------------------------
    def test_batch_update_invalidates_location_authorization_resolution(self):
        batch = self.submit()
        item = self.full_case(batch)
        self.assertEqual(item["status"], "resolved")
        self.assertTrue(item["payload"]["resolution"]["cleared"])

        updated = self.service.edit_batch(batch["payload"]["batch_no"], {
            "base_version": batch["version"],
            "fields": {"strength_dbm": -88.0},
        }, "duty-b", "monitor")
        self.assertEqual(updated["version"], 2)
        refreshed = self.service.get_item(item["id"])
        self.assertEqual(refreshed["status"], "assessed")
        self.assertIsNone(refreshed["payload"]["suspend_authorization"])
        self.assertNotIn("resolution", refreshed["payload"])
        self.assertNotIn("location", refreshed["payload"])
        archived = refreshed["payload"]["invalidated_artifacts"]
        self.assertEqual(
            sorted(entry["kind"] for entry in archived),
            ["location", "resolution", "suspend_authorization"],
        )
        # 旧授权/结案在审计中仍可查
        event_types = [event["event_type"] for event in refreshed["audit"]]
        self.assertIn("measurement_invalidated", event_types)

        # 重新确认必须重新走定位 -> 目标辖区停用 -> 目标辖区结案
        refreshed = self.service.act(refreshed["id"], "locate",
                                     {"location": "tower-4", "confidence": 0.95},
                                     "field-2", "field_operator", refreshed["version"])
        refreshed = self.service.act(refreshed["id"], "suspend",
                                     {"authorization_code": "REG-SOUTH-10"},
                                     "coord-s", "coordinator", refreshed["version"], "south")
        refreshed = self.service.act(refreshed["id"], "coordinate",
                                     {"coordination_agreement": "AGR-10"},
                                     "coord-n", "coordinator", refreshed["version"], "north")
        refreshed = self.service.act(refreshed["id"], "resolve",
                                     {"measurement_cleared": True, "evidence": "scan-10"},
                                     "coord-s", "coordinator", refreshed["version"], "south")
        self.assertEqual(refreshed["status"], "resolved")

    def test_batch_update_from_merged_report_also_invalidates(self):
        batch = self.submit()
        item = self.full_case(batch)
        merged = self.submit(strength_dbm=-47.0)
        self.assertEqual(merged["outcome"], "merged")
        refreshed = self.service.get_item(item["id"])
        self.assertEqual(refreshed["status"], "assessed")
        self.assertNotIn("resolution", refreshed["payload"])

    # 3. 跨区协调 --------------------------------------------------------
    def coordinating_case(self, batch, origin="north", target="south"):
        """走到目标辖区已停用、原辖区已发起协调（尚未结案）。"""
        item = self.service.create_item({
            "frequency_mhz": 925.0,
            "bandwidth_mhz": 5.0,
            "station_id": "ST-10",
            "region": origin,
            "target_region": target,
            "strength_dbm": -50.0,
            "detected_at": "2026-09-30T02:00:00+00:00",
            "reporter": "duty-a",
            "batch_no": batch["payload"]["batch_no"],
        }, "duty-a", "monitor")
        item = self.service.act(item["id"], "assess", {}, "duty-a", "monitor", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "tower-3", "confidence": 0.9},
                                "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-SOUTH-9"},
                                "coord-s", "coordinator", item["version"], target)
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGR-9"},
                                "coord-n", "coordinator", item["version"], origin)
        return item

    def test_origin_region_can_propose_without_version(self):
        batch = self.submit()
        item = self.coordinating_case(batch)
        version_before = item["version"]
        proposed = self.service.act(item["id"], "propose",
                                    {"opinion": "建议先现场核查再结案"},
                                    "coord-n", "coordinator", None, "north")
        self.assertEqual(proposed["status"], "coordinating")
        self.assertEqual(proposed["version"], version_before)  # 意见不占用版本号
        self.assertEqual(proposed["payload"]["disposition_proposals"][-1]["opinion"],
                         "建议先现场核查再结案")

    def test_target_region_only_can_confirm_suspend_and_resolve(self):
        batch = self.submit()
        item = self.service.create_item({
            "frequency_mhz": 925.0,
            "bandwidth_mhz": 5.0,
            "station_id": "ST-10",
            "region": "north",
            "target_region": "south",
            "strength_dbm": -50.0,
            "detected_at": "2026-09-30T02:00:00+00:00",
            "reporter": "duty-a",
            "batch_no": batch["payload"]["batch_no"],
        }, "duty-a", "monitor")
        item = self.service.act(item["id"], "assess", {}, "duty-a", "monitor", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "tower-3", "confidence": 0.9},
                                "field-1", "field_operator", item["version"])

        # 原辖区试图确认停用 -> 拒绝且留痕，原记录保持不变
        with self.assertRaises(DomainError) as denied:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-NORTH-1"},
                             "coord-n", "coordinator", item["version"], "north")
        self.assertEqual(denied.exception.status, 403)
        untouched = self.service.get_item(item["id"])
        self.assertEqual(untouched["status"], "located")
        self.assertIsNone(untouched["payload"]["suspend_authorization"])
        self.assertEqual(untouched["version"], item["version"])
        audit_types = [event["event_type"] for event in untouched["audit"]]
        self.assertEqual(audit_types.count("action_denied"), 1)

        # 目标辖区确认停用
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-SOUTH-9"},
                                "coord-s", "coordinator", item["version"], "south")
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGR-9"},
                                "coord-n", "coordinator", item["version"], "north")

        with self.assertRaises(DomainError):
            self.service.act(item["id"], "resolve",
                             {"measurement_cleared": True, "evidence": "x"},
                             "coord-n", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "resolve",
                                {"measurement_cleared": True, "evidence": "ok"},
                                "coord-s", "coordinator", item["version"], "south")
        self.assertEqual(item["status"], "resolved")

    def test_origin_region_cannot_be_hijacked_by_target_for_proposal(self):
        batch = self.submit()
        item = self.full_case(batch)
        with self.assertRaises(DomainError) as denied:
            self.service.act(item["id"], "propose", {"opinion": "越区意见"},
                             "coord-s", "coordinator", None, "south")
        self.assertEqual(denied.exception.status, 403)
        self.assertNotIn("disposition_proposals", self.service.get_item(item["id"])["payload"])

    # 4. 并发编辑：字段合并 + 冲突选择 -----------------------------------
    def test_concurrent_edits_merge_non_conflicting_fields(self):
        batch = self.submit()
        # 值班员 A 先把 note 改成 A-note（版本 2）
        self.service.edit_batch(batch["payload"]["batch_no"], {
            "base_version": 1, "fields": {"note": "A-note"},
        }, "duty-a", "monitor")
        # 值班员 B 基于版本 1 只改强度，无冲突字段 -> 自动合并
        merged = self.service.edit_batch(batch["payload"]["batch_no"], {
            "base_version": 1, "fields": {"strength_dbm": -61.0},
        }, "duty-b", "monitor")
        self.assertEqual(merged["payload"]["note"], "A-note")
        self.assertEqual(merged["payload"]["strength_dbm"], -61.0)
        self.assertEqual(merged["version"], 3)

    def test_concurrent_conflicting_edits_list_conflicts_and_require_choice(self):
        batch = self.submit()
        self.service.edit_batch(batch["payload"]["batch_no"], {
            "base_version": 1, "fields": {"strength_dbm": -60.0, "note": "A-note"},
        }, "duty-a", "monitor")
        try:
            self.service.edit_batch(batch["payload"]["batch_no"], {
                "base_version": 1, "fields": {"strength_dbm": -80.0},
            }, "duty-b", "monitor")
            self.fail("expected merge_conflict")
        except ConflictError as exc:
            self.assertEqual(exc.code, "merge_conflict")
            conflict_fields = {c["field"] for c in exc.conflicts}
            self.assertEqual(conflict_fields, {"strength_dbm"})
            self.assertEqual(exc.conflicts[0]["current"], -60.0)
            self.assertEqual(exc.conflicts[0]["incoming"], -80.0)
            token = exc.merge_token

        # 未解决前批次仍保留先提交的值
        current = self.service.get_batch(batch["payload"]["batch_no"])
        self.assertEqual(current["payload"]["strength_dbm"], -60.0)

        # 人工选择 incoming（-80），A 的非冲突字段 note 保留
        resolved = self.service.resolve_merge(token, {"strength_dbm": "incoming"}, "lead", "coordinator")
        self.assertEqual(resolved["payload"]["strength_dbm"], -80.0)
        self.assertEqual(resolved["payload"]["note"], "A-note")

        # 同一冲突会话不能重复解决
        with self.assertRaises(ConflictError):
            self.service.resolve_merge(token, {"strength_dbm": "current"}, "lead", "coordinator")

    # 5/6. 历史记录 ------------------------------------------------------
    def test_legacy_item_without_batch_keeps_original_investigation(self):
        item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-OLD",
            "region": "west",
            "strength_dbm": -35,
            "detected_at": "2026-09-30T03:00:00+00:00",
            "reporter": "old-monitor",
        }, "m", "monitor")
        self.assertNotIn("batch_no", item["payload"])
        item = self.service.act(item["id"], "assess", {}, "m", "monitor", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "old-cell", "confidence": 0.8},
                                "f", "field_operator", item["version"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-WEST-1"},
                                "c", "coordinator", item["version"], "west")
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGR-1"},
                                "c", "coordinator", item["version"], "west")
        item = self.service.act(item["id"], "resolve",
                                {"measurement_cleared": True, "evidence": "done"},
                                "c", "coordinator", item["version"], "west")
        self.assertEqual(item["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
