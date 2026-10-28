#!/bin/sh
set -eu
: "${DATABASE_URL:?DATABASE_URL is required}"
exec alembic upgrade head
