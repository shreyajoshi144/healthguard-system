
import pytest
from uuid import uuid4
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException
from db.models import Base, User, PatientProfile, PatientAccess, AccessRole
from domain.authorization import patient_for_user

@pytest.fixture
def db():
    engine=create_engine("sqlite:///:memory:",connect_args={"check_same_thread":False})
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    s=Session()
    try: yield s
    finally: s.close()

def user(db,email):
    u=User(username=email.split("@")[0],email=email,password_hash="x");db.add(u);db.commit();return u

def test_cross_user_patient_access_is_forbidden(db):
    a=user(db,"a@example.com"); b=user(db,"b@example.com")
    p=PatientProfile(owner_user_id=b.id,first_name="B",last_name="Patient");db.add(p);db.commit()
    with pytest.raises(HTTPException) as exc: patient_for_user(db,a,p.id)
    assert exc.value.status_code==403

def test_explicit_family_access_is_allowed(db):
    a=user(db,"a@example.com"); b=user(db,"b@example.com")
    p=PatientProfile(owner_user_id=b.id,first_name="B",last_name="Patient");db.add(p);db.commit()
    db.add(PatientAccess(patient_id=p.id,user_id=a.id,role=AccessRole.FAMILY,granted_by=b.id));db.commit()
    assert patient_for_user(db,a,p.id).id==p.id
