from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .domain import (ConflictError, NotFoundError, ValidationError,
                     ensure_role, normalize_severity, parse_utc,
                     require_number, require_text)
from .repository import Repository
from .rules import (ASSIGNMENT_ENTITY, AUDIT_ROLES, CREATE_ROLES,
                    DISPATCH_ROLES, ENTITY, RECORD_ROLES,
                    RESOURCE_CREATE_ROLES, RESOURCE_ENTITY,
                    RESOURCE_TYPE_LABELS, RESOURCE_TYPES, RELEASE_ROLES,
                    TERMINAL_STATES, TITLE, VIEW_ROLES, completion_blockers,
                    current_area, escalation_required, priority_score,
                    resource_busy_blockers, response_deadline_hours,
                    role_for_transition, validate_transition)


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
        return self._enrich_item(item)

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
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        if target in TERMINAL_STATES:
            active = self.repository.active_assignments_for_item(item_id)
            busy_blockers, callsigns = resource_busy_blockers(
                [a["callsign"] for a in active])
            if busy_blockers:
                raise ConflictError(
                    "；".join(busy_blockers) + "：" + "、".join(callsigns),
                    {"reason": "resources_still_active", "callsigns": callsigns})
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self._enrich_item(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._enrich_item(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self._enrich_item(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def register_resource(self, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        callsign = require_text(payload.get("callsign"), "callsign", 60)
        resource_type = payload.get("resource_type")
        if resource_type not in RESOURCE_TYPES:
            raise ValidationError("resource_type不在允许范围内")
        home_area = require_text(payload.get("home_area"), "home_area", 120)
        resource = self.repository.create_resource(
            callsign, resource_type, home_area, actor)
        self.repository.append_audit(
            "resource_register", RESOURCE_ENTITY, resource["id"], actor, {
                "callsign": callsign,
                "resource_type": resource_type,
                "home_area": home_area,
            })
        return self._resource_view(resource, None)

    def list_resources(self, role: str) -> list:
        self._view(role)
        resources = self.repository.list_resources()
        active = {a["resource_id"]: a
                  for a in self.repository.all_active_assignments()}
        return [self._resource_view(r, active.get(r["id"])) for r in resources]

    def dispatch(self, item_id: int, payload: Dict[str, Any], actor: str,
                 role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事件已关闭，不能再调派资源")
        callsign = require_text(payload.get("callsign"), "callsign", 60)
        resource = self.repository.get_resource_by_callsign(callsign)
        if resource is None:
            raise NotFoundError("资源不存在")
        work_area = require_text(payload.get("work_area"), "work_area", 120)
        person_in_charge = require_text(
            payload.get("person_in_charge"), "person_in_charge", 100)
        task = require_text(payload.get("task"), "task")
        planned_start = parse_utc(payload.get("planned_start"), "planned_start")
        planned_end = parse_utc(payload.get("planned_end"), "planned_end")
        if planned_end <= planned_start:
            raise ValidationError("planned_end必须晚于planned_start")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        cross_area = work_area != resource["home_area"]
        assignment = self.repository.create_assignment(
            resource["id"], item_id, work_area, cross_area, person_in_charge,
            task, planned_start, planned_end, actor, external_ref)
        self.repository.append_audit(
            "dispatch", ASSIGNMENT_ENTITY, assignment["id"], actor, {
                "assignment_id": assignment["id"], "item_id": item_id,
                "callsign": callsign, "resource_type": resource["resource_type"],
                "work_area": work_area, "cross_area": cross_area,
                "person_in_charge": person_in_charge,
                "planned_start": planned_start, "planned_end": planned_end,
            })
        return self._assignment_view(assignment)

    def release(self, assignment_id: int, payload: Dict[str, Any], actor: str,
                role: str) -> Dict[str, Any]:
        ensure_role(role, RELEASE_ROLES)
        actor = require_text(actor, "actor", 100)
        existing = self.repository.get_assignment(assignment_id)
        note = payload.get("release_note")
        if note is not None:
            note = require_text(note, "release_note")
        actual_minutes = payload.get("actual_minutes")
        if actual_minutes is None:
            actual_minutes = self._elapsed_minutes(
                existing["created_at"])
        else:
            actual_minutes = int(require_number(
                actual_minutes, "actual_minutes"))
        released = self.repository.release_assignment(
            assignment_id, actor, actual_minutes, note)
        self.repository.append_audit(
            "release", ASSIGNMENT_ENTITY, assignment_id, actor, {
                "item_id": released["item_id"],
                "actual_minutes": actual_minutes,
                "release_note": note,
            })
        return self._assignment_view(released)

    def list_assignments(self, item_id: Optional[int], role: str,
                         status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in ("active", "released"):
            raise ValidationError("status必须是active或released")
        rows = self.repository.list_assignments(item_id, status)
        return [self._assignment_view(a) for a in rows]

    @staticmethod
    def _elapsed_minutes(start_iso: str) -> int:
        start = datetime.fromisoformat(start_iso)
        delta = datetime.now(start.tzinfo) - start
        return max(0, int(round(delta.total_seconds() / 60.0)))

    def _resource_view(self, resource: Dict[str, Any],
                       mine: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = dict(resource)
        result["resource_type_label"] = RESOURCE_TYPE_LABELS.get(
            resource["resource_type"], resource["resource_type"])
        result["occupied"] = mine is not None
        result["current_area"] = current_area(
            resource["home_area"], mine["work_area"] if mine else None)
        result["current_assignment_id"] = mine["id"] if mine else None
        result["current_item_id"] = mine["item_id"] if mine else None
        return result

    def _assignment_view(self, assignment: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(assignment)
        result["cross_area"] = bool(assignment["cross_area"])
        resource = self.repository.get_resource(assignment["resource_id"])
        result["callsign"] = resource["callsign"]
        result["resource_type"] = resource["resource_type"]
        result["resource_type_label"] = RESOURCE_TYPE_LABELS.get(
            resource["resource_type"], resource["resource_type"])
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

    def _enrich_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = self.enrich(item)
        active = self.repository.active_assignments_for_item(item["id"])
        result["active_resource_count"] = len(active)
        result["active_callsigns"] = [a["callsign"] for a in active]
        return result
