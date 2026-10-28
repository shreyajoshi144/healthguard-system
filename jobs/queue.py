from __future__ import annotations
import json, os, time, logging, re, uuid

class QueueUnavailable(RuntimeError):
    pass
try:
    import redis
except ImportError:
    redis=None

QUEUE_KEY="healthguard:document_jobs"
PROCESSING_KEY="healthguard:document_jobs:processing"
QUEUE_DEDUP_PREFIX="healthguard:document_job:queued:"
QUARANTINE_KEY="healthguard:document_jobs:quarantine"
logger=logging.getLogger(__name__)
from domain.observability import inc, observe
VISIBILITY_SECONDS=int(os.getenv("HEALTHGUARD_QUEUE_VISIBILITY_SECONDS","120"))
REDIS_CONNECT_TIMEOUT=float(os.getenv("HEALTHGUARD_REDIS_CONNECT_TIMEOUT_SECONDS","3"))
REDIS_SOCKET_TIMEOUT=float(os.getenv("HEALTHGUARD_REDIS_SOCKET_TIMEOUT_SECONDS","5"))
REDIS_HEALTH_CHECK_SECONDS=int(os.getenv("HEALTHGUARD_REDIS_HEALTH_CHECK_SECONDS","30"))


def _client():
    url=os.getenv("REDIS_URL")
    if not url or redis is None:
        raise QueueUnavailable("Document processing queue is unavailable.")
    try:
        return redis.from_url(
            url, decode_responses=True,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
            socket_timeout=REDIS_SOCKET_TIMEOUT,
            retry_on_timeout=True,
            health_check_interval=REDIS_HEALTH_CHECK_SECONDS,
        )
    except Exception as exc:
        raise QueueUnavailable("Document processing queue is unavailable.") from exc


def _close(client) -> None:
    close=getattr(client,"close",None)
    if close:
        try: close()
        except Exception: pass


def enqueue_document(job_id:str)->str:
    """Atomically enqueue a job once until its current queue/in-flight message is acked.

    SETNX + RPUSH are one Redis Lua operation. If a client times out after Redis
    accepted the script, a retry observes the dedup key rather than creating a
    second queue message. The key is released only by acknowledge_document.
    """
    url=os.getenv("REDIS_URL")
    production=os.getenv("APP_ENV","development").lower()=="production"
    if url and redis:
        client=None
        try:
            client=_client()
            key=f"{QUEUE_DEDUP_PREFIX}{job_id}"
            script="""
local created = redis.call('SET', KEYS[2], ARGV[2], 'NX')
if not created then return 0 end
redis.call('RPUSH', KEYS[1], ARGV[1])
return 1
"""
            raw=json.dumps({"job_id":str(job_id),"enqueued_at":time.time()})
            started=time.monotonic(); result=client.eval(script,2,QUEUE_KEY,key,raw,str(job_id))
            observe("healthguard_queue_enqueue_duration", (time.monotonic()-started)*1000, component="redis")
            outcome="enqueued" if int(result or 0)==1 else "deduplicated"
            inc("healthguard_jobs_enqueue_total", outcome=outcome)
            return "REDIS" if int(result or 0)==1 else "REDIS_ALREADY_QUEUED"
        except QueueUnavailable:
            raise
        except Exception as exc:
            inc("healthguard_dependency_failures_total", dependency="redis", operation="enqueue", error_type=type(exc).__name__)
            raise QueueUnavailable("Document processing queue is unavailable.") from exc
        finally:
            if client is not None: _close(client)
    if production:
        raise QueueUnavailable("REDIS_URL and redis client are required for the production processing queue.")
    return "DATABASE"


