# AGENTS.md — guide for AI coding agents

This file is for AI agents (Claude Code, Codex, Cursor, etc.) installing, operating, debugging or changing ALFA LINK. Humans should read `README.md`.

## What this project does

It gives an Apple Silicon Mac internet through a Linux-only USB Wi-Fi adapter:

```
Mac apps → wg-quick (utun, 10.66.0.2) → UDP 51820 → Ubuntu VM in UTM (wg0 10.66.0.1)
         → nftables NAT → USB Wi-Fi (wlx…, passthrough) → Wi-Fi AP → Internet
```

- **VM:** Ubuntu 24.04 ARM64 cloud image, UTM **QEMU backend**, "Shared Network" (vmnet, usually `192.168.64.0/24`, Mac side `bridge100`).
- **Local state:** `~/.config/alfa-link/` holds `vm-ip`, `wg/{mac.key,mac.pub,server.pub,alfa.conf}` and `venv/`. Override it with `ALFA_LINK_HOME`. Override the VM name (default `alfa-vm`) with `ALFA_VM_NAME`.

## Install procedure (follow in order; verify each checkpoint)

1. Preconditions:
   ```bash
   uname -sm                 # Darwin arm64
   command -v brew           # Homebrew present
   ```
2. Run `./install.sh`. It is idempotent. Run it in the background or with a long timeout: the image download is ~600 MB and cloud-init takes minutes.
3. **Checkpoint A, VM up:**
   ```bash
   utmctl status alfa-vm                                    # started
   ssh alfa@$(cat ~/.config/alfa-link/vm-ip) 'cloud-init status; uname -r'
   ```
   Expect `status: done` and kernel ≥ 6.8.
4. **Checkpoint B, VM configured:**
   ```bash
   ssh alfa@$IP 'modinfo -F filename mt7921u rtw88_8822bu; sudo wg show wg0 | head -3; sudo nft list ruleset | grep masquerade'
   ```
5. **Checkpoint C, adapter.** The user must physically plug it in. Then:
   ```bash
   utmctl usb list                        # shows 0E8D:7961 or 2001:331C
   utmctl usb connect alfa-vm 0E8D:7961
   ssh alfa@$IP 'alfa-status'             # JSON with "iface": "wlx…"
   ```
6. **Checkpoint D, Wi-Fi.** Get the SSID from the user. The password must be typed **by the user** (interactive `mac/wifi.sh`, or the `alfa` dashboard). Never put a Wi-Fi password in a file, a commit, or a command line on the Mac. Then:
   ```bash
   ssh alfa@$IP alfa-status
   ```
   Expect `"default_via_wl": true` and a numeric `ping_ms`.
7. **Checkpoint E, tunnel.** This needs `sudo`, so the **user** runs `mac/vpn-on.sh` or presses `t` in `alfa`. You can verify without sudo:
   ```bash
   ifconfig | grep -B4 'inet 10.66.0.2 '                       # utun present on the Mac
   ssh alfa@$IP 'sudo wg show wg0 latest-handshakes'           # recent epoch
   ```

## Hard-won invariants. Do NOT "simplify" these away

Each of these was a real failure during development.

| # | Invariant | What breaks without it |
|---|---|---|
| 1 | Cloud-init seed ISO is attached as a **VirtIO disk** (`Drive.1.Interface=VirtIO`, `ImageType=Disk`), not a USB CD | Ubuntu's kernel doesn't see the USB CD → cloud-init disabled → no user, no network, VM looks "stuck" waiting for network |
| 2 | Install `linux-modules-extra-$(uname -r)` | The cloud kernel ships without Wi-Fi drivers (`mt7921u`, `rtw88_*`) |
| 3 | Blacklist `btusb`/`btmtk` (`vm/etc/no-bluetooth.conf`) | The MT7921AU Bluetooth function triggers a kernel oops in `btmtk_usb_hci_wmt_sync` under passthrough, wedges the USB hub workers (D state), and `lsusb`/SSH commands hang |
| 4 | UTM USB controller stays **3.0** (`Input.UsbBusSupport="3.0"`) | On 2.0 the Alfa failed every probe (`mt7921u: probe … failed with error -5`) |
| 5 | Mac→VM traffic goes through **WireGuard**, not routes | `route add 0/1 <vm>`: vmnet drops Mac-originated internet traffic routed into the VM. A scoped `default link#N bridge100` route also hijacks lookups |
| 6 | Use **`wg-quick`** (Homebrew), not the App Store WireGuard app | Network Extension tunnels send their own UDP out the primary interface (en1), so they never reach `192.168.64.x`. Verified: 0 packets at the VM |
| 7 | WG `AllowedIPs = 0.0.0.0/1, 128.0.0.0/1, ::/1, 8000::/1`, plus `mac/wg-prep.sh` adding an **unscoped** `route add -host <vm> -interface bridgeN` | With `0.0.0.0/0`, wg-quick pins the endpoint via the Wi-Fi gateway. Without the host route the endpoint lookup is scoped → "Host is down" |
| 8 | `wg-prep.sh` deletes stale cloned host routes on the bridge (e.g. `1.1.1.1 link#24`) | They black-hole the tunnel's DNS servers |
| 9 | Never run `arp -d -a -i bridge100` | It deletes the Mac's own bridge entry and the VM becomes unreachable |
| 10 | In the VM, the vmnet NIC has `use-dns: false`, `accept-ra: false`, `dhcp6: false`, `route-metric: 1000` (`vm/etc/netplan-vmnet.yaml`) | DNS goes to the Mac, which resolves via the Mac's (possibly offline) Wi-Fi → DNS errors |
| 11 | nftables: masquerade only `oifname "wl*"`; drop `wg0→en*` and `en*→en*` | Traffic loops back through the Mac or leaks out the Mac's Wi-Fi |
| 12 | After choosing a network, all other saved Wi-Fi profiles get `autoconnect no` (`alfa-connect`, `alfa-up`) | NetworkManager silently switches to another saved network when the chosen one drops |
| 13 | Run `wg-quick` as `sudo $(brew --prefix)/bin/bash $(brew --prefix)/bin/wg-quick …` | Under sudo, `#!/usr/bin/env bash` may resolve to macOS bash 3.2, which wg-quick rejects |
| 14 | `netplan apply` in the VM drops the current Wi-Fi connection | `setup-vm.sh` only applies when the file changed. Reconnect with `alfa-connect "<ssid>" </dev/null` (saved profile) afterwards |

