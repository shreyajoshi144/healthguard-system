
from __future__ import annotations
import enum
import uuid
from datetime import date, datetime, timezone, timedelta
from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

def utcnow(): return datetime.now(timezone.utc)

class Base(DeclarativeBase): pass

class AccountStatus(str, enum.Enum):
    ACTIVE="ACTIVE"; DEACTIVATED="DEACTIVATED"; DELETED="DELETED"
class AccessRole(str, enum.Enum):
    OWNER="OWNER"; FAMILY="FAMILY"; CAREGIVER="CAREGIVER"; DOCTOR="DOCTOR"
class AccessGrantStatus(str, enum.Enum):
    ACTIVE="ACTIVE"; REVOKED="REVOKED"; EXPIRED="EXPIRED"
class AccessScope(str, enum.Enum):
    PROFILE="PROFILE"; DOCUMENTS="DOCUMENTS"; MEDICATIONS="MEDICATIONS"; INVESTIGATIONS="INVESTIGATIONS"; TIMELINE="TIMELINE"; HANDOFFS="HANDOFFS"; HEALTH_ACTIONS="HEALTH_ACTIONS"; MEASUREMENTS="MEASUREMENTS"
class Provenance(str, enum.Enum):
    USER_REPORTED="USER_REPORTED"; FAMILY_REPORTED="FAMILY_REPORTED"; DOCUMENT_VERIFIED="DOCUMENT_VERIFIED"; SYSTEM_DERIVED="SYSTEM_DERIVED"
class EventType(str, enum.Enum):
    CONSULTATION="CONSULTATION"; DIAGNOSIS_DOCUMENTED="DIAGNOSIS_DOCUMENTED"; MEDICATION_STARTED="MEDICATION_STARTED"; MEDICATION_CHANGED="MEDICATION_CHANGED"; MEDICATION_STOPPED="MEDICATION_STOPPED"; INVESTIGATION_ORDERED="INVESTIGATION_ORDERED"; INVESTIGATION_COMPLETED="INVESTIGATION_COMPLETED"; RESULT_RECORDED="RESULT_RECORDED"; HOSPITALIZATION="HOSPITALIZATION"; SURGERY="SURGERY"; DISCHARGE="DISCHARGE"; FOLLOW_UP="FOLLOW_UP"; VACCINATION="VACCINATION"; ALLERGY_RECORDED="ALLERGY_RECORDED"; OTHER="OTHER"
class DatePrecision(str, enum.Enum):
    EXACT="EXACT"; APPROXIMATE="APPROXIMATE"; YEAR="YEAR"; UNKNOWN="UNKNOWN"
class EventSource(str, enum.Enum):
    MANUAL="MANUAL"; DOCUMENT="DOCUMENT"; SYSTEM="SYSTEM"
class MedicationEventType(str, enum.Enum):
    STARTED="STARTED"; CHANGED="CHANGED"; STOPPED="STOPPED"
class MedicationStatus(str, enum.Enum):
    ACTIVE="ACTIVE"; STOPPED="STOPPED"; HISTORICAL="HISTORICAL"
class InvestigationStatus(str, enum.Enum):
    ORDERED="ORDERED"; SCHEDULED="SCHEDULED"; COMPLETED="COMPLETED"; CANCELLED="CANCELLED"
class DocumentType(str, enum.Enum):
    UNKNOWN="UNKNOWN"; PRESCRIPTION="PRESCRIPTION"; LAB_REPORT="LAB_REPORT"; IMAGING_REPORT="IMAGING_REPORT"; DISCHARGE_SUMMARY="DISCHARGE_SUMMARY"; CONSULTATION_NOTE="CONSULTATION_NOTE"; BILL="BILL"; VACCINATION_RECORD="VACCINATION_RECORD"; MEDICAL_CERTIFICATE="MEDICAL_CERTIFICATE"; OTHER="OTHER"
class DocumentStatus(str, enum.Enum):
    UPLOADED="UPLOADED"; QUEUED="QUEUED"; PROCESSING="PROCESSING"; COMPLETED="COMPLETED"; FAILED="FAILED"; DELETION_PENDING="DELETION_PENDING"; DELETED="DELETED"
