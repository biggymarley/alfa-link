#!/bin/bash
# Pick a Wi-Fi network for the USB adapter inside the VM (numbered list).
source "$(dirname "$(readlink -f "$0")")/common.sh"; require_vm_ip
if [ "$(utmctl status "$VM_NAME" 2>/dev/null)" != "started" ]; then
  echo "Starting the VM..."; utmctl start "$VM_NAME"
  until vm_ssh true 2>/dev/null; do sleep 3; done
fi
for ID in 0E8D:7961 2001:331C; do   # Alfa AWUS036AXML, D-Link DWA-182 rev D1
  utmctl usb list 2>/dev/null | grep -qi "$ID" && utmctl usb connect "$VM_NAME" "$ID" >/dev/null 2>&1 && sleep 5
done
ssh -t "alfa@$VM_IP" alfa-up "$@"
