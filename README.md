# ALFA LINK

Use a **Linux-only USB Wi-Fi adapter** (like the **Alfa AWUS036AXML**) as the internet connection of an **Apple Silicon Mac** — for every app, not just the browser.

macOS has no drivers for these adapters, and Apple Silicon doesn't allow third-party kernel drivers. ALFA LINK works around that: a small Ubuntu VM runs the adapter with Linux's built-in driver, and the Mac sends all its traffic to that VM through a WireGuard tunnel.

```
Mac apps ──▶ WireGuard tunnel ──▶ Ubuntu VM (UTM) ──▶ USB Wi-Fi adapter ──▶ Wi-Fi network ──▶ Internet
```

It comes with **`alfa`**, a terminal dashboard to pick Wi-Fi networks, switch the tunnel on/off, and watch signal, latency and throughput live.

```
 ●  MAC IS ONLINE THROUGH THE USB ADAPTER   Mac ━▶ Tunnel ━▶ VM ━▶ Alfa AWUS036AXML ━▶ MyWiFi ━▶ Internet
 ┌ VM ──────────────┐┌ Adapter ─────────────────────┐┌ Tunnel ──────────┐┌ Internet ─────────┐
 │ State   started  ││ Network  MyWiFi              ││ Mac side  up     ││ Latency   18 ms   │
 │ Uptime  2h 14m   ││ Signal   ████████░░░░  60%   ││ Handshake 12s ago││ Public IP …       │
 └──────────────────┘└──────────────────────────────┘└──────────────────┘└───────────────────┘
```

---

## Requirements

