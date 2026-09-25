import unittest
from src import rules
from src.domain import ConflictError, ValidationError
class RulesTest(unittest.TestCase):
    def test_priority_deadline_and_escalation(self):
        low=rules.priority_score(rules.SEVERITIES[0],1,10,0); high=rules.priority_score(rules.SEVERITIES[-1],30,10,3)
        self.assertGreater(high,low); self.assertLessEqual(rules.response_deadline_hours(rules.SEVERITIES[-1],30,10),rules.response_deadline_hours(rules.SEVERITIES[0],1,10))
        self.assertTrue(rules.escalation_required(rules.SEVERITIES[-1],1,10)); self.assertTrue(rules.escalation_required(rules.SEVERITIES[0],10,10))
    def test_transition_guards(self):
        self.assertTrue(rules.can_transition(rules.STATES[0],rules.STATES[1]))
        with self.assertRaises(ConflictError): rules.validate_transition(rules.STATES[0],rules.STATES[-1])
        with self.assertRaises(ValidationError): rules.priority_score("not-a-severity",1,1)
    def test_renewal_rules(self):
        self.assertEqual(rules.missing_required_materials([]),list(rules.REQUIRED_MATERIALS))
        self.assertEqual(rules.missing_required_materials(list(rules.REQUIRED_MATERIALS)),[])
        self.assertEqual(rules.review_blockers(15,10,0),["拟变更产能超过许可量"])
        self.assertEqual(rules.review_blockers(9,10,2),["仍有未关闭事项"])
        self.assertEqual(rules.review_blockers(9,10,0),[])
        self.assertEqual(rules.compute_permit_expiry("2026-10-01"),"2031-10-01")
        self.assertEqual(rules.compute_permit_expiry("2024-02-29"),"2029-02-28")
        self.assertTrue(rules.can_renewal_transition("returned","submitted"))
        with self.assertRaises(ConflictError): rules.validate_renewal_transition("approved","submitted")
if __name__=="__main__": unittest.main()
