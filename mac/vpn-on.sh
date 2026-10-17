#!/bin/bash
# Send ALL Mac traffic through the VM's USB Wi-Fi (WireGuard tunnel).
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_vm_ip
CUR=$(vm_ssh "nmcli -t -f NAME,DEVICE con show --active | grep ':wl' | cut -d: -f1")
echo "VM Wi-Fi is on: ${CUR:-(not connected)}"
read -r -p "Press Enter to keep it, or c to choose another network: " ANS
if [ "$ANS" = c ] || [ "$ANS" = C ] || [ -z "$CUR" ]; then ssh -t "alfa@$VM_IP" alfa-up || exit 1; fi
vm_ssh 'ip route show default | head -1 | grep -q " dev wl"' \
  || { echo "❌ The VM isn't online through the USB Wi-Fi adapter. Tunnel NOT started."; exit 1; }
sudo "$BREW/bin/bash" "$BREW/bin/wg-quick" down "$WG_CONF" >/dev/null 2>&1
sudo "$BREW/bin/bash" "$BREW/bin/wg-quick" up "$WG_CONF" || { echo "❌ Tunnel failed to start"; exit 1; }
sleep 3
sudo "$BREW/bin/wg" show | grep -E 'latest handshake|transfer'
echo -n "ping 1.1.1.1 via tunnel: "; ping -c3 -t10 1.1.1.1 | tail -1
echo -n "Public IP: "; curl -s --max-time 30 https://ifconfig.me; echo
echo "✅ Every app now uses the VM's Wi-Fi. Undo: vpn-off.sh"
