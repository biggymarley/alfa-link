#!/bin/bash
# ALFA LINK installer — run on an Apple Silicon Mac.
# Creates an Ubuntu 24.04 ARM64 VM in UTM, sets it up as a USB Wi-Fi router with a
# WireGuard server, and installs the `alfa` dashboard on the Mac.
# Safe to re-run: every step checks what already exists.
#
#   ./install.sh            full install
#   ./install.sh --vm-only  re-run only the VM configuration step
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
STATE="${ALFA_LINK_HOME:-$HOME/.config/alfa-link}"
VM_NAME="${ALFA_VM_NAME:-alfa-vm}"
VM_DISK_GB="${ALFA_VM_DISK_GB:-20}"
IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img"
SUMS_URL="https://cloud-images.ubuntu.com/noble/current/SHA256SUMS"
UTM_DOCS="$HOME/Library/Containers/com.utmapp.UTM/Data/Documents"
WORK="$STATE/build"

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✔ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "This needs an Apple Silicon Mac (macOS, arm64)."
command -v brew >/dev/null || die "Homebrew is required: https://brew.sh"
BREW="$(brew --prefix)"
mkdir -p "$STATE/wg" "$WORK"; chmod 700 "$STATE" "$STATE/wg"

vm_bundle() { find "$UTM_DOCS" -maxdepth 1 -name "$VM_NAME.utm" 2>/dev/null | head -1; }
utm_ready() { for _ in $(seq 1 30); do utmctl list >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }

# ─────────────────────────────────────────────────────────────── 1. tools
install_tools() {
  step "1/6  Mac tools (UTM, qemu-img, WireGuard tools, Python)"
  [ -d /Applications/UTM.app ] || brew install --cask utm
  command -v qemu-img >/dev/null || brew install qemu
  command -v wg-quick >/dev/null || brew install wireguard-tools
  command -v python3  >/dev/null || brew install python
  [ -f "$HOME/.ssh/id_ed25519" ] || ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519" -C "alfa-link" -q
  ok "tools ready"
}

# ─────────────────────────────────────────────────────────────── 2. VM
create_vm() {
  step "2/6  Ubuntu VM \"$VM_NAME\" in UTM"
  open -a UTM; utm_ready || die "UTM didn't start"
  if utmctl list | awk '{print $NF}' | grep -qx "$VM_NAME"; then ok "VM already exists — skipping creation"; return; fi

  cd "$WORK"
  if [ ! -f base.img ]; then
    echo "Downloading Ubuntu 24.04 cloud image (~600 MB)…"
    curl -fL --progress-bar -o base.img.part "$IMG_URL" && mv base.img.part base.img
  fi
  expected=$(curl -fsS "$SUMS_URL" | awk '/noble-server-cloudimg-arm64.img$/{print $1}')
  [ "$(shasum -a 256 base.img | cut -d' ' -f1)" = "$expected" ] || { rm -f base.img; die "Image checksum mismatch — re-run to download again"; }
  ok "image verified"
  cp base.img disk.qcow2 && qemu-img resize -q disk.qcow2 "${VM_DISK_GB}G"

  rm -rf seed && mkdir seed
  sed "s|__SSH_PUBLIC_KEY__|$(cat "$HOME/.ssh/id_ed25519.pub")|" "$REPO/vm/cloud-init/user-data.tmpl" > seed/user-data
  cp "$REPO/vm/cloud-init/meta-data" "$REPO/vm/cloud-init/network-config" seed/
  rm -f seed.iso && hdiutil makehybrid -quiet -iso -joliet -default-volume-name cidata -o seed.iso seed/

  osascript <<OSA >/dev/null
tell application "UTM"
  set vm to make new virtual machine with properties {backend:qemu, configuration:{name:"$VM_NAME", architecture:"aarch64", machine:"virt", memory:4096, cpu cores:2, hypervisor:true, uefi:true, drives:{{interface:VirtIO, source:POSIX file "$WORK/disk.qcow2"}, {removable:true, interface:USB, source:POSIX file "$WORK/seed.iso"}}, network interfaces:{{index:0, mode:shared}}}}
end tell
OSA
  B="$(vm_bundle)"; [ -n "$B" ] || die "VM bundle not found after creation"

  # Fixes UTM's scripting interface can't express (UTM must be closed while editing):
  #  - seed ISO as a VirtIO disk (Ubuntu's kernel doesn't see a USB CD → cloud-init never runs)
  #  - USB sharing on (USB 3.0 controller; the Alfa failed to attach on 2.0)
  #  - a display so the VM window shows the console
  osascript -e 'quit app "UTM"'; sleep 4
  cp "$WORK/seed.iso" "$B/Data/seed.iso"
  P="$B/config.plist"
  plutil -replace Drive.1.Interface -string VirtIO "$P"
  plutil -replace Drive.1.ImageType -string Disk "$P"
  plutil -replace Drive.1.ImageName -string seed.iso "$P"
  plutil -replace Input.UsbSharing -bool true "$P"
  plutil -replace Input.UsbBusSupport -string "3.0" "$P"
  plutil -replace Display -json '[{"Hardware":"virtio-ramfb","DynamicResolution":true,"NativeResolution":false,"UpscalingFilter":"Nearest","DownscalingFilter":"Linear"}]' "$P"
  plutil -lint -s "$P"
  rm -f "$WORK/disk.qcow2"     # UTM copied it into the VM bundle
  open -a UTM; utm_ready
  ok "VM created"
}

