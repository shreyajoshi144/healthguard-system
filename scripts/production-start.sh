#!/bin/sh
set -eu

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${REDIS_URL:?REDIS_URL is required}"
: "${HEALTHGUARD_JWT_SECRET:?HEALTHGUARD_JWT_SECRET is required}"
: "${HEALTHGUARD_PUBLIC_BASE_URL:?HEALTHGUARD_PUBLIC_BASE_URL is required}"
: "${HEALTHGUARD_METRICS_TOKEN:?HEALTHGUARD_METRICS_TOKEN is required}"

python scripts/check-schema.py
# Per-client rate limiting and security events key on the peer address. Behind a load balancer that address is the
# balancer's, so every anonymous user would share one login bucket. Set HEALTHGUARD_TRUSTED_PROXY_IPS to the proxy
# address(es) whose X-Forwarded-For may be trusted (comma separated). Do NOT use "*": clients could then spoof their IP.
if [ -z "${HEALTHGUARD_TRUSTED_PROXY_IPS:-}" ]; then
  echo "WARNING: HEALTHGUARD_TRUSTED_PROXY_IPS is not set; forwarded client IPs are ignored. Behind a load balancer all" \
       "anonymous clients will share one rate-limit bucket." >&2
fi
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips "${HEALTHGUARD_TRUSTED_PROXY_IPS:-127.0.0.1}"
