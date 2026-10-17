# shellcheck shell=bash disable=SC2034
# Shared settings for the Mac-side scripts (sourced, not run).
ALFA_STATE="${ALFA_LINK_HOME:-$HOME/.config/alfa-link}"
VM_NAME="${ALFA_VM_NAME:-alfa-vm}"
BREW="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
VM_IP="$(cat "$ALFA_STATE/vm-ip" 2>/dev/null || true)"
WG_CONF="$ALFA_STATE/wg/alfa.conf"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=5)
vm_ssh() { ssh "${SSH_OPTS[@]}" "alfa@$VM_IP" "$@"; }
require_vm_ip() { [ -n "$VM_IP" ] || { echo "No VM address in $ALFA_STATE/vm-ip — run install.sh first."; exit 1; }; }
