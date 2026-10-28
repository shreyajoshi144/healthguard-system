#!/bin/sh
set -eu

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${REDIS_URL:?REDIS_URL is required}"
: "${HEALTHGUARD_JWT_SECRET:?HEALTHGUARD_JWT_SECRET is required}"
: "${HEALTHGUARD_METRICS_TOKEN:?HEALTHGUARD_METRICS_TOKEN is required}"

python scripts/check-schema.py
exec python -m jobs.worker