class HandoffStatus(str, enum.Enum):
    DRAFT="DRAFT"; FINAL="FINAL"
class HealthActionStatus(str, enum.Enum):
    PLANNED="PLANNED"; SCHEDULED="SCHEDULED"; COMPLETED="COMPLETED"; CANCELLED="CANCELLED"
class HealthActionSource(str, enum.Enum):
    USER_CREATED="USER_CREATED"; DOCUMENT_DERIVED="DOCUMENT_DERIVED"

class User(Base):
    __tablename__="users"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str]=mapped_column(String(40), unique=True)
    email: Mapped[str]=mapped_column(String(320), unique=True)
    password_hash: Mapped[str]=mapped_column(String(255))
    email_verified: Mapped[bool]=mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[AccountStatus]=mapped_column(Enum(AccountStatus), default=AccountStatus.ACTIVE, nullable=False)
    deletion_pending: Mapped[bool]=mapped_column(Boolean, default=False, nullable=False)
    session_version: Mapped[int]=mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    patients: Mapped[list["PatientProfile"]]=relationship(back_populates="owner", cascade="all, delete-orphan", foreign_keys="PatientProfile.owner_user_id")
    access_grants: Mapped[list["PatientAccess"]]=relationship(back_populates="user", cascade="all, delete-orphan", foreign_keys="PatientAccess.user_id")

class PatientProfile(Base):
    __tablename__="patient_profiles"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    first_name: Mapped[str]=mapped_column(String(100), nullable=False)
    last_name: Mapped[str]=mapped_column(String(100), nullable=False)
    date_of_birth: Mapped[date|None]=mapped_column(Date)
    gender: Mapped[str|None]=mapped_column(String(50))
    blood_group: Mapped[str|None]=mapped_column(String(20))
    deletion_pending: Mapped[bool]=mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    owner: Mapped[User]=relationship(back_populates="patients", foreign_keys=[owner_user_id])
    access_grants: Mapped[list["PatientAccess"]]=relationship(back_populates="patient", cascade="all, delete-orphan")
    events: Mapped[list["HealthEvent"]]=relationship(back_populates="patient", cascade="all, delete-orphan")
    medications: Mapped[list["Medication"]]=relationship(back_populates="patient", cascade="all, delete-orphan")
    investigations: Mapped[list["Investigation"]]=relationship(back_populates="patient", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]]=relationship(back_populates="patient", cascade="all, delete-orphan")
    health_facts: Mapped[list["HealthFact"]]=relationship(back_populates="patient", cascade="all, delete-orphan")
    handoffs: Mapped[list["Handoff"]]=relationship(back_populates="patient", cascade="all, delete-orphan")
    health_actions: Mapped[list["HealthAction"]]=relationship(back_populates="patient", cascade="all, delete-orphan")
    measurements: Mapped[list["Measurement"]]=relationship(back_populates="patient", cascade="all, delete-orphan")

class PatientAccess(Base):
    __tablename__="patient_access"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[AccessRole]=mapped_column(Enum(AccessRole), nullable=False, default=AccessRole.FAMILY)
    granted_by: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[AccessGrantStatus]=mapped_column(Enum(AccessGrantStatus, native_enum=False, length=16), default=AccessGrantStatus.ACTIVE, nullable=False)
    scopes: Mapped[list|dict]=mapped_column(JSON, nullable=False, default=list)
    write_scopes: Mapped[list|dict]=mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    __table_args__=(UniqueConstraint("patient_id","user_id",name="uq_patient_access_user"), CheckConstraint("role <> 'OWNER'", name="ck_patient_access_secondary_role"),)
    patient: Mapped[PatientProfile]=relationship(back_populates="access_grants")
    user: Mapped[User]=relationship(back_populates="access_grants", foreign_keys=[user_id])

