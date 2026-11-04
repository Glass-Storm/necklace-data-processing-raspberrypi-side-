#!/usr/bin/env bash
# Join the phone hotspot on wlan0 with wpa_supplicant + systemd-networkd DHCP.
#
# Usage:  ./scripts/join-hotspot.sh <ssid> <psk>
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
# The PSK is written only to a root-owned, mode-600 file. It is never echoed,
# logged, or placed in a repository file.

set -euo pipefail

readonly IFACE="wlan0"
readonly WPA_CONF="/etc/wpa_supplicant/wpa_supplicant-${IFACE}.conf"
readonly NETWORK_FILE="/etc/systemd/network/25-${IFACE}.network"
readonly NETPLAN_DROPIN="/etc/netplan/90-ecosys-${IFACE}.yaml"
readonly MANAGEMENT_IFACES=("usb0" "eth0")
# Regulatory country for the station. Override with ECOSYS_COUNTRY if needed.
readonly COUNTRY="${ECOSYS_COUNTRY:-US}"
readonly VERIFY_TIMEOUT_S="${ECOSYS_VERIFY_TIMEOUT_S:-30}"

die() {
	echo "join-hotspot: FAIL: $*" >&2
	exit 1
}

usage() {
	echo "usage: $0 <ssid> <psk>" >&2
	exit 2
}

if [ "$#" -ne 2 ]; then
	usage
fi

SSID="$1"
PSK="$2"

case "$SSID" in
"") die "SSID must not be empty" ;;
*$'\n'* | *$'\r'*) die "SSID must not contain a newline" ;;
esac
case "$PSK" in
"") die "PSK must not be empty" ;;
*$'\n'* | *$'\r'*) die "PSK must not contain a newline" ;;
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
for tool in ip wpa_cli wpa_supplicant mktemp networkctl; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		missing_tools+=("$tool")
	fi
done
if compgen -G "/etc/netplan/*.yaml" >/dev/null; then
	if ! command -v netplan >/dev/null 2>&1; then
		missing_tools+=("netplan")
	fi
else
	if ! command -v systemctl >/dev/null 2>&1; then
		missing_tools+=("systemctl")
	fi
fi
if [ "${#missing_tools[@]}" -gt 0 ]; then
	die "missing required tool(s): ${missing_tools[*]}"
fi

# Escape a value for a double-quoted wpa_supplicant/netplan string.
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

# 1. Write the wpa_supplicant configuration. mktemp + mv keeps it 0600 even if
# the destination already exists with looser permissions.
mkdir -p /etc/wpa_supplicant
wpa_tmp="$(mktemp /etc/wpa_supplicant/.wpa_supplicant-wlan0.XXXXXX)"
chmod 600 "$wpa_tmp"
{
	printf 'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n'
	printf 'update_config=1\n'
	printf 'country=%s\n' "$COUNTRY"
	printf '\nnetwork={\n'
	printf '\tssid="%s"\n' "$(escape_value "$SSID")"
	printf '\tpsk="%s"\n' "$(escape_value "$PSK")"
	printf '\tkey_mgmt=WPA-PSK\n'
	printf '}\n'
} >"$wpa_tmp"
mv "$wpa_tmp" "$WPA_CONF"
chmod 600 "$WPA_CONF"

# 2. Configure the data path.
if compgen -G "/etc/netplan/*.yaml" >/dev/null; then
	# The host is netplan-managed: render a drop-in instead of fighting it with
	# a hand-written systemd-networkd unit. netplan owns wpa_supplicant here.
	netplan_tmp="$(mktemp /etc/netplan/.90-ecosys-wlan0.XXXXXX)"
	chmod 600 "$netplan_tmp"
	{
		printf 'network:\n'
		printf '  version: 2\n'
		printf '  wifis:\n'
		printf '    %s:\n' "$IFACE"
		printf '      dhcp4: true\n'
		printf '      access-points:\n'
		printf '        "%s":\n' "$(escape_value "$SSID")"
		printf '          password: "%s"\n' "$(escape_value "$PSK")"
	} >"$netplan_tmp"
	mv "$netplan_tmp" "$NETPLAN_DROPIN"
	chmod 600 "$NETPLAN_DROPIN"
	# `networkctl reload` alone does NOT render netplan YAML, so generate first.
	netplan generate
	networkctl reload
else
	# Plain systemd-networkd + wpa_supplicant. Enable the per-interface unit
	# (enable --now does not restart it if it is already active) and add the
	# DHCP network file if none exists.
	mkdir -p /etc/systemd/network
	if [ ! -e "$NETWORK_FILE" ]; then
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
	systemctl enable --now "wpa_supplicant@${IFACE}"
	# Reload the rendered networkd configuration without dropping any link.
	networkctl reload
fi

# If the service was already running, make it re-read the new configuration.
wpa_cli -i "$IFACE" reconfigure >/dev/null 2>&1 || true

# 3. Verify association and DHCP before claiming success.
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
			grep -iE '4-Way Handshake failed|pre-shared key may be incorrect|WRONG_KEY' |
			tail -n 1 || true)"
	fi
	if [ -n "$journal_hint" ]; then
		die "wrong PSK or association rejected for SSID '${SSID}': ${journal_hint}"
	fi
	die "wlan0 did not associate with SSID '${SSID}' (${wpa_state:-wpa_state=unknown}, ${ipv4:-no IPv4}); check the PSK and that the phone hotspot is on"
fi

if [ -n "$BASELINE_MANAGEMENT_LINKS" ]; then
	management_note="$(printf '%s' "$BASELINE_MANAGEMENT_LINKS" | tr '\n' ' ')"
else
	management_note="none had an IPv4 before the join"
fi

echo "join-hotspot: ${IFACE} joined '${SSID}' (${wpa_state})"
echo "join-hotspot: ${IFACE} ${ipv4}"
echo "join-hotspot: management links intact: ${management_note}"