| | |
|---|---|
| Mac | Apple Silicon (M1–M4), macOS 14 or newer (developed on macOS 26) |
| Software | [Homebrew](https://brew.sh). Everything else is installed for you: UTM, qemu, wireguard-tools, Python |
| Disk / RAM | ~6 GB free disk, the VM uses 4 GB RAM and 2 CPU cores |
| Adapter | One of the tested adapters below |
| Admin | Your Mac password (needed to turn the tunnel on/off) |

**Tested adapters**

| Adapter | Chip | USB ID | Notes |
|---|---|---|---|
| Alfa AWUS036AXML | MediaTek MT7921AU | `0e8d:7961` | Wi-Fi 6E. Its Bluetooth part is disabled (it crashes the VM) |
| D-Link DWA-182 rev D1 | Realtek RTL8812BU | `2001:331c` | AC1300 |

Other adapters with a built-in Linux driver (mt76, rtw88 families) will probably work. To add one, put its USB ID in `ADAPTER_IDS` in `mac/alfa_tui.py`, in the list in `mac/wifi.sh`, and in `ADAPTERS` in `vm/bin/alfa-status`.

---

## Install

```bash
git clone https://github.com/biggymarley/alfa-link.git
cd alfa-link
./install.sh
```

Takes 10–30 minutes, mostly downloads. The installer:

1. Installs UTM, qemu, wireguard-tools and Python with Homebrew, and creates an SSH key if you don't have one.
2. Downloads Ubuntu 24.04 (ARM64), verifies its checksum, and creates a VM called **`alfa-vm`** in UTM.
3. Boots it and waits for its first-boot setup.
4. Installs the Wi-Fi drivers, a router/firewall and a WireGuard server inside the VM.
5. Creates the Mac's tunnel config in `~/.config/alfa-link/`.
6. Installs the `alfa` command.

You can run it again at any time; it skips what's already done.

> **Keep the repo folder where it is after installing.** The `alfa` command and the tunnel config point into it. If you move it, run `./install.sh` again.

---

## Use

1. **Plug the adapter into the Mac.** Use a port directly on the Mac, not a hub, because the Alfa needs real power.
2. Run **`alfa`** in a terminal window. Use a normal terminal: full-screen apps don't display inside some embedded terminals.
3. Press **`a`** to attach the adapter to the VM, **`w`** to pick a Wi-Fi network, then **`t`** to turn the tunnel on.

Everything can be clicked with the mouse too.

| Key | Action |
|---|---|
| `w` | Wi-Fi picker. Filter by band with `b` / `0` `2` `5` `6`. Enter or a second click connects. `f` forgets a saved network. `r` rescans |
| `t` | Tunnel on/off. Asks for your Mac password once |
| `a` | Attach the adapter to the VM |
| `s` | Start/stop the VM |
| `p` · `r` · `q` | Refresh public IP · refresh · quit |

Wi-Fi passwords are sent straight to the VM and stored only there. They never touch the Mac's disk.

### Without the dashboard

```bash
mac/wifi.sh      # pick a network (numbered list; r = rescan)
mac/vpn-on.sh    # tunnel on  (all Mac traffic → adapter)
mac/vpn-off.sh   # tunnel off (back to the Mac's own Wi-Fi)
```

### Good to know

- **The VM must be running while the tunnel is on.** If the VM or the adapter goes down, the Mac has no internet until you turn the tunnel off (`t`). This is on purpose: traffic never silently falls back to the Mac's own Wi-Fi.
- **The Wi-Fi icon in the menu bar won't reflect this.** It only shows the Mac's own Wi-Fi. The dashboard is the source of truth.
- **The Mac's own Wi-Fi can stay connected,** even to a network with no internet. It isn't used while the tunnel is on.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Adapter card says *not in VM* | Press `a`. If it still doesn't show up, **unplug the adapter, wait 5 s, plug it back in**, then press `a`. Use a USB 3 port. |
| UTM shows `usbredir protocol parse error` | Same fix: replug the adapter. If it keeps happening, quit and reopen UTM. |
| Tunnel is on but nothing loads | Look at the route bar: the first red step is the broken link. Usually the adapter has lost its Wi-Fi network; press `w` to reconnect, or `t` to turn the tunnel off. |
| Browser says *DNS address could not be found* | The tunnel is off, or the Mac is still using a cached failure. Turn the tunnel on, then restart the browser. |
| VM doesn't answer | `s` twice (stop/start), or in UTM: stop, then start `alfa-vm`. |
| Weak or slow connection | The Signal bar should be above ~40%. Try a 5 GHz network (`w`, then `5`), and point the antenna at the access point. |
| Moved the repo folder | Run `./install.sh` again. |

To look inside the VM: `ssh alfa@$(cat ~/.config/alfa-link/vm-ip)`.

---

## How it works (and why not something simpler)

- **Why a VM?** These adapters only have Linux drivers. Apple Silicon blocks third-party kernel extensions, and DriverKit doesn't allow Wi-Fi drivers. UTM passes the USB device through to an Ubuntu VM, which uses its built-in driver (`mt7921u` / `rtw88_8822bu`).
- **Why WireGuard and not routes or a proxy?**
  - **Routes:** macOS's VM networking (vmnet) drops internet traffic that the Mac routes *into* the VM.
  - **Proxy:** many apps ignore the system proxy.
  - **WireGuard:** the Mac only sends ordinary encrypted packets *to the VM's own address*, which vmnet allows. The VM decrypts them and sends them out through the adapter. Every app is covered.
- **Why the command-line WireGuard and not the App Store app?** macOS forces VPN apps to send their own packets out the Mac's main network interface, so they can never reach a local VM. `wg-quick` has no such restriction.

What the VM does, all in `vm/`:
- NAT out of the USB Wi-Fi only, with forwarding back to the Mac blocked so there are no loops or leaks (`vm/etc/nftables.conf`).
- No DNS or IPv6 through the Mac link (`vm/etc/netplan-vmnet.yaml`).
- The MediaTek Bluetooth driver is blocked (`vm/etc/no-bluetooth.conf`).

---

## Files

```
install.sh              one-shot installer (Mac)
mac/alfa_tui.py         the dashboard (Python + Textual)
mac/alfa                launcher (linked into Homebrew's bin as `alfa`)
mac/vpn-on.sh / vpn-off.sh / wifi.sh   command-line versions
mac/wg-prep.sh          routing fix-up run by wg-quick
vm/setup-vm.sh          configures the VM (run by install.sh over SSH)
vm/bin/                 alfa-up, alfa-status, alfa-scan, alfa-connect (installed in the VM)
vm/etc/                 firewall, network, Bluetooth-block configs
vm/cloud-init/          first-boot config for the VM
```

Everything personal lives **outside** the repo, in `~/.config/alfa-link/`: tunnel keys, the VM address, and the Python environment. Wi-Fi passwords stay inside the VM.

---

## Uninstall

```bash
mac/vpn-off.sh                         # make sure the tunnel is off
utmctl stop alfa-vm; utmctl delete alfa-vm
rm -rf ~/.config/alfa-link
rm "$(brew --prefix)/bin/alfa"
# optional: brew uninstall --cask utm; brew uninstall qemu wireguard-tools
```

## Security notes

- The VM user `alfa` has passwordless sudo and accepts only your SSH key. The VM is reachable only from your Mac (UTM "Shared Network").
- Tunnel keys are generated on install. The Mac's private key never leaves `~/.config/alfa-link/wg/` (mode 700).
- `.gitignore` blocks keys, disk images and local state from being committed.

## License

MIT — see [LICENSE](LICENSE).
