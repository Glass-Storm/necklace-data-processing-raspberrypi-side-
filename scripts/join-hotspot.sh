#!/usr/bin/env bash
# Join the phone hotspot on wlan0 with wpa_supplicant + systemd-networkd DHCP.
#
# Usage:  ./scripts/join-hotspot.sh <ssid> <psk> [wpa2|wpa3]
#
# The security mode defaults to wpa2. Use wpa3 for a WPA3-SAE (H2E) access
# point (for example an Android "WPA3-Personal" hotspot); wpa2 keeps the
# original WPA-PSK network block.
#
# The script is additive and remembers every network it has joined: an
# existing wpa_supplicant-wlan0.conf is never overwritten. The requested SSID
# is updated in place when already present, or appended as a new network block
# otherwise, through wpa_cli + save_config. wpa_supplicant therefore
# auto-connects to whichever remembered network is available at boot and after
# a reconnect.
#
# This script is for a Pi running systemd-networkd + wpa_supplicant and no
# nmcli. It configures ONLY wlan0. The usb0 and eth0 interfaces are the lab
# management/SSH links and are never brought down, reconfigured, or restarted:
# losing usb0 would cut the only remote shell.
#
# It also never restarts systemd-networkd wholesale. New configuration is
# rendered and picked up with `networkctl reload`, which keeps existing links
# (including usb0/eth0) up.
#
# The script's own historical netplan drop-in is ALWAYS removed and
# netplan-wpa-wlan0 is stopped, because otherwise it starts a second
# wpa_supplicant at boot, holds the wlan0 control interface, and makes
# wpa_supplicant@wlan0 fail with "ctrl_iface exists" (status 255). This script
# never writes a netplan drop-in.
#
# The PSK is written only to a root-owned, mode-600 file. The SSID and PSK are
# never echoed, logged, or placed in a repository file.

set -euo pipefail
umask 077

readonly IFACE="wlan0"
readonly WPA_CONF="/etc/wpa_supplicant/wpa_supplicant-${IFACE}.conf"
readonly NETWORK_FILE="/etc/systemd/network/25-${IFACE}.network"
readonly NETPLAN_DROPIN="/etc/netplan/90-ecosys-${IFACE}.yaml"
readonly NETPLAN_UNIT="netplan-wpa-${IFACE}.service"
readonly MANAGEMENT_IFACES=("usb0" "eth0")
# Regulatory country for the station. Override with ECOSYS_COUNTRY if needed.
readonly COUNTRY="${ECOSYS_COUNTRY:-US}"
readonly VERIFY_TIMEOUT_S="${ECOSYS_VERIFY_TIMEOUT_S:-30}"

die() {
	echo "join-hotspot: FAIL: $*" >&2
	exit 1
}

usage() {
	echo "usage: $0 <ssid> <psk> [wpa2|wpa3]" >&2
	exit 2
}

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
	usage
fi

SSID="$1"
PSK="$2"
MODE="${3:-wpa2}"

case "$MODE" in
wpa2 | wpa3) ;;
*) die "unknown security mode '${MODE}' (expected wpa2 or wpa3)" ;;
esac

case "$SSID" in
"") die "SSID must not be empty" ;;
*$'\n'* | *$'\r'* | *$'\t'*) die "SSID must not contain control characters" ;;
esac
case "$PSK" in
"") die "PSK must not be empty" ;;
*$'\n'* | *$'\r'* | *$'\t'*) die "PSK must not contain control characters" ;;
esac

# Refuse to configure a management link even if a future caller changes IFACE.
for management_iface in "${MANAGEMENT_IFACES[@]}"; do
	if [ "$IFACE" = "$management_iface" ]; then
		die "refusing to touch management interface $management_iface"
	fi
done

if [ "$(id -u)" -ne 0 ]; then
	die "must run as root (it writes /etc/wpa_supplicant and /etc/systemd/network)"
fi

# Missing tooling is a named failure, not a half-applied network change.
missing_tools=()
for tool in ip wpa_cli wpa_supplicant mktemp networkctl systemctl awk grep; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		missing_tools+=("$tool")
	fi
done
if [ "${#missing_tools[@]}" -gt 0 ]; then
	die "missing required tool(s): ${missing_tools[*]}"
fi

# Escape a value for a double-quoted wpa_supplicant string.
escape_value() {
	local value="$1"
	value="${value//\\/\\\\}"
	value="${value//\"/\\\"}"
	printf '%s' "$value"
}

# Interfaces (of the management set) that currently carry an IPv4 address.
management_links_with_ipv4() {
	local iface addresses
	for iface in "${MANAGEMENT_IFACES[@]}"; do
		addresses="$(ip -4 addr show dev "$iface" 2>/dev/null || true)"
		case "$addresses" in
		*"inet "*) printf '%s\n' "$iface" ;;
		esac
	done
}