class HealthEvent(Base):
    __tablename__="health_events"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[EventType]=mapped_column(Enum(EventType), nullable=False)
    event_date: Mapped[date|None]=mapped_column(Date)
    date_precision: Mapped[DatePrecision]=mapped_column(Enum(DatePrecision), default=DatePrecision.UNKNOWN, nullable=False)
    title: Mapped[str]=mapped_column(String(200), nullable=False)
    description: Mapped[str|None]=mapped_column(Text)
    provenance: Mapped[Provenance]=mapped_column(Enum(Provenance, native_enum=False, length=32), default=Provenance.USER_REPORTED, nullable=False)
    source_type: Mapped[EventSource]=mapped_column(Enum(EventSource), default=EventSource.MANUAL, nullable=False)
    source_id: Mapped[uuid.UUID|None]=mapped_column()
    created_by: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    patient: Mapped[PatientProfile]=relationship(back_populates="events")
    __table_args__=(Index("ix_health_events_patient_date","patient_id","event_date"),)

class Medication(Base):
    __tablename__="medications"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_name: Mapped[str]=mapped_column(String(200), nullable=False)
    dose: Mapped[str|None]=mapped_column(String(100))
    frequency: Mapped[str|None]=mapped_column(String(100))
    route: Mapped[str|None]=mapped_column(String(100))
    start_date: Mapped[date|None]=mapped_column(Date)
    end_date: Mapped[date|None]=mapped_column(Date)
    status: Mapped[MedicationStatus]=mapped_column(Enum(MedicationStatus), default=MedicationStatus.HISTORICAL, nullable=False)
    notes: Mapped[str|None]=mapped_column(Text)
    provenance: Mapped[Provenance]=mapped_column(Enum(Provenance, native_enum=False, length=32), default=Provenance.USER_REPORTED, nullable=False)
    source_id: Mapped[uuid.UUID|None]=mapped_column()
    created_by: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    patient: Mapped[PatientProfile]=relationship(back_populates="medications")
    events: Mapped[list["MedicationEvent"]]=relationship(back_populates="medication", cascade="all, delete-orphan")
class MedicationEvent(Base):
    __tablename__="medication_events"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    medication_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("medications.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[MedicationEventType]=mapped_column(Enum(MedicationEventType), nullable=False)
    event_date: Mapped[date|None]=mapped_column(Date)
    dose: Mapped[str|None]=mapped_column(String(100))
    frequency: Mapped[str|None]=mapped_column(String(100))
    notes: Mapped[str|None]=mapped_column(Text)
    provenance: Mapped[Provenance]=mapped_column(Enum(Provenance, native_enum=False, length=32), default=Provenance.USER_REPORTED, nullable=False)
    created_by: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    medication: Mapped[Medication]=relationship(back_populates="events")

class Investigation(Base):
    __tablename__="investigations"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str]=mapped_column(String(200), nullable=False)
    ordered_date: Mapped[date|None]=mapped_column(Date)
    performed_date: Mapped[date|None]=mapped_column(Date)
    status: Mapped[InvestigationStatus]=mapped_column(Enum(InvestigationStatus), default=InvestigationStatus.ORDERED, nullable=False)
    notes: Mapped[str|None]=mapped_column(Text)
    provenance: Mapped[Provenance]=mapped_column(Enum(Provenance, native_enum=False, length=32), default=Provenance.USER_REPORTED, nullable=False)
    created_by: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    patient: Mapped[PatientProfile]=relationship(back_populates="investigations")
    results: Mapped[list["InvestigationResult"]]=relationship(back_populates="investigation", cascade="all, delete-orphan")
class InvestigationResult(Base):
    __tablename__="investigation_results"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    investigation_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False, index=True)
    result_summary: Mapped[str]=mapped_column(Text, nullable=False)
    result_date: Mapped[date|None]=mapped_column(Date)
    notes: Mapped[str|None]=mapped_column(Text)
    source_id: Mapped[uuid.UUID|None]=mapped_column()
    created_by: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    investigation: Mapped[Investigation]=relationship(back_populates="results")