## Known flaky area: USB passthrough of the Alfa

- **If it won't attach** (`probe … error -5`, then `USB disconnect`, or stuck at `new high-speed USB device`): ask the user to **unplug and replug** the adapter, preferably into a USB 3 port. Then `utmctl usb connect alfa-vm 0E8D:7961`. If that still fails, quit and reopen UTM.
- **`usbredir protocol parse error`** in UTM: same remedy.
- **Don't** try `usbcore.quirks=0e8d:7961:k`; it was tested and doesn't help.

## Safety rules

- **Never** try to port or write a macOS driver, disable SIP, or install kexts.
- **Never** commit anything from `~/.config/alfa-link/`, `*.key`, `*.conf` with keys, disk images or ISOs (`.gitignore` covers these).
- **Wi-Fi and Mac passwords come from the user interactively.**
  - Don't echo them, log them or store them on the Mac.
  - `alfa-connect` reads the Wi-Fi password from stdin. The dashboard collects the Mac password in a password field and passes it to `sudo -S -v` only.
- **Before changing Mac routing or DNS, tell the user how to undo it.** `mac/vpn-off.sh` undoes the tunnel. A reboot clears any stray routes.
- **Stopping the VM or the adapter while the tunnel is on removes the Mac's internet.** Warn the user first.

## Debugging cheat-sheet

```bash
IP=$(cat ~/.config/alfa-link/vm-ip)
ssh alfa@$IP alfa-status                    # one-shot JSON health (adapter, ssid, signal, ping, wg)
ssh alfa@$IP 'alfa-scan --cached'           # networks JSON
ssh alfa@$IP 'timeout 5 sudo dmesg | grep -iE "mt79|rtw|usb [0-9]-|oops" | tail -20'
ssh alfa@$IP 'resolvectl status | grep -E "Link|DNS"; ip route'
utmctl usb list; utmctl status alfa-vm
netstat -rn -f inet | grep -E '^(0/1|128.0/1|default)|bridge'     # Mac routes
scutil --dns | grep nameserver | head -3
```

- **VM never gets an IP:** check whether cloud-init ran by reading the serial console. `utmctl attach alfa-vm` prints a PTY path; read from it while booting.
- **`lsusb` hangs in the VM:** a USB driver crashed (see invariant 3). Fix with `utmctl stop alfa-vm --kill`, then start it again.

## Changing the code

- **Dashboard** (`mac/alfa_tui.py`, Textual ≥ 8):
  - All blocking I/O runs in `@work(thread=True)` workers. From a worker, schedule UI updates and other workers with `self.call_from_thread(...)`.
  - Textual CSS doesn't accept names like `grey37`; use hex colors.
  - In button labels, `[x]` is parsed as markup; use `(x)`.
  - Headless test pattern:
    ```python
    async with AlfaLink().run_test(size=(150, 44)) as pilot:
        await pilot.pause(6)
        await pilot.click("#b-wifi")
        ...
    ```
    Read state from widgets (`app.query_one("#route").render()`). `app.save_screenshot()` produces an SVG.
- **VM helpers** (`vm/bin/*`):
  - `alfa-status` and `alfa-scan` print one JSON object on the last line. Keep that contract: the dashboard parses `out.splitlines()[-1]`.
  - After editing, deploy to a live VM with `scp vm/bin/* alfa@$IP:/tmp/ && ssh alfa@$IP 'sudo install -m755 /tmp/alfa-* /usr/local/bin/'`, or `./install.sh --vm-only`. Note invariant 14 before re-running setup.
- **Lint:** `shellcheck -S warning install.sh vm/setup-vm.sh vm/bin/alfa-up vm/bin/alfa-connect mac/*.sh mac/alfa` must stay clean.
- **Adding an adapter:** add its USB ID in three places — `ADAPTER_IDS` (`mac/alfa_tui.py`), the loop in `mac/wifi.sh`, and `ADAPTERS` (`vm/bin/alfa-status`). Confirm the Ubuntu 6.8 kernel has an in-tree driver (`modinfo`).
