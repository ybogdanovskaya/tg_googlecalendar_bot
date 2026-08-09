#!/usr/bin/env bash
set -Eeuo pipefail

REVISION="${1:?usage: rehearse_miniapp_candidate.sh <40-character-revision>}"
if [[ ! "${REVISION}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "rehearsal_failed reason=invalid_revision"
    exit 1
fi

APP_DIR="/opt/calendar-bot"
DATA_DIR="/var/lib/calendar-bot"
REHEARSAL_ROOT="${DATA_DIR}/rehearsals"
WORK_DIR="${REHEARSAL_ROOT}/miniapp-${REVISION}"
CANDIDATE_DIR="${WORK_DIR}/candidate"
CLONE_PATH="${WORK_DIR}/rehearsal.sqlite3"
API_LOG="${WORK_DIR}/miniapp_api.log"
API_PORT="8011"
PYTHON="${APP_DIR}/.venv/bin/python"
REHEARSAL_PYTHON="${WORK_DIR}/venv/bin/python"
API_PID=""
API_READY=false

cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "${API_PID}" ]] && kill -0 "${API_PID}" 2>/dev/null; then
        kill "${API_PID}" 2>/dev/null || true
        wait "${API_PID}" 2>/dev/null || true
    fi
    rm -rf -- "${WORK_DIR}"
    echo "rehearsal_cleanup=ok"
    exit "${status}"
}
trap cleanup EXIT

if [[ ! -d "${CANDIDATE_DIR}/app" || ! -f "${CANDIDATE_DIR}/scripts/backup.py" ]]; then
    echo "rehearsal_failed reason=candidate_not_staged"
    exit 1
fi
if ss -ltn | grep -q ":${API_PORT} "; then
    echo "rehearsal_failed reason=api_port_in_use"
    exit 1
fi

runuser -u calendarbot -- "${PYTHON}" "${CANDIDATE_DIR}/scripts/backup.py" \
    --database "${DATA_DIR}/calendar_bot.sqlite3" \
    --destination "${WORK_DIR}" \
    --keep-days 1 >/dev/null

SNAPSHOT_PATH="$(find "${WORK_DIR}" -maxdepth 1 -type f -name 'calendar_bot_*.sqlite3' -print -quit)"
if [[ -z "${SNAPSHOT_PATH}" ]]; then
    echo "rehearsal_failed reason=snapshot_not_created"
    exit 1
fi
mv "${SNAPSHOT_PATH}" "${CLONE_PATH}"
chown calendarbot:calendarbot "${CLONE_PATH}"
echo "database_snapshot=ok"

runuser -u calendarbot -- "${PYTHON}" -m venv "${WORK_DIR}/venv"
runuser -u calendarbot -- "${REHEARSAL_PYTHON}" -m pip install \
    --disable-pip-version-check --no-input -r "${CANDIDATE_DIR}/requirements.txt" >/dev/null
echo "candidate_dependencies=ok"

runuser -u calendarbot -- env PYTHONPATH="${CANDIDATE_DIR}" \
    "${REHEARSAL_PYTHON}" -m compileall -q "${CANDIDATE_DIR}/app" "${CANDIDATE_DIR}/scripts" "${CANDIDATE_DIR}/tests"
echo "candidate_compile=ok"

runuser -u calendarbot -- env PYTHONPATH="${CANDIDATE_DIR}" \
    "${REHEARSAL_PYTHON}" -m unittest discover -s "${CANDIDATE_DIR}/tests" >/dev/null
echo "candidate_tests=ok"

runuser -u calendarbot -- bash -c '
    set -Eeuo pipefail
    set -a
    . "$1"
    set +a
    export PYTHONPATH="$2"
    export DATABASE_PATH="$3"
    export LOG_PATH="$4"
    export MINIAPP_API_BIND_HOST=127.0.0.1
    export MINIAPP_API_BIND_PORT=8011
    export MINIAPP_COOKIE_SECURE=true
    exec "$5" -m app.miniapp_api
' bash \
    /etc/calendar-bot/calendar-bot.env \
    "${CANDIDATE_DIR}" \
    "${CLONE_PATH}" \
    "${API_LOG}" \
    "${REHEARSAL_PYTHON}" >"${API_LOG}" 2>&1 &
API_PID="$!"

for attempt in {1..10}; do
    if curl --fail --silent --show-error --max-time 2 "http://127.0.0.1:${API_PORT}/api/v1/health" >/dev/null; then
        echo "api_health=ok"
        API_READY=true
        break
    fi
    sleep 1
done
if [[ "${API_READY}" != "true" ]]; then
    echo "rehearsal_failed reason=api_health_unavailable"
    if [[ -f "${API_LOG}" ]]; then
        tail -n 12 "${API_LOG}" | sed -E 's/([A-Za-z_]*(TOKEN|SECRET|URL)[A-Za-z_]*)=[^[:space:]]+/\1=[REDACTED]/g'
    fi
    exit 1
fi

kill "${API_PID}"
wait "${API_PID}" 2>/dev/null || true
API_PID=""
if ss -ltn | grep -q ":${API_PORT} "; then
    echo "rehearsal_failed reason=api_port_still_in_use"
    exit 1
fi
echo "api_shutdown=ok"

systemctl is-active --quiet calendar-bot.service
systemctl is-active --quiet calendar-miniapp-preview.service
echo "rehearsal=ok"