class Document(Base):
    __tablename__="documents"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    uploaded_by: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    filename: Mapped[str]=mapped_column(String(255), nullable=False)
    content_type: Mapped[str]=mapped_column(String(120), nullable=False)
    file_size: Mapped[int]=mapped_column(Integer, nullable=False)
    storage_key: Mapped[str]=mapped_column(String(600), unique=True, nullable=False)
    checksum: Mapped[str]=mapped_column(String(64), nullable=False, index=True)
    document_type: Mapped[DocumentType]=mapped_column(Enum(DocumentType), default=DocumentType.UNKNOWN, nullable=False)
    status: Mapped[DocumentStatus]=mapped_column(Enum(DocumentStatus, name="documentstatus_document", native_enum=False, length=16), default=DocumentStatus.UPLOADED, nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    patient: Mapped[PatientProfile]=relationship(back_populates="documents")
    __table_args__=(UniqueConstraint("patient_id","checksum",name="uq_documents_patient_checksum"),)

class ProcessingJob(Base):
    __tablename__="processing_jobs"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, unique=True)
    status: Mapped[DocumentStatus]=mapped_column(Enum(DocumentStatus, name="documentstatus_job", native_enum=False, length=16), default=DocumentStatus.QUEUED, nullable=False)
    attempts: Mapped[int]=mapped_column(Integer, default=0, nullable=False)
    lease_expires_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), index=True)
    next_attempt_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str|None]=mapped_column(String(120))
    quarantined_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), index=True)
    quarantine_reason: Mapped[str|None]=mapped_column(String(60))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    document: Mapped[Document]=relationship()

class DocumentTextBlock(Base):
    __tablename__="document_text_blocks"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number: Mapped[int]=mapped_column(Integer, nullable=False)
    block_index: Mapped[int]=mapped_column(Integer, nullable=False)
    source_text: Mapped[str]=mapped_column(Text, nullable=False)
    extraction_method: Mapped[str]=mapped_column(String(32), nullable=False)
    confidence: Mapped[float|None]=mapped_column()
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    document: Mapped[Document]=relationship()
    __table_args__=(UniqueConstraint("document_id","page_number","block_index",name="uq_document_text_block"), Index("ix_document_text_blocks_document_page","document_id","page_number"))



class HealthFact(Base):
    __tablename__="health_facts"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    fact_type: Mapped[str]=mapped_column(String(80), nullable=False)
    value: Mapped[str]=mapped_column(Text, nullable=False)
    normalized_value: Mapped[str|None]=mapped_column(Text)
    temporal_date: Mapped[date|None]=mapped_column(Date)
    temporal_precision: Mapped[DatePrecision]=mapped_column(Enum(DatePrecision, name="healthfactdateprecision", native_enum=False, length=16), nullable=False, default=DatePrecision.UNKNOWN, server_default="UNKNOWN")
    source_date_text: Mapped[str|None]=mapped_column(Text)
    source_page: Mapped[int]=mapped_column(Integer, nullable=False)
    source_text: Mapped[str]=mapped_column(Text, nullable=False)
    confidence: Mapped[float|None]=mapped_column()
    extraction_method: Mapped[str]=mapped_column(String(40), nullable=False)
    provenance: Mapped[Provenance]=mapped_column(Enum(Provenance, name="healthfactprovenance", native_enum=False, length=32), nullable=False, default=Provenance.DOCUMENT_VERIFIED)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    patient: Mapped[PatientProfile]=relationship(back_populates="health_facts")
    document: Mapped[Document]=relationship()
    __table_args__=(Index("ix_health_facts_patient_document","patient_id","document_id"), Index("ix_health_facts_patient_temporal_date","patient_id","temporal_date"),)


class Handoff(Base):
    __tablename__="handoffs"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str]=mapped_column(String(200), nullable=False)
    handoff_type: Mapped[str]=mapped_column(String(40), nullable=False)
    status: Mapped[HandoffStatus]=mapped_column(Enum(HandoffStatus, native_enum=False, length=16), default=HandoffStatus.DRAFT, nullable=False)
    content: Mapped[str]=mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list|dict]=mapped_column(JSON, nullable=False, default=list)
    generation_metadata: Mapped[dict|None]=mapped_column(JSON)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    patient: Mapped[PatientProfile]=relationship(back_populates="handoffs")
    __table_args__=(Index("ix_handoffs_patient_created","patient_id","created_at"),)

