#!/usr/bin/env bash
set -euo pipefail

REPO="${SOFAR_REPO_DIR:-/home/dm/sofar-control}"
cd "$REPO"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Deploy aborted: tracked files in $REPO have local changes." >&2
  exit 1
fi

git fetch --quiet origin main
git merge --ff-only origin/main

PORT="$(grep '^SOFAR_WEB_PORT=' .env | cut -d= -f2- | tr -d '"' || true)"
PORT="${PORT:-8765}"
USER_NAME="$(grep '^SOFAR_WEB_USERNAME=' .env | cut -d= -f2- | tr -d '"')"
PASSWORD="$(grep '^SOFAR_WEB_PASSWORD=' .env | cut -d= -f2- | tr -d '"')"

STATUS="$(curl -fsS -u "$USER_NAME:$PASSWORD" "http://127.0.0.1:$PORT/api/optimization-plan" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))')" || {
  echo "Deploy aborted: scheduler state is unreadable (is the app running?)." >&2
  exit 1
}

case "$STATUS" in
  starting|charging|transitioning|returning_to_self_use)
    echo "Deploy postponed: scheduler is '$STATUS'." >&2
    exit 75
    ;;
esac

docker compose up -d --build

for _ in $(seq 1 30); do
  if curl -fsS -u "$USER_NAME:$PASSWORD" "http://127.0.0.1:$PORT/api/optimization-plan" >/dev/null 2>&1; then
    echo "Deploy OK: $(git rev-parse --short HEAD)"
    exit 0
  fi
  sleep 2
done
echo "Deploy finished but the app did not answer on port $PORT." >&2
exit 1
