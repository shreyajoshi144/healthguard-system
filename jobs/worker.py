from __future__ import annotations
import json, logging, os, time, signal, threading, hashlib, hmac
from datetime import datetime, timedelta, timezone
from uuid import UUID
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from sqlalchemy import select, update

from db.models import Document, DocumentStatus, DocumentTextBlock, PatientProfile, ProcessingJob
from db.session import SessionLocal
from jobs.extractors import ExtractionError, extractor
from jobs.queue import QueueUnavailable, _client, QUEUE_KEY, PROCESSING_KEY, VISIBILITY_SECONDS, acknowledge_document, recover_stale_queue_items, enqueue_document
from storage.service import StorageError, storage
from jobs.state import InvalidProcessingTransition, validate_transition
from ai.document_provider import get_production_provider
from domain.consent import owner_consented
from domain.document_analysis import analyze_document

logger=logging.getLogger(__name__)
from domain.observability import inc, observe, set_gauge, prometheus
MAX_ATTEMPTS=int(os.getenv("HEALTHGUARD_JOB_MAX_ATTEMPTS","3"))
LEASE_SECONDS=int(os.getenv("HEALTHGUARD_JOB_LEASE_SECONDS",str(VISIBILITY_SECONDS)))
BACKOFF_BASE_SECONDS=int(os.getenv("HEALTHGUARD_JOB_BACKOFF_SECONDS","5"))
SHUTDOWN_EVENT=threading.Event()
WORKER_ID=os.getenv("HEALTHGUARD_WORKER_ID") or f"worker-{os.getpid()}"
WORKER_METRIC_ID=hashlib.sha256(WORKER_ID.encode("utf-8")).hexdigest()[:12]
HEARTBEAT_INTERVAL_SECONDS=max(1,int(os.getenv("HEALTHGUARD_WORKER_HEARTBEAT_SECONDS","15")))
HEARTBEAT_TTL_SECONDS=max(HEARTBEAT_INTERVAL_SECONDS * 3, 30)
WORKER_HEARTBEAT_KEY_PREFIX="healthguard:worker:heartbeat:"
WORKER_METRICS_PORT=int(os.getenv("PORT", os.getenv("HEALTHGUARD_WORKER_METRICS_PORT", "9101")))
WORKER_METRICS_TOKEN=os.getenv("HEALTHGUARD_METRICS_TOKEN", "")
_METRICS_SERVER=None


