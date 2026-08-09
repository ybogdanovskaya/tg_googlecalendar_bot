#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/calendar-bot"
DATA_DIR="/var/lib/calendar-bot"
BACKUP_DIR="${DATA_DIR}/backups"
LOG_DIR="/var/log/calendar-bot"
SERVICE="calendar-bot.service"
HEALTH_SERVICE="calendar-bot-health.service"
MINIAPP_SERVICE="calendar-miniapp.service"
STATIC_DIR="${APP_DIR}/miniapp-static"
PYTHON="${APP_DIR}/.venv/bin/python"
LOCK_FILE="/run/lock/calendar-bot-deploy.lock"
MAX_ARCHIVE_BYTES=$((20 * 1024 * 1024))

install -d -o calendarbot -g calendarbot -m 0700 "${BACKUP_DIR}" "${LOG_DIR}"
exec > >(tee -a "${LOG_DIR}/deployments.log") 2>&1

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "deploy_failed reason=another_deployment_is_running"
    exit 1
fi

STAGE_DIR="$(mktemp -d /tmp/calendar-bot-deploy.XXXXXX)"
ARCHIVE="${STAGE_DIR}/release.tar.gz"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CODE_BACKUP="${BACKUP_DIR}/calendar-bot-code_${STAMP}.tar.gz"
DB_BACKUP=""
MINIAPP_SERVICE_PRESENT=false
MINIAPP_WAS_ACTIVE=false
STATIC_NEXT=""

service_exists() {
    systemctl cat "$1" >/dev/null 2>&1
}

cleanup() {
    if [[ -n "${STATIC_NEXT}" && -d "${STATIC_NEXT}" ]]; then
        rm -rf -- "${STATIC_NEXT}"
    fi
    rm -rf -- "${STAGE_DIR}"
}

restore_previous_release() {
    local original_status=$?
    trap - ERR
    set +e
    echo "deploy_rollback_started status=${original_status}"
    if [[ "${MINIAPP_SERVICE_PRESENT}" == "true" ]]; then
        systemctl stop "${MINIAPP_SERVICE}"
    fi
    systemctl stop "${SERVICE}"
    rm -rf -- "${APP_DIR}/app" "${APP_DIR}/scripts" "${APP_DIR}/deploy" "${STATIC_DIR}"
    tar -xzf "${CODE_BACKUP}" -C "${APP_DIR}"
    for code_dir in "${APP_DIR}/app" "${APP_DIR}/scripts" "${APP_DIR}/deploy"; do
        if [[ -d "${code_dir}" ]]; then
            chown -R calendarbot:calendarbot "${code_dir}"
        fi
    done
    if [[ -d "${STATIC_DIR}" ]]; then
        chown -R root:root "${STATIC_DIR}"
        chmod -R a+rX "${STATIC_DIR}"
    fi
    if [[ -n "${DB_BACKUP}" && -f "${DB_BACKUP}" ]]; then
        runuser -u calendarbot -- env PYTHONPATH="${APP_DIR}" \
            "${PYTHON}" "${APP_DIR}/scripts/restore_backup.py" \
            --backup "${DB_BACKUP}" \
            --target "${DATA_DIR}/calendar_bot.sqlite3" \
            --confirm
    fi
    systemctl start "${SERVICE}"
    sleep 5
    systemctl is-active --quiet "${SERVICE}"
    if [[ "${MINIAPP_SERVICE_PRESENT}" == "true" && "${MINIAPP_WAS_ACTIVE}" == "true" ]]; then
        systemctl start "${MINIAPP_SERVICE}"
        sleep 3
        curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8001/api/v1/health >/dev/null
    fi
    echo "deploy_rollback_completed"
    cleanup
    exit "${original_status}"
}

trap cleanup EXIT

if ! timeout 60s dd if=/dev/stdin of="${ARCHIVE}" bs=1M count=21 status=none; then
    echo "deploy_failed reason=archive_read_timeout"
    exit 1
fi
ARCHIVE_SIZE="$(stat -c %s "${ARCHIVE}")"
if (( ARCHIVE_SIZE == 0 || ARCHIVE_SIZE > MAX_ARCHIVE_BYTES )); then
    echo "deploy_failed reason=invalid_archive_size bytes=${ARCHIVE_SIZE}"
    exit 1
fi

