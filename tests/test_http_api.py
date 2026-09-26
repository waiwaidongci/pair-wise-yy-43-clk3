import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, static_dir))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.repo.close(); self.tmp.cleanup()

    def _req(self, method, path, body=None, role="viewer", actor="tester"):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Actor": actor, "X-Role": role}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers,
            method=method)
        try:
            with request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except Exception as exc:
            resp = getattr(exc, "response", None) or exc
            return getattr(resp, "code", 500), json.loads(resp.read())

    def test_close_conflict_lists_active_callsigns(self):
        _, item = self._req("POST", "/api/items", {
            "title": "spill", "description": "d", "severity": "minor",
        }, role="observer", actor="obs")
        self._req("POST", "/api/resources", {
            "callsign": "M-9", "resource_type": "monitoring_personnel",
            "home_area": "北码头",
        }, role="response_commander")
        status, body = self._req(
            "POST", f"/api/items/{item['id']}/assignments", {
                "callsign": "M-9", "work_area": "南滩",
                "person_in_charge": "王工", "task": "岸线监测",
                "planned_start": "2026-09-26T08:00Z",
                "planned_end": "2026-09-26T20:00Z"},
            role="response_commander")
        self.assertEqual(status, 201, body)

        current = item
        for target in STATES[1:]:
            status, body = self._req(
                "POST", f"/api/items/{current['id']}/transition",
                {"target": target, "expected_version": current["version"]},
                role=TRANSITION_ROLES[target][0])
            if target == "closed":
                self.assertEqual(status, 409)
                self.assertEqual(body["details"]["reason"],
                                 "resources_still_active")
                self.assertEqual(body["details"]["callsigns"], ["M-9"])
                self.assertIn("M-9", body["message"])
            else:
                self.assertEqual(status, 200, body)
                current = body

        # 撤收后关闭成功
        _, assignments = self._req(
            "GET", f"/api/items/{item['id']}/assignments")
        aid = assignments["assignments"][0]["id"]
        status, body = self._req(
            "POST", f"/api/assignments/{aid}/release",
            {"actual_minutes": 600}, role="operations")
        self.assertEqual(status, 200, body)
        status, body = self._req(
            "POST", f"/api/items/{current['id']}/transition",
            {"target": "closed", "expected_version": current["version"]},
            role="response_commander")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "closed")


if __name__ == "__main__":
    unittest.main()
