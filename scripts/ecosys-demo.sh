#!/usr/bin/env bash
# One-key live-demo launcher: restart the ecosys-pi client and show its health.
#
# Usage:  sudo ./scripts/ecosys-demo.sh
#
# Restarts ecosys-client.service (picking up the current
# /etc/ecosys/ecosys-client.env), waits briefly for the stream to come up, then
# prints the full unit status and the last 20 journal lines. Exits 0 only when
# the service is running, so it can gate a demo script.

set -euo pipefail

readonly SERVICE="ecosys-client.service"
# Seconds to let the client dial the hub and open the stream before reporting.
readonly SETTLE_SECONDS="${ECOSYS_DEMO_SETTLE_SECONDS:-2}"

die() {
	echo "ecosys-demo: FAIL: $*" >&2
	exit 1
}

if [ "$(id -u)" -ne 0 ]; then
	die "must run as root (systemctl restart ${SERVICE})"
fi

for tool in systemctl journalctl; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		die "missing required tool: ${tool}"
	fi
done

# systemctl restart fails loudly if the unit is not installed; that is the
# correct one-key error (run scripts/install-systemd.sh once first).
systemctl restart "$SERVICE"
echo "ecosys-demo: restarted ${SERVICE}"
sleep "$SETTLE_SECONDS"

# `status` exits non-zero for an inactive unit; report it, then decide below.
systemctl --no-pager --full status "$SERVICE" || true

echo
echo "--- last 20 journal lines (journalctl -u ${SERVICE} -n 20) ---"
journalctl -u "$SERVICE" -n 20 --no-pager || true

echo
if systemctl is-active --quiet "$SERVICE"; then
	echo "ecosys-demo: ${SERVICE} is running"
else
	die "${SERVICE} is not running; check the journal output above"
fi
