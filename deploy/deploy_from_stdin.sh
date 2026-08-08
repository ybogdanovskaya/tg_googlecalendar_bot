#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/calendar-bot"
DATA_DIR="/var/lib/calendar-bot"
BACKUP_DIR="${DATA_DIR}/backups"
LOG_DIR="/var/log/calendar-bot"
SERVICE="calendar-bot.service"
HEALTH_SERVICE="calendar-bot-health.service"
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

cleanup() {
    rm -rf -- "${STAGE_DIR}"
}

restore_previous_release() {
    local original_status=$?
    trap - ERR
    set +e
    echo "deploy_rollback_started status=${original_status}"
    systemctl stop "${SERVICE}"
    rm -rf -- "${APP_DIR}/app" "${APP_DIR}/scripts"
    tar -xzf "${CODE_BACKUP}" -C "${APP_DIR}"
    chown -R calendarbot:calendarbot "${APP_DIR}/app" "${APP_DIR}/scripts"
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
    echo "deploy_rollback_completed"
    cleanup
    exit "${original_status}"
}

trap cleanup EXIT

dd if=/dev/stdin of="${ARCHIVE}" bs=1M count=21 status=none
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

for required in app scripts tests requirements.txt README.md REVISION; do
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

BACKUP_ITEMS=(app scripts requirements.txt README.md)
if [[ -f "${APP_DIR}/REVISION" ]]; then
    BACKUP_ITEMS+=(REVISION)
fi
tar -czf "${CODE_BACKUP}" -C "${APP_DIR}" "${BACKUP_ITEMS[@]}"
chown calendarbot:calendarbot "${CODE_BACKUP}"
chmod 0600 "${CODE_BACKUP}"

trap restore_previous_release ERR
systemctl stop "${SERVICE}"
DB_BACKUP="$(runuser -u calendarbot -- "${PYTHON}" "${APP_DIR}/scripts/backup.py" \
    --database "${DATA_DIR}/calendar_bot.sqlite3" \
    --destination "${BACKUP_DIR}" \
    --keep-days 30)"

rm -rf -- "${APP_DIR}/app" "${APP_DIR}/scripts"
cp -a "${STAGE_DIR}/app" "${APP_DIR}/app"
cp -a "${STAGE_DIR}/scripts" "${APP_DIR}/scripts"
install -o calendarbot -g calendarbot -m 0644 \
    "${STAGE_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"
install -o calendarbot -g calendarbot -m 0644 \
    "${STAGE_DIR}/README.md" "${APP_DIR}/README.md"
install -o calendarbot -g calendarbot -m 0644 \
    "${STAGE_DIR}/REVISION" "${APP_DIR}/REVISION"
chown -R calendarbot:calendarbot "${APP_DIR}/app" "${APP_DIR}/scripts"

systemctl start "${SERVICE}"
sleep 5
systemctl is-active --quiet "${SERVICE}"
systemctl start "${HEALTH_SERVICE}"

trap - ERR
echo "deploy_ok revision=${REVISION} database_backup=${DB_BACKUP} code_backup=${CODE_BACKUP}"
