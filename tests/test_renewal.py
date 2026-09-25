import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
class RenewalTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"renewal permit","description":"permit to renew","severity":"medium","quantity":8,"threshold":10,"external_ref":"REN-1"},"creator","applicant")
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _create(self,capacity=9.0):
        return self.service.create_renewal(self.item["id"],{"proposed_capacity":capacity,"effective_date":"2026-10-01"},"applicant","applicant")
    def _materials(self,renewal_id):
        self.service.add_renewal_material(renewal_id,{"kind":"test_report","detail":"emission test report","external_ref":"M-TR"},"applicant","applicant")
        self.service.add_renewal_material(renewal_id,{"kind":"facility_operation_record","detail":"facility operation log","external_ref":"M-FR"},"applicant","applicant")
    def test_full_cycle_issues_new_permit_version(self):
        renewal=self._create()
        with self.assertRaises(ConflictError): self._create()
        with self.assertRaises(ConflictError): self.service.submit_renewal(renewal["id"],renewal["version"],"applicant","applicant")
        self._materials(renewal["id"])
        submitted=self.service.submit_renewal(renewal["id"],renewal["version"],"applicant","applicant")
        self.assertEqual(submitted["status"],"submitted")
        approved=self.service.review_renewal(renewal["id"],{"expected_version":submitted["version"]},"manager","compliance_manager")
        self.assertEqual(approved["status"],"approved"); self.assertEqual(approved["issued_version"],2); self.assertEqual(approved["issued_expires_at"],"2031-10-01")
        item=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(item["permit_version"],2); self.assertEqual(item["permit_expires_at"],"2031-10-01")
        renewals=self.service.list_renewals(self.item["id"],"viewer")
        self.assertEqual(len(renewals),1); self.assertEqual(renewals[0]["status"],"approved")
        self.assertEqual(self._create()["status"],"draft")
        self.assertTrue(self.repo.verify_audit_chain())
    def test_return_for_supplement_then_approve(self):
        renewal=self._create(capacity=15.0); self._materials(renewal["id"])
        submitted=self.service.submit_renewal(renewal["id"],renewal["version"],"applicant","applicant")
        with self.assertRaises(ValidationError): self.service.review_renewal(renewal["id"],{"expected_version":submitted["version"]},"manager","compliance_manager")
        returned=self.service.review_renewal(renewal["id"],{"expected_version":submitted["version"],"comment":"产能超许可量，请调整"},"manager","compliance_manager")
        self.assertEqual(returned["status"],"returned"); self.assertEqual(returned["review_comment"],"产能超许可量，请调整")
        with self.assertRaises(ConflictError): self._create()
        amended=self.service.amend_renewal(renewal["id"],{"proposed_capacity":9.0,"effective_date":"2026-11-01","expected_version":returned["version"]},"applicant","applicant")
        resubmitted=self.service.submit_renewal(renewal["id"],amended["version"],"applicant","applicant")
        self.assertIsNone(resubmitted["review_comment"])
        approved=self.service.review_renewal(renewal["id"],{"expected_version":resubmitted["version"]},"manager","compliance_manager")
        self.assertEqual(approved["status"],"approved"); self.assertEqual(approved["issued_expires_at"],"2031-11-01")
    def test_open_records_block_approval(self):
        self.service.add_record(self.item["id"],{"kind":"correction","detail":"unresolved fix","status":"open","external_ref":"OP-1"},"inspector","inspector")
        renewal=self._create(); self._materials(renewal["id"])
        submitted=self.service.submit_renewal(renewal["id"],renewal["version"],"applicant","applicant")
        returned=self.service.review_renewal(renewal["id"],{"expected_version":submitted["version"],"comment":"先关闭整改"},"manager","compliance_manager")
        self.assertEqual(returned["status"],"returned")
    def test_permission_and_version_guards(self):
        renewal=self._create()
        with self.assertRaises(PermissionDenied): self.service.create_renewal(self.item["id"],{"proposed_capacity":9,"effective_date":"2026-10-01"},"attacker","viewer")
        with self.assertRaises(PermissionDenied): self.service.add_renewal_material(renewal["id"],{"kind":"other","detail":"x"},"attacker","viewer")
        with self.assertRaises(ValidationError): self.service.add_renewal_material(renewal["id"],{"kind":"unknown","detail":"x"},"applicant","applicant")
        self._materials(renewal["id"])
        with self.assertRaises(ConflictError): self.service.submit_renewal(renewal["id"],99,"applicant","applicant")
        with self.assertRaises(PermissionDenied): self.service.submit_renewal(renewal["id"],renewal["version"],"attacker","viewer")
        submitted=self.service.submit_renewal(renewal["id"],renewal["version"],"applicant","applicant")
        with self.assertRaises(ConflictError): self.service.add_renewal_material(renewal["id"],{"kind":"other","detail":"late material"},"applicant","applicant")
        with self.assertRaises(PermissionDenied): self.service.review_renewal(renewal["id"],{"expected_version":submitted["version"]},"applicant","applicant")
    def test_get_renewal_shows_missing_materials(self):
        renewal=self._create()
        detail=self.service.get_renewal(renewal["id"],"viewer")
        self.assertEqual(detail["missing_materials"],["test_report","facility_operation_record"])
        self._materials(renewal["id"])
        detail=self.service.get_renewal(renewal["id"],"viewer")
        self.assertEqual(detail["missing_materials"],[]); self.assertEqual(len(detail["materials"]),2)
if __name__=="__main__": unittest.main()