# ─────────────────────────────────────────────────────────────── 3. boot
boot_vm() {
  step "3/6  Boot the VM and wait for first-boot setup"
  [ "$(utmctl status "$VM_NAME")" = started ] || utmctl start "$VM_NAME"
  local ip="" seen=""
  echo -n "waiting for the VM's address"
  for _ in $(seq 1 120); do
    # newest lease for this VM name (macOS lists newest first)
    ip=$(awk -v n="$VM_NAME" '/^\{/{name="";addr=""} /name=/{split($0,a,"=");name=a[2]} /ip_address=/{split($0,a,"=");addr=a[2]} /^\}/{if(name==n && !found)found=addr} END{print found}' /var/db/dhcpd_leases 2>/dev/null)
    [ -n "$ip" ] && [ "$ip" != "$seen" ] && { ssh-keygen -R "$ip" >/dev/null 2>&1 || true; seen="$ip"; }
    [ -n "$ip" ] && ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new "alfa@$ip" true 2>/dev/null && break
    ip=""; echo -n "."; sleep 5
  done; echo
  [ -n "$ip" ] || die "VM never became reachable over SSH. Open its window in UTM to see the console."
  echo "$ip" > "$STATE/vm-ip"
  echo "VM is at $ip — waiting for cloud-init (installs packages; can take several minutes)…"
  ssh "alfa@$ip" 'cloud-init status --wait >/dev/null; cloud-init status'
  ok "VM booted"
}

# ─────────────────────────────────────────────────────────────── 4. configure VM
configure_vm() {
  step "4/6  Configure the VM (Wi-Fi drivers, router, WireGuard server)"
  local ip; ip=$(cat "$STATE/vm-ip")
  umask 077
  [ -f "$STATE/wg/mac.key" ] || wg genkey > "$STATE/wg/mac.key"      # private key never leaves the Mac
  wg pubkey < "$STATE/wg/mac.key" > "$STATE/wg/mac.pub"
  ssh "alfa@$ip" 'rm -rf /tmp/alfa-link-vm && mkdir -p /tmp/alfa-link-vm'
  scp -q -r "$REPO/vm/." "alfa@$ip:/tmp/alfa-link-vm/"
  out=$(ssh "alfa@$ip" "sudo bash /tmp/alfa-link-vm/setup-vm.sh '$(cat "$STATE/wg/mac.pub")'" | tee /dev/stderr)
  srv=$(echo "$out" | awk -F= '/^SERVER_PUBLIC_KEY=/{print $2}')
  [ -n "$srv" ] || die "VM setup didn't return its WireGuard key"
  echo "$srv" > "$STATE/wg/server.pub"
  ok "VM configured"
}

# ─────────────────────────────────────────────────────────────── 5. tunnel config
write_tunnel() {
  step "5/6  Tunnel config on the Mac"
  local ip; ip=$(cat "$STATE/vm-ip")
  umask 077
  cat > "$STATE/wg/alfa.conf" <<EOF
[Interface]
PrivateKey = $(cat "$STATE/wg/mac.key")
Address = 10.66.0.2/32
DNS = 1.1.1.1, 8.8.8.8
MTU = 1380
PreUp = $REPO/mac/wg-prep.sh up $ip
PostDown = $REPO/mac/wg-prep.sh down $ip

[Peer]
PublicKey = $(cat "$STATE/wg/server.pub")
Endpoint = $ip:51820
# Two halves instead of 0.0.0.0/0 so wg-quick doesn't pin the endpoint to the Wi-Fi router
AllowedIPs = 0.0.0.0/1, 128.0.0.0/1, ::/1, 8000::/1
PersistentKeepalive = 25
EOF
  ok "wrote $STATE/wg/alfa.conf"
}

# ─────────────────────────────────────────────────────────────── 6. dashboard
install_dashboard() {
  step "6/6  Dashboard (alfa)"
  [ -x "$STATE/venv/bin/python" ] || python3 -m venv "$STATE/venv"
  "$STATE/venv/bin/pip" install -q --upgrade pip
  "$STATE/venv/bin/pip" install -q -r "$REPO/mac/requirements.txt"
  chmod +x "$REPO"/mac/*.sh "$REPO/mac/alfa" "$REPO"/vm/bin/* "$REPO/vm/setup-vm.sh"
  ln -sf "$REPO/mac/alfa" "$BREW/bin/alfa"
  ok "run:  alfa"
}

if [ "${1:-}" = "--vm-only" ]; then
  configure_vm; write_tunnel; exit 0
fi
install_tools; create_vm; boot_vm; configure_vm; write_tunnel; install_dashboard

cat <<'EOF'

All set. Next:
  1. Plug the USB Wi-Fi adapter (Alfa AWUS036AXML or D-Link DWA-182) into the Mac.
  2. Run `alfa` in a terminal.
  3. Press  a  (attach adapter) →  w  (pick a Wi-Fi network) →  t  (tunnel on).
EOF