# Record the management links before any change so a loss is detectable.
BASELINE_MANAGEMENT_LINKS="$(management_links_with_ipv4 || true)"

verify_management_links() {
	local iface addresses lost=()
	while IFS= read -r iface; do
		[ -n "$iface" ] || continue
		addresses="$(ip -4 addr show dev "$iface" 2>/dev/null || true)"
		case "$addresses" in
		*"inet "*) ;;
		*) lost+=("$iface") ;;
		esac
	done <<<"$BASELINE_MANAGEMENT_LINKS"
	if [ "${#lost[@]}" -gt 0 ]; then
		die "management link(s) lost their IPv4 address: ${lost[*]}"
	fi
}

# Always assert the management link survived, including on verification failure.
trap 'verify_management_links' EXIT

# Insert a global option before the first network block, preserving every
# existing network block. A no-op when the option is already present.
ensure_global_option() {
	local key="$1" line="$2" tmp
	if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$WPA_CONF"; then
		return 0
	fi
	tmp="$(mktemp /etc/wpa_supplicant/.wpa_supplicant-wlan0.XXXXXX)"
	chmod 600 "$tmp"
	awk -v line="$line" '
		/^[[:space:]]*network[[:space:]]*=/ && !inserted { print line; inserted = 1 }
		{ print }
		END { if (!inserted) print line }
	' "$WPA_CONF" >"$tmp"
	mv "$tmp" "$WPA_CONF"
	chmod 600 "$WPA_CONF"
}

# 1. Remove the script's own stale netplan drop-in and stop the netplan
# supplicant. Left in place, netplan-wpa-wlan0 starts its own wpa_supplicant at
# boot, holds the wlan0 control interface, and wpa_supplicant@wlan0 fails with
# "ctrl_iface exists" (status 255), so the Pi never auto-connects. This script
# never writes a netplan drop-in in either mode.
if [ -e "$NETPLAN_DROPIN" ]; then
	rm -f "$NETPLAN_DROPIN"
fi
if command -v netplan >/dev/null 2>&1; then
	# Drop the rendered /run/netplan/wpa-wlan0.conf that came from the drop-in.
	netplan generate || die "netplan generate failed after removing ${NETPLAN_DROPIN}"
fi
systemctl stop "$NETPLAN_UNIT" >/dev/null 2>&1 || true

# 2. Create the base configuration only when it is absent. An existing file is
# never overwritten: its network blocks are what make the Pi remember every
# network it has joined. mktemp + mv keeps it 0600 even if the destination
# already exists with looser permissions.
mkdir -p /etc/wpa_supplicant
if [ ! -f "$WPA_CONF" ]; then
	wpa_tmp="$(mktemp /etc/wpa_supplicant/.wpa_supplicant-wlan0.XXXXXX)"
	chmod 600 "$wpa_tmp"
	{
		printf 'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n'
		printf 'update_config=1\n'
		printf 'country=%s\n' "$COUNTRY"
		if [ "$MODE" = "wpa3" ]; then
			# Global SAE option: H2E (hash-to-element) is what Android WPA3
			# hotspots negotiate. It must sit outside the network block.
			printf 'sae_pwe=2\n'
		fi
	} >"$wpa_tmp"
	mv "$wpa_tmp" "$WPA_CONF"
fi
chmod 600 "$WPA_CONF"

# A config written by an older run may lack the globals required by
# save_config and SAE. Insert them before the first network block so the
# remembered networks stay untouched.
ensure_global_option ctrl_interface 'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev'
ensure_global_option update_config 'update_config=1'
ensure_global_option country "country=${COUNTRY}"
if [ "$MODE" = "wpa3" ]; then
	ensure_global_option sae_pwe 'sae_pwe=2'
fi

# 3. Ensure a systemd-networkd DHCP unit exists for wlan0. This is independent
# of netplan: the box may have no rendered wlan0 .network at all.
mkdir -p /etc/systemd/network
if ! compgen -G "/etc/systemd/network/*wlan0*.network" >/dev/null; then
	network_tmp="$(mktemp /etc/systemd/network/.25-wlan0.XXXXXX)"
	chmod 644 "$network_tmp"
	{
		printf '[Match]\n'
		printf 'Name=%s\n' "$IFACE"
		printf '\n[Network]\n'
		printf 'DHCP=yes\n'
	} >"$network_tmp"
	mv "$network_tmp" "$NETWORK_FILE"
fi

# 4. Make wpa_supplicant@wlan0 the only wlan0 supplicant. A stale control
# socket from a crashed or netplan-owned supplicant blocks startup, so drop it
# while the unit is inactive. Restart (not just enable --now) so a config
# edited above is re-read by a unit that was already running.
if ! systemctl is-active --quiet "wpa_supplicant@${IFACE}"; then
	rm -f "/run/wpa_supplicant/${IFACE}" "/var/run/wpa_supplicant/${IFACE}"
	systemctl reset-failed "wpa_supplicant@${IFACE}" >/dev/null 2>&1 || true
