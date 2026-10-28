import os
import sys
import redis

url = os.getenv("REDIS_URL")
worker_id = os.getenv("HEALTHGUARD_WORKER_ID")
if not url or not worker_id:
    raise SystemExit(1)
try:
    client = redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=float(os.getenv("HEALTHGUARD_REDIS_CONNECT_TIMEOUT_SECONDS", "3")),
        socket_timeout=float(os.getenv("HEALTHGUARD_REDIS_SOCKET_TIMEOUT_SECONDS", "5")),
    )
    value = client.get(f"healthguard:worker:heartbeat:{worker_id}")
    client.close()
    raise SystemExit(0 if value else 1)
except Exception:
    raise SystemExit(1)
