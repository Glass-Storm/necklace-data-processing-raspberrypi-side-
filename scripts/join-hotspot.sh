#!/usr/bin/env bash
# Join the phone hotspot on wlan0 with wpa_supplicant + systemd-networkd DHCP.
#
# Usage:  ./scripts/join-hotspot.sh <ssid> <psk> [wpa2|wpa3]
#
# The security mode defaults to wpa2. Use wpa3 for a WPA3-SAE (H2E) access
# point (for example an Android "WPA3-Personal" hotspot); wpa2 keeps the
# original WPA-PSK block.
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
for tool in ip wpa_cli wpa_supplicant mktemp networkctl systemctl; do
	if ! command -v "$tool" >/dev/null 2>&1; then
		missing_tools+=("$tool")
	fi
done
# netplan is only consulted for the WPA2 path; the WPA3 path deliberately
# leaves netplan alone so a stale WPA2 drop-in cannot override the SAE config.
if [ "$MODE" = "wpa2" ] && compgen -G "/etc/netplan/*.yaml" >/dev/null; then
	if ! command -v netplan >/dev/null 2>&1; then
		missing_tools+=("netplan")
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
	if [ "$MODE" = "wpa3" ]; then
		# Global SAE option: H2E (hash-to-element) is what Android WPA3
		# hotspots negotiate. It must sit outside the network block.
		printf 'sae_pwe=2\n'
	fi
	printf '\nnetwork={\n'
	printf '\tssid="%s"\n' "$(escape_value "$SSID")"
	if [ "$MODE" = "wpa3" ]; then
		printf '\tkey_mgmt=SAE\n'
		printf '\tsae_password="%s"\n' "$(escape_value "$PSK")"
		printf '\tieee80211w=2\n'
	else
		printf '\tpsk="%s"\n' "$(escape_value "$PSK")"
		printf '\tkey_mgmt=WPA-PSK\n'
	fi
	printf '}\n'
} >"$wpa_tmp"
mv "$wpa_tmp" "$WPA_CONF"
chmod 600 "$WPA_CONF"

# 2. Ensure a systemd-networkd DHCP unit exists for wlan0. This is independent
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

# 3. WPA2 on a netplan-managed host still uses a drop-in (the historical
# path). WPA3 deliberately does not: a leftover WPA2 drop-in would render a
# PSK config that conflicts with the SAE file written above.
if [ "$MODE" = "wpa2" ] && compgen -G "/etc/netplan/*.yaml" >/dev/null; then
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
fi

# 4. ALWAYS start the per-interface unit and reload networkd in BOTH modes.
# `enable --now` does not restart an already-running unit, so explicitly ask
# wpa_supplicant to re-read the new file below. This is the step the old
# netplan branch skipped, leaving wlan0 without a supplicant.
systemctl enable --now "wpa_supplicant@${IFACE}"

# Reload the rendered networkd configuration without dropping any link.
networkctl reload

# If the service was already running, make it re-read the new configuration.
wpa_cli -i "$IFACE" reconfigure >/dev/null 2>&1 || true

# 5. Verify association and DHCP before claiming success.
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
		die "wrong PSK or association rejected for SSID '${SSID}': ${journal_hint}"
	fi
	die "wlan0 did not associate with SSID '${SSID}' (${wpa_state:-wpa_state=unknown}, ${ipv4:-no IPv4}); check the PSK and that the phone hotspot is on"
fi

if [ -n "$BASELINE_MANAGEMENT_LINKS" ]; then
	management_note="$(printf '%s' "$BASELINE_MANAGEMENT_LINKS" | tr '\n' ' ')"
else
	management_note="none had an IPv4 before the join"
fi

echo "join-hotspot: ${IFACE} joined '${SSID}' (${wpa_state}, ${MODE})"
echo "join-hotspot: ${IFACE} ${ipv4}"
echo "join-hotspot: management links intact: ${management_note}"
