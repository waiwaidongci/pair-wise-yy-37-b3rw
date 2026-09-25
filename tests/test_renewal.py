import tempfile, unittest
from datetime import date, timedelta
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import compute_expiry


def future_date(days=30):
    return (date.today() + timedelta(days=days)).isoformat()


class RenewalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _item(self, threshold=100.0, external_ref="RN-1"):
        return self.service.create_item({
            "title": "renewal permit", "description": "renewal scenarios",
            "severity": "high", "quantity": 80, "threshold": threshold,
            "external_ref": external_ref,
        }, "creator", "applicant")

    def _materials(self):
        return [
            {"kind": "test_report", "detail": "检测报告附件"},
            {"kind": "facility_operation", "detail": "治理设施运行记录附件"},
        ]

    def test_full_renewal_flow_generates_permit_version(self):
        item = self._item()
        payload = {"proposed_capacity": 90, "effective_date": future_date(),
                   "materials": self._materials()}
        renewal = self.service.create_renewal(item["id"], payload, "creator", "applicant")
        self.assertEqual(renewal["status"], "draft")
        self.assertEqual(renewal["version"], 1)

        with self.assertRaises(ConflictError):
            self.service.create_renewal(item["id"], payload, "creator", "applicant")

        submitted = self.service.submit_renewal(renewal["id"], renewal["version"],
                                                "creator", "applicant")
        self.assertEqual(submitted["status"], "submitted")
        self.assertEqual(submitted["version"], 2)

        approved = self.service.review_renewal(submitted["id"],
                                               {"expected_version": 2},
                                               "manager", "compliance_manager")
        self.assertEqual(approved["status"], "approved")

        permits = self.service.list_permits(item["id"], "viewer")
        self.assertEqual(len(permits), 1)
        permit = permits[0]
        self.assertEqual(permit["version"], 1)
        self.assertEqual(permit["capacity"], 90)
        self.assertEqual(permit["expires_at"],
                         compute_expiry(payload["effective_date"]))
        self.assertEqual(self.service.get_item(item["id"], "viewer")["quantity"], 90)

        second = self.service.create_renewal(
            item["id"], {"proposed_capacity": 95, "effective_date": future_date(60),
                         "materials": self._materials()},
            "creator", "applicant")
        history = self.service.list_renewals(item["id"], "viewer")
        self.assertEqual([r["id"] for r in history], [renewal["id"], second["id"]])
        self.assertEqual(history[0]["status"], "approved")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_missing_required_material_blocks_submit(self):
        item = self._item(external_ref="RN-2")
        renewal = self.service.create_renewal(item["id"], {
            "proposed_capacity": 90, "effective_date": future_date(),
            "materials": [{"kind": "other_doc", "detail": "其他材料"}],
        }, "creator", "applicant")
        with self.assertRaises(ConflictError) as ctx:
            self.service.submit_renewal(renewal["id"], 1, "creator", "applicant")
        message = str(ctx.exception)
        self.assertIn("检测报告", message)
        self.assertIn("治理设施运行记录", message)

    def test_over_capacity_returns_with_comment_then_resubmit(self):
        item = self._item(threshold=100.0, external_ref="RN-3")
        renewal = self.service.create_renewal(item["id"], {
            "proposed_capacity": 120, "effective_date": future_date(),
            "materials": self._materials(),
        }, "creator", "applicant")
        self.service.submit_renewal(renewal["id"], 1, "creator", "applicant")
        with self.assertRaises(ValidationError):
            self.service.review_renewal(renewal["id"],
                                        {"expected_version": 2},
                                        "manager", "compliance_manager")
        returned = self.service.review_renewal(renewal["id"],
                                               {"expected_version": 2,
                                                "comment": "产能超许可量，请调整"},
                                               "manager", "compliance_manager")
        self.assertEqual(returned["status"], "returned")
        self.assertEqual(returned["review_comment"], "产能超许可量，请调整")
        self.assertEqual(self.service.list_permits(item["id"], "viewer"), [])

        updated = self.service.update_renewal(renewal["id"], {
            "proposed_capacity": 88, "effective_date": future_date(10),
            "materials": self._materials(), "expected_version": 3,
        }, "creator", "applicant")
        self.assertEqual(updated["status"], "returned")
        self.assertIsNone(updated["review_comment"])
        resubmitted = self.service.submit_renewal(renewal["id"], updated["version"],
                                                  "creator", "applicant")
        approved = self.service.review_renewal(renewal["id"],
                                               {"expected_version": resubmitted["version"]},
                                               "manager", "compliance_manager")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(self.service.list_permits(item["id"], "viewer")[0]["capacity"], 88)

    def test_open_record_blocks_approval_until_closed(self):
        item = self._item(external_ref="RN-4")
        record = self.service.add_record(item["id"], {
            "kind": "action", "detail": "整改事项", "status": "open",
            "external_ref": "ACT-1",
        }, "recorder", "inspector")
        renewal = self.service.create_renewal(item["id"], {
            "proposed_capacity": 90, "effective_date": future_date(),
            "materials": self._materials(),
        }, "creator", "applicant")
        self.service.submit_renewal(renewal["id"], 1, "creator", "applicant")
        returned = self.service.review_renewal(renewal["id"], {
            "expected_version": 2, "comment": "存在未关闭整改",
        }, "manager", "compliance_manager")
        self.assertEqual(returned["status"], "returned")

        self.service.close_record(item["id"], record["id"], "recorder", "inspector")
        self.service.submit_renewal(renewal["id"], 3, "creator", "applicant")
        approved = self.service.review_renewal(renewal["id"],
                                               {"expected_version": 4},
                                               "manager", "compliance_manager")
        self.assertEqual(approved["status"], "approved")

    def test_permissions_and_version_conflict(self):
        item = self._item(external_ref="RN-5")
        with self.assertRaises(PermissionDenied):
            self.service.create_renewal(item["id"], {
                "proposed_capacity": 90, "effective_date": future_date(),
                "materials": self._materials(),
            }, "creator", "viewer")
        renewal = self.service.create_renewal(item["id"], {
            "proposed_capacity": 90, "effective_date": future_date(),
            "materials": self._materials(),
        }, "creator", "applicant")
        with self.assertRaises(PermissionDenied):
            self.service.submit_renewal(renewal["id"], 1, "spy", "inspector")
        self.service.submit_renewal(renewal["id"], 1, "creator", "applicant")
        with self.assertRaises(PermissionDenied):
            self.service.review_renewal(renewal["id"],
                                        {"expected_version": 2},
                                        "creator", "applicant")
        with self.assertRaises(ConflictError):
            self.service.review_renewal(renewal["id"],
                                        {"expected_version": 1},
                                        "manager", "compliance_manager")


if __name__ == "__main__":
    unittest.main()
