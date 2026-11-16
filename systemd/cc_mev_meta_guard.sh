#!/bin/sh
# cc_mev_meta_guard.sh — D3 meta-supervision guard (W2 design §D3).
# Runs as a user systemd oneshot every 15 min. Exits 0 or 1; touches no
# Hermes process; the meta ledger is append-only. Never raises into a host.
set -u

LEDGER_DIR="${HOME}/Desktop/roof/digital_brain/ops_command/ledgers"
LEDGER="${LEDGER_DIR}/watchdog-meta.jsonl"
WATCHDOG_TIMER="cc_mev_watchdog.timer"
STATUS_FILE="${HOME}/Desktop/roof/digital_brain/ops_command/log/seat-state/cmd-code-main/.cc-watcher.status"
STALE_MINS=30

mkdir -p "${LEDGER_DIR}"
now_epoch="$(date +%s)"
append_row() {
    # append_row <action> <prior_state> <detail>
    printf '{"ts":"%s","action":"%s","prior_state":"%s","detail":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%S%z)" "$1" "$2" "$3" >> "${LEDGER}" 2>/dev/null
}

# (a) self-heal: re-enable the watchdog timer if anything stopped it
state="$(systemctl --user is-active "${WATCHDOG_TIMER}" 2>/dev/null || true)"
if [ "${state}" != "active" ]; then
    systemctl --user enable --now "${WATCHDOG_TIMER}" 2>/dev/null
    append_row "re-enabled" "${state:-unknown}" "meta-guard restored ${WATCHDOG_TIMER}"
fi

# (b) staleness observability: active timer but no fresh watcher status
state="$(systemctl --user is-active "${WATCHDOG_TIMER}" 2>/dev/null || true)"
if [ "${state}" = "active" ] && [ -f "${STATUS_FILE}" ]; then
    mtime="$(stat -c %Y "${STATUS_FILE}" 2>/dev/null || echo "${now_epoch}")"
    age_min=$(( (now_epoch - mtime) / 60 ))
    if [ "${age_min}" -gt "${STALE_MINS}" ]; then
        append_row "stale-but-active" "active" "status mtime ${age_min}min old"
    fi
fi

exit 0
