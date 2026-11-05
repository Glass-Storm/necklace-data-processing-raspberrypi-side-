#!/usr/bin/env bash
# Install the ecosys-pi client as a boot-time systemd service.
#
# Usage:  sudo ./scripts/install-systemd.sh
#
# The script is idempotent: re-running it re-renders the unit from
# deploy/systemd/ecosys-client.service with this checkout's paths, but it NEVER
# overwrites an existing /etc/ecosys/ecosys-client.env, so the hub address and
# camera settings survive upgrades. The service only runs the client; it does
# not manage Wi-Fi, usb0, eth0, or the phone hotspot (see
# scripts/join-hotspot.sh for the hotspot join).
#
# It installs:
#   /etc/ecosys/ecosys-client.env         (from the example, once)
#   /etc/systemd/system/ecosys-client.service  (rendered, mode 0644)
# then daemon-reloads, enables the unit, and restarts it.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT

readonly PYTHON="${REPO_ROOT}/.venv/bin/python"
readonly UNIT_TEMPLATE="${REPO_ROOT}/deploy/systemd/ecosys-client.service"
readonly ENV_EXAMPLE="${REPO_ROOT}/deploy/systemd/ecosys-client.env.example"
readonly ENV_DIR="/etc/ecosys"
readonly ENV_FILE="${ENV_DIR}/ecosys-client.env"
readonly UNIT_NAME="ecosys-client.service"
readonly UNIT_DEST="/etc/systemd/system/${UNIT_NAME}"

die() {
	echo "install-systemd: FAIL: $*" >&2
	exit 1
}

# Escape a value for the right-hand side of a sed s||| replacement.
sed_escape() {
	printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

if [ "$(id -u)" -ne 0 ]; then
	die "must run as root (it writes ${ENV_DIR} and /etc/systemd/system)"
fi

for tool in sed install systemctl mktemp; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		die "missing required tool: ${tool}"
	fi
done

if [ ! -f "$UNIT_TEMPLATE" ]; then
	die "unit template not found: ${UNIT_TEMPLATE}"
fi
if [ ! -f "$ENV_EXAMPLE" ]; then
	die "env example not found: ${ENV_EXAMPLE}"
fi
if [ ! -x "$PYTHON" ]; then
	die "venv python not found at ${PYTHON}; create it first: cd ${REPO_ROOT} && uv venv --system-site-packages && uv sync"
fi

# 1. Environment file: create it once, never clobber the operator's edits.
install -d -m 0755 "$ENV_DIR"
if [ -e "$ENV_FILE" ]; then
	echo "install-systemd: keeping existing ${ENV_FILE}"
else
	install -m 0600 "$ENV_EXAMPLE" "$ENV_FILE"
	echo "install-systemd: installed ${ENV_FILE} from the example"
fi

# 2. Render the unit from the template. Paths are substituted with sed because
# the placeholders are not systemd specifiers (%); @REPO@ and @PYTHON@ are this
# checkout's absolute paths.
tmp_unit="$(mktemp /tmp/ecosys-client.service.XXXXXX)"
trap 'rm -f "$tmp_unit"' EXIT
sed -e "s|@REPO@|$(sed_escape "$REPO_ROOT")|g" \
	-e "s|@PYTHON@|$(sed_escape "$PYTHON")|g" \
	"$UNIT_TEMPLATE" >"$tmp_unit"

if grep -q '@[A-Z_][A-Z_]*@' "$tmp_unit"; then
	die "unrendered placeholder left in the generated unit"
fi

# 3. Install the rendered unit (0644, as systemd expects).
install -m 0644 "$tmp_unit" "$UNIT_DEST"
echo "install-systemd: installed ${UNIT_DEST}"

# 4. Reload, enable at boot, and start now. Restart (not start) also picks up a
# changed unit for a service that was already running.
systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl restart "$UNIT_NAME"

echo "install-systemd: enabled and restarted ${UNIT_NAME}"
echo
echo "One-key restart:  systemctl restart ecosys-client"
echo "Follow the logs:  journalctl -u ecosys-client -f"
echo
echo "Edit ${ENV_FILE} to change PI_ECOSYS_HUB or the camera settings."