class _WorkerMetricsHandler(BaseHTTPRequestHandler):
    def _write(self, status: int, body: bytes, content_type: str = "text/plain; version=0.0.4") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._write(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if self.path == "/metrics":
            supplied = self.headers.get("X-Metrics-Token", "")
            if not WORKER_METRICS_TOKEN or not supplied or not hmac.compare_digest(supplied, WORKER_METRICS_TOKEN):
                self._write(403, b"metrics access denied\n", "text/plain; charset=utf-8")
                return
            self._write(200, prometheus().encode("utf-8"))
            return
        self._write(404, b"not found\n", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        logger.info("worker_metrics_http_request path=%s status=%s", self.path, str(args[1]) if len(args) > 1 else "unknown")


def start_metrics_server() -> None:
    global _METRICS_SERVER
    if _METRICS_SERVER is not None:
        return
    server = ThreadingHTTPServer(("0.0.0.0", WORKER_METRICS_PORT), _WorkerMetricsHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="worker-metrics", daemon=True)
    thread.start()
    _METRICS_SERVER = server
    logger.info("worker_metrics_server_started port=%s", WORKER_METRICS_PORT)


def stop_metrics_server() -> None:
    global _METRICS_SERVER
    if _METRICS_SERVER is not None:
        _METRICS_SERVER.shutdown()
        _METRICS_SERVER.server_close()
        _METRICS_SERVER = None


def worker_heartbeat() -> bool:
    """Publish a bounded Redis heartbeat; Redis failure never terminates the worker."""
    client = None
    try:
        client = _client()
        key = f"{WORKER_HEARTBEAT_KEY_PREFIX}{WORKER_ID}"
        client.set(key, str(int(time.time())), ex=HEARTBEAT_TTL_SECONDS)
        set_gauge("healthguard_worker_heartbeat", 1, worker=WORKER_METRIC_ID)
        set_gauge("healthguard_worker_heartbeat_age_seconds", 0, worker=WORKER_METRIC_ID)
        return True
    except QueueUnavailable as exc:
        set_gauge("healthguard_worker_heartbeat", 0, worker=WORKER_METRIC_ID)
        logger.warning("Worker heartbeat dependency unavailable dependency=redis error_type=%s", type(exc).__name__)
        return False
    except Exception as exc:
        set_gauge("healthguard_worker_heartbeat", 0, worker=WORKER_METRIC_ID)
        logger.warning("Worker heartbeat failed error_type=%s", type(exc).__name__)
        return False
    finally:
        if client is not None:
            try:
                from jobs.queue import _close
                _close(client)
            except Exception:
                pass


def _patient_scoped_document(doc: Document) -> bool:
    prefix=f"patients/{doc.patient_id}/documents/{doc.id}/"
    return bool(doc.storage_key and doc.storage_key.startswith(prefix))


def recover_stuck_jobs() -> int:
    """Recover jobs whose worker lease expired; bounded attempts prevent infinite retries."""
    now=datetime.now(timezone.utc)
    db=SessionLocal(); recovered=0
    try:
        jobs=list(db.scalars(select(ProcessingJob).where(ProcessingJob.status==DocumentStatus.PROCESSING, ProcessingJob.lease_expires_at.is_not(None), ProcessingJob.lease_expires_at <= now)).all())
        for job in jobs:
            doc=db.get(Document,job.document_id)
            if not doc or doc.status==DocumentStatus.DELETED:
                job.status=DocumentStatus.FAILED
                job.last_error="document_unavailable"
                if doc and doc.status==DocumentStatus.PROCESSING: doc.status=DocumentStatus.FAILED
                continue
            if job.attempts >= MAX_ATTEMPTS:
                job.status=DocumentStatus.FAILED; doc.status=DocumentStatus.FAILED; job.last_error="lease_expired_max_attempts"
            else:
                job.status=DocumentStatus.QUEUED; doc.status=DocumentStatus.QUEUED
                job.next_attempt_at=now
                job.lease_expires_at=None
                job.last_error="worker_lease_expired"
                recovered += 1
                inc("healthguard_jobs_recovered_total", reason="lease_expired")
        db.commit()
        return recovered
    except Exception:
        db.rollback(); raise
    finally:
        db.close()


def _claim_job(db, job_id: UUID) -> bool:
    now=datetime.now(timezone.utc)
    lease=now+timedelta(seconds=LEASE_SECONDS)
    result=db.execute(update(ProcessingJob).where(ProcessingJob.id==job_id,ProcessingJob.status==DocumentStatus.QUEUED,((ProcessingJob.next_attempt_at.is_(None))|(ProcessingJob.next_attempt_at<=now))).values(status=DocumentStatus.PROCESSING,attempts=ProcessingJob.attempts+1,lease_expires_at=lease,last_error=None))
    if result.rowcount!=1: return False
    db.execute(update(Document).where(Document.id==select(ProcessingJob.document_id).where(ProcessingJob.id==job_id).scalar_subquery(),Document.status!=DocumentStatus.DELETED).values(status=DocumentStatus.PROCESSING))
    db.commit(); return True


def process_job(job_id: str|UUID) -> str:
    job_uuid=UUID(str(job_id)); db=SessionLocal()
    try:
        started=time.monotonic()
        inc("healthguard_jobs_processing_total", outcome="started")
        job=db.scalar(select(ProcessingJob).where(ProcessingJob.id==job_uuid))
        if job is None: raise ValueError("Processing job not found")
        if job.status==DocumentStatus.COMPLETED: return "ALREADY_READY"
        if job.status==DocumentStatus.FAILED: return "IGNORED_FAILED"
        if job.status!=DocumentStatus.QUEUED: return f"IGNORED_{job.status.value}"
        if not _claim_job(db,job_uuid): return "ALREADY_CLAIMED"
        job=db.scalar(select(ProcessingJob).where(ProcessingJob.id==job_uuid)); doc=db.scalar(select(Document).where(Document.id==job.document_id)) if job else None
        patient=db.scalar(select(PatientProfile).where(PatientProfile.id==doc.patient_id)) if doc else None
        if not job or not doc or not patient or patient.deletion_pending or doc.status==DocumentStatus.DELETED:
            raise ProcessingError("Document is not available for processing.")
        if not _patient_scoped_document(doc): raise ProcessingError("Document storage scope is invalid.")
        if not owner_consented(db,patient):
            # Not a retryable failure: waiting cannot create consent. Fail without calling the provider; granting consent re-queues the job.
            _mark_consent_blocked(job_uuid,doc.id); inc("healthguard_jobs_failed_total", error_type="ConsentRequired"); return "FAILED"
        try:
            payload=storage.get(doc.storage_key)
            blocks=extractor.extract(__import__('io').BytesIO(payload),doc.content_type)
            if not blocks: raise ProcessingError("No extractable text was produced.")
            validate_transition(DocumentStatus.PROCESSING,DocumentStatus.COMPLETED)
            db.execute(__import__('sqlalchemy').delete(DocumentTextBlock).where(DocumentTextBlock.document_id==doc.id))
            for index,block in enumerate(blocks):
                db.add(DocumentTextBlock(document_id=doc.id,page_number=block.page_number,block_index=block.block_index,source_text=block.source_text,extraction_method=block.extraction_method,confidence=block.confidence))
            db.flush(); provider=get_production_provider(); analyze_document(db,doc,provider)
            job.status=DocumentStatus.COMPLETED; job.lease_expires_at=None; job.next_attempt_at=None; job.last_error=None; doc.status=DocumentStatus.COMPLETED
            db.commit(); inc("healthguard_jobs_completed_total", outcome="completed"); observe("healthguard_job_processing_duration", (time.monotonic()-started)*1000, outcome="completed"); return "READY"
        except Exception as exc:
            db.rollback(); _mark_failed(job_uuid,doc.id if doc else None,exc); inc("healthguard_jobs_failed_total", error_type=type(exc).__name__); observe("healthguard_job_processing_duration", (time.monotonic()-started)*1000, outcome="failed"); return "FAILED"
    finally: db.close()

class ProcessingError(RuntimeError): pass


CONSENT_BLOCKED="consent_required"


def _mark_consent_blocked(job_id:UUID,document_id:UUID)->None:
    db=SessionLocal()
    try:
        job=db.scalar(select(ProcessingJob).where(ProcessingJob.id==job_id)); doc=db.get(Document,document_id)
        if job:
            job.status=DocumentStatus.FAILED; job.lease_expires_at=None; job.next_attempt_at=None; job.last_error=CONSENT_BLOCKED
        if doc and doc.status==DocumentStatus.PROCESSING: doc.status=DocumentStatus.FAILED
        db.commit()
        logger.info("Document processing blocked: AI-processing consent not granted job=%s",job_id)
    finally: db.close()


def _mark_failed(job_id:UUID,document_id:UUID|None,exc:Exception)->None:
    db=SessionLocal()
    try:
        job=db.scalar(select(ProcessingJob).where(ProcessingJob.id==job_id)); doc=db.scalar(select(Document).where(Document.id==document_id)) if document_id else None
        if job and job.status==DocumentStatus.PROCESSING:
            if job.attempts < MAX_ATTEMPTS and doc and doc.status!=DocumentStatus.DELETED:
                inc("healthguard_job_retries_total", outcome="scheduled")
                job.status=DocumentStatus.QUEUED; doc.status=DocumentStatus.QUEUED; job.next_attempt_at=datetime.now(timezone.utc)+timedelta(seconds=BACKOFF_BASE_SECONDS*(2**max(0,job.attempts-1))); job.lease_expires_at=None; job.last_error=type(exc).__name__[:120]
            else:
                inc("healthguard_job_retries_total", outcome="exhausted")
                job.status=DocumentStatus.FAILED; job.lease_expires_at=None; job.next_attempt_at=None; job.last_error=type(exc).__name__[:120]
                if doc and doc.status==DocumentStatus.PROCESSING: doc.status=DocumentStatus.FAILED
            db.commit()
        logger.error("Document processing failed job=%s error_type=%s attempt=%s",job_id,type(exc).__name__,getattr(job,"attempts",None))
    finally: db.close()


def _enqueue_due_jobs()->int:
    db=SessionLocal(); count=0; now=datetime.now(timezone.utc)
    try:
        rows=list(db.scalars(select(ProcessingJob).where(ProcessingJob.status==DocumentStatus.QUEUED,((ProcessingJob.next_attempt_at.is_(None))|(ProcessingJob.next_attempt_at<=now)))).all())
        for job in rows:
            result=enqueue_document(str(job.id))
            inc("healthguard_jobs_queue_state_total", state="queued", outcome=result)
            if result in {"REDIS", "REDIS_ALREADY_QUEUED"}:
                count+=1
        return count
    finally: db.close()


def _redis_failure_backoff(attempt:int)->float:
    return min(10.0, max(1.0, float(2 ** min(attempt, 3))))


def request_shutdown(signum=None, frame=None) -> None:
    SHUTDOWN_EVENT.set()
    logger.info("Document worker shutdown requested signal=%s", signum)


def run_once(timeout:int=1)->bool:
    # DB lease recovery is independent of Redis and must continue to run. Redis
    # dependency failures are deliberately contained so the worker process stays alive.
    recover_stuck_jobs()
    try:
        recover_stale_queue_items()
        _enqueue_due_jobs()
        if SHUTDOWN_EVENT.is_set():
            return False
        raw_claimed = __import__('jobs.queue', fromlist=['claim_document_message']).claim_document_message(timeout=timeout)
    except QueueUnavailable as exc:
        inc("healthguard_worker_dependency_failures_total", dependency="redis", error_type=type(exc).__name__)
        logger.warning("Document worker dependency unavailable dependency=redis error_type=%s", type(exc).__name__)
        return False
    if not raw_claimed:
        return False
    try:
        payload=json.loads(raw_claimed)
        result=process_job(payload["job_id"])
        acknowledge_document(raw_claimed)
        return result not in {"FAILED", "ALREADY_CLAIMED"}
    except QueueUnavailable as exc:
        inc("healthguard_worker_dependency_failures_total", dependency="redis", error_type=type(exc).__name__)
        logger.warning("Document worker dependency unavailable dependency=redis error_type=%s", type(exc).__name__)
        # Keep the claimed message in processing. Visibility recovery will make it
        # available again after Redis recovers; no DB job state is changed here.
        return False
    except Exception:
        logger.exception("Worker execution failed")
        return False


def reminder_tick()->None:
    """Dispatch due email reminders. Isolated: a failure here must never stop document processing."""
    if os.getenv("HEALTHGUARD_REMINDERS_ENABLED","true").lower()!="true": return
    try:
        from domain import reminders
        counts=reminders.run_tick(SessionLocal)
        if any(counts[k] for k in ("sent","retry","failed","planned","cancelled","recovered")):
            logger.info("Reminder tick %s", counts)
    except Exception as exc:
        inc("healthguard_worker_dependency_failures_total", dependency="reminders", error_type=type(exc).__name__)
        logger.warning("Reminder tick failed error_type=%s", type(exc).__name__)


def alert_tick()->None:
    """Evaluate operational conditions and deliver alerts. A failure here must never stop document processing."""
    db=SessionLocal()
    try:
        from domain.alert_delivery import run_alert_cycle
        run_alert_cycle(db)
    except Exception as exc:
        inc("healthguard_worker_dependency_failures_total", dependency="alert_cycle", error_type=type(exc).__name__)
        logger.warning("Alert cycle failed error_type=%s", type(exc).__name__)
    finally: db.close()


def preflight()->None:
    """Startup checks. In production a misconfigured worker exits non-zero here instead of failing per reminder/document later."""
    from utils.production_config import is_production, validate_worker_config
    from jobs.extractors import check_ocr_runtime
    validate_worker_config()
    try:
        logger.info("ocr_runtime_ok tesseract=%s", check_ocr_runtime())
    except ExtractionError as exc:
        if is_production():
            raise RuntimeError(f"OCR runtime check failed: {exc}") from exc
        logger.warning("ocr_runtime_unavailable (scanned PDFs/images will fail): %s", exc)


def run_forever()->None:
    preflight()
    previous = {}
    failure_count=0
    last_heartbeat=0.0
    last_reminder=0.0
    last_alert=0.0
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, request_shutdown)
    start_metrics_server()
    try:
        while not SHUTDOWN_EVENT.is_set():
            now=time.monotonic()
            if now-last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                worker_heartbeat()
                last_heartbeat=now
            if now-last_reminder >= float(os.getenv("HEALTHGUARD_REMINDER_TICK_SECONDS","30")):
                reminder_tick()
                last_reminder=now
            if now-last_alert >= float(os.getenv("HEALTHGUARD_ALERT_CYCLE_SECONDS","60")):
                alert_tick()
                last_alert=now
            try:
                progressed=run_once(timeout=min(5, max(1, VISIBILITY_SECONDS // 10)))
                failure_count=0 if progressed else min(failure_count+1, 4)
                if not progressed:
                    time.sleep(0.25 if failure_count == 1 else min(2.0, _redis_failure_backoff(failure_count)/4))
            except QueueUnavailable as exc:
                failure_count=min(failure_count+1, 4)
                inc("healthguard_worker_dependency_failures_total", dependency="redis", error_type=type(exc).__name__)
                logger.warning("Document worker dependency unavailable dependency=redis error_type=%s", type(exc).__name__)
                time.sleep(_redis_failure_backoff(failure_count))
            except Exception:
                logger.exception("Document worker iteration failed")
                time.sleep(1.0)
    finally:
        SHUTDOWN_EVENT.set()
        logger.info("Document worker stopped gracefully")
        stop_metrics_server()
        for sig, handler in previous.items():
            signal.signal(sig, handler)

if __name__=="__main__": run_forever()
