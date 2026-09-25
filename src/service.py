from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_date, require_number,
                     require_text, require_version)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, MATERIAL_KINDS,
                    MATERIAL_LABELS, MATERIAL_ROLES, RECORD_ROLES,
                    RENEWAL_AMENDABLE_STATES, RENEWAL_AMEND_ROLES,
                    RENEWAL_CREATE_ROLES, RENEWAL_ENTITY, RENEWAL_REVIEW_ROLES,
                    RENEWAL_SUBMIT_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, compute_permit_expiry,
                    escalation_required, missing_required_materials,
                    priority_score, response_deadline_hours, review_blockers,
                    role_for_transition, validate_renewal_transition,
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

    def create_renewal(self, item_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        proposed_capacity = require_number(payload.get("proposed_capacity"),
                                           "proposed_capacity")
        effective_date = require_date(payload.get("effective_date"), "effective_date")
        renewal = self.repository.create_renewal(item_id, proposed_capacity,
                                                 effective_date, actor)
        self.repository.append_audit("renewal_create", RENEWAL_ENTITY, renewal["id"],
                                     actor, {"item_id": item_id,
                                             "proposed_capacity": proposed_capacity,
                                             "effective_date": effective_date})
        return renewal

    def add_renewal_material(self, renewal_id: int, payload: Dict[str, Any],
                             actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, MATERIAL_ROLES)
        actor = require_text(actor, "actor", 100)
        renewal = self.repository.get_renewal(renewal_id)
        if renewal["status"] not in RENEWAL_AMENDABLE_STATES:
            raise ConflictError("当前状态不能补充材料")
        kind = require_text(payload.get("kind"), "kind", 100)
        if kind not in MATERIAL_KINDS:
            raise ValidationError("kind不在允许范围内")
        detail = require_text(payload.get("detail"), "detail")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        material = self.repository.add_material(renewal_id, kind, detail,
                                                external_ref, actor)
        self.repository.append_audit("renewal_material", RENEWAL_ENTITY, renewal_id,
                                     actor, {"material_id": material["id"],
                                             "kind": kind})
        return material

    def amend_renewal(self, renewal_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_AMEND_ROLES)
        actor = require_text(actor, "actor", 100)
        renewal = self.repository.get_renewal(renewal_id)
        if renewal["status"] not in RENEWAL_AMENDABLE_STATES:
            raise ConflictError("当前状态不能修改申请")
        proposed_capacity = require_number(payload.get("proposed_capacity"),
                                           "proposed_capacity")
        effective_date = require_date(payload.get("effective_date"), "effective_date")
        expected_version = require_version(payload.get("expected_version"))
        updated = self.repository.amend_renewal(renewal_id, proposed_capacity,
                                                effective_date, expected_version, actor)
        self.repository.append_audit("renewal_amend", RENEWAL_ENTITY, renewal_id,
                                     actor, {"proposed_capacity": proposed_capacity,
                                             "effective_date": effective_date})
        return updated

    def submit_renewal(self, renewal_id: int, expected_version: Any, actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = require_version(expected_version)
        renewal = self.repository.get_renewal(renewal_id)
        validate_renewal_transition(renewal["status"], "submitted")
        kinds = [m["kind"] for m in self.repository.list_materials(renewal_id)]
        missing = missing_required_materials(kinds)
        if missing:
            labels = "、".join(MATERIAL_LABELS[kind] for kind in missing)
            raise ConflictError(f"缺少必备材料：{labels}")
        updated = self.repository.transition_renewal(renewal_id, "submitted",
                                                     expected_version, actor)
        self.repository.append_audit("renewal_submit", RENEWAL_ENTITY, renewal_id,
                                     actor, {"from": renewal["status"],
                                             "to": "submitted"})
        return updated

    def review_renewal(self, renewal_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = require_version(payload.get("expected_version"))
        renewal = self.repository.get_renewal(renewal_id)
        validate_renewal_transition(renewal["status"], "approved")
        item = self.repository.get_item(renewal["item_id"])
        blockers = review_blockers(renewal["proposed_capacity"], item["threshold"],
                                   self.repository.open_record_count(item["id"]))
        if blockers:
            comment = require_text(payload.get("comment"), "comment")
            updated = self.repository.transition_renewal(renewal_id, "returned",
                                                         expected_version, actor, comment)
            self.repository.append_audit("renewal_return", RENEWAL_ENTITY, renewal_id,
                                         actor, {"blockers": blockers,
                                                 "comment": comment})
            return updated
        new_version = int(item["permit_version"]) + 1
        expires_at = compute_permit_expiry(renewal["effective_date"])
        updated = self.repository.approve_renewal(renewal_id, expected_version,
                                                  new_version, expires_at, actor)
        self.repository.append_audit("renewal_approve", RENEWAL_ENTITY, renewal_id,
                                     actor, {"item_id": item["id"],
                                             "issued_version": new_version,
                                             "issued_expires_at": expires_at})
        return updated

    def list_renewals(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_renewals(item_id)

    def get_renewal(self, renewal_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        renewal = self.repository.get_renewal(renewal_id)
        materials = self.repository.list_materials(renewal_id)
        result = dict(renewal)
        result["materials"] = materials
        result["missing_materials"] = missing_required_materials(
            [m["kind"] for m in materials])
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