fi
systemctl enable "wpa_supplicant@${IFACE}" >/dev/null
ip link set "$IFACE" up >/dev/null 2>&1 || true
systemctl restart "wpa_supplicant@${IFACE}"

# Wait for the control interface before issuing wpa_cli commands.
ctrl_deadline=$((SECONDS + 10))
until wpa_cli -i "$IFACE" ping >/dev/null 2>&1; do
	if [ "$SECONDS" -ge "$ctrl_deadline" ]; then
		die "wpa_supplicant@${IFACE} did not open its control interface"
	fi
	sleep 1
done

# Reload the rendered networkd configuration without dropping any link.
networkctl reload

# 5. Add or update the requested network through wpa_cli. list_networks is
# captured, never printed, so the SSID never reaches stdout. Existing network
# blocks stay as they are, which is what makes the Pi remember every network.
wpa_command() {
	local action="$1" result
	shift
	result="$(wpa_cli -i "$IFACE" "$action" "$@" 2>/dev/null | tail -n 1 || true)"
	if [ "$result" != "OK" ]; then
		die "wpa_cli ${action} failed"
	fi
}

networks="$(wpa_cli -i "$IFACE" list_networks 2>/dev/null || true)"
network_id="$(printf '%s\n' "$networks" | SSID="$SSID" awk -F '\t' \
	'$1 ~ /^[0-9]+$/ && $2 == ENVIRON["SSID"] { print $1; exit }')"

if [ -z "$network_id" ]; then
	network_id="$(wpa_cli -i "$IFACE" add_network 2>/dev/null | tail -n 1 || true)"
	case "$network_id" in
	'' | *[!0-9]*) die "could not add a wpa_supplicant network" ;;
	esac
fi

wpa_command set_network "$network_id" ssid "\"$(escape_value "$SSID")\""
if [ "$MODE" = "wpa3" ]; then
	wpa_command set_network "$network_id" key_mgmt SAE
	wpa_command set_network "$network_id" sae_password "\"$(escape_value "$PSK")\""
	wpa_command set_network "$network_id" ieee80211w 2
else
	wpa_command set_network "$network_id" psk "\"$(escape_value "$PSK")\""
	wpa_command set_network "$network_id" key_mgmt WPA-PSK
fi
wpa_command enable_network "$network_id"
wpa_command save_config
# wpa_supplicant rewrites the file with its own umask; re-tighten it here.
chmod 600 "$WPA_CONF"
wpa_command reconfigure

# 6. Verify association and DHCP before claiming success.
status=""
deadline=$((SECONDS + VERIFY_TIMEOUT_S))
while [ "$SECONDS" -lt "$deadline" ]; do
	status="$(wpa_cli -i "$IFACE" status 2>/dev/null || true)"
	if [[ "$status" == *"wpa_state=COMPLETED"* ]]; then
		addresses="$(ip -4 addr show dev "$IFACE" 2>/dev/null || true)"
		case "$addresses" in
		*"inet "*) break ;;
		esac
	fi
	sleep 1
done

wpa_state="$(printf '%s\n' "$status" | grep '^wpa_state=' || true)"
ipv4="$(ip -4 addr show dev "$IFACE" 2>/dev/null | grep -o 'inet [0-9./]*' | head -n 1 || true)"

if [ -z "$wpa_state" ] || ! printf '%s\n' "$wpa_state" | grep -q 'COMPLETED' || [ -z "$ipv4" ]; then
	# Best-effort named reason: a rejected key shows up in the service journal.
	journal_hint=""
	if command -v journalctl >/dev/null 2>&1; then
		journal_hint="$(journalctl -u "wpa_supplicant@${IFACE}" --no-pager -n 40 2>/dev/null |
			grep -iE '4-Way Handshake failed|pre-shared key may be incorrect|WRONG_KEY|SAE' |
			tail -n 1 || true)"
	fi
	if [ -n "$journal_hint" ]; then
		die "wrong PSK or association rejected for the configured SSID: ${journal_hint}"
	fi
	die "wlan0 did not associate with the configured SSID (${wpa_state:-wpa_state=unknown}, ${ipv4:-no IPv4}); check the PSK and that the phone hotspot is on"
fi

if [ -n "$BASELINE_MANAGEMENT_LINKS" ]; then
	management_note="$(printf '%s' "$BASELINE_MANAGEMENT_LINKS" | tr '\n' ' ')"
else
	management_note="none had an IPv4 before the join"
fi

echo "join-hotspot: ${IFACE} associated (${wpa_state}, ${MODE}, network id ${network_id})"
echo "join-hotspot: ${IFACE} ${ipv4}"
echo "join-hotspot: management links intact: ${management_note}"
