#!/bin/bash
# Called by wg-quick (as root) before the tunnel comes up and after it goes down.
# Usage: wg-prep.sh up|down <vm-ip>
# macOS scopes the VM network route to its bridge interface only; add an unscoped host
# route so the tunnel's encrypted packets go straight to the VM, and clear stale cloned
# entries (e.g. 1.1.1.1 pinned to the bridge) that would black-hole DNS.
ACTION="$1"; VM_IP="$2"
SUBNET=$(echo "$VM_IP" | cut -d. -f1-3)
BRIDGE=""
for i in $(ifconfig -l); do
  case "$i" in bridge*) ifconfig "$i" | grep -q "inet $SUBNET\." && BRIDGE="$i";; esac
done
BRIDGE=${BRIDGE:-bridge100}
for ip in $(netstat -rn -f inet | awk -v b="$BRIDGE" -v s="^${SUBNET//./\\.}\\." '$4==b && $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $1 !~ s && $1 !~ /^2(2[4-9]|3[0-9])\./ {print $1}'); do
  route -n delete -host "$ip" -ifscope "$BRIDGE" >/dev/null 2>&1; route -n delete -host "$ip" >/dev/null 2>&1
done
route -n delete -host "$VM_IP" -interface "$BRIDGE" >/dev/null 2>&1
[ "$ACTION" = up ] && route -n add -host "$VM_IP" -interface "$BRIDGE" >/dev/null
exit 0