while IFS= read -r entry; do
    if [[ "${entry}" == /* || "/${entry}/" == *"/../"* ]]; then
        echo "deploy_failed reason=unsafe_archive_entry entry=${entry}"
        exit 1
    fi
done < <(tar -tzf "${ARCHIVE}")

tar -xzf "${ARCHIVE}" -C "${STAGE_DIR}"
if find "${STAGE_DIR}" -type l -print -quit | grep -q .; then
    echo "deploy_failed reason=symlinks_are_not_allowed"
    exit 1
fi

for required in app scripts deploy tests miniapp/dist/index.html requirements.txt README.md REVISION; do
    if [[ ! -e "${STAGE_DIR}/${required}" ]]; then
        echo "deploy_failed reason=missing_release_item item=${required}"
        exit 1
    fi
done

REVISION="$(tr -d '[:space:]' < "${STAGE_DIR}/REVISION")"
if [[ ! "${REVISION}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "deploy_failed reason=invalid_revision"
    exit 1
fi

if ! cmp -s "${STAGE_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"; then
    echo "deploy_failed reason=requirements_changed_manual_dependency_update_required"
    exit 1
fi

env PYTHONPATH="${STAGE_DIR}" "${PYTHON}" -m compileall -q \
    "${STAGE_DIR}/app" "${STAGE_DIR}/scripts" "${STAGE_DIR}/tests"
env PYTHONPATH="${STAGE_DIR}" "${PYTHON}" -m unittest discover \
    -s "${STAGE_DIR}/tests"

if service_exists "${MINIAPP_SERVICE}"; then
    MINIAPP_SERVICE_PRESENT=true
    if systemctl is-active --quiet "${MINIAPP_SERVICE}"; then
        MINIAPP_WAS_ACTIVE=true
    fi
fi

BACKUP_ITEMS=(app scripts requirements.txt README.md)
if [[ -d "${APP_DIR}/deploy" ]]; then
    BACKUP_ITEMS+=(deploy)
fi
if [[ -d "${STATIC_DIR}" ]]; then
    BACKUP_ITEMS+=(miniapp-static)
fi
if [[ -f "${APP_DIR}/REVISION" ]]; then
    BACKUP_ITEMS+=(REVISION)
fi
tar -czf "${CODE_BACKUP}" -C "${APP_DIR}" "${BACKUP_ITEMS[@]}"
chown calendarbot:calendarbot "${CODE_BACKUP}"
chmod 0600 "${CODE_BACKUP}"

trap restore_previous_release ERR
if [[ "${MINIAPP_SERVICE_PRESENT}" == "true" ]]; then
    systemctl stop "${MINIAPP_SERVICE}"
fi
systemctl stop "${SERVICE}"
DB_BACKUP="$(runuser -u calendarbot -- "${PYTHON}" "${APP_DIR}/scripts/backup.py" \
    --database "${DATA_DIR}/calendar_bot.sqlite3" \
    --destination "${BACKUP_DIR}" \
    --keep-days 30)"

rm -rf -- "${APP_DIR}/app" "${APP_DIR}/scripts" "${APP_DIR}/deploy"
cp -a "${STAGE_DIR}/app" "${APP_DIR}/app"
cp -a "${STAGE_DIR}/scripts" "${APP_DIR}/scripts"
cp -a "${STAGE_DIR}/deploy" "${APP_DIR}/deploy"
install -o calendarbot -g calendarbot -m 0644 \
    "${STAGE_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"
install -o calendarbot -g calendarbot -m 0644 \
    "${STAGE_DIR}/README.md" "${APP_DIR}/README.md"
install -o calendarbot -g calendarbot -m 0644 \
    "${STAGE_DIR}/REVISION" "${APP_DIR}/REVISION"
chown -R calendarbot:calendarbot "${APP_DIR}/app" "${APP_DIR}/scripts" "${APP_DIR}/deploy"

STATIC_NEXT="$(mktemp -d "${APP_DIR}/.miniapp-static.${STAMP}.XXXXXX")"
cp -a "${STAGE_DIR}/miniapp/dist/." "${STATIC_NEXT}/"
chown -R root:root "${STATIC_NEXT}"
chmod -R a+rX "${STATIC_NEXT}"
if [[ -d "${STATIC_DIR}" ]]; then
    mv "${STATIC_DIR}" "${BACKUP_DIR}/calendar-miniapp-static_${STAMP}"
fi
mv "${STATIC_NEXT}" "${STATIC_DIR}"
STATIC_NEXT=""

systemctl start "${SERVICE}"
sleep 5
systemctl is-active --quiet "${SERVICE}"
if [[ "${MINIAPP_SERVICE_PRESENT}" == "true" ]]; then
    systemctl start "${MINIAPP_SERVICE}"
    sleep 3
    curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8001/api/v1/health >/dev/null
fi
systemctl start "${HEALTH_SERVICE}"

trap - ERR
echo "deploy_ok revision=${REVISION} database_backup=${DB_BACKUP} code_backup=${CODE_BACKUP}"
