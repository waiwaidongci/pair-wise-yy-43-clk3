import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "spill", "description": "ledger flow",
             "severity": "major", "quantity": 5, "threshold": 10},
            "creator", "observer")
        self.vessel = self.service.register_resource(
            {"callsign": "V-001", "resource_type": "containment_vessel",
             "home_area": "A区"}, "ops", "response_commander")

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def _dispatch(self, callsign="V-001", item_id=None, area="B区", ref=None):
        payload = {"callsign": callsign, "work_area": area,
                   "person_in_charge": "张队", "task": "布放围油栏",
                   "planned_start": "2026-09-26T08:00Z",
                   "planned_end": "2026-09-26T18:00Z"}
        if ref: payload["external_ref"] = ref
        return self.service.dispatch(item_id or self.item["id"], payload,
                                     "cmd", "response_commander")

    def test_resource_unique_callsign_and_view(self):
        with self.assertRaises(ConflictError):
            self.service.register_resource(
                {"callsign": "V-001", "resource_type": "recovery_team",
                 "home_area": "C区"}, "ops", "operations")
        view = next(r for r in self.service.list_resources("viewer")
                    if r["callsign"] == "V-001")
        self.assertFalse(view["occupied"])
        self.assertEqual(view["current_area"], "A区")

    def test_dispatch_records_ledger_and_occupies(self):
        assignment = self._dispatch(ref="D-1")
        self.assertEqual(assignment["status"], "active")
        self.assertTrue(assignment["cross_area"])
        self.assertEqual(assignment["planned_start"], "2026-09-26T08:00:00+00:00")
        view = next(r for r in self.service.list_resources("viewer")
                    if r["callsign"] == "V-001")
        # 跨区域调派时原区域立即释放，当前值守区域变为任务区域
        self.assertTrue(view["occupied"])
        self.assertEqual(view["current_area"], "B区")
        self.assertEqual(view["current_item_id"], self.item["id"])
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["active_resource_count"], 1)
        self.assertEqual(item["active_callsigns"], ["V-001"])

    def test_active_resource_cannot_be_reassigned(self):
        self._dispatch()
        other = self.service.create_item(
            {"title": "spill 2", "description": "other",
             "severity": "minor"}, "creator", "observer")
        with self.assertRaises(ConflictError):
            self._dispatch(item_id=other["id"])
        # 没有写入半条记录：另一单上没有任何调派
        self.assertEqual(self.service.list_assignments(other["id"], "viewer"), [])

    def test_release_restores_area_keeps_history_and_records_time(self):
        assignment = self._dispatch()
        released = self.service.release(
            assignment["id"], {"actual_minutes": 125,
                               "release_note": "围油栏回收完成"},
            "ops", "operations")
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["actual_minutes"], 125)
        with self.assertRaises(ConflictError):
            self.service.release(assignment["id"], {}, "ops", "operations")
        view = next(r for r in self.service.list_resources("viewer")
                    if r["callsign"] == "V-001")
        self.assertFalse(view["occupied"])
        self.assertEqual(view["current_area"], "A区")
        # 历史调派保留，撤收后可以再次调派
        history = self.service.list_assignments(self.item["id"], "viewer")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "released")
        second = self._dispatch(area="A区")
        self.assertEqual(second["status"], "active")

    def test_close_blocked_with_active_callsigns(self):
        self._dispatch()
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                current["id"], "closed", current["version"], "reviewer",
                TRANSITION_ROLES["closed"][0])
        self.assertEqual(ctx.exception.details["reason"],
                         "resources_still_active")
        self.assertEqual(ctx.exception.details["callsigns"], ["V-001"])
        self.assertIn("V-001", str(ctx.exception))

    def test_duplicate_dispatch_ref_writes_nothing(self):
        self._dispatch(ref="DUP")
        self.service.release(
            self.service.list_assignments(self.item["id"], "viewer")[0]["id"],
            {"actual_minutes": 10}, "ops", "operations")
        before = len(self.service.list_assignments(self.item["id"], "viewer"))
        with self.assertRaises(ConflictError):
            self._dispatch(ref="DUP")
        after = len(self.service.list_assignments(self.item["id"], "viewer"))
        self.assertEqual(before, after)

    def test_dispatch_validation_and_permissions(self):
        from src.domain import PermissionDenied
        with self.assertRaises(ValidationError):
            self.service.dispatch(self.item["id"], {
                "callsign": "V-001", "work_area": "B区",
                "person_in_charge": "张队", "task": "x",
                "planned_start": "2026-09-26T18:00Z",
                "planned_end": "2026-09-26T08:00Z"}, "cmd", "response_commander")
        with self.assertRaises(NotFoundError):
            self._dispatch(callsign="GHOST")
        with self.assertRaises(PermissionDenied):
            self.service.dispatch(self.item["id"], {
                "callsign": "V-001", "work_area": "B区",
                "person_in_charge": "张队", "task": "x",
                "planned_start": "2026-09-26T08:00Z",
                "planned_end": "2026-09-26T18:00Z"}, "cmd", "viewer")

    def test_dispatch_to_closed_item_rejected(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self._dispatch()


if __name__ == "__main__":
    unittest.main()
