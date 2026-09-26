import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "锚地溢油", "description": "dispatch scenarios", "severity": "major",
             "quantity": 5, "threshold": 10, "external_ref": "DSP-ITEM-1"},
            "creator", "observer")
        self.service.register_resource(
            {"call_sign": "CB-01", "kind": "containment_vessel", "home_area": "东港区"},
            "commander", "response_commander")
        self.service.register_resource(
            {"call_sign": "RC-02", "kind": "recovery_team", "home_area": "西港区"},
            "commander", "response_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _dispatch(self, **kw):
        payload = {"call_sign": "CB-01", "leader": "张三", "task": "布放围油栏",
                   "planned_start": "2026-09-26T08:00:00+00:00",
                   "planned_end": "2026-09-26T20:00:00+00:00",
                   "external_ref": "DSP-1"}
        payload.update(kw)
        return self.service.dispatch(self.item["id"], payload, "commander",
                                     "response_commander")

    def _walk_to_monitoring(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_dispatch_occupancy_release_and_history(self):
        assignment = self._dispatch()
        self.assertEqual(assignment["status"], "active")
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual([a["call_sign"] for a in item["active_assignments"]], ["CB-01"])
        listed = self.service.list_items("viewer")
        self.assertEqual(len(listed[0]["active_assignments"]), 1)
        resources = {r["call_sign"]: r for r in self.service.list_resources("viewer")}
        self.assertTrue(resources["CB-01"]["on_duty"])
        self.assertFalse(resources["RC-02"]["on_duty"])
        released = self.service.release_assignment(
            assignment["id"], {"released_at": "2026-09-26T10:30:00+00:00"},
            "commander", "operations")
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["actual_hours"], 2.5)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["active_assignments"], [])
        resources = {r["call_sign"]: r for r in self.service.list_resources("viewer")}
        self.assertFalse(resources["CB-01"]["on_duty"])
        self.assertEqual(resources["CB-01"]["current_area"], "东港区")
        history = self.service.list_assignments(self.item["id"], "viewer")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "released")
        self.assertEqual(history[0]["leader"], "张三")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_reassign_before_release_rejected(self):
        self._dispatch()
        with self.assertRaises(ConflictError):
            self._dispatch(external_ref="DSP-2")
        with self.assertRaises(ConflictError):
            self._dispatch(call_sign="CB-01", external_ref="DSP-3", task="改派任务")
        self.assertEqual(len(self.service.list_assignments(self.item["id"], "viewer")), 1)

    def test_duplicate_dispatch_writes_no_partial_record(self):
        self._dispatch()
        with self.assertRaises(ConflictError):
            self._dispatch()
        history = self.service.list_assignments(self.item["id"], "viewer")
        self.assertEqual(len(history), 1)
        dispatches = [e for e in self.service.audit("viewer", self.item["id"])
                      if e["action"] == "dispatch"]
        self.assertEqual(len(dispatches), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_duplicate_external_ref_after_release_still_rejected(self):
        assignment = self._dispatch()
        self.service.release_assignment(assignment["id"], {}, "commander", "operations")
        with self.assertRaises(ConflictError):
            self._dispatch(call_sign="RC-02", task="回收污油")
        history = self.service.list_assignments(self.item["id"], "viewer")
        self.assertEqual(len(history), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_close_blocked_until_all_released(self):
        assignment = self._dispatch()
        self._dispatch(call_sign="RC-02", external_ref="DSP-9", task="回收污油")
        current = self._walk_to_monitoring()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.assertIn("CB-01", str(ctx.exception))
        self.assertIn("RC-02", str(ctx.exception))
        self.service.release_assignment(assignment["id"], {}, "commander", "operations")
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        remaining = [a for a in self.service.list_assignments(self.item["id"], "viewer")
                     if a["call_sign"] == "RC-02"]
        self.service.release_assignment(remaining[0]["id"], {}, "commander", "operations")
        closed = self.service.transition(current["id"], STATES[-1], current["version"],
                                         "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(ConflictError):
            self._dispatch(external_ref="DSP-10")

    def test_cross_region_dispatch_releases_home_area(self):
        assignment = self._dispatch(area="外锚地", external_ref="DSP-X")
        self.assertEqual(assignment["area"], "外锚地")
        resources = {r["call_sign"]: r for r in self.service.list_resources("viewer")}
        self.assertEqual(resources["CB-01"]["current_area"], "外锚地")
        events = [e for e in self.service.audit("viewer", self.item["id"])
                  if e["action"] == "dispatch"]
        self.assertTrue(events[0]["detail"]["cross_region"])
        self.assertEqual(events[0]["detail"]["released_area"], "东港区")
        self.service.release_assignment(assignment["id"], {}, "commander", "operations")
        resources = {r["call_sign"]: r for r in self.service.list_resources("viewer")}
        self.assertEqual(resources["CB-01"]["current_area"], "东港区")

    def test_dispatch_validation_and_permissions(self):
        payload = {"call_sign": "CB-01", "leader": "张三", "task": "布放围油栏",
                   "planned_start": "2026-09-26T08:00:00+00:00",
                   "planned_end": "2026-09-26T20:00:00+00:00"}
        with self.assertRaises(PermissionDenied):
            self.service.dispatch(self.item["id"], payload, "attacker", "viewer")
        with self.assertRaises(ValidationError):
            self._dispatch(planned_end="2026-09-26T07:00:00+00:00")
        with self.assertRaises(ValidationError):
            self._dispatch(planned_start="not-a-time")
        with self.assertRaises(NotFoundError):
            self._dispatch(call_sign="GHOST-9")
        with self.assertRaises(ValidationError):
            self.service.register_resource(
                {"call_sign": "XX-1", "kind": "unknown", "home_area": "东港区"},
                "commander", "response_commander")
        with self.assertRaises(PermissionDenied):
            self.service.register_resource(
                {"call_sign": "XX-2", "kind": "monitoring", "home_area": "东港区"},
                "attacker", "operations")

    def test_release_twice_rejected(self):
        assignment = self._dispatch()
        self.service.release_assignment(assignment["id"], {}, "commander", "operations")
        with self.assertRaises(ConflictError):
            self.service.release_assignment(assignment["id"], {}, "commander", "operations")
        with self.assertRaises(NotFoundError):
            self.service.release_assignment(9999, {}, "commander", "operations")


if __name__ == "__main__":
    unittest.main()
