
"""HealthGuard Sprint 1 secure foundation."""
from alembic import op
import sqlalchemy as sa
revision="0001_healthguard_sprint1"
down_revision=None
branch_labels=None
depends_on=None

def _enum(name, *values):
    return sa.Enum(*values,name=name)

def upgrade():
    op.create_table("users",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),
        sa.Column("username",sa.String(40),nullable=False), sa.Column("email",sa.String(320),nullable=False),
        sa.Column("password_hash",sa.String(255),nullable=False), sa.Column("email_verified",sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column("status",_enum("accountstatus","ACTIVE","DEACTIVATED","DELETED"),nullable=False,server_default="ACTIVE"),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint("username"),sa.UniqueConstraint("email"))

    op.create_table("patient_profiles",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("owner_user_id",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="CASCADE"),nullable=False),
        sa.Column("first_name",sa.String(100),nullable=False),sa.Column("last_name",sa.String(100),nullable=False),sa.Column("date_of_birth",sa.Date()),
        sa.Column("gender",sa.String(50)),sa.Column("blood_group",sa.String(20)),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False))
    op.create_index("ix_patient_profiles_owner_user_id","patient_profiles",["owner_user_id"])

    op.create_table("patient_access",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("patient_id",sa.Uuid(as_uuid=True),sa.ForeignKey("patient_profiles.id",ondelete="CASCADE"),nullable=False),
        sa.Column("user_id",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="CASCADE"),nullable=False),sa.Column("role",_enum("accessrole","OWNER","FAMILY","CAREGIVER","DOCTOR"),nullable=False),
        sa.Column("granted_by",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="RESTRICT"),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("revoked_at",sa.DateTime(timezone=True)),
        sa.UniqueConstraint("patient_id","user_id",name="uq_patient_access_user"))
    op.create_index("ix_patient_access_patient_id","patient_access",["patient_id"]);op.create_index("ix_patient_access_user_id","patient_access",["user_id"])

    op.create_table("health_events",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("patient_id",sa.Uuid(as_uuid=True),sa.ForeignKey("patient_profiles.id",ondelete="CASCADE"),nullable=False),
        sa.Column("event_type",_enum("eventtype","CONSULTATION","DIAGNOSIS_DOCUMENTED","MEDICATION_STARTED","MEDICATION_CHANGED","MEDICATION_STOPPED","INVESTIGATION_ORDERED","INVESTIGATION_COMPLETED","RESULT_RECORDED","HOSPITALIZATION","SURGERY","DISCHARGE","FOLLOW_UP","VACCINATION","ALLERGY_RECORDED","OTHER"),nullable=False),
        sa.Column("event_date",sa.Date()),sa.Column("date_precision",_enum("dateprecision","EXACT","APPROXIMATE","YEAR","UNKNOWN"),nullable=False),
        sa.Column("title",sa.String(200),nullable=False),sa.Column("description",sa.Text()),sa.Column("provenance",_enum("provenance_health_event","USER_REPORTED","FAMILY_REPORTED","DOCUMENT_VERIFIED","SYSTEM_DERIVED"),nullable=False),
        sa.Column("source_type",_enum("eventsource","MANUAL","DOCUMENT","SYSTEM"),nullable=False),sa.Column("source_id",sa.Uuid(as_uuid=True)),
        sa.Column("created_by",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="RESTRICT"),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False))
    op.create_index("ix_health_events_patient_id","health_events",["patient_id"]);op.create_index("ix_health_events_patient_date","health_events",["patient_id","event_date"])

    op.create_table("medications",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("patient_id",sa.Uuid(as_uuid=True),sa.ForeignKey("patient_profiles.id",ondelete="CASCADE"),nullable=False),
        sa.Column("medicine_name",sa.String(200),nullable=False),sa.Column("dose",sa.String(100)),sa.Column("frequency",sa.String(100)),sa.Column("route",sa.String(100)),
        sa.Column("start_date",sa.Date()),sa.Column("end_date",sa.Date()),sa.Column("status",_enum("medicationstatus","ACTIVE","STOPPED","HISTORICAL"),nullable=False),
        sa.Column("notes",sa.Text()),sa.Column("provenance",_enum("provenance_medication","USER_REPORTED","FAMILY_REPORTED","DOCUMENT_VERIFIED","SYSTEM_DERIVED"),nullable=False),
        sa.Column("source_id",sa.Uuid(as_uuid=True)),sa.Column("created_by",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="RESTRICT"),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False))
    op.create_index("ix_medications_patient_id","medications",["patient_id"])

    op.create_table("medication_events",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("medication_id",sa.Uuid(as_uuid=True),sa.ForeignKey("medications.id",ondelete="CASCADE"),nullable=False),
        sa.Column("event_type",_enum("medicationeventtype","STARTED","CHANGED","STOPPED"),nullable=False),sa.Column("event_date",sa.Date()),sa.Column("dose",sa.String(100)),sa.Column("frequency",sa.String(100)),sa.Column("notes",sa.Text()),
        sa.Column("provenance",_enum("provenance_medication_event","USER_REPORTED","FAMILY_REPORTED","DOCUMENT_VERIFIED","SYSTEM_DERIVED"),nullable=False),
        sa.Column("created_by",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="RESTRICT"),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False))
    op.create_index("ix_medication_events_medication_id","medication_events",["medication_id"])

    op.create_table("investigations",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("patient_id",sa.Uuid(as_uuid=True),sa.ForeignKey("patient_profiles.id",ondelete="CASCADE"),nullable=False),
        sa.Column("name",sa.String(200),nullable=False),sa.Column("ordered_date",sa.Date()),sa.Column("performed_date",sa.Date()),sa.Column("status",_enum("investigationstatus","ORDERED","SCHEDULED","COMPLETED","CANCELLED"),nullable=False),
        sa.Column("notes",sa.Text()),sa.Column("provenance",_enum("provenance_investigation","USER_REPORTED","FAMILY_REPORTED","DOCUMENT_VERIFIED","SYSTEM_DERIVED"),nullable=False),
        sa.Column("created_by",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="RESTRICT"),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False))
    op.create_index("ix_investigations_patient_id","investigations",["patient_id"])
    op.create_table("investigation_results",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("investigation_id",sa.Uuid(as_uuid=True),sa.ForeignKey("investigations.id",ondelete="CASCADE"),nullable=False),
        sa.Column("result_summary",sa.Text(),nullable=False),sa.Column("result_date",sa.Date()),sa.Column("notes",sa.Text()),sa.Column("source_id",sa.Uuid(as_uuid=True)),
        sa.Column("created_by",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="RESTRICT"),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False))
    op.create_index("ix_investigation_results_investigation_id","investigation_results",["investigation_id"])

    op.create_table("documents",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("patient_id",sa.Uuid(as_uuid=True),sa.ForeignKey("patient_profiles.id",ondelete="CASCADE"),nullable=False),
        sa.Column("uploaded_by",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="RESTRICT"),nullable=False),sa.Column("filename",sa.String(255),nullable=False),sa.Column("content_type",sa.String(120),nullable=False),
        sa.Column("file_size",sa.Integer(),nullable=False),sa.Column("storage_key",sa.String(600),nullable=False),sa.Column("checksum",sa.String(64),nullable=False),
        sa.Column("document_type",_enum("documenttype","UNKNOWN","PRESCRIPTION","LAB_REPORT","IMAGING_REPORT","DISCHARGE_SUMMARY","CONSULTATION_NOTE","BILL","VACCINATION_RECORD","MEDICAL_CERTIFICATE","OTHER"),nullable=False),
        sa.Column("status",_enum("documentstatus_document","UPLOADED","QUEUED","PROCESSING","COMPLETED","FAILED","DELETED"),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint("storage_key"),sa.UniqueConstraint("patient_id","checksum",name="uq_documents_patient_checksum"))
    op.create_index("ix_documents_patient_id","documents",["patient_id"]);op.create_index("ix_documents_checksum","documents",["checksum"])

    op.create_table("processing_jobs",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("document_id",sa.Uuid(as_uuid=True),sa.ForeignKey("documents.id",ondelete="CASCADE"),nullable=False,unique=True),
        sa.Column("status",_enum("documentstatus_job","UPLOADED","QUEUED","PROCESSING","COMPLETED","FAILED","DELETED"),nullable=False),sa.Column("attempts",sa.Integer(),nullable=False,server_default="0"),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False))

    op.create_table("audit_logs",
        sa.Column("id",sa.Uuid(as_uuid=True),primary_key=True),sa.Column("actor_user_id",sa.Uuid(as_uuid=True),sa.ForeignKey("users.id",ondelete="SET NULL")),
        sa.Column("action",sa.String(80),nullable=False),sa.Column("resource_type",sa.String(80),nullable=False),sa.Column("resource_id",sa.String(80)),sa.Column("occurred_at",sa.DateTime(timezone=True),nullable=False),sa.Column("context",sa.JSON()))
    op.create_index("ix_audit_logs_actor_user_id","audit_logs",["actor_user_id"])

def downgrade():
    for table in ["audit_logs","processing_jobs","documents","investigation_results","investigations","medication_events","medications","health_events","patient_access","patient_profiles","users"]:
        op.drop_table(table)
    bind=op.get_bind()
    if bind.dialect.name=="postgresql":
        for name in ["accountstatus","accessrole","eventtype","dateprecision","eventsource","medicationstatus","medicationeventtype","investigationstatus","provenance_health_event","provenance_medication","provenance_medication_event","provenance_investigation","documenttype","documentstatus_document","documentstatus_job"]:
            op.execute(sa.text(f"DROP TYPE IF EXISTS {name}"))
