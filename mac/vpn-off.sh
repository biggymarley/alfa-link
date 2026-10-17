#!/bin/bash
# Stop the tunnel — back to the Mac's own Wi-Fi.
source "$(dirname "$(readlink -f "$0")")/common.sh"
sudo "$BREW/bin/bash" "$BREW/bin/wg-quick" down "$WG_CONF" && echo "✅ Tunnel off. Mac uses its own Wi-Fi again."
