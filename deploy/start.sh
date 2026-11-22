#!/bin/sh
# API and worker share one SQLite file. The worker restarts if it exits
# (the checkout_crash demo exits on purpose to show recovery).
set -eu
cd /app/backend
(
  while true; do
    python -m app.workflow.worker || echo "worker exited ($?); restarting in 2s"
    sleep 2
  done
) &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}" --proxy-headers --forwarded-allow-ips='*'
