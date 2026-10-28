
from __future__ import annotations
from datetime import date, datetime
from enum import Enum
from uuid import UUID
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from db.models import EventType, DatePrecision, Provenance, MedicationStatus, MedicationEventType, InvestigationStatus, DocumentType

class PatientCreate(BaseModel):
    first_name:str=Field(min_length=1,max_length=100); last_name:str=Field(min_length=1,max_length=100)
    date_of_birth:date|None=None; gender:str|None=Field(default=None,max_length=50); blood_group:str|None=Field(default=None,max_length=20)
class PatientUpdate(BaseModel):
    first_name:str|None=Field(default=None,min_length=1,max_length=100); last_name:str|None=Field(default=None,min_length=1,max_length=100)
    date_of_birth:date|None=None; gender:str|None=Field(default=None,max_length=50); blood_group:str|None=Field(default=None,max_length=20)
class PatientOut(PatientCreate):
    model_config=ConfigDict(from_attributes=True)
    id:UUID; owner_user_id:UUID; created_at:datetime; updated_at:datetime

class FamilyGrantCreate(BaseModel):
    user_email:EmailStr
    role:str="FAMILY"
    scopes:list[str]=Field(default=["PROFILE"], min_length=1)
    write_scopes:list[str]=Field(default_factory=list)
class FamilyGrantOut(BaseModel):
    id:UUID; patient_id:UUID; user_id:UUID; role:str; revoked_at:datetime|None=None

class HealthEventCreate(BaseModel):
    event_type:EventType; event_date:date|None=None; date_precision:DatePrecision=DatePrecision.UNKNOWN
    title:str=Field(min_length=1,max_length=200); description:str|None=Field(default=None,max_length=10000)
    def model_post_init(self, __context):
        if self.event_date is None and self.date_precision != DatePrecision.UNKNOWN:
            raise ValueError("An uncertain date precision requires an event date; use UNKNOWN when no date is known.")
        if self.event_date is not None and self.date_precision == DatePrecision.UNKNOWN:
            raise ValueError("A supplied event date must have an explicit date precision.")
        if self.date_precision == DatePrecision.YEAR and self.event_date is not None and (self.event_date.month != 1 or self.event_date.day != 1):
            raise ValueError("YEAR precision must use January 1 as the stored date marker.")

class HealthEventOut(HealthEventCreate):
    model_config=ConfigDict(from_attributes=True)
    id:UUID; patient_id:UUID; source_type:str; created_by:UUID; created_at:datetime; updated_at:datetime; provenance:Provenance
class HealthEventUpdate(BaseModel):
    event_date:date|None=None; date_precision:DatePrecision|None=None; title:str|None=None; description:str|None=None

class MedicationCreate(BaseModel):
    medicine_name:str=Field(min_length=1,max_length=200); dose:str|None=Field(default=None,max_length=100); frequency:str|None=Field(default=None,max_length=100); route:str|None=Field(default=None,max_length=100)
    start_date:date|None=None; end_date:date|None=None; status:MedicationStatus=MedicationStatus.HISTORICAL; notes:str|None=Field(default=None,max_length=10000)
class MedicationOut(MedicationCreate):
    model_config=ConfigDict(from_attributes=True)
    id:UUID; patient_id:UUID; created_by:UUID; created_at:datetime; updated_at:datetime; provenance:Provenance
class MedicationEventCreate(BaseModel):
    event_type:MedicationEventType; event_date:date|None=None; dose:str|None=Field(default=None,max_length=100); frequency:str|None=Field(default=None,max_length=100); notes:str|None=Field(default=None,max_length=10000)

class InvestigationCreate(BaseModel):
    name:str=Field(min_length=1,max_length=200); ordered_date:date|None=None; performed_date:date|None=None
    status:InvestigationStatus=InvestigationStatus.ORDERED; notes:str|None=Field(default=None,max_length=10000)
class InvestigationResultCreate(BaseModel):
    result_summary:str=Field(min_length=1,max_length=20000); result_date:date|None=None; notes:str|None=Field(default=None,max_length=10000)
class InvestigationOut(InvestigationCreate):
    model_config=ConfigDict(from_attributes=True)
    id:UUID; patient_id:UUID; created_by:UUID; created_at:datetime; updated_at:datetime; provenance:Provenance

class DocumentOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:UUID; patient_id:UUID; uploaded_by:UUID; filename:str; content_type:str; file_size:int; checksum:str
    document_type:DocumentType; status:str; created_at:datetime; updated_at:datetime

