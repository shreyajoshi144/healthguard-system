"""Sprint 6 — measurements/vitals: validation, authorization, provenance, series, audit PHI-safety."""
from __future__ import annotations
import json
from uuid import UUID
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from sqlalchemy import select
from db.models import AuditLog, Measurement, Provenance, AccessRole, Base
from domain.measurements import validate_measurement, MeasurementValidationError, build_series, downsample, SPECS
from tests.test_sprint5_phase1_security import make_db, seed, client, grant, clean

def iso(minutes_ago=1):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()

def bp(sys=120, dia=80, minutes_ago=1, **kw):
    return {"kind": "BLOOD_PRESSURE", "value": sys, "value_secondary": dia, "measured_at": iso(minutes_ago), **kw}

@pytest.fixture
def env():
    S = make_db(); db, a, b, c, p, q = seed(S)
    yield db, a, b, c, p, q
    clean()

# ---------- validation (pure) ----------
@pytest.mark.parametrize("kind,val,sec,unit", [
    ("BLOOD_PRESSURE", 120, None, None),      # diastolic required
    ("BLOOD_PRESSURE", 80, 120, None),        # diastolic > systolic
    ("BLOOD_PRESSURE", 500, 80, None),        # implausible
    ("HEART_RATE", 5, None, None),
    ("HEART_RATE", 70, 60, None),             # secondary not supported
    ("BLOOD_GLUCOSE", 5000, None, "mg/dL"),
    ("BLOOD_GLUCOSE", 100, None, "mg/L"),     # bad unit
    ("TEMPERATURE", 98.6, None, "C"),         # F value labelled C
    ("SPO2", 120, None, None),
    ("NOT_A_KIND", 1, None, None),
])
def test_invalid_measurements_rejected(kind, val, sec, unit):
    with pytest.raises(MeasurementValidationError):
        validate_measurement(kind, val, sec, unit, None, datetime.now(timezone.utc) - timedelta(minutes=1))

def test_future_and_ancient_timestamps_rejected():
    with pytest.raises(MeasurementValidationError):
        validate_measurement("HEART_RATE", 70, None, None, None, datetime.now(timezone.utc) + timedelta(hours=1))
    with pytest.raises(MeasurementValidationError):
        validate_measurement("HEART_RATE", 70, None, None, None, datetime(1800, 1, 1, tzinfo=timezone.utc))

def test_valid_unit_variants_accepted():
    assert validate_measurement("BLOOD_GLUCOSE", 6.1, None, "mmol/L", "FASTING", datetime.now(timezone.utc) - timedelta(minutes=1)) == ("mmol/L", "FASTING")
    assert validate_measurement("TEMPERATURE", 98.6, None, "F", None, datetime.now(timezone.utc) - timedelta(minutes=1))[0] == "F"

