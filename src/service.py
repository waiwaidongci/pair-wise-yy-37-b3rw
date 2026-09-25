from __future__ import annotations

from typing import Any, Dict, Optional
from datetime import date

from .domain import (ConflictError, NotFoundError, ensure_role,
                     normalize_severity, require_date, require_materials,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    RENEWAL_ENTITY, TITLE, VIEW_ROLES, completion_blockers,
                    compute_expiry, escalation_required, material_blockers,
                    priority_score, response_deadline_hours, review_blockers,
                    role_for_renewal, role_for_transition,
                    validate_renewal_transition, validate_transition)


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
        return self.enrich(item)

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

    def close_record(self, item_id: int, record_id: int, actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        existing = next(
            (r for r in self.repository.list_records(item_id) if r["id"] == record_id),
            None,
        )
        if existing is None:
            raise NotFoundError("记录不存在")
        closed = self.repository.close_record(record_id, actor)
        self.repository.append_audit("record_close", ENTITY, item_id, actor, {
            "record_id": record_id, "kind": existing["kind"],
        })
        return closed

    @staticmethod
    def _require_expected_version(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("expected_version必须是正整数")
        return value

    def _renewal_fields(self, payload: Dict[str, Any]) -> tuple:
        capacity = require_number(payload.get("proposed_capacity"),
                                  "proposed_capacity", 0.0)
        effective_date = require_date(payload.get("effective_date"),
                                      "effective_date")
        if date.fromisoformat(effective_date) < date.today():
            raise ConflictError("生效日期不能早于今天")
        materials = require_materials(payload.get("materials", []))
        return capacity, effective_date, materials

    def create_renewal(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        capacity, effective_date, materials = self._renewal_fields(payload)
        self.repository.get_item(item_id)
        renewal = self.repository.create_renewal(
            item_id, capacity, effective_date, materials, actor)
        self.repository.append_audit("renewal_create", RENEWAL_ENTITY,
                                     renewal["id"], actor, {
            "item_id": item_id, "proposed_capacity": capacity,
            "effective_date": effective_date, "materials": len(materials),
        })
        return renewal

    def update_renewal(self, renewal_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = self._require_expected_version(
            payload.get("expected_version"))
        capacity, effective_date, materials = self._renewal_fields(payload)
        renewal = self.repository.update_renewal(
            renewal_id, capacity, effective_date, materials, expected_version)
        self.repository.append_audit("renewal_update", RENEWAL_ENTITY,
                                     renewal_id, actor, {
            "item_id": renewal["item_id"], "proposed_capacity": capacity,
            "effective_date": effective_date, "materials": len(materials),
        })
        return renewal

    def submit_renewal(self, renewal_id: int, expected_version: int,
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, role_for_renewal("submitted"))
        actor = require_text(actor, "actor", 100)
        expected_version = self._require_expected_version(expected_version)
        renewal = self.repository.get_renewal(renewal_id)
        validate_renewal_transition(renewal["status"], "submitted")
        blockers = material_blockers(renewal["materials"])
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.submit_renewal(renewal_id, expected_version)
        self.repository.append_audit("renewal_submit", RENEWAL_ENTITY,
                                     renewal_id, actor, {
            "item_id": renewal["item_id"], "from": renewal["status"],
        })
        return updated

    def review_renewal(self, renewal_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, role_for_renewal("approved"))
        actor = require_text(actor, "actor", 100)
        expected_version = self._require_expected_version(
            payload.get("expected_version"))
        renewal = self.repository.get_renewal(renewal_id)
        validate_renewal_transition(renewal["status"], "approved")
        item = self.repository.get_item(renewal["item_id"])
        blockers = review_blockers(renewal["proposed_capacity"],
                                   item["threshold"],
                                   self.repository.open_record_count(item["id"]))
        if blockers:
            comment = require_text(payload.get("comment"), "退回意见", 2000)
            updated = self.repository.transition_renewal(
                renewal_id, "returned", expected_version, actor, comment,
                ("submitted",))
            self.repository.append_audit("renewal_return", RENEWAL_ENTITY,
                                         renewal_id, actor, {
                "item_id": item["id"], "reasons": blockers, "comment": comment,
            })
            return updated
        comment = payload.get("comment")
        if comment is not None:
            comment = require_text(comment, "comment", 2000)
        expires_at = compute_expiry(renewal["effective_date"])
        approved = self.repository.approve_renewal(
            renewal_id, item["id"], renewal["proposed_capacity"],
            renewal["effective_date"], expires_at, expected_version,
            actor, comment)
        self.repository.append_audit("renewal_approve", RENEWAL_ENTITY,
                                     renewal_id, actor, {
            "item_id": item["id"], "expires_at": expires_at,
            "capacity": renewal["proposed_capacity"],
        })
        return approved

    def get_renewal(self, renewal_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_renewal(renewal_id)

    def list_renewals(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_renewals(item_id)

    def list_permits(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.list_permit_versions(item_id)

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
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

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
