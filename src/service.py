from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_resource_kind, normalize_severity, require_number,
                     require_text, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DISPATCH_ROLES, ENTITY,
                    RECORD_ROLES, RESOURCE_ENTITY, RESOURCE_ROLES,
                    TERMINAL_STATES, TITLE, VIEW_ROLES, assignment_blockers,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self._enrich(item, [])

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        with self.repository.locked():
            blockers = completion_blockers(
                target, self.repository.open_record_count(item_id))
            blockers += assignment_blockers(
                target, self.repository.active_call_signs(item_id))
            if blockers:
                raise ConflictError("；".join(blockers))
            updated = self.repository.transition_item(item_id, target,
                                                      expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self._enrich(updated)

    def register_resource(self, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ROLES)
        actor = require_text(actor, "actor", 100)
        call_sign = require_text(payload.get("call_sign"), "call_sign", 50)
        kind = normalize_resource_kind(payload.get("kind"))
        home_area = require_text(payload.get("home_area"), "home_area", 200)
        resource = self.repository.create_resource(call_sign, kind, home_area, actor)
        self.repository.append_audit("resource_register", RESOURCE_ENTITY,
                                     resource["id"], actor, {
                                         "call_sign": call_sign, "kind": kind,
                                         "home_area": home_area,
                                     })
        return resource

    def list_resources(self, role: str) -> list:
        self._view(role)
        return self.repository.list_resources()

    def dispatch(self, item_id: int, payload: Dict[str, Any], actor: str,
                 role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("事件已关闭，不能调派资源")
        call_sign = require_text(payload.get("call_sign"), "call_sign", 50)
        leader = require_text(payload.get("leader"), "leader", 100)
        task = require_text(payload.get("task"), "task", 500)
        planned_start = require_timestamp(payload.get("planned_start"), "planned_start")
        planned_end = require_timestamp(payload.get("planned_end"), "planned_end")
        if planned_end <= planned_start:
            raise ValidationError("planned_end必须晚于planned_start")
        resource = self.repository.get_resource_by_call_sign(call_sign)
        area = payload.get("area")
        area = require_text(area, "area", 200) if area is not None else resource["home_area"]
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        if self.repository.active_assignment_for_resource(resource["id"]) is not None:
            raise ConflictError(f"资源{call_sign}尚未撤收，不能改派")
        cross_region = area != resource["home_area"]
        return self.repository.create_assignment(
            item_id, resource["id"], area, leader, task,
            planned_start.isoformat(), planned_end.isoformat(), external_ref, actor, {
                "call_sign": call_sign, "area": area, "leader": leader,
                "task": task, "planned_start": planned_start.isoformat(),
                "planned_end": planned_end.isoformat(), "cross_region": cross_region,
                "released_area": resource["home_area"] if cross_region else None,
            })

    def release_assignment(self, assignment_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        assignment = self.repository.get_assignment(assignment_id)
        released_raw = payload.get("released_at")
        released_text = (require_timestamp(released_raw, "released_at").isoformat()
                         if released_raw is not None else utc_now())
        actual = payload.get("actual_hours")
        if actual is not None:
            actual_hours = require_number(actual, "actual_hours")
        else:
            start = require_timestamp(assignment["planned_start"], "planned_start")
            end = require_timestamp(released_text, "released_at")
            actual_hours = max(0.0, round((end - start).total_seconds() / 3600.0, 2))
        return self.repository.release_assignment(
            assignment_id, released_text, actual_hours, actor, {
                "assignment_id": assignment_id,
                "call_sign": assignment["call_sign"],
                "released_at": released_text, "actual_hours": actual_hours,
            })

    def list_assignments(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_assignments(item_id)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        occupancy = self.repository.active_assignments_by_item()
        return [self._enrich(item, occupancy.get(item["id"], []))
                for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def _enrich(self, item: Dict[str, Any],
                active_assignments: Optional[list] = None) -> Dict[str, Any]:
        result = self.enrich(item)
        if active_assignments is None:
            active_assignments = self.repository.active_assignments(item["id"])
        result["active_assignments"] = active_assignments
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
