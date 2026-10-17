#!/bin/bash
# Runs INSIDE the VM as root (install.sh copies this folder over and runs it).
# Idempotent: safe to run again. Usage: sudo ./setup-vm.sh <mac-wireguard-public-key>
set -euo pipefail
MAC_PUB="${1:?usage: setup-vm.sh <mac-wireguard-public-key>}"
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive

echo "== Packages (Wi-Fi drivers live in linux-modules-extra; cloud images ship without them)"
apt-get update -qq
apt-get install -y -qq "linux-modules-extra-$(uname -r)" linux-generic wireguard-tools nftables \
  network-manager iw usbutils wireless-tools linux-firmware >/dev/null

echo "== Helper commands"
install -m755 bin/alfa-up bin/alfa-status bin/alfa-scan bin/alfa-connect bin/alfa-hotspot /usr/local/bin/

echo "== Block the crashing MediaTek Bluetooth driver"
install -m644 etc/no-bluetooth.conf /etc/modprobe.d/no-bluetooth.conf
update-initramfs -u >/dev/null 2>&1 || true

echo "== Routing: forwarding on, NAT out the USB Wi-Fi only"
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-forward.conf
sysctl -q --system
install -m644 etc/nftables.conf /etc/nftables.conf
systemctl enable nftables >/dev/null 2>&1
nft -f /etc/nftables.conf

echo "== DNS/IPv6 never via the Mac link"
NETPLAN=/etc/netplan/50-cloud-init.yaml
if ! cmp -s etc/netplan-vmnet.yaml "$NETPLAN"; then
  install -m600 etc/netplan-vmnet.yaml "$NETPLAN"
  echo "network: {config: disabled}" > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
  netplan apply 2>/dev/null || true
fi
mkdir -p /etc/systemd/resolved.conf.d
install -m644 etc/resolved-fallback.conf /etc/systemd/resolved.conf.d/fallback.conf
systemctl restart systemd-resolved

echo "== WireGuard server (wg0, 10.66.0.1, udp/51820)"
mkdir -p /etc/wireguard && chmod 700 /etc/wireguard
umask 077
[ -f /etc/wireguard/server.key ] || wg genkey > /etc/wireguard/server.key
wg pubkey < /etc/wireguard/server.key > /etc/wireguard/server.pub
cat > /etc/wireguard/wg0.conf <<CONF
[Interface]
Address = 10.66.0.1/24
ListenPort = 51820
PrivateKey = $(cat /etc/wireguard/server.key)
MTU = 1380

[Peer]
# Mac
PublicKey = $MAC_PUB
AllowedIPs = 10.66.0.2/32
CONF
systemctl enable wg-quick@wg0 >/dev/null 2>&1
systemctl restart wg-quick@wg0

echo "SERVER_PUBLIC_KEY=$(cat /etc/wireguard/server.pub)"
