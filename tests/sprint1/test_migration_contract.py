
from pathlib import Path

def test_sprint1_migration_exists_and_is_explicit():
    path=Path(__file__).parents[2]/"alembic/versions/0001_healthguard_sprint1_foundation.py"
    text=path.read_text()
    assert 'revision="0001_healthguard_sprint1"' in text
    for table in ["users","patient_profiles","patient_access","health_events","medications","medication_events","investigations","investigation_results","documents","processing_jobs","audit_logs"]:
        assert f'create_table("{table}"' in text
    assert "def downgrade():" in text


def test_hardening_migration_contains_security_contract():
    path=Path(__file__).parents[2]/"alembic/versions/0002_sprint1_hardening.py"
    text=path.read_text()
    assert 'revision="0002_sprint1_hardening"' in text
    assert "DELETION_PENDING" in text
    assert "email_verification_tokens" in text
    assert "ck_patient_access_secondary_role" in text


def test_model_migration_enum_values_match():
    from db.models import EventType, DatePrecision, AccessRole, Provenance, MedicationStatus, MedicationEventType, InvestigationStatus, DocumentType, DocumentStatus
    model_values={cls.__name__: {x.value for x in cls} for cls in [EventType,DatePrecision,AccessRole,Provenance,MedicationStatus,MedicationEventType,InvestigationStatus,DocumentType,DocumentStatus]}
    text=(Path(__file__).parents[2]/"alembic/versions/0001_healthguard_sprint1_foundation.py").read_text() + (Path(__file__).parents[2]/"alembic/versions/0002_sprint1_hardening.py").read_text()
    assert all(v in text for values in model_values.values() for v in values)
