from __future__ import annotations
from datetime import datetime
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
RENEWAL_ENTITY='许可续期申请'; RENEWAL_STATES=['draft', 'submitted', 'returned', 'approved']; RENEWAL_TRANSITIONS={'draft': ['submitted'], 'submitted': ['returned', 'approved'], 'returned': ['submitted'], 'approved': []}; RENEWAL_TERMINAL_STATES=set(['approved']); RENEWAL_AMENDABLE_STATES=set(['draft', 'returned'])
MATERIAL_KINDS=['test_report', 'facility_operation_record', 'other']; REQUIRED_MATERIALS=('test_report', 'facility_operation_record'); MATERIAL_LABELS={'test_report': '检测报告', 'facility_operation_record': '治理设施运行记录', 'other': '其他材料'}
RENEWAL_CREATE_ROLES=set(['applicant']); MATERIAL_ROLES=set(['applicant']); RENEWAL_SUBMIT_ROLES=set(['applicant']); RENEWAL_AMEND_ROLES=set(['applicant']); RENEWAL_REVIEW_ROLES=set(['compliance_manager'])
PERMIT_VALIDITY_YEARS=5
def can_renewal_transition(current,target): return target in RENEWAL_TRANSITIONS.get(current,[])
def validate_renewal_transition(current,target):
    if current not in RENEWAL_STATES or target not in RENEWAL_STATES: raise ValidationError("未知续期状态")
    if not can_renewal_transition(current,target): raise ConflictError(f"续期申请不能从{current}转换到{target}")
def missing_required_materials(kinds): return [kind for kind in REQUIRED_MATERIALS if kind not in set(kinds)]
def review_blockers(proposed_capacity,permitted_capacity,open_records):
    blockers=[]
    if proposed_capacity>permitted_capacity: blockers.append("拟变更产能超过许可量")
    if open_records>0: blockers.append("仍有未关闭事项")
    return blockers
def compute_permit_expiry(effective_date):
    start=datetime.strptime(effective_date,"%Y-%m-%d").date()
    try: return start.replace(year=start.year+PERMIT_VALIDITY_YEARS).isoformat()
    except ValueError: return start.replace(year=start.year+PERMIT_VALIDITY_YEARS,day=28).isoformat()