class HealthFactOut(BaseModel):
    id:UUID
    patient_id:UUID
    document_id:UUID
    fact_type:str
    value:str
    normalized_value:str|None=None
    temporal_date:date|None=None
    temporal_precision:DatePrecision
    source_date_text:str|None=None
    source_page:int
    source_text:str
    confidence:float|None=None
    extraction_method:str
    provenance:Provenance
    created_at:datetime
    model_config=ConfigDict(from_attributes=True)

class RetrievalRequest(BaseModel):
    query:str=Field(min_length=1,max_length=1000)
    date_from:date|None=None
    date_to:date|None=None
    fact_type:str|None=Field(default=None,max_length=80,description="Explicit source-scoped fact type filter; LAB_RESULT selects HealthFact LAB_RESULT records only.")
    limit:int=Field(default=20,ge=1,le=100)
    retrieval_mode:str=Field(default="deterministic",pattern="^(deterministic|semantic|hybrid)$")

class AskHealthRecordsRequest(BaseModel):
    question:str=Field(min_length=1,max_length=2000)
    date_from:date|None=None
    date_to:date|None=None
    fact_type:str|None=Field(default=None,max_length=80)
    retrieval_mode:str=Field(default="hybrid",pattern="^(deterministic|semantic|hybrid)$")

class HandoffCreate(BaseModel):
    title:str=Field(default="Doctor handoff",min_length=1,max_length=200)
    handoff_type:str=Field(default="DOCTOR_HANDOFF",min_length=1,max_length=40)
class HandoffUpdate(BaseModel):
    title:str|None=Field(default=None,min_length=1,max_length=200)
    content:str|None=Field(default=None,max_length=50000)
class HandoffOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:UUID; patient_id:UUID; title:str; handoff_type:str; status:str; content:str; evidence_refs:list|dict; generation_metadata:dict|None
    created_at:datetime; updated_at:datetime

class HealthActionCreate(BaseModel):
    title:str=Field(min_length=1,max_length=200)
    description:str|None=Field(default=None,max_length=10000)
    action_type:str=Field(default="FOLLOW_UP",min_length=1,max_length=80)
    source:str="USER_CREATED"
    due_date:date|None=None
    temporal_precision:DatePrecision=DatePrecision.UNKNOWN
    source_document_id:UUID|None=None
    source_fact_id:UUID|None=None
    source_event_id:UUID|None=None
    source_investigation_id:UUID|None=None
    source_medication_event_id:UUID|None=None
class HealthActionUpdate(BaseModel):
    title:str|None=Field(default=None,min_length=1,max_length=200)
    description:str|None=Field(default=None,max_length=10000)
    due_date:date|None=None
    temporal_precision:DatePrecision|None=None
    status:str|None=None
class HealthActionOut(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id:UUID; patient_id:UUID; title:str; description:str|None; action_type:str; source:str; provenance:Provenance; status:str
    due_date:date|None; temporal_precision:DatePrecision
    source_document_id:UUID|None; source_fact_id:UUID|None; source_event_id:UUID|None; source_investigation_id:UUID|None; source_medication_event_id:UUID|None
    created_at:datetime; updated_at:datetime; completed_at:datetime|None; confirmed_at:datetime|None=None


class RelationshipCreate(BaseModel):
    user_email:EmailStr
    role:str=Field(default="FAMILY",pattern="^(FAMILY|CAREGIVER)$")
    scopes:list[str]=Field(min_length=1)
    write_scopes:list[str]=Field(default_factory=list)
    expires_at:datetime|None=None
class RelationshipOut(BaseModel):
    id:UUID; patient_id:UUID; user_id:UUID; role:str; status:str; scopes:list[str]; write_scopes:list[str]=[]; expires_at:datetime|None; revoked_at:datetime|None; created_at:datetime; updated_at:datetime
class TemporaryShareCreate(BaseModel):
    scopes:list[str]=Field(min_length=1)
    resources:dict[str,list[UUID]]={}
    expires_at:datetime
class TemporaryShareOut(BaseModel):
    id:UUID; patient_id:UUID; role:str; scopes:list[str]; resources:dict; expires_at:datetime; revoked_at:datetime|None; created_at:datetime
class ShareAccessRequest(BaseModel):
    token:str=Field(min_length=40,max_length=256)
class MedicationContextRequest(BaseModel):
    question:str=Field(min_length=1,max_length=2000)
class MedicationContextResponse(BaseModel):
    answer:str; grounded:bool; patient_facts:list[dict]; educational:dict; safety_flags:list[str]