class HealthAction(Base):
    __tablename__="health_actions"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str]=mapped_column(String(200), nullable=False)
    description: Mapped[str|None]=mapped_column(Text)
    action_type: Mapped[str]=mapped_column(String(80), nullable=False)
    source: Mapped[HealthActionSource]=mapped_column(Enum(HealthActionSource, native_enum=False, length=24), nullable=False)
    provenance: Mapped[Provenance]=mapped_column(Enum(Provenance, native_enum=False, length=32), nullable=False)
    status: Mapped[HealthActionStatus]=mapped_column(Enum(HealthActionStatus, native_enum=False, length=16), default=HealthActionStatus.PLANNED, nullable=False)
    due_date: Mapped[date|None]=mapped_column(Date)
    temporal_precision: Mapped[DatePrecision]=mapped_column(Enum(DatePrecision, native_enum=False, length=16), default=DatePrecision.UNKNOWN, nullable=False)
    source_document_id: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    source_fact_id: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("health_facts.id", ondelete="SET NULL"))
    source_event_id: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("health_events.id", ondelete="SET NULL"))
    source_investigation_id: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("investigations.id", ondelete="SET NULL"))
    source_medication_event_id: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("medication_events.id", ondelete="SET NULL"))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))   # set when the user confirms a document-derived suggestion
    patient: Mapped[PatientProfile]=relationship(back_populates="health_actions")
    __table_args__=(Index("ix_health_actions_patient_status_due","patient_id","status","due_date"),)

class EmailVerificationToken(Base):
    __tablename__="email_verification_tokens"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str]=mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

class TemporaryShare(Base):
    __tablename__="temporary_shares"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    token_hash: Mapped[str]=mapped_column(String(64), unique=True, nullable=False, index=True)
    role: Mapped[AccessRole]=mapped_column(Enum(AccessRole, native_enum=False, length=16), nullable=False, default=AccessRole.DOCTOR)
    scopes: Mapped[list|dict]=mapped_column(JSON, nullable=False, default=list)
    resources: Mapped[dict|list]=mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__=(Index("ix_temporary_shares_patient_active","patient_id","expires_at","revoked_at"),)


class AuditLog(Base):
    __tablename__="audit_logs"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str]=mapped_column(String(80), nullable=False)
    resource_type: Mapped[str]=mapped_column(String(80), nullable=False)
    resource_id: Mapped[str|None]=mapped_column(String(80))
    occurred_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    context: Mapped[dict|None]=mapped_column(JSON)


class Measurement(Base):
    """User-recorded or document-derived health measurement (BP, glucose, ...).

    Values are stored exactly as supplied (after plausibility validation); the
    platform never labels a value normal/abnormal. Provenance distinguishes
    user-entered readings from document-derived ones.
    """
    __tablename__="measurements"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str]=mapped_column(String(32), nullable=False)
    value: Mapped[float]=mapped_column(Float, nullable=False)
    value_secondary: Mapped[float|None]=mapped_column(Float)
    unit: Mapped[str]=mapped_column(String(16), nullable=False)
    measured_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    context: Mapped[str|None]=mapped_column(String(32))
    note: Mapped[str|None]=mapped_column(String(500))
    provenance: Mapped[Provenance]=mapped_column(Enum(Provenance, native_enum=False, length=32), nullable=False)
    source_type: Mapped[EventSource]=mapped_column(Enum(EventSource, native_enum=False, length=16), nullable=False, default=EventSource.MANUAL)
    source_document_id: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    source_fact_id: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("health_facts.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    patient: Mapped[PatientProfile]=relationship(back_populates="measurements")
    __table_args__=(Index("ix_measurements_patient_kind_time","patient_id","kind","measured_at"),)


class AdminRole(str, enum.Enum):
    VIEWER="VIEWER"        # read-only operational views
    OPERATOR="OPERATOR"    # VIEWER + audited operational actions (retry, quarantine, ack alerts, revoke sessions)

class AdminGrant(Base):
    """Operational admin identity. Created only by the offline CLI (scripts/admin.py);
    there is no API to self-promote. The TOTP secret is never stored: it is derived
    from HEALTHGUARD_ADMIN_TOTP_KEY and this per-grant salt."""
    __tablename__="admin_grants"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    role: Mapped[AdminRole]=mapped_column(Enum(AdminRole, native_enum=False, length=16), nullable=False, default=AdminRole.VIEWER)
    totp_salt: Mapped[str]=mapped_column(String(64), nullable=False)
    last_totp_step: Mapped[int|None]=mapped_column(BigInteger)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

class OpsAlert(Base):
    """Operational alert. Never contains PHI; titles/details are fixed server-side strings."""
    __tablename__="ops_alerts"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str]=mapped_column(String(80), nullable=False, index=True)
    source: Mapped[str]=mapped_column(String(32), nullable=False)
    severity: Mapped[str]=mapped_column(String(16), nullable=False)
    title: Mapped[str]=mapped_column(String(160), nullable=False)
    detail: Mapped[str|None]=mapped_column(String(300))
    status: Mapped[str]=mapped_column(String(16), nullable=False, default="OPEN")
    occurrences: Mapped[int]=mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    acknowledged_by: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    acknowledged_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    delivery_state: Mapped[str]=mapped_column(String(24), nullable=False, default="NOT_CONFIGURED")
    delivery_attempts: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
    last_delivery_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    __table_args__=(Index("ix_ops_alerts_status_seen","status","last_seen_at"),)

