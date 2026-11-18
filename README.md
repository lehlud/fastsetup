# Fedora Kiosk Installer

A bootable USB installer that turns an x86_64 laptop into a headless Fedora 44 server. The installer runs in one keyboard-operated terminal. The server comes with SSH, NetworkManager, Podman, KVM/libvirt, Fish, and battery protection.

**Experimental.** The live installer has booted with Secure Boot in a VM. Full installation and hardware validation are pending.

## Build

Use Linux or macOS with Python 3.9+, Docker or rootful Podman, and Internet access. The engine needs privileged loop mounts, about 8 GB RAM and 35 GB free disk space. On Arm hosts it also needs x86_64 container emulation. The container supplies Fedora and QEMU.

```sh
python3 build/build.py
```

The build writes `out/kiosk-live.iso` and its SHA-256 checksum after checking the hybrid BIOS/UEFI image and Fedora boot binaries. Choose a fresh output directory for another build, for example `python3 build/build.py out-next`. See `builder.log` and `compose.log` in the output directory if the build fails.

## Install

1. Verify the checksum, then write the ISO to a USB drive as a disk image. See the [flashing instructions](docs/operations.md#flash-the-usb).
2. Boot from the USB. For Secure Boot, the firmware must trust the Microsoft third-party UEFI CA used by Fedora shim.
3. Confirm the keyboard layout and connect to the Internet over Wi-Fi or Ethernet. Select a target disk and configure the account and SSH access. **Installation erases the selected disk.** Start the install by typing its exact device path on the review screen.
4. Remove the USB after the machine powers off, then boot the server.

Passwords are visible while you type. SSH password login is disabled by default, so add a public key from GitHub or paste one. The account password is still used for local login and sudo. The server boots unattended with an unencrypted target disk.

VPN is optional and off by default. Import a self-contained WireGuard or OpenVPN client profile over HTTPS or enter it in Neovim or Helix. A configured tunnel starts at boot and retries connections. Routing follows the profile, and normal network traffic can continue during a VPN outage.

By default, the server installs updates every eight hours with automatic reboots disabled. The update interval and reboot policy are configurable.

## Configure the server

Fish shows the configuration command on interactive login. Run it to list available commands.

```sh
sudo server-config
sudo server-config ssh --edit-keys
sudo server-config vpn --off
sudo server-config updates --hours 8 --reboot never
sudo server-config network
```

Use `sudo server-config battery` to inspect charge limits and UPS settings. On supported laptops, the default charge corridor is 45–55%. Use an adequately rated adapter. On mains the battery should rest near the upper limit and report `Not charging`. Continuous charging and discharging on mains indicates a fault. On confirmed adapter loss, the UPS guard shuts down below 20% by default.

The live installer and server are configured to keep running with the lid closed on AC power, on battery, and while docked. Suspend and hibernation are disabled. See [laptop power checks](docs/operations.md#check-laptop-power-policy) for verification on hardware.

## Preview and test

Run checks separately from the image build.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 build/preview.py
python3 build/test.py
python3 build/test.py --iso out/kiosk-live.iso
```

The preview uses simulated hardware. The ISO test boots the image with Secure Boot in a container. See [operations](docs/operations.md) for the full VM harness, guest examples, and recovery.

## Secure Boot limits

Secure Boot verifies the firmware-to-kernel chain and enforces kernel module signatures. It does not verify the initramfs, kernel command line, squashfs, or kiosk scripts. Fedora's integrity lockdown blocks `/dev/mem`, rejects unsigned modules, restricts `kexec`, and limits some tracing and BPF features. Ordinary Podman and KVM use remains available.