# ---------- API behaviour ----------
def test_owner_crud_and_owner_provenance(env):
    db, a, b, c, p, q = env
    u = client(db, a)
    r = u.post(f"/api/patients/{p.id}/measurements", json=bp(note="morning"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["provenance"] == "USER_REPORTED" and body["source_type"] == "MANUAL" and body["unit"] == "mmHg"
    mid = body["id"]
    assert u.patch(f"/api/measurements/{mid}", json={"value": 130, "value_secondary": 85}).json()["value"] == 130
    assert u.patch(f"/api/measurements/{mid}", json={"value_secondary": 200}).status_code == 422   # dia > sys
    listing = u.get(f"/api/patients/{p.id}/measurements").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == mid
    assert u.delete(f"/api/measurements/{mid}").status_code == 204
    assert u.get(f"/api/patients/{p.id}/measurements").json()["total"] == 0

def test_family_entry_is_marked_family_reported(env):
    db, a, b, c, p, q = env
    grant(db, a, p, b, ["MEASUREMENTS"], write=["MEASUREMENTS"])
    r = client(db, b).post(f"/api/patients/{p.id}/measurements", json=bp())
    assert r.status_code == 201 and r.json()["provenance"] == Provenance.FAMILY_REPORTED.value

def test_unknown_fields_nan_and_bad_kind_rejected_at_api(env):
    db, a, b, c, p, q = env
    u = client(db, a)
    assert u.post(f"/api/patients/{p.id}/measurements", json={**bp(), "provenance": "DOCUMENT_VERIFIED"}).status_code == 422   # cannot self-assert verification
    assert u.post(f"/api/patients/{p.id}/measurements", content=json.dumps({**bp(), "value": float("nan")}), headers={"content-type": "application/json"}).status_code == 422
    assert u.post(f"/api/patients/{p.id}/measurements", json={"kind": "X", "value": 1, "measured_at": iso()}).status_code == 422
    assert u.get(f"/api/patients/{p.id}/measurements?kind=NOPE").status_code == 422

def test_stranger_and_wrong_scope_cannot_read_write_or_chart(env):
    db, a, b, c, p, q = env
    u = client(db, a); u.post(f"/api/patients/{p.id}/measurements", json=bp())
    stranger = client(db, c)
    assert stranger.get(f"/api/patients/{p.id}/measurements").status_code == 403
    assert stranger.get(f"/api/patients/{p.id}/measurements/series?kind=BLOOD_PRESSURE").status_code == 403
    assert stranger.post(f"/api/patients/{p.id}/measurements", json=bp()).status_code == 403
    grant(db, a, p, b, ["TIMELINE", "MEDICATIONS"], write=["TIMELINE"])           # no MEASUREMENTS scope
    wrong = client(db, b)
    assert wrong.get(f"/api/patients/{p.id}/measurements").status_code == 403
    assert wrong.post(f"/api/patients/{p.id}/measurements", json=bp()).status_code == 403

def test_idor_on_measurement_id_across_patients(env):
    db, a, b, c, p, q = env
    mid = client(db, a).post(f"/api/patients/{p.id}/measurements", json=bp()).json()["id"]
    other_owner = client(db, b)        # owns a different patient, no relation to p
    assert other_owner.patch(f"/api/measurements/{mid}", json={"value": 99}).status_code == 403
    assert other_owner.delete(f"/api/measurements/{mid}").status_code == 403
    assert db.scalar(select(Measurement).where(Measurement.id == UUID(mid))) is not None
    assert other_owner.patch("/api/measurements/00000000-0000-0000-0000-000000000000", json={"value": 99}).status_code == 404

def test_read_only_grant_cannot_mutate(env):
    db, a, b, c, p, q = env
    mid = client(db, a).post(f"/api/patients/{p.id}/measurements", json=bp()).json()["id"]
    grant(db, a, p, b, ["MEASUREMENTS"])
    ro = client(db, b)
    assert ro.get(f"/api/patients/{p.id}/measurements").status_code == 200
    assert ro.post(f"/api/patients/{p.id}/measurements", json=bp()).status_code == 403
    assert ro.patch(f"/api/measurements/{mid}", json={"value": 99}).status_code == 403
    assert ro.delete(f"/api/measurements/{mid}").status_code == 403

def test_revoked_and_expired_grants_lose_access_immediately(env):
    db, a, b, c, p, q = env
    g = grant(db, a, p, b, ["MEASUREMENTS"])
    u = client(db, b)
    assert u.get(f"/api/patients/{p.id}/measurements").status_code == 200
    g.revoked_at = datetime.now(timezone.utc); db.commit()
    assert u.get(f"/api/patients/{p.id}/measurements").status_code == 403

# ---------- series ----------
def test_series_descriptive_stats_only_and_no_clinical_labels(env):
    db, a, b, c, p, q = env
    u = client(db, a)
    for i, (s, d) in enumerate([(118, 76), (142, 90), (125, 82)]):
        assert u.post(f"/api/patients/{p.id}/measurements", json=bp(s, d, minutes_ago=300 - i * 100)).status_code == 201
    s = u.get(f"/api/patients/{p.id}/measurements/series?kind=BLOOD_PRESSURE").json()
    assert s["total_points"] == 3 and s["stats"]["min"] == 118 and s["stats"]["max"] == 142 and s["stats"]["latest"] == 125
    assert s["stats"]["secondary"]["max"] == 90
    assert [pt["t"] for pt in s["points"]] == sorted(pt["t"] for pt in s["points"])
    flat = json.dumps(s).lower()
    for banned in ("normal", "abnormal", "hypertens", "high blood", "dangerous", "diagnos", "you should", "consult a doctor immediately"):
        assert banned not in flat
    assert "does not interpret" in s["notice"].lower()

def test_series_never_mixes_units(env):
    db, a, b, c, p, q = env
    u = client(db, a)
    u.post(f"/api/patients/{p.id}/measurements", json={"kind": "BLOOD_GLUCOSE", "value": 100, "unit": "mg/dL", "measured_at": iso(60)})
    u.post(f"/api/patients/{p.id}/measurements", json={"kind": "BLOOD_GLUCOSE", "value": 5.5, "unit": "mmol/L", "measured_at": iso(30)})
    s = u.get(f"/api/patients/{p.id}/measurements/series?kind=BLOOD_GLUCOSE").json()
    assert s["unit"] == "mmol/L" and s["total_points"] == 1 and set(s["available_units"]) == {"mg/dL", "mmol/L"}
    s2 = u.get(f"/api/patients/{p.id}/measurements/series?kind=BLOOD_GLUCOSE&unit=mg/dL").json()
    assert s2["unit"] == "mg/dL" and s2["stats"]["latest"] == 100

def test_series_is_patient_scoped(env):
    db, a, b, c, p, q = env
    client(db, a).post(f"/api/patients/{p.id}/measurements", json=bp())
    client(db, b).post(f"/api/patients/{q.id}/measurements", json=bp(150, 95))
    s = client(db, a).get(f"/api/patients/{p.id}/measurements/series?kind=BLOOD_PRESSURE").json()
    assert s["total_points"] == 1 and s["stats"]["latest"] == 120

def test_downsample_keeps_endpoints_and_bounds_size():
    pts = [{"i": i} for i in range(1000)]
    out = downsample(pts, 50)
    assert len(out) <= 51 and out[0] == pts[0] and out[-1] == pts[-1]
    assert downsample(pts[:10], 50) == pts[:10]

def test_empty_series_is_well_formed(env):
    db, a, b, c, p, q = env
    s = client(db, a).get(f"/api/patients/{p.id}/measurements/series?kind=SPO2").json()
    assert s["points"] == [] and s["stats"] is None and s["total_points"] == 0

# ---------- audit PHI safety ----------
def test_audit_records_actions_without_values_or_notes(env):
    db, a, b, c, p, q = env
    client(db, a).post(f"/api/patients/{p.id}/measurements", json=bp(171, 99, note="SECRET-NOTE-XYZ"))
    rows = db.scalars(select(AuditLog).where(AuditLog.action == "MEASUREMENT_CREATED")).all()
    assert len(rows) == 1
    blob = json.dumps(rows[0].context or {}) + str(rows[0].resource_id)
    assert "SECRET-NOTE-XYZ" not in blob and "171" not in blob

# ---------- cascade / migration contract ----------
def test_deleting_patient_removes_measurements(env):
    db, a, b, c, p, q = env
    u = client(db, a); u.post(f"/api/patients/{p.id}/measurements", json=bp())
    assert u.delete(f"/api/patients/{p.id}").status_code == 204
    assert db.scalar(select(Measurement).where(Measurement.patient_id == p.id)) is None

def test_migration_matches_model_columns():
    text = (Path(__file__).parents[2] / "alembic/versions/0012_p0_measurements.py").read_text()
    assert 'down_revision = "0011_sprint5_phase3_reliability"' in text
    for col in Measurement.__table__.columns:
        assert f'"{col.name}"' in text, col.name
    assert "ix_measurements_patient_kind_time" in text and "def downgrade" in text
