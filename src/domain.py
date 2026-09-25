from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; ROLES=['applicant', 'inspector', 'compliance_manager', 'viewer']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
@dataclass(frozen=True)
class Renewal:
    id:int; item_id:int; proposed_capacity:float; effective_date:str; materials:list; status:str; review_comment:Optional[str]; version:int; created_by:str; created_at:str; updated_at:str; reviewed_by:Optional[str]; reviewed_at:Optional[str]
@dataclass(frozen=True)
class PermitVersion:
    id:int; item_id:int; renewal_id:int; version:int; capacity:float; effective_date:str; expires_at:str; created_by:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
def require_date(value,field):
    value=require_text(value,field,10)
    try: datetime.strptime(value,"%Y-%m-%d")
    except ValueError: raise ValidationError(f"{field}必须是YYYY-MM-DD日期")
    return value
def require_materials(value):
    if not isinstance(value,list): raise ValidationError("materials必须是清单数组")
    result=[]
    for index,entry in enumerate(value):
        if not isinstance(entry,dict): raise ValidationError(f"materials[{index}]必须是对象")
        kind=require_text(entry.get("kind"),f"materials[{index}].kind",100)
        detail=require_text(entry.get("detail"),f"materials[{index}].detail")
        result.append({"kind":kind,"detail":detail})
    return result
