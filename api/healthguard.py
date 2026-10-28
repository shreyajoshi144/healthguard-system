
from __future__ import annotations
import hashlib, os, tempfile, json
from pathlib import Path
from uuid import UUID
from datetime import date, datetime, timezone
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status, Query
from fastapi.responses import Response
from sqlalchemy import select, delete, cast, String
from sqlalchemy.orm import Session
from db.session import get_db
from db.models import *
from backend.auth import get_current_user
from domain.retention import purge_document_derived_content
from domain.consent import require_ai_consent
from domain.authorization import patient_for_user, resource_patient, processing_job_for_user, provenance_for_actor, GRANTABLE_ROLES, require_scope, active_grant
from domain.audit import audit
from api.schemas import *
from storage.service import storage, validate_upload, validate_upload_file, StorageError
import storage.service as storage_service
from jobs.queue import enqueue_document, QueueUnavailable
from domain.observability import inc
from domain.rate_limit import rate_limit

router=APIRouter(prefix="/api")
MAX_PAGE_SIZE=100

def page_params(page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=MAX_PAGE_SIZE)):
    return page,page_size

def paginate(stmt,page,page_size):
    return stmt.offset((page-1)*page_size).limit(page_size)

def commit(db): db.commit()

@router.post("/patients",response_model=PatientOut,status_code=201)
def create_patient(payload:PatientCreate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    p=PatientProfile(owner_user_id=user.id,**payload.model_dump()); db.add(p); db.flush()
    audit(db,user,"PATIENT_CREATED","patient",p.id); commit(db); db.refresh(p); return p

@router.get("/patients",response_model=list[PatientOut])
def list_patients(page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=MAX_PAGE_SIZE),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    now=datetime.now(timezone.utc)
    stmt=select(PatientProfile).outerjoin(PatientAccess,PatientAccess.patient_id==PatientProfile.id).where(
        (PatientProfile.owner_user_id==user.id) | ((PatientAccess.user_id==user.id)&(PatientAccess.revoked_at.is_(None))&(PatientAccess.status==AccessGrantStatus.ACTIVE)&((PatientAccess.expires_at.is_(None))|(PatientAccess.expires_at>now))&(cast(PatientAccess.scopes,String).like("%PROFILE%")))
    ).distinct().order_by(PatientProfile.created_at.desc(),PatientProfile.id.desc())
    return list(db.scalars(paginate(stmt,page,page_size)).all())

@router.get("/patients/{patient_id}",response_model=PatientOut)
def get_patient(patient_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return require_scope(db,user,patient_id,AccessScope.PROFILE)

@router.patch("/patients/{patient_id}",response_model=PatientOut)
def update_patient(patient_id:UUID,payload:PatientUpdate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    p=require_scope(db,user,patient_id,AccessScope.PROFILE,write=True)
    for k,v in payload.model_dump(exclude_unset=True).items(): setattr(p,k,v)
    audit(db,user,"PATIENT_UPDATED","patient",p.id); commit(db); db.refresh(p); return p

@router.delete("/patients/{patient_id}",status_code=204)
def delete_patient(patient_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    p=require_scope(db,user,patient_id,AccessScope.PROFILE,write=True)
    if p.owner_user_id!=user.id: raise HTTPException(403,"Only the patient owner can delete a patient profile.")
    p.deletion_pending=True
    db.commit()
    try:
        for d in list(p.documents):
            if d.status==DocumentStatus.DELETED: continue
            try:
                storage.delete(d.storage_key)
            except Exception as exc:
                raise StorageError("Patient storage deletion is pending.") from exc
            d.status=DocumentStatus.DELETED
        db.commit()
    except StorageError:
        db.rollback()
        # Re-mark pending in a fresh transaction. This state is durable and the
        # same DELETE request can be retried safely after storage recovers.
        p=db.get(PatientProfile,patient_id)
        if p:
            p.deletion_pending=True
            db.commit()
        raise HTTPException(503,"Patient deletion is pending because storage could not be updated. Please retry.")
    p=db.get(PatientProfile,patient_id)
    if not p: return
    audit(db,user,"PATIENT_DELETED","patient",p.id)
    db.delete(p); commit(db)

@router.post("/patients/{patient_id}/family",response_model=FamilyGrantOut,status_code=201)
def grant_family(patient_id:UUID,payload:FamilyGrantCreate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    p=require_scope(db,user,patient_id,AccessScope.PROFILE,write=True)
    if p.owner_user_id!=user.id: raise HTTPException(403,"Only the patient owner can manage family access.")
    target=db.scalar(select(User).where(User.email==str(payload.user_email).lower()))
    if not target: raise HTTPException(404,"Family member account not found.")
    if target.id==user.id: raise HTTPException(409,"Owner already has access.")
    try: role=AccessRole(payload.role.upper())
    except ValueError: raise HTTPException(422,"Unsupported family role.")
    from domain.sharing import grant_relationship
    try:
        grant=grant_relationship(db,patient=p,actor=user,target=target,role=role,scopes=payload.scopes,write_scopes=payload.write_scopes,expires_at=None)
    except HTTPException:
        db.rollback(); raise
    audit(db,user,"FAMILY_ACCESS_GRANTED","patient_access",grant.id); commit(db); db.refresh(grant)
    return {"id":grant.id,"patient_id":grant.patient_id,"user_id":grant.user_id,"role":grant.role.value,"revoked_at":grant.revoked_at}

@router.get("/patients/{patient_id}/family")
def list_family(patient_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    p=require_scope(db,user,patient_id,AccessScope.PROFILE,write=True)
    if p.owner_user_id!=user.id: raise HTTPException(403,"Only the patient owner can manage family access.")
    rows=list(db.scalars(select(PatientAccess).where(PatientAccess.patient_id==patient_id).order_by(PatientAccess.created_at.asc(),PatientAccess.id.asc())).all())
    return [{"id":str(g.id),"patient_id":str(g.patient_id),"user_id":str(g.user_id),"role":g.role.value,"revoked_at":g.revoked_at,"email":g.user.email} for g in rows]

@router.delete("/patients/{patient_id}/family/{user_id}",status_code=204)
def revoke_family(patient_id:UUID,user_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    p=require_scope(db,user,patient_id,AccessScope.PROFILE,write=True)
    if p.owner_user_id!=user.id: raise HTTPException(403,"Only the patient owner can manage family access.")
    grant=db.scalar(select(PatientAccess).where(PatientAccess.patient_id==patient_id,PatientAccess.user_id==user_id,PatientAccess.revoked_at.is_(None)))
    if not grant: raise HTTPException(404,"Access grant not found.")
    from datetime import datetime,timezone
    grant.revoked_at=datetime.now(timezone.utc); audit(db,user,"FAMILY_ACCESS_REVOKED","patient_access",grant.id); commit(db)

@router.post("/patients/{patient_id}/events",response_model=HealthEventOut,status_code=201)
def create_event(patient_id:UUID,payload:HealthEventCreate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.TIMELINE,write=True)
    e=HealthEvent(patient_id=patient_id,created_by=user.id,source_type=EventSource.MANUAL,provenance=provenance_for_actor(db,user,patient_id),**payload.model_dump()); db.add(e); audit(db,user,"HEALTH_EVENT_CREATED","health_event",e.id); commit(db); db.refresh(e); return e

@router.get("/patients/{patient_id}/events",response_model=list[HealthEventOut])
def list_events(patient_id:UUID,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=MAX_PAGE_SIZE),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.TIMELINE)
    stmt=select(HealthEvent).where(HealthEvent.patient_id==patient_id).order_by(HealthEvent.event_date.desc().nullslast(),HealthEvent.created_at.desc(),HealthEvent.id.desc())
    return list(db.scalars(paginate(stmt,page,page_size)).all())

@router.patch("/events/{event_id}",response_model=HealthEventOut)
def update_event(event_id:UUID,payload:HealthEventUpdate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    e,p=resource_patient(db,user,HealthEvent,event_id,scope=AccessScope.TIMELINE,write=True)
    values=payload.model_dump(exclude_unset=True)
    new_date=values.get("event_date",e.event_date)
    new_precision=values.get("date_precision",e.date_precision)
    if new_date is None and new_precision!=DatePrecision.UNKNOWN: raise HTTPException(422,"An unknown event date must use UNKNOWN precision.")
    if new_date is not None and new_precision==DatePrecision.UNKNOWN: raise HTTPException(422,"A supplied event date must have an explicit date precision.")
    if new_precision==DatePrecision.YEAR and new_date is not None and (new_date.month!=1 or new_date.day!=1): raise HTTPException(422,"YEAR precision must use January 1 as the stored date marker.")
    for k,v in values.items(): setattr(e,k,v)
    audit(db,user,"HEALTH_EVENT_UPDATED","health_event",e.id); commit(db); db.refresh(e); return e

@router.delete("/events/{event_id}",status_code=204)
def delete_event(event_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    e,p=resource_patient(db,user,HealthEvent,event_id,scope=AccessScope.TIMELINE,write=True); audit(db,user,"HEALTH_EVENT_DELETED","health_event",e.id); db.delete(e); commit(db)

@router.post("/patients/{patient_id}/medications",response_model=MedicationOut,status_code=201)
def create_medication(patient_id:UUID,payload:MedicationCreate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.MEDICATIONS,write=True)
    if payload.start_date and payload.end_date and payload.end_date<payload.start_date: raise HTTPException(422,"Medication end date cannot precede start date.")
    m=Medication(patient_id=patient_id,created_by=user.id,provenance=provenance_for_actor(db,user,patient_id),**payload.model_dump()); db.add(m); audit(db,user,"MEDICATION_CREATED","medication",m.id); commit(db); db.refresh(m); return m

@router.get("/patients/{patient_id}/medications",response_model=list[MedicationOut])
def list_medications(patient_id:UUID,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=MAX_PAGE_SIZE),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.MEDICATIONS)
    stmt=select(Medication).where(Medication.patient_id==patient_id).order_by(Medication.start_date.desc().nullslast(),Medication.created_at.desc(),Medication.id.desc())
    return list(db.scalars(paginate(stmt,page,page_size)).all())

@router.get("/medications/{medication_id}/events")
def list_medication_events(medication_id:UUID,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=MAX_PAGE_SIZE),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    m,p=resource_patient(db,user,Medication,medication_id,scope=AccessScope.MEDICATIONS)
    stmt=select(MedicationEvent).where(MedicationEvent.medication_id==m.id).order_by(MedicationEvent.event_date.desc().nullslast(),MedicationEvent.created_at.desc(),MedicationEvent.id.desc())
    rows=list(db.scalars(paginate(stmt,page,page_size)).all())
    return [{"id":str(e.id),"medication_id":str(e.medication_id),"event_type":e.event_type.value,"event_date":e.event_date,"dose":e.dose,"frequency":e.frequency,"notes":e.notes,"provenance":e.provenance.value} for e in rows]

@router.post("/medications/{medication_id}/events",status_code=201)
def create_medication_event(medication_id:UUID,payload:MedicationEventCreate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    m,p=resource_patient(db,user,Medication,medication_id,scope=AccessScope.MEDICATIONS,write=True)
    ev=MedicationEvent(medication_id=m.id,created_by=user.id,provenance=provenance_for_actor(db,user,p.id),**payload.model_dump()); db.add(ev)
    if payload.event_type==MedicationEventType.STOPPED: m.status=MedicationStatus.STOPPED
    elif payload.event_type==MedicationEventType.STARTED: m.status=MedicationStatus.ACTIVE
    audit(db,user,"MEDICATION_UPDATED","medication",m.id); commit(db); db.refresh(ev)
    return {"id":str(ev.id),"medication_id":str(m.id),"event_type":ev.event_type.value,"event_date":ev.event_date,"provenance":ev.provenance.value}

@router.post("/patients/{patient_id}/investigations",response_model=InvestigationOut,status_code=201)
def create_investigation(patient_id:UUID,payload:InvestigationCreate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.INVESTIGATIONS,write=True)
    if payload.ordered_date and payload.performed_date and payload.performed_date<payload.ordered_date: raise HTTPException(422,"Performed date cannot precede ordered date.")
    i=Investigation(patient_id=patient_id,created_by=user.id,provenance=provenance_for_actor(db,user,patient_id),**payload.model_dump()); db.add(i); audit(db,user,"INVESTIGATION_CREATED","investigation",i.id); commit(db); db.refresh(i); return i

@router.get("/patients/{patient_id}/investigations",response_model=list[InvestigationOut])
def list_investigations(patient_id:UUID,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=MAX_PAGE_SIZE),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.INVESTIGATIONS)
    stmt=select(Investigation).where(Investigation.patient_id==patient_id).order_by(Investigation.performed_date.desc().nullslast(),Investigation.created_at.desc(),Investigation.id.desc())
    return list(db.scalars(paginate(stmt,page,page_size)).all())

@router.post("/investigations/{investigation_id}/results",status_code=201)
def create_investigation_result(investigation_id:UUID,payload:InvestigationResultCreate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    inv,p=resource_patient(db,user,Investigation,investigation_id,scope=AccessScope.INVESTIGATIONS,write=True)
    result=InvestigationResult(investigation_id=inv.id,created_by=user.id,**payload.model_dump())
    db.add(result); audit(db,user,"INVESTIGATION_RESULT_CREATED","investigation_result",result.id); commit(db); db.refresh(result)
    return {"id":str(result.id),"investigation_id":str(inv.id),"result_summary":result.result_summary,"result_date":result.result_date,"notes":result.notes}

@router.get("/investigations/{investigation_id}/results")
def list_investigation_results(investigation_id:UUID,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=MAX_PAGE_SIZE),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    inv,p=resource_patient(db,user,Investigation,investigation_id,scope=AccessScope.INVESTIGATIONS)
    stmt=select(InvestigationResult).where(InvestigationResult.investigation_id==inv.id).order_by(InvestigationResult.result_date.desc().nullslast(),InvestigationResult.created_at.desc(),InvestigationResult.id.desc())
    rows=list(db.scalars(paginate(stmt,page,page_size)).all())
    return [{"id":str(r.id),"investigation_id":str(r.investigation_id),"result_summary":r.result_summary,"result_date":r.result_date,"notes":r.notes} for r in rows]

@router.post("/patients/{patient_id}/documents",response_model=DocumentOut,status_code=201)
async def upload_document(patient_id:UUID,file:UploadFile=File(...),db:Session=Depends(get_db),user:User=Depends(get_current_user),_rl=Depends(rate_limit("document_upload",30))):
    require_scope(db,user,patient_id,AccessScope.DOCUMENTS,write=True)
    temp_path=None
    try:
        temp_path,size,checksum=await _stream_upload(file)
        safe=validate_upload_file(temp_path,file.filename or "document",file.content_type or "",size)
        duplicate=db.scalar(select(Document).where(Document.patient_id==patient_id,Document.checksum==checksum,Document.status!=DocumentStatus.DELETED))
        if duplicate and duplicate.status!=DocumentStatus.FAILED:
            raise HTTPException(409,"This document has already been uploaded for this patient.")

        if duplicate and duplicate.status==DocumentStatus.FAILED:
            # A failed upload/queue attempt is intentionally retryable by re-sending
            # the same content. The durable Document/ProcessingJob records are reused.
            doc=duplicate
            doc.filename=safe; doc.content_type=file.content_type; doc.file_size=size
            job=db.scalar(select(ProcessingJob).where(ProcessingJob.document_id==doc.id))
            if job is None:
                job=ProcessingJob(document_id=doc.id,status=DocumentStatus.FAILED)
                db.add(job); db.flush()
        else:
            doc=Document(patient_id=patient_id,uploaded_by=user.id,filename=safe,content_type=file.content_type,file_size=size,storage_key="pending",checksum=checksum,document_type=DocumentType.UNKNOWN,status=DocumentStatus.UPLOADED)
            db.add(doc); db.flush()
            key=storage.key(patient_id,doc.id,safe); doc.storage_key=key
            job=ProcessingJob(document_id=doc.id,status=DocumentStatus.QUEUED)
            db.add(job)
            audit(db,user,"DOCUMENT_UPLOADED","document",doc.id)
        db.commit()
        key=doc.storage_key
        try:
            storage.put_file(key,temp_path,file.content_type)
        except Exception:
            doc=db.get(Document,doc.id); job=db.scalar(select(ProcessingJob).where(ProcessingJob.document_id==doc.id))
            doc.status=DocumentStatus.FAILED; job.status=DocumentStatus.FAILED; db.commit()
            raise HTTPException(503,"Document upload is recoverable; please retry the same upload.")
        doc=db.get(Document,doc.id); job=db.scalar(select(ProcessingJob).where(ProcessingJob.document_id==doc.id))
        # Commit the durable QUEUED state before publishing to Redis. If Redis is
        # unavailable, the database record remains recoverable by the worker/recovery loop.
        doc.status=DocumentStatus.QUEUED; job.status=DocumentStatus.QUEUED; job.next_attempt_at=None; job.lease_expires_at=None
        commit(db); db.refresh(doc)
        try:
            enqueue_document(str(job.id))
        except QueueUnavailable:
            raise HTTPException(503,"Document processing is temporarily unavailable; please retry the upload.")
        return doc
    except HTTPException:
        raise
    except StorageError as exc:
        raise HTTPException(413 if "size" in str(exc).lower() else 422,str(exc))
    finally:
        if temp_path: temp_path.unlink(missing_ok=True)

async def _stream_upload(file:UploadFile):
    fd,temp=tempfile.mkstemp(prefix="healthguard-upload-")
    os.close(fd)
    path=Path(temp)
    size=0
    digest=hashlib.sha256()
    try:
        with path.open("wb") as out:
            while True:
                chunk=await file.read(1024*1024)
                if not chunk: break
                size+=len(chunk)
                if size>storage_service.MAX_FILE_SIZE: raise StorageError("File exceeds the allowed size.")
                out.write(chunk); digest.update(chunk)
        return path,size,digest.hexdigest()
    except Exception:
        path.unlink(missing_ok=True)
        raise

@router.get("/patients/{patient_id}/documents",response_model=list[DocumentOut])
def list_documents(patient_id:UUID,page:int=Query(1,ge=1),page_size:int=Query(50,ge=1,le=MAX_PAGE_SIZE),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.DOCUMENTS)
    stmt=select(Document).where(Document.patient_id==patient_id,Document.status!=DocumentStatus.DELETED).order_by(Document.created_at.desc(),Document.id.desc())
    return list(db.scalars(paginate(stmt,page,page_size)).all())

@router.get("/documents/{document_id}")
def get_document(document_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    doc,p=resource_patient(db,user,Document,document_id,scope=AccessScope.DOCUMENTS)
    if doc.status in {DocumentStatus.DELETED,DocumentStatus.DELETION_PENDING}: raise HTTPException(404,"Document not found.")
    audit(db,user,"DOCUMENT_VIEWED","document",doc.id); commit(db)
    url=storage.signed_url(doc.storage_key)
    if url: return {"id":str(doc.id),"download_url":url,"expires_in":300}
    return {"id":str(doc.id),"content_url":f"/api/documents/{doc.id}/content","content_type":doc.content_type,"filename":doc.filename}

@router.get("/documents/{document_id}/content")
def download_document(document_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    doc,p=resource_patient(db,user,Document,document_id,scope=AccessScope.DOCUMENTS)
    if doc.status in {DocumentStatus.DELETED,DocumentStatus.DELETION_PENDING}: raise HTTPException(404,"Document not found.")
    try: data=storage.get(doc.storage_key)
    except Exception: raise HTTPException(404,"Document content is unavailable.")
    audit(db,user,"DOCUMENT_VIEWED","document",doc.id); commit(db)
    return Response(content=data,media_type=doc.content_type,headers={"Content-Disposition":f'attachment; filename="{doc.filename}"',"Cache-Control":"private, no-store"})

@router.delete("/documents/{document_id}",status_code=204)
def delete_document(document_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    doc,p=resource_patient(db,user,Document,document_id,scope=AccessScope.DOCUMENTS,write=True)
    if p.owner_user_id!=user.id and doc.uploaded_by!=user.id: raise HTTPException(403,"Document deletion is restricted.")
    if doc.status==DocumentStatus.DELETED:
        # Idempotent retry: also scrubs derived content left behind by deletions made before purge-on-delete existed.
        purge_document_derived_content(db,doc.id); commit(db); return
    doc.status=DocumentStatus.DELETION_PENDING
    db.commit()
    try:
        storage.delete(doc.storage_key)
    except Exception:
        # Keep the deletion-pending state durable. A later retry can safely
        # repeat the storage delete; we never claim permanent deletion here.
        raise HTTPException(503,"Document deletion is pending because storage could not be updated. Please retry.")
    doc=db.get(Document,document_id)
    doc.status=DocumentStatus.DELETED
    purged=purge_document_derived_content(db,doc.id)
    audit(db,user,"DOCUMENT_DELETED","document",doc.id,context={"purged_text_blocks":purged["text_blocks"],"purged_facts":purged["facts"]})
    commit(db)

@router.post("/documents/{document_id}/retry-processing")
def retry_document_processing(document_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user),_rl=Depends(rate_limit("document_processing",20))):
    doc,p=resource_patient(db,user,Document,document_id,scope=AccessScope.DOCUMENTS,write=True)
    if doc.status not in {DocumentStatus.FAILED, DocumentStatus.QUEUED}:
        raise HTTPException(409,"Document is not in a retryable state.")
    if job := db.scalar(select(ProcessingJob).where(ProcessingJob.document_id==doc.id)):
        if job.quarantined_at is not None:
            raise HTTPException(409,"Processing for this document is on hold for operational review.")
        if job.status==DocumentStatus.PROCESSING:
            raise HTTPException(409,"Document processing is already in progress.")
    job=db.scalar(select(ProcessingJob).where(ProcessingJob.document_id==doc.id))
    if not job:
        job=ProcessingJob(document_id=doc.id,status=DocumentStatus.FAILED)
        db.add(job); db.flush()
    try:
        # The storage object must already exist for a queue-only retry.
        storage.get(doc.storage_key)
    except Exception:
        raise HTTPException(409,"Document content is unavailable; retry the upload instead.")
    job.status=DocumentStatus.QUEUED; doc.status=DocumentStatus.QUEUED; job.next_attempt_at=None; job.lease_expires_at=None; job.last_error=None
    # Publish only after the durable state is committed; a queue failure leaves
    # a recoverable QUEUED job rather than a falsely terminal failure.
    commit(db)
    try:
        enqueue_document(str(job.id))
    except QueueUnavailable:
        raise HTTPException(503,"Document processing is temporarily unavailable; please retry.")
    return {"id":str(doc.id),"job_id":str(job.id),"status":job.status.value,"attempts":job.attempts}

@router.get("/patients/{patient_id}/health-facts", response_model=list[HealthFactOut])
def list_health_facts(
    patient_id:UUID,
    fact_type:str|None=Query(default=None,max_length=80),
    document_id:UUID|None=Query(default=None),
    start_date:date|None=Query(default=None),
    end_date:date|None=Query(default=None),
    min_confidence:float|None=Query(default=None,ge=0.0,le=1.0),
    page:int=Query(1,ge=1),
    page_size:int=Query(50,ge=1,le=MAX_PAGE_SIZE),
    db:Session=Depends(get_db),
    user:User=Depends(get_current_user),
):
    require_scope(db,user,patient_id,AccessScope.DOCUMENTS)
    if start_date and end_date and end_date < start_date:
        raise HTTPException(422,"end_date cannot precede start_date.")
    from domain.longitudinal import retrieve_health_facts
    return retrieve_health_facts(db,patient_id,fact_type=fact_type,document_id=document_id,start_date=start_date,end_date=end_date,min_confidence=min_confidence,page=page,page_size=page_size)

@router.get("/patients/{patient_id}/timeline")
def get_patient_timeline(patient_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.TIMELINE)
    from domain.longitudinal import build_longitudinal_timeline
    return {"patient_id":str(patient_id),"items":build_longitudinal_timeline(db,patient_id,allowed_scopes=_authorized_retrieval_scopes(db,user,patient_id))}

@router.get("/patients/{patient_id}/health-snapshot")
def get_patient_health_snapshot(patient_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.TIMELINE)
    from domain.longitudinal import build_health_snapshot
    return build_health_snapshot(db,patient_id,allowed_scopes=_authorized_retrieval_scopes(db,user,patient_id))

@router.get("/processing-jobs/{job_id}")
def get_processing_job(job_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    job,document,patient=processing_job_for_user(db,user,job_id)
    return {"id":str(job.id),"document_id":str(job.document_id),"status":job.status.value,"attempts":job.attempts,"created_at":job.created_at,"updated_at":job.updated_at}



@router.post("/patients/{patient_id}/handoffs", response_model=HandoffOut, status_code=201)
def create_handoff(patient_id:UUID, payload:HandoffCreate, db:Session=Depends(get_db), user:User=Depends(get_current_user), _rl=Depends(rate_limit("handoff_generation",10))):
    patient=require_scope(db,user,patient_id,AccessScope.HANDOFFS,write=True)
    require_ai_consent(db,patient)
    from domain.handoff import generate_handoff
    try:
        artifact=generate_handoff(db,patient,title=payload.title,handoff_type=payload.handoff_type,allowed_scopes=_authorized_retrieval_scopes(db,user,patient_id))
    except Exception as exc:
        db.rollback()
        audit(db,user,"HANDOFF_GENERATION_FAILED","patient",patient_id,context={"error_type":type(exc).__name__})
        db.commit()
        raise HTTPException(503,"Handoff generation is temporarily unavailable.") from exc
    audit(db,user,"HANDOFF_CREATED","handoff",artifact.id,context={"patient_id":str(patient_id),"status":artifact.status.value})
    commit(db); db.refresh(artifact); return artifact

@router.get("/patients/{patient_id}/handoffs", response_model=list[HandoffOut])
def list_handoffs(patient_id:UUID, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.HANDOFFS)
    return list(db.scalars(select(Handoff).where(Handoff.patient_id==patient_id).order_by(Handoff.created_at.desc(),Handoff.id.desc())).all())

@router.get("/handoffs/{handoff_id}", response_model=HandoffOut)
def get_handoff(handoff_id:UUID, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    artifact,patient=resource_patient(db,user,Handoff,handoff_id,scope=AccessScope.HANDOFFS)
    audit(db,user,"HANDOFF_VIEWED","handoff",artifact.id); commit(db)
    return artifact

@router.patch("/handoffs/{handoff_id}", response_model=HandoffOut)
def update_handoff(handoff_id:UUID, payload:HandoffUpdate, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    artifact,patient=resource_patient(db,user,Handoff,handoff_id,scope=AccessScope.HANDOFFS,write=True)
    if artifact.status==HandoffStatus.FINAL:
        raise HTTPException(409,"Finalized handoffs are immutable; create a new draft to make changes.")
    changes=payload.model_dump(exclude_unset=True)
    for key,value in changes.items(): setattr(artifact,key,value)
    audit(db,user,"HANDOFF_UPDATED","handoff",artifact.id,context={"fields":list(changes)})
    commit(db); db.refresh(artifact); return artifact

@router.post("/handoffs/{handoff_id}/finalize", response_model=HandoffOut)
def finalize_handoff(handoff_id:UUID, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    artifact,patient=resource_patient(db,user,Handoff,handoff_id,scope=AccessScope.HANDOFFS,write=True)
    if artifact.status==HandoffStatus.FINAL: return artifact
    from domain.handoff import authorized_snapshot_for_handoff
    from domain.grounding import validate_grounded_answer
    from ai.answer_provider import AnswerClaim, AnswerResult
    try:
        evidence=authorized_snapshot_for_handoff(db,artifact,allowed_scopes=_authorized_retrieval_scopes(db,user,patient.id))
    except ValueError as exc:
        raise HTTPException(422,"The reviewed handoff evidence is no longer available or authorized; create a new draft.") from exc
    stored_refs={str(ref.get("source_id")):ref for ref in (artifact.evidence_refs or []) if ref.get("source_id")}
    remapped=[]
    for item in evidence:
        ref=stored_refs.get(str(item.get("source_id")))
        if ref:
            clone=dict(item); clone["evidence_id"]=ref["evidence_id"]; remapped.append(clone)
    lines=[x.strip() for x in artifact.content.splitlines() if x.strip()]
    sentences=[]
    for line in lines:
        if line.startswith(("Patient:","Date of birth:","Gender:","Blood group:")):
            continue
        sentences.extend([x.strip() for x in __import__('re').split(r"(?<=[.!?])\s+",line) if x.strip()])
    if not remapped or not sentences:
        raise HTTPException(422,"Handoff does not contain enough evidence-backed content to finalize.")
    result=AnswerResult(answer=artifact.content,claims=[AnswerClaim(text=x,citations=[str(i["evidence_id"]) for i in remapped]) for x in sentences],citations=[str(i["evidence_id"]) for i in remapped],grounded=True,uncertainty=None)
    validation=validate_grounded_answer(result,remapped,expected_patient_id=str(patient.id))
    if not validation.valid:
        from domain.observability import inc
        inc("healthguard_ai_grounding_rejections_total", surface="handoff", reason=validation.reason or "invalid")
        raise HTTPException(422,"Handoff content contains unsupported or unsafe claims; review the evidence before finalizing.")
    artifact.status=HandoffStatus.FINAL
    artifact.generation_metadata={**(artifact.generation_metadata or {}),"finalized_grounded":True,"finalized_citations":validation.citations}
    audit(db,user,"HANDOFF_FINALIZED","handoff",artifact.id,context={"evidence_ids":validation.citations})
    commit(db); db.refresh(artifact); return artifact

def _require_source_scopes(db, user, patient_id, *, document=False, timeline=False, medications=False, investigations=False):
    requested=[]
    if document: requested.append(AccessScope.DOCUMENTS)
    if timeline: requested.append(AccessScope.TIMELINE)
    if medications: requested.append(AccessScope.MEDICATIONS)
    if investigations: requested.append(AccessScope.INVESTIGATIONS)
    for scope in requested:
        require_scope(db,user,patient_id,scope)


@router.post("/patients/{patient_id}/health-actions", response_model=HealthActionOut, status_code=201)
def create_health_action(patient_id:UUID, payload:HealthActionCreate, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    patient=require_scope(db,user,patient_id,AccessScope.HEALTH_ACTIONS,write=True)
    _require_source_scopes(db,user,patient_id,document=bool(payload.source_document_id or payload.source_fact_id),timeline=bool(payload.source_event_id),investigations=bool(payload.source_investigation_id),medications=bool(payload.source_medication_event_id))
    try:
        source=HealthActionSource(payload.source)
        if source==HealthActionSource.DOCUMENT_DERIVED and not any([payload.source_document_id,payload.source_fact_id,payload.source_event_id,payload.source_investigation_id,payload.source_medication_event_id]):
            raise ValueError("Document-derived actions require a supporting source reference.")
        refs=[payload.source_document_id,payload.source_fact_id,payload.source_event_id,payload.source_investigation_id,payload.source_medication_event_id]
        if source == HealthActionSource.DOCUMENT_DERIVED:
            from domain.health_actions import validate_document_derived_sources
            validate_document_derived_sources(
                db, patient_id=patient_id,
                source_document_id=payload.source_document_id,
                source_fact_id=payload.source_fact_id,
                source_event_id=payload.source_event_id,
                source_investigation_id=payload.source_investigation_id,
                source_medication_event_id=payload.source_medication_event_id,
            )
        elif any(refs):
            if payload.source_document_id and not db.scalar(select(Document.id).where(Document.id==payload.source_document_id,Document.patient_id==patient_id,Document.status!=DocumentStatus.DELETED)):
                raise ValueError("The source document does not belong to this patient.")
            if payload.source_fact_id and not db.scalar(select(HealthFact.id).where(HealthFact.id==payload.source_fact_id,HealthFact.patient_id==patient_id)):
                raise ValueError("The source health fact does not belong to this patient.")
            if payload.source_event_id and not db.scalar(select(HealthEvent.id).where(HealthEvent.id==payload.source_event_id,HealthEvent.patient_id==patient_id)):
                raise ValueError("The source timeline event does not belong to this patient.")
            if payload.source_investigation_id and not db.scalar(select(Investigation.id).where(Investigation.id==payload.source_investigation_id,Investigation.patient_id==patient_id)):
                raise ValueError("The source investigation does not belong to this patient.")
            if payload.source_medication_event_id and not db.scalar(select(MedicationEvent.id).join(Medication,Medication.id==MedicationEvent.medication_id).where(MedicationEvent.id==payload.source_medication_event_id,Medication.patient_id==patient_id)):
                raise ValueError("The source medication event does not belong to this patient.")
        prov=Provenance.DOCUMENT_VERIFIED if source==HealthActionSource.DOCUMENT_DERIVED else provenance_for_actor(db,user,patient_id)
        precision=payload.temporal_precision
        if source==HealthActionSource.USER_CREATED and payload.due_date is not None and precision==DatePrecision.UNKNOWN:
            precision=DatePrecision.EXACT
        from domain.health_actions import create_health_action
        action=create_health_action(db,patient_id=patient_id,title=payload.title,description=payload.description,action_type=payload.action_type,source=source,provenance=prov,due_date=payload.due_date,temporal_precision=precision,source_document_id=payload.source_document_id,source_fact_id=payload.source_fact_id,source_event_id=payload.source_event_id,source_investigation_id=payload.source_investigation_id,source_medication_event_id=payload.source_medication_event_id)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    audit(db,user,"HEALTH_ACTION_CREATED","health_action",action.id,context={"patient_id":str(patient_id),"source":action.source.value})
    commit(db); db.refresh(action); return action

@router.post("/patients/{patient_id}/health-actions/suggest-from-evidence")
def suggest_health_action_from_evidence(patient_id:UUID, source_type:str=Query(...), source_id:UUID=Query(...), db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.HEALTH_ACTIONS)
    require_scope(db,user,patient_id,AccessScope.DOCUMENTS)
    from domain.followup_extraction import suggest_from_evidence
    try: suggestion=suggest_from_evidence(db,patient_id,source_type=source_type,source_id=source_id)
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    if suggestion is None:
        return {"suggested":False,"reason":"No sufficiently explicit follow-up action was found in the selected evidence."}
    audit(db,user,"HEALTH_ACTION_SUGGESTED","patient",patient_id,context={"source_type":source_type,"source_id":str(source_id)})
    commit(db)
    return {"suggested":True,"action":suggestion,"requires_user_confirmation":True}

@router.get("/patients/{patient_id}/health-actions", response_model=list[HealthActionOut])
def list_health_actions(patient_id:UUID, status_filter:str|None=Query(default=None,alias="status"), action_type:str|None=Query(default=None), due_date:date|None=Query(default=None), db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    require_scope(db,user,patient_id,AccessScope.HEALTH_ACTIONS)
    stmt=select(HealthAction).where(HealthAction.patient_id==patient_id)
    if status_filter: stmt=stmt.where(HealthAction.status==status_filter)
    if action_type: stmt=stmt.where(HealthAction.action_type==action_type)
    if due_date: stmt=stmt.where(HealthAction.due_date==due_date)
    return list(db.scalars(stmt.order_by(HealthAction.due_date.asc().nullslast(),HealthAction.created_at.desc())).all())

@router.get("/health-actions/{action_id}", response_model=HealthActionOut)
def get_health_action(action_id:UUID, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    action,patient=resource_patient(db,user,HealthAction,action_id,scope=AccessScope.HEALTH_ACTIONS)
    return action

@router.patch("/health-actions/{action_id}", response_model=HealthActionOut)
def update_health_action(action_id:UUID, payload:HealthActionUpdate, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    action,patient=resource_patient(db,user,HealthAction,action_id,scope=AccessScope.HEALTH_ACTIONS,write=True)
    if action.status in {HealthActionStatus.COMPLETED,HealthActionStatus.CANCELLED}:
        raise HTTPException(409,"Completed or cancelled actions cannot be edited.")
    changes=payload.model_dump(exclude_unset=True)
    if "status" in changes:
        try: changes["status"]=HealthActionStatus(changes["status"])
        except ValueError as exc: raise HTTPException(422,"Invalid action status.") from exc
    if "temporal_precision" in changes and changes["temporal_precision"] is not None:
        from domain.health_actions import validate_due_date
        try: validate_due_date(due_date=changes.get("due_date",action.due_date),temporal_precision=changes["temporal_precision"],source=action.source)
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    for key,value in changes.items(): setattr(action,key,value)
    audit(db,user,"HEALTH_ACTION_UPDATED","health_action",action.id,context={"fields":list(changes)})
    commit(db); db.refresh(action); return action

@router.post("/health-actions/{action_id}/complete", response_model=HealthActionOut)
def complete_health_action(action_id:UUID, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    action,patient=resource_patient(db,user,HealthAction,action_id,scope=AccessScope.HEALTH_ACTIONS,write=True)
    if action.status==HealthActionStatus.CANCELLED: raise HTTPException(409,"Cancelled actions cannot be completed.")
    action.status=HealthActionStatus.COMPLETED; action.completed_at=datetime.now(timezone.utc)
    audit(db,user,"HEALTH_ACTION_COMPLETED","health_action",action.id); commit(db); db.refresh(action); return action

@router.post("/health-actions/{action_id}/cancel", response_model=HealthActionOut)
def cancel_health_action(action_id:UUID, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    action,patient=resource_patient(db,user,HealthAction,action_id,scope=AccessScope.HEALTH_ACTIONS,write=True)
    if action.status==HealthActionStatus.COMPLETED: raise HTTPException(409,"Completed actions cannot be cancelled.")
    action.status=HealthActionStatus.CANCELLED
    audit(db,user,"HEALTH_ACTION_CANCELLED","health_action",action.id); commit(db); db.refresh(action); return action

@router.post("/health-actions/{action_id}/confirm", response_model=HealthActionOut)
def confirm_health_action(action_id:UUID, db:Session=Depends(get_db), user:User=Depends(get_current_user)):
    action,patient=resource_patient(db,user,HealthAction,action_id,scope=AccessScope.HEALTH_ACTIONS,write=True)
    if action.source!=HealthActionSource.DOCUMENT_DERIVED: raise HTTPException(409,"Only document-derived actions require confirmation.")
    if action.status==HealthActionStatus.CANCELLED: raise HTTPException(409,"Cancelled actions cannot be confirmed.")
    if action.confirmed_at is None: action.confirmed_at=datetime.now(timezone.utc)
    action.status=HealthActionStatus.SCHEDULED if action.due_date else HealthActionStatus.PLANNED
    audit(db,user,"HEALTH_ACTION_CONFIRMED","health_action",action.id); commit(db); db.refresh(action); return action



@router.get("/patients/{patient_id}/access")
def list_access_relationships(patient_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    patient=require_scope(db,user,patient_id,AccessScope.PROFILE)
    if patient.owner_user_id!=user.id: raise HTTPException(403,"Only the patient owner can manage access relationships.")
    rows=list(db.scalars(select(PatientAccess).where(PatientAccess.patient_id==patient_id).order_by(PatientAccess.created_at.asc())).all())
    now=datetime.now(timezone.utc)
    out=[]
    for g in rows:
        if g.status==AccessGrantStatus.ACTIVE and g.expires_at and g.expires_at<=now and g.revoked_at is None: g.status=AccessGrantStatus.EXPIRED
        out.append({"id":g.id,"patient_id":g.patient_id,"user_id":g.user_id,"role":g.role.value,"status":g.status.value,"scopes":list(g.scopes or []),"write_scopes":list(g.write_scopes or []),"expires_at":g.expires_at,"revoked_at":g.revoked_at,"created_at":g.created_at,"updated_at":g.updated_at,"email":g.user.email})
    db.commit(); return out

@router.post("/patients/{patient_id}/access",status_code=201)
def create_access_relationship(patient_id:UUID,payload:RelationshipCreate,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    patient=require_scope(db,user,patient_id,AccessScope.PROFILE,write=True)
    target=db.scalar(select(User).where(User.email==str(payload.user_email).lower()))
    if not target: raise HTTPException(404,"User account not found.")
    from domain.sharing import grant_relationship
    try: role=AccessRole(payload.role.upper()); grant=grant_relationship(db,patient=patient,actor=user,target=target,role=role,scopes=payload.scopes,write_scopes=payload.write_scopes,expires_at=payload.expires_at)
    except HTTPException: db.rollback(); raise
    audit(db,user,"ACCESS_GRANTED","patient_access",grant.id,context={"patient_id":str(patient_id),"target_user_id":str(target.id),"role":grant.role.value,"scopes":grant.scopes}); commit(db); db.refresh(grant)
    return {"id":grant.id,"patient_id":grant.patient_id,"user_id":grant.user_id,"role":grant.role.value,"status":grant.status.value,"scopes":grant.scopes,"write_scopes":grant.write_scopes,"expires_at":grant.expires_at,"revoked_at":grant.revoked_at,"created_at":grant.created_at,"updated_at":grant.updated_at}

@router.delete("/patients/{patient_id}/access/{user_id}",status_code=204)
def revoke_access_relationship(patient_id:UUID,user_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    from domain.sharing import revoke_relationship
    grant=revoke_relationship(db,patient_id=patient_id,target_user_id=user_id,actor=user)
    audit(db,user,"ACCESS_REVOKED","patient_access",grant.id,context={"patient_id":str(patient_id),"target_user_id":str(user_id)}); commit(db)

@router.get("/patients/{patient_id}/shares")
def list_temporary_shares(patient_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    patient=require_scope(db,user,patient_id,AccessScope.PROFILE)
    if patient.owner_user_id!=user.id: raise HTTPException(403,"Only the patient owner can manage temporary shares.")
    rows=list(db.scalars(select(TemporaryShare).where(TemporaryShare.patient_id==patient_id).order_by(TemporaryShare.created_at.desc())).all())
    now=datetime.now(timezone.utc)
    out=[]
    for sh in rows:
        status_value="REVOKED" if sh.revoked_at else ("EXPIRED" if sh.expires_at<=now else "ACTIVE")
        out.append({"id":sh.id,"patient_id":sh.patient_id,"role":sh.role.value,"scopes":sh.scopes,"resources":sh.resources,"expires_at":sh.expires_at,"revoked_at":sh.revoked_at,"status":status_value,"created_at":sh.created_at})
    return out

@router.post("/patients/{patient_id}/shares",status_code=201)
def create_temporary_share(patient_id:UUID,payload:TemporaryShareCreate,db:Session=Depends(get_db),user:User=Depends(get_current_user),_rl=Depends(rate_limit("share_create",10))):
    patient=require_scope(db,user,patient_id,AccessScope.PROFILE,write=True)
    from domain.sharing import create_share
    try: share,raw=create_share(db,patient=patient,actor=user,scopes=payload.scopes,resources=payload.resources,expires_at=payload.expires_at)
    except HTTPException: db.rollback(); raise
    audit(db,user,"SHARE_CREATED","temporary_share",share.id,context={"patient_id":str(patient_id),"scopes":share.scopes,"resources":share.resources}); commit(db)
    return {"id":share.id,"patient_id":share.patient_id,"role":share.role.value,"scopes":share.scopes,"resources":share.resources,"expires_at":share.expires_at,"revoked_at":None,"created_at":share.created_at,"token":raw}

@router.post("/shares/{share_id}/revoke",status_code=204)
def revoke_temporary_share(share_id:UUID,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    share=db.get(TemporaryShare,share_id)
    if not share: raise HTTPException(404,"Share not found.")
    patient=require_scope(db,user,share.patient_id,AccessScope.PROFILE,write=True)
    if patient.owner_user_id!=user.id: raise HTTPException(403,"Only the patient owner can revoke a temporary share.")
    if share.revoked_at is None: share.revoked_at=datetime.now(timezone.utc)
    audit(db,user,"SHARE_REVOKED","temporary_share",share.id,context={"patient_id":str(share.patient_id)}); commit(db)

@router.post("/shares/access")
def access_temporary_share(payload:ShareAccessRequest,db:Session=Depends(get_db),_rl=Depends(rate_limit("share_access",30))):
    from domain.sharing import resolve_share
    share=resolve_share(db,payload.token)
    patient=db.get(PatientProfile,share.patient_id)
    if not patient: raise HTTPException(404,"Share not found or expired.")
    result={"share_id":str(share.id),"role":share.role.value,"scopes":share.scopes,"expires_at":share.expires_at,"resources":{}}
    if "PROFILE" in set(share.scopes or []):
        result["patient"]={"id":str(patient.id),"first_name":patient.first_name,"last_name":patient.last_name,"date_of_birth":patient.date_of_birth,"gender":patient.gender,"blood_group":patient.blood_group}
    for scope,ids in (share.resources or {}).items():
        items=[]
        for rid in ids:
            try: obj=__import__('domain.sharing',fromlist=['validate_resource']).validate_resource(db,patient.id,scope,UUID(rid))
            except Exception: continue
            if scope=="HANDOFFS": items.append({"id":str(obj.id),"title":obj.title,"status":obj.status.value,"content":obj.content,"evidence_refs":obj.evidence_refs})
            elif scope=="DOCUMENTS": items.append({"id":str(obj.id),"filename":obj.filename,"document_type":obj.document_type.value,"status":obj.status.value})
            elif scope=="MEDICATIONS": items.append({"id":str(obj.id),"medicine_name":obj.medicine_name,"dose":obj.dose,"frequency":obj.frequency,"status":obj.status.value,"provenance":obj.provenance.value})
            elif scope=="INVESTIGATIONS": items.append({"id":str(obj.id),"name":obj.name,"ordered_date":obj.ordered_date,"performed_date":obj.performed_date,"status":obj.status.value,"provenance":obj.provenance.value})
            elif scope=="TIMELINE": items.append({"id":str(obj.id),"event_type":obj.event_type.value,"event_date":obj.event_date,"title":obj.title,"provenance":obj.provenance.value})
            elif scope=="HEALTH_ACTIONS": items.append({"id":str(obj.id),"title":obj.title,"status":obj.status.value,"due_date":obj.due_date,"provenance":obj.provenance.value})
        result["resources"][scope]=items
    audit(db,None,"SHARE_ACCESSED","temporary_share",share.id,context={"patient_id":str(share.patient_id),"scopes":share.scopes,"resource_counts":{k:len(v) for k,v in result["resources"].items()}}); commit(db)
    return result

@router.post("/patients/{patient_id}/medications/{medication_id}/context",response_model=MedicationContextResponse)
def medication_context(patient_id:UUID,medication_id:UUID,payload:MedicationContextRequest,db:Session=Depends(get_db),user:User=Depends(get_current_user),_rl=Depends(rate_limit("medication_context",20))):
    require_scope(db,user,patient_id,AccessScope.MEDICATIONS)
    from domain.medication_context import medication_context
    ctx=medication_context(db,patient_id,medication_id,payload.question,allowed_scopes=_authorized_retrieval_scopes(db,user,patient_id))
    if ctx is None: raise HTTPException(404,"Medication not found.")
    med,evidence=ctx
    from domain.ai_safety import classify_medical_safety
    decision=classify_medical_safety(payload.question)
    flags=list(dict.fromkeys(decision.flags))
    if flags:
        inc("healthguard_ai_safety_rejections_total", surface="medication_context")
        answer="This question asks for a personalized medical decision. HealthGuard can provide educational context, but a qualified healthcare professional should make that decision." if 'MEDICAL_DECISION_REQUEST' in flags else "If this may be an urgent medical situation, seek appropriate emergency or urgent medical care rather than relying on HealthGuard."
        return {"answer":answer,"grounded":False,"patient_facts":[],"educational":{},"safety_flags":flags}
    projected=[]
    for i,c in enumerate(evidence,1):
        item=c.evidence.as_dict(); item["evidence_id"]=f"evidence_{i}"; projected.append(item)
    from ai.medication_context_provider import MEDICATION_CONTEXT_INSTRUCTIONS,get_medication_context_provider
    from domain.grounding import validate_medication_context_output,SAFE_INSUFFICIENT_ANSWER
    med_payload={"medicine_name":med.medicine_name,"dose":med.dose,"frequency":med.frequency,"route":med.route,"status":med.status.value,"provenance":med.provenance.value}
    try: result=get_medication_context_provider().answer(question=payload.question,medication=med_payload,evidence=projected,system_instructions=MEDICATION_CONTEXT_INSTRUCTIONS)
    except Exception as exc:
        audit(db,user,"MEDICATION_CONTEXT","medication",med.id,context={"patient_id":str(patient_id),"provider_error":type(exc).__name__}); db.commit(); raise HTTPException(503,"Medication context is temporarily unavailable.") from exc
    validation=validate_medication_context_output(result,projected,expected_patient_id=str(patient_id))
    if not validation.valid:
        inc("healthguard_ai_grounding_rejections_total", surface="ask", reason=validation.reason or "invalid")
        return {"answer":SAFE_INSUFFICIENT_ANSWER,"grounded":False,"patient_facts":[],"educational":{},"safety_flags":["AI_SAFETY_VALIDATION_FAILED"]}
    audit(db,user,"MEDICATION_CONTEXT","medication",med.id,context={"patient_id":str(patient_id),"evidence_ids":[x['evidence_id'] for x in projected],"grounded":validation.grounded,"safety_flags":result.safety_flags}); db.commit()
    return {"answer":validation.answer,"grounded":validation.grounded,"patient_facts":[{"text":c.text,"citations":c.citations} for c in result.patient_claims],"educational":result.educational,"safety_flags":result.safety_flags}

@router.post("/account/deactivate",status_code=204)
def deactivate_account(db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    user.status=AccountStatus.DEACTIVATED; audit(db,user,"ACCOUNT_DEACTIVATED","user",user.id); commit(db)

@router.delete("/account",status_code=204)
def delete_account(db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    # Mark the account first so a partial external-storage failure is durable.
    user.deletion_pending=True
    db.commit()
    try:
        patients=list(db.scalars(select(PatientProfile).where(PatientProfile.owner_user_id==user.id)).all())
        for p in patients:
            p.deletion_pending=True
            for d in list(p.documents):
                if d.status==DocumentStatus.DELETED: continue
                try:
                    storage.delete(d.storage_key)
                except Exception as exc:
                    raise StorageError("Account storage deletion is pending.") from exc
                d.status=DocumentStatus.DELETED
        db.commit()
    except StorageError:
        db.rollback()
        user=db.get(User,user.id)
        if user:
            user.deletion_pending=True
            db.commit()
        raise HTTPException(503,"Account deletion is pending because storage could not be updated. Please retry.")
    user=db.get(User,user.id)
    if not user: return
    audit(db,user,"ACCOUNT_DELETED","user",user.id)
    db.delete(user)
    commit(db)

def _authorized_retrieval_scopes(db, user, patient_id):
    patient=db.get(PatientProfile,patient_id)
    if not patient: raise HTTPException(404,"Patient not found.")
    if patient.owner_user_id==user.id:
        return {s.value for s in AccessScope}
    grant=active_grant(db,patient_id,user.id)
    if not grant:
        raise HTTPException(403,"You do not have access to this patient.")
    scopes=set(grant.scopes or [])
    if not scopes.intersection({"DOCUMENTS","MEDICATIONS","INVESTIGATIONS","TIMELINE","HEALTH_ACTIONS"}):
        raise HTTPException(403,"No health-record retrieval scope is granted.")
    return scopes


@router.post("/patients/{patient_id}/retrieve")
def retrieve_patient_records(patient_id:UUID,payload:RetrievalRequest,db:Session=Depends(get_db),user:User=Depends(get_current_user),_rl=Depends(rate_limit("record_retrieve",30))):
    # Authorization intentionally precedes every retrieval query.
    authorized_scopes=_authorized_retrieval_scopes(db,user,patient_id)
    if payload.date_from and payload.date_to and payload.date_to < payload.date_from:
        raise HTTPException(422,"date_to cannot precede date_from.")
    from domain.retrieval import retrieve_patient_evidence, retrieve_patient_semantic_evidence, retrieve_patient_hybrid_evidence
    try:
        retriever = (retrieve_patient_hybrid_evidence if payload.retrieval_mode == "hybrid" else retrieve_patient_semantic_evidence if payload.retrieval_mode == "semantic" else retrieve_patient_evidence)
        evidence=retriever(db,patient_id,payload.query,date_from=payload.date_from,date_to=payload.date_to,fact_type=payload.fact_type,limit=payload.limit,allowed_scopes=authorized_scopes)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    return {"evidence":evidence}


def _llm_evidence_projection(evidence:list[dict]):
    """Expose only server-normalized evidence; patient identity never enters the provider payload."""
    projected=[]
    for index,item in enumerate(evidence,1):
        projected.append({
            "evidence_id":f"evidence_{index}",
            "source_type":item.get("source_type"),
            "source_id":str(item.get("source_id")),
            "document_id":str(item["document_id"]) if item.get("document_id") else None,
            "fact_id":str(item["fact_id"]) if item.get("fact_id") else None,
            "page":item.get("page"),
            "source_text":item.get("source_text"),
            "value":item.get("value"),
            "details":item.get("details") or {},
            "date":item.get("date"),
            "date_precision":item.get("date_precision"),
            "provenance":item.get("provenance"),
            "confidence":item.get("confidence"),
        })
    return projected


@router.post("/patients/{patient_id}/ask")
def ask_health_records(patient_id:UUID,payload:AskHealthRecordsRequest,db:Session=Depends(get_db),user:User=Depends(get_current_user),_rl=Depends(rate_limit("record_ask",20))):
    # Security invariant: authorization happens before retrieval, and retrieval happens before provider creation/call.
    authorized_scopes=_authorized_retrieval_scopes(db,user,patient_id)
    from domain.ai_safety import classify_medical_safety, SAFE_MEDICAL_RESPONSE
    safety = classify_medical_safety(payload.question)
    if not safety.safe_for_provider:
        inc("healthguard_ai_safety_rejections_total", surface="ask")
        audit(db,user,"HEALTH_RECORD_ANSWER","patient",patient_id,context={"question":"[REDACTED]","question_sha256":hashlib.sha256(payload.question.encode("utf-8")).hexdigest(),"safety_flags":list(safety.flags),"provider_called":False,"grounded":False})
        db.commit()
        return {"answer":SAFE_MEDICAL_RESPONSE,"citations":[],"grounded":False,"evidence":[],"safety_flags":list(safety.flags)}
    if payload.date_from and payload.date_to and payload.date_to < payload.date_from:
        raise HTTPException(422,"date_to cannot precede date_from.")
    require_ai_consent(db, db.get(PatientProfile,patient_id))   # the record owner must have agreed before record text can reach the AI provider
    from domain.retrieval import retrieve_patient_evidence, retrieve_patient_semantic_evidence, retrieve_patient_hybrid_evidence
    from domain.grounding import SAFE_INSUFFICIENT_ANSWER, validate_grounded_answer
    from ai.answer_provider import GROUNDED_SYSTEM_INSTRUCTIONS, get_health_record_answer_provider
    try:
        retriever = (retrieve_patient_hybrid_evidence if payload.retrieval_mode == "hybrid" else retrieve_patient_semantic_evidence if payload.retrieval_mode == "semantic" else retrieve_patient_evidence)
        evidence=retriever(db,patient_id,payload.question,date_from=payload.date_from,date_to=payload.date_to,fact_type=payload.fact_type,limit=20,allowed_scopes=authorized_scopes)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    projected=_llm_evidence_projection(evidence)
    if not projected:
        inc("healthguard_ai_insufficient_evidence_total", surface="ask")
        audit(db,user,"HEALTH_RECORD_ANSWER","patient",patient_id,context={"question":"[REDACTED]","question_sha256":hashlib.sha256(payload.question.encode("utf-8")).hexdigest(),"retrieval_mode":payload.retrieval_mode,"evidence_ids":[],"validation_valid":False,"validation_reason":"empty evidence","grounded":False})
        db.commit()
        return {"answer":SAFE_INSUFFICIENT_ANSWER,"citations":[],"grounded":False,"evidence":[]}
    try:
        provider=get_health_record_answer_provider()
        result=provider.answer(question=payload.question,evidence=projected,system_instructions=GROUNDED_SYSTEM_INSTRUCTIONS)
    except Exception as exc:
        audit(db,user,"HEALTH_RECORD_ANSWER","patient",patient_id,context={"question":"[REDACTED]","question_sha256":hashlib.sha256(payload.question.encode("utf-8")).hexdigest(),"retrieval_mode":payload.retrieval_mode,"evidence_ids":[x.get("evidence_id") for x in projected],"provider_error":type(exc).__name__,"provider_response_sha256":None,"validation_valid":False,"grounded":False})
        db.commit()
        raise HTTPException(503,"Health-record answer generation is temporarily unavailable.") from exc
    validation=validate_grounded_answer(result,projected,expected_patient_id=str(patient_id),evidence_patient_ids={f"evidence_{i}": str(item.get("patient_id")) for i,item in enumerate(evidence,1)})
    audit(db,user,"HEALTH_RECORD_ANSWER","patient",patient_id,context={
        "question": "[REDACTED]",
        "question_sha256": hashlib.sha256(payload.question.encode("utf-8")).hexdigest(),
        "retrieval_mode": payload.retrieval_mode,
        "evidence_ids": [x.get("evidence_id") for x in projected],
        "provider_claim_count": len(result.claims),
        "provider_citations": result.citations,
        "provider_response_sha256": hashlib.sha256(json.dumps({"answer": result.answer, "claims": [{"text": c.text, "citations": c.citations} for c in result.claims], "citations": result.citations, "grounded": result.grounded, "uncertainty": result.uncertainty}, sort_keys=True, default=str).encode()).hexdigest(),
        "validation_valid": validation.valid,
        "validation_reason": validation.reason,
        "grounded": validation.grounded,
    })
    db.commit()
    from domain.grounding import _contradiction_groups
    conflicts=[sorted(g) for g in _contradiction_groups(projected)]
    if not validation.valid:
        return {"answer":SAFE_INSUFFICIENT_ANSWER,"citations":[],"grounded":False,"evidence":projected,"uncertainty":validation.uncertainty,"conflicts":conflicts}
    return {"answer":validation.answer,"citations":validation.citations,"grounded":validation.grounded,"evidence":projected,"uncertainty":validation.uncertainty,"conflicts":conflicts}
