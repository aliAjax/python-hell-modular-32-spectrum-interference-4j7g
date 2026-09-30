import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def _measurement(station="ST-01", freq=2400.0, observed="2026-09-27T10:00:00+00:00", strength=-60, reporter="monitor-1"):
    return {
        "station_id": station,
        "frequency_mhz": freq,
        "observed_at": observed,
        "strength_dbm": strength,
        "bandwidth_mhz": 10.0,
        "reporter": reporter,
    }


class BatchMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -60,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_duplicate_reports_merge_into_one_batch(self):
        first = self.service.submit_measurement(self.item["id"], _measurement(strength=-60), "m", "monitor")
        second = self.service.submit_measurement(self.item["id"], _measurement(strength=-63, reporter="monitor-2"), "m", "monitor")
        self.assertEqual(first["batch"]["id"], second["batch"]["id"])
        self.assertEqual(second["batch"]["report_count"], 2)
        self.assertEqual(second["batch"]["payload"]["strength_dbm"], -63)
        batches = self.repo.list_batches(self.item["id"])
        self.assertEqual(len(batches), 1)

    def test_different_station_freq_time_stay_separate(self):
        self.service.submit_measurement(self.item["id"], _measurement(station="ST-01"), "m", "monitor")
        self.service.submit_measurement(self.item["id"], _measurement(station="ST-02"), "m", "monitor")
        self.service.submit_measurement(self.item["id"], _measurement(freq=5800.0), "m", "monitor")
        self.service.submit_measurement(self.item["id"], _measurement(observed="2026-09-27T11:00:00+00:00"), "m", "monitor")
        batches = self.repo.list_batches(self.item["id"])
        self.assertEqual(len(batches), 4)

    def test_new_batch_on_pending_item_auto_assesses(self):
        result = self.service.submit_measurement(self.item["id"], _measurement(), "m", "monitor")
        self.assertEqual(result["item"]["status"], "assessed")
        self.assertIn("assessment", result["item"]["payload"])


class BatchInvalidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -40,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        self.item = self.service.act(self.item["id"], "assess", {}, "analyst-1", "analyst", self.item["version"])
        self.item = self.service.act(self.item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", self.item["version"])
        self.item = self.service.act(self.item["id"], "suspend", {"authorization_code": "REG-NORTH-1"}, "coord-1", "coordinator", self.item["version"], "north")
        self.item = self.service.act(self.item["id"], "coordinate", {"coordination_agreement": "AGC-7"}, "coord-1", "coordinator", self.item["version"], "north")
        self.item = self.service.act(self.item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-7"}, "coord-1", "coordinator", self.item["version"], "north")
        self.assertEqual(self.item["status"], "resolved")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_batch_update_invalidates_location_authorization_resolution(self):
        result = self.service.submit_measurement(self.item["id"], _measurement(strength=-70), "m", "monitor")
        self.assertEqual(result["item"]["status"], "assessed")
        payload = result["item"]["payload"]
        self.assertNotIn("location", payload)
        self.assertNotIn("suspend_authorization", payload)
        self.assertNotIn("resolution", payload)
        self.assertNotIn("coordination_agreement", payload)
        # The new measurement is synced and re-assessed.
        self.assertEqual(payload["strength_dbm"], -70)
        self.assertIn("assessment", payload)

    def test_batch_update_on_cancelled_item_stays_cancelled(self):
        cancelled = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-03",
            "region": "north",
            "strength_dbm": -40,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        cancelled = self.service.act(cancelled["id"], "cancel", {"reason": "false alarm"}, "coord-1", "coordinator", cancelled["version"], "north")
        result = self.service.submit_measurement(cancelled["id"], _measurement(station="ST-03"), "m", "monitor")
        self.assertEqual(result["item"]["status"], "cancelled")


class CrossRegionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -40,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        self.item = self.service.act(self.item["id"], "assess", {}, "analyst-1", "analyst", self.item["version"])
        self.item = self.service.act(self.item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", self.item["version"])

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _version(self):
        return self.service.get_item(self.item["id"])["version"]

    def test_home_region_submits_opinion_target_confirms(self):
        opinion = self.service.act(
            self.item["id"], "submit_opinion",
            {"opinion": "建议南辖区协查停用", "target_region": "south"},
            "north-coord", "coordinator", self._version(), "north",
        )
        self.assertEqual(opinion["status"], "coordinating")
        self.assertEqual(opinion["payload"]["target_region"], "south")
        # Target jurisdiction confirms deactivation.
        suspended = self.service.act(
            self.item["id"], "suspend", {"authorization_code": "REG-SOUTH-1"},
            "south-coord", "coordinator", self._version(), "south",
        )
        self.assertEqual(suspended["status"], "suspended")
        # Target jurisdiction closes the case.
        resolved = self.service.act(
            self.item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-south"},
            "south-coord", "coordinator", self._version(), "south",
        )
        self.assertEqual(resolved["status"], "resolved")

    def test_home_region_cannot_suspend_or_resolve(self):
        self.service.act(
            self.item["id"], "submit_opinion",
            {"opinion": "请协查", "target_region": "south"},
            "north-coord", "coordinator", self._version(), "north",
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.act(self.item["id"], "suspend", {"authorization_code": "REG-NORTH-1"}, "north-coord", "coordinator", self._version(), "north")
        self.assertEqual(ctx.exception.code, "region_mismatch")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(self.item["id"], "resolve", {"measurement_cleared": True, "evidence": "x"}, "north-coord", "coordinator", self._version(), "north")
        self.assertEqual(ctx.exception.code, "region_mismatch")
        # The original record remains queryable and unchanged.
        record = self.service.get_item(self.item["id"])
        self.assertEqual(record["status"], "coordinating")
        self.assertIsNone(record["payload"].get("suspend_authorization"))

    def test_opinion_from_wrong_region_or_role_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.act(self.item["id"], "submit_opinion", {"opinion": "x", "target_region": "south"}, "south-coord", "coordinator", self._version(), "south")
        self.assertEqual(ctx.exception.code, "region_mismatch")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(self.item["id"], "submit_opinion", {"opinion": "x", "target_region": "south"}, "north-analyst", "analyst", self._version(), "north")
        self.assertEqual(ctx.exception.code, "forbidden")


class ConcurrentMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -60,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        created = self.service.submit_measurement(self.item["id"], _measurement(strength=-60), "m", "monitor")
        self.batch = created["batch"]

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_non_conflicting_fields_kept_conflicts_listed(self):
        base = dict(self.batch["payload"])
        # Officer A changes strength only.
        a = self.service.merge_batch(
            self.item["id"], self.batch["id"], {"strength_dbm": -70},
            "alice", "monitor", self.batch["version"], base,
        )
        self.assertEqual(a["conflicts"], [])
        # Officer B read the same base and changes bandwidth + strength.
        b = self.service.merge_batch(
            self.item["id"], self.batch["id"], {"strength_dbm": -80, "bandwidth_mhz": 25.0},
            "bob", "monitor", self.batch["version"], base,
        )
        conflict_fields = [c["field"] for c in b["conflicts"]]
        self.assertIn("strength_dbm", conflict_fields)
        # B's non-conflicting bandwidth change is kept.
        self.assertEqual(b["batch"]["payload"]["bandwidth_mhz"], 25.0)
        # The conflicting strength is left for the person to choose.
        strength_conflict = next(c for c in b["conflicts"] if c["field"] == "strength_dbm")
        self.assertEqual(strength_conflict["base"], -60)
        self.assertEqual(strength_conflict["theirs"], -80)
        self.assertEqual(strength_conflict["current"], -70)

    def test_merge_without_base_on_version_mismatch_conflicts(self):
        base = dict(self.batch["payload"])
        self.service.merge_batch(self.item["id"], self.batch["id"], {"strength_dbm": -70}, "alice", "monitor", self.batch["version"], base)
        with self.assertRaises(ConflictError):
            self.service.merge_batch(self.item["id"], self.batch["id"], {"strength_dbm": -80}, "bob", "monitor", self.batch["version"], None)


class IdempotentRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -60,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_retry_with_batch_number_does_not_add_records(self):
        payload = _measurement()
        payload["batch_number"] = "B-0001"
        first = self.service.submit_measurement(self.item["id"], payload, "m", "monitor")
        self.assertTrue(first["created"])
        # Simulate a failed write: the client retries with the same batch number.
        retry = self.service.submit_measurement(self.item["id"], payload, "m", "monitor")
        self.assertFalse(retry["created"])
        self.assertEqual(retry["batch"]["id"], first["batch"]["id"])
        self.assertEqual(retry["batch"]["report_count"], 1)
        self.assertEqual(len(self.repo.list_batches(self.item["id"])), 1)


class LegacyRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_item_without_batches_keeps_original_flow(self):
        item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -35,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-NORTH-1"}, "coord-1", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGC-7"}, "coord-1", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(item["batches"], [])


if __name__ == "__main__":
    unittest.main()