class SecurityEvent(Base):
    """Durable, PHI-free security signal (auth/authorization failures, rate limiting, CSRF)."""
    __tablename__="security_events"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    event_type: Mapped[str]=mapped_column(String(32), nullable=False, index=True)
    route: Mapped[str]=mapped_column(String(120), nullable=False)
    method: Mapped[str]=mapped_column(String(8), nullable=False)
    status_code: Mapped[int]=mapped_column(Integer, nullable=False)
    actor_user_id: Mapped[uuid.UUID|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    client_hash: Mapped[str|None]=mapped_column(String(16))
    request_id: Mapped[str|None]=mapped_column(String(64))


class Consent(Base):
    """A user's explicit, versioned decision about one privacy purpose (current state; every change is also audit-logged).

    ``policy_version`` records which wording the user agreed to: when the wording changes, the old consent stops counting."""
    __tablename__="consents"
    __table_args__=(UniqueConstraint("user_id","purpose",name="uq_consents_user_purpose"),)
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    purpose: Mapped[str]=mapped_column(String(40), nullable=False)
    policy_version: Mapped[str]=mapped_column(String(20), nullable=False)
    granted: Mapped[bool]=mapped_column(Boolean, nullable=False, default=False)
    decided_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

class ReminderPreference(Base):
    """A user's opt-in to email reminders for one patient's confirmed/scheduled follow-ups. Email only."""
    __tablename__="reminder_preferences"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    enabled: Mapped[bool]=mapped_column(Boolean, nullable=False, default=False)
    lead_days: Mapped[list]=mapped_column(JSON, nullable=False, default=list)
    send_hour: Mapped[int]=mapped_column(Integer, nullable=False, default=9)
    timezone: Mapped[str]=mapped_column(String(64), nullable=False, default="UTC")
    include_title: Mapped[bool]=mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__=(UniqueConstraint("user_id","patient_id",name="uq_reminder_pref_user_patient"),)

class ReminderDelivery(Base):
    """One email to send. (user, action, lead_days, due_date) is unique, which makes planning idempotent."""
    __tablename__="reminder_deliveries"
    id: Mapped[uuid.UUID]=mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("patient_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    action_id: Mapped[uuid.UUID]=mapped_column(ForeignKey("health_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_days: Mapped[int]=mapped_column(Integer, nullable=False)
    due_date: Mapped[date]=mapped_column(Date, nullable=False)
    scheduled_for: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str]=mapped_column(String(16), nullable=False, default="PENDING")
    attempts: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    last_error: Mapped[str|None]=mapped_column(String(40))
    sent_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__=(UniqueConstraint("user_id","action_id","lead_days","due_date",name="uq_reminder_delivery_idempotency"),
                    Index("ix_reminder_deliveries_due","status","scheduled_for"))