def claim_document_message(timeout: int = 5) -> str | None:
    """Atomically claim one queue message, quarantining malformed payloads."""
    client = _client()
    now = str(time.time())
    script = """
local item = redis.call('RPOP', KEYS[1])
if not item then return false end
local ok, payload = pcall(cjson.decode, item)
if not ok or type(payload) ~= 'table' or type(payload['job_id']) ~= 'string' or payload['job_id'] == '' then
    redis.call('LPUSH', KEYS[3], item)
    return '__POISON__'
end
payload['claimed_at'] = tonumber(ARGV[1])
local claimed = cjson.encode(payload)
redis.call('LPUSH', KEYS[2], claimed)
return claimed
    """
    try:
        result = client.eval(script, 3, QUEUE_KEY, PROCESSING_KEY, QUARANTINE_KEY, now)
        if result == '__POISON__':
            inc("healthguard_queue_poison_messages_total", location="queue")
            logger.warning("Quarantined malformed document queue message reason=invalid_payload")
            return None
        if result:
            inc("healthguard_jobs_claim_total", outcome="claimed")
        return result or None
    except QueueUnavailable:
        raise
    except Exception as exc:
        raise QueueUnavailable("Document processing queue is unavailable.") from exc
    finally:
        _close(client)


def acknowledge_document(raw_item:str) -> None:
    client=_client()
    try:
        payload=json.loads(raw_item)
        job_id=str(payload["job_id"])
        script="""
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 1 then redis.call('DEL', KEYS[2]) end
return removed
"""
        client.eval(script,2,PROCESSING_KEY,f"{QUEUE_DEDUP_PREFIX}{job_id}",raw_item)
        inc("healthguard_jobs_ack_total", outcome="acknowledged")
    except QueueUnavailable:
        raise
    except Exception as exc:
        raise QueueUnavailable("Document processing queue is unavailable.") from exc
    finally:
        _close(client)


def _safe_job_id_from_malformed(raw: str) -> str | None:
    """Extract only a strict UUID-shaped job_id from malformed JSON."""
    matches = re.findall(r'"job_id"\s*:\s*"([0-9a-fA-F-]{36})"', raw)
    if len(matches) != 1:
        return None
    try:
        return str(uuid.UUID(matches[0]))
    except (ValueError, AttributeError):
        return None


def recover_stale_queue_items() -> int:
    """Atomically recover stale messages; quarantine malformed ones and reconcile dedup."""
    client=_client(); now=time.time(); recovered=0
    recover_script="""
local raw = ARGV[1]
local ok, payload = pcall(cjson.decode, raw)
if not ok or type(payload) ~= 'table' or type(payload['job_id']) ~= 'string' or payload['job_id'] == '' then return -1 end
local claimed = tonumber(payload['claimed_at'] or 0)
local now = tonumber(ARGV[2])
local visibility = tonumber(ARGV[3])
if now - claimed < visibility then return 0 end
local removed = redis.call('LREM', KEYS[1], 1, raw)
if removed ~= 1 then return 0 end
payload['recovered_at'] = now
redis.call('RPUSH', KEYS[2], cjson.encode(payload))
return 1
    """
    poison_script="""
local raw = ARGV[1]
local removed = redis.call('LREM', KEYS[1], 1, raw)
if removed ~= 1 then return 0 end
redis.call('LPUSH', KEYS[2], raw)
if ARGV[3] ~= '' then redis.call('DEL', ARGV[3]) end
return 1
    """
    try:
        for raw in client.lrange(PROCESSING_KEY,0,-1):
            try:
                result=int(client.eval(recover_script,2,PROCESSING_KEY,QUEUE_KEY,raw,str(now),str(VISIBILITY_SECONDS)) or 0)
                if result == 1:
                    recovered += 1
                    inc("healthguard_jobs_recovered_total", reason="stale_queue")
                elif result == -1:
                    job_id=_safe_job_id_from_malformed(raw)
                    dedup=f"{QUEUE_DEDUP_PREFIX}{job_id}" if job_id else ''
                    client.eval(poison_script,2,PROCESSING_KEY,QUARANTINE_KEY,raw,dedup)
                    inc("healthguard_queue_poison_messages_total", location="processing")
                    logger.warning("Quarantined malformed processing message reason=invalid_payload job_id_present=%s", bool(job_id))
            except QueueUnavailable:
                raise
            except Exception as exc:
                raise QueueUnavailable("Document processing queue is unavailable.") from exc
        return recovered
    except QueueUnavailable:
        raise
    except Exception as exc:
        raise QueueUnavailable("Document processing queue is unavailable.") from exc
    finally:
        _close(client)

