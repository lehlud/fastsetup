# Fedora kiosk installer stick

An x86_64 Fedora 44 live USB with Cage, one fullscreen Foot terminal, a keyboard
probe, and a keyboard-only installer for a headless Fedora server. The chosen
disk is erased. The target gets SSH, Podman, KVM/libvirt, and battery UPS policy.

**Status: image build and Secure Boot smoke verified; installation/hardware acceptance pending.**
The portable command has completed on an ARM64 OrbStack engine using its x86_64
container environment, producing a 772 MiB ISO with passing artifact checks.
A separate smoke test reached the keyboard screen with Secure Boot enabled.
The full T1–T21 matrix is not passed. Do not use this on a disk containing data
you need. Build and run the disposable VM tests first.

## Compatibility decisions

The original `liveinst --kickstart` handoff is not supported by upstream:
[liveinst rejects kickstart](https://github.com/rhinstaller/anaconda/blob/main/data/liveinst/liveinst),
and [Anaconda documents that restriction](https://github.com/rhinstaller/anaconda/blob/main/data/anaconda_options.txt).
This implementation uses **stock `anaconda --cmdline --kickstart=/tmp/kiosk.ks
--nosave=all`**, in package installation mode with Fedora repositories. It does
not copy the kiosk root onto the target. `--cmdline` makes incomplete automation
fail instead of opening an unusable TUI. Running that mode from this custom live
environment still requires a full VM installation test; a successful boot smoke
does not establish that it works.

`/etc/anaconda/conf.d/99-kiosk.conf` explicitly selects its `UNKNOWN` installation
environment so Anaconda cannot reboot the live machine from an early exception
handler. The kiosk owns runtime networking, keyboard configuration and shutdown;
the kickstart owns target configuration. This integration choice also belongs
in the mandatory full-install tests.

Kickstart uses the supported `layout (variant)` spelling, for example
`keyboard --vckeymap=de-latin1-nodeadkeys --xlayouts='de (nodeadkeys)'`.
Fedora 44 provides Neovim, Helix and the `dnf5-plugin-automatic` update tooling.

Several specification constraints need explicit interpretation:

- Identical level-zero maps cannot identify a region or dead-key variant. Ties
  always reach the shortlist. Raw probes never make an arbitrary tie-breaking
  selection. Hardware hints order choices; they do not exclude candidates.
- The Indian XKB profile currently has a US Linux VT fallback because Fedora's
  console map set lacks a matching default Indic map. Full console parity for
  this profile remains an FR-KB.4 acceptance gap.
- AZERTY digits may be printed on the shifted level. The table records those
  digit labels at build time so pressing the labeled key needs no Shift.
- A bad confirmation echo stays on screen with a validation error. Correct a
  typing error there; reboot to change the layout. Re-probing and changing the
  compositor layout again would violate the requirement of one session restart.
  Echo requires actual input. Account fields offer a generated password in both
  password fields; replacing it requires matching replacements in both fields.
- `/tmp`, `/run`, and live logs are volatile state. NetworkManager keyfiles must
  briefly exist in its `/run/NetworkManager/system-connections` directory.
  Target configuration and battery mode are intentionally persistent.
- Anaconda exposes no stable percent API here. Installation shows a single
  activity line with elapsed time, without inventing a percentage.
- The battery boot service runs after `multi-user.target`, activated by a
  boot timer. Enabling a service both after and as a dependency of that target
  would create an ordering cycle.

## Source tree

`build/kiosk-live.ks` and `image/` define the image. `build/prepare.py` embeds the
tree and the selected target payload into a self-contained kickstart. The XKB
matrix is regenerated **inside the image root** by `generate-keymaps.py`, using
that image's XKB database and libxkbcommon. Raw keycode translation uses the
generated TSV, without Python XKB bindings or `xkbcli`. The terminal interface
uses Fedora's `python3-textual` package (Textual 4.0.0).

`image/usr/local/lib/kiosk/` contains the terminal, probe, network, storage,
installer, and battery implementations. Small executables live in
`image/usr/local/bin/`. The installed server receives battery support, the server
self-check, and `server-config` with the shared SSH, VPN and update policy code.

## Interface and preview

The installer is a Python application using
[Textual](https://textual.textualize.io/), with its input widgets and event loop.
It uses a centered 72-column canvas of at most twelve lines, without panels or
desktop chrome. Arrow keys select menu entries across pages; numbers select an
entry on the visible page, Enter accepts, and n/p or Page Up/Down change pages.
Input fields support cursor movement, Home/End, UTF-8 paste, Tab and Shift-Tab.
Timezone names have inline suggestions accepted with Right.

Identity and account each use one form. Validation errors stay beside the
existing fields. Esc goes back; completed steps keep their values. F2 on the
summary opens settings for editing. The final review requires the exact target
path once and has no default. Network scans, connections, key fetches and other
blocking operations run off the UI thread; Ctrl-C remains available throughout.
In the live session, Ctrl-C requests power-off before Python waits for a blocked
worker thread to finish. The preview's abort only exits the application.
Passwords stay visible while editing, then their disposable widgets are cleared.
The full-screen application uses the terminal's alternate buffer and no history;
secrets are not retained in a back-navigation draft or returned as an app result.

Try the actual wizard with an inert backend on Linux or macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 build/preview.py
```

The preview uses dummy adapters, disks, SSH keys, VPN profiles, hashes and battery state.
It executes no installation, external network requests, or power-off. It uses
the production forms, navigation, network selection and disk confirmation flow.
Building the image still needs only Python and the container engine on the host;
these Python UI dependencies are for preview and testing.

## Build

Build on **Linux (any distribution) or macOS** with Python 3.9+ and a working
Docker or rootful Podman engine. Fedora and QEMU are **not host prerequisites**.
Windows is outside the build-host scope. The engine needs working loop devices,
filesystem mounts, about 8 GB RAM and 35 GB free storage for its build workspace,
plus room for the exported ISO and logs.

```sh
python3 build/build.py
# Explicit engine / alternate empty output directory:
python3 build/build.py --engine docker out-next
# Linux rootful Podman:
sudo python3 build/build.py --engine podman
# Inspect commands without downloads or execution:
python3 build/build.py --dry-run
```

The entry point pulls `registry.fedoraproject.org/fedora:44` and installs build
tools from Fedora's Internet repositories using `build/Containerfile`. The
internal `build/build-live.sh` still runs as root in that Fedora environment.
Its compatibility pin remains **livecd-tools-31.0-20.fc44**, the
[Fedora 44 packaged version](https://packages.fedoraproject.org/pkgs/livecd-tools/livecd-tools/).
Review boot-file placement and rerun acceptance before changing that pin. The
reference shim, GRUB and kernel RPMs live inside the builder as well.

The builder runs privileged because livecd-creator needs loop mounts. Rootless
engines are rejected. On Linux, ensure the engine host has the `loop` driver
available (`sudo modprobe loop` if needed). On macOS, Docker Desktop or a rootful
Podman machine supplies the Linux kernel; its VM needs sufficient memory and
disk space and must share the project/output directory. No separate QEMU host
installation or manually managed Fedora VM is required. Both build and run use
`linux/amd64`, including on Apple Silicon, where the engine must support x86_64
container emulation. That path is slower and still needs end-to-end acceptance.

Only a snapshot of `build/`, `image/` and project metadata is mounted
read-only into the builder. Host `.git`, credential directories and unrelated
files are excluded. An anonymous engine volume holds loop files and temporary
root filesystems on a Linux filesystem, then is removed with the container.
The named `fedora-kiosk-rpms-44-amd64` volume caches package downloads across
retries. The output directory receives artifacts and logs owned by the caller.
The Fedora builder image also remains cached for reuse. No host SELinux policy change
is made. Use a fresh output directory for each build.

`out/builder.log` captures container output; `out/compose.log` contains the compose
detail. Failure diagnostics are exported too. The ISO is exported only after
signature and artifact checks pass; the host then checks
its checksum again. Package resolution or unavailable engine capabilities fail
the build with diagnostics rather than bypassing a gate.
If composition completed but artifact verification failed, the diagnostic ISO
is retained as `kiosk-live.unverified.iso`; it is not a release artifact.

The wrapper validates kickstart syntax, composes the ISO, checks
the 2.5 GB limit and hybrid BIOS/UEFI boot catalog, extracts shim/GRUB/kernel,
checks signature metadata with both `sbverify` and `pesign`, and compares the
boot binaries with strong digests from their Fedora RPM payload records. It checks the FAT EFI system
partition as well as the ISO tree. The live post script rejects non-Fedora
package vendors and unexpected module tooling. A signer name alone is not proof
of cryptographic trust.
Generated ESP copies are RPM ghost files; their permissions are not a binary
integrity check. The verifier uses the recorded vendor payloads in `/usr/lib/efi`
and `/lib/modules`. Fedora's kernel leaf CN can be `kernel-signer`; the Fedora
issuer identity is checked in `sbverify` output, alongside `pesign`'s signature
listing and the kernel's RPM payload digest. Results are recorded in
`verification/provenance.json`.

The pinned creator's legacy Mac HFS boot image is omitted by `build/create-live.py`;
BIOS and the signed FAT UEFI boot chain remain included. Live files are unpacked
with Python because GNU tar's implicit-directory extraction fails on ext4 under
some x86_64 container emulators.
Boot files are staged after the live post script regenerates the initramfs, with
an explicit exported command path for dracut's helper processes.

Rebuilding requires this repository, the container engine, and Internet access
to Fedora's registry and repositories. Fedora update repositories change, so
**byte-for-byte reproducibility is not promised**: retain the package manifest, exact
RPMs, host report and repository snapshots to reproduce a release's package set.
There is no cached secret or hardware-specific configuration in the source.

## Tests

Building does not run the test suite or boot a VM. Invoke tests separately:

```sh
python3 build/test.py
python3 build/test.py --iso out/kiosk-live.iso
```

The first command runs unit tests. With `--iso`, the test command additionally
uses the tools container created by the build to run `build/smoke.py`. It boots
the ISO **as a raw USB disk** in q35/OVMF with SMM,
enrolled keys and Secure Boot. A ready message must arrive from Python inside
Foot with `mokutil` reporting enabled. Missing messages, a security violation,
or failure to reach the terminal fail the test. Software rendering and TCG
exercise operation without host GPU acceleration or KVM. QEMU runs **inside the
tools container**, without privileged access. The smoke deadline is 900 seconds,
configurable with the test command's `--timeout`, to accommodate emulation.
Artifacts go to `smoke/` beside the ISO, or the test command's `--output` directory.
Use a fresh smoke directory for each run. A built ISO must pass these tests and
the acceptance matrix before release.

## Full VM acceptance

The standalone interactive harness below needs QEMU on its test machine. It is
separate from image building and from the headless smoke test above.

```sh
python3 build/vm-test.py install
# Complete the UI, selecting /dev/vda, then wait for the VM to power off.
python3 build/vm-test.py boot
ssh -p 2222 server@127.0.0.1
sudo mokutil --sb-state
systemctl status server-self-check.service
sudo test -e /var/lib/server-self-check.ok
```

The harness creates a new disposable 32 GB target and refuses to overwrite an
existing test disk. Run `--bios --directory out/bios-vm` for legacy BIOS. The
installed disk boots with the same enrolled OVMF variables and the USB removed.
The default SSH forwarding listens on localhost only. KVM acceleration is used
when available; TCG is slower and cannot validate usable nested KVM.

T3 also needs a separate test image with a deliberately missing mandatory
kickstart field: expect a noninteractive failure, log locations, Enter, and
poweroff. Do not accept a timeout, TUI, or reboot as success. This destructive
full-install test and T18 are **release acceptance gates**, additional to the
separate unit and Secure Boot smoke tests.

## Flash and operate

Verify `out/kiosk-live.iso.sha256`, identify the **whole USB disk**, unmount its
partitions, then substitute its actual path for `/dev/sdX`:

```sh
(cd out && sha256sum -c kiosk-live.iso.sha256)
lsblk -o NAME,SIZE,MODEL,TRAN,MOUNTPOINTS
sudo dd if=out/kiosk-live.iso of=/dev/sdX bs=4M status=progress conv=fsync
sync
```

Boot from the USB with Secure Boot enabled. Firmware must trust the Microsoft
third-party UEFI CA used by Fedora shim; some corporate firmware disables that
CA. Such a machine cannot meet the no-firmware-change requirement as specified.
No custom key enrollment is part of the default flow.

Identify the printed keyboard, confirm its visible echo, set hostname/timezone,
connect Wi-Fi or Ethernet, select a disk, create the account, and select SSH keys.
Passphrases and passwords are visible while typing; the line and scrollback are
erased immediately afterward. Long input stays within one terminal row. Open,
WPA2 and WPA3-SAE networks are supported; enterprise Wi-Fi is not. SSIDs sort by
signal; hidden networks have a manual entry. The Internet gate requires both
ICMP to `1.1.1.1` and DNS for Fedora mirrors, even if ICMP is locally filtered.

NetworkManager manages networking on both the live stick and the installed
server. Both include `nmtui` through Fedora's
[NetworkManager-tui package](https://packages.fedoraproject.org/pkgs/NetworkManager/NetworkManager-tui/).
On the server, run `sudo nmtui` to edit connections; on the stick it is available
in recovery.

Both images explicitly install the split WLAN firmware packages for Intel,
Atheros/Qualcomm, Broadcom (`brcmfmac`), MediaTek, Realtek, NXP/Marvell and older
supported adapters. `linux-firmware` alone is insufficient when weak dependencies
are excluded: several vendor firmware packages are only recommended, and Intel
firmware is packaged separately. The target also includes `kernel-modules-extra`
so a live-session driver is available after installation.

Before listing network paths, the installer enables Wi-Fi in NetworkManager and
allows five seconds for device discovery after udev settles. Hardware radio
blocks get an explicit message; software cannot override a physical radio switch.
If Wi-Fi is absent, the screen says so and offers **Retry detection**, alongside
Ethernet when present. It no longer silently forces an Ethernet-only path.
An unrecognized or unsupported chip can still require investigation on that
laptop. From the recovery shell, inspect:

```sh
nmcli -f DEVICE,TYPE,STATE device
nmcli radio
journalctl -k -b --no-pager -g 'firmware|iwlwifi|ath[0-9]|brcm|mt7|rtw|rfkill'
```

Linux TTYs use the larger 24-pixel Terminus font `ter-v24n`, supplied by
[terminus-fonts-console](https://packages.fedoraproject.org/pkgs/terminus-fonts/terminus-fonts-console/fedora-44.html).
The live session applies it at startup, and installation persists it in
`/etc/vconsole.conf` alongside the chosen keyboard map. A legacy text-mode
adapter that rejects the font retains its existing font during the live session.
The Wayland installer terminal has its own 14-point font setting in
`image/etc/xdg/foot/foot.ini`.

The live medium must be resolved before any target is accepted. Mounted disks,
active swap, partitions, and disks under 8 GB are rejected. Select an eligible
disk in the list, then type its path on the final review to start installation.
Inventory is refreshed to detect a changed disk. Actual capacity needed by the requested package set is
an unmeasured acceptance item; the 8 GB eligibility floor is not a measured fit
guarantee.

GitHub key retrieval has time and size limits. Key selection shows the beginning
and end of each long public key as `START...END`; the complete key is installed.
Pasted keys are checked for type, base64 structure and OpenSSH validity.
**Disable SSH password login** is checked by default. Space toggles the checkbox
and Enter continues; the summary records the selected policy. With it checked,
the target accepts public-key authentication and disables both password and
keyboard-interactive authentication. The setting is written to
`/etc/ssh/sshd_config.d/00-kiosk-auth.conf`, using OpenSSH's
[authentication settings](https://man.openbsd.org/sshd_config).

Skipping keys requires explicitly unchecking that option and shows a
password-only SSH warning. A keyless, password-disabled setup is rejected in
the UI and kickstart renderer. Unchecking the option allows SSH password login
alongside any installed keys. This changes SSH access only; the account password
remains available for local login and sudo. Root is locked, and wheel sudo
requires a password. There is no target LUKS:
unattended power-loss recovery requires automatic boot. SELinux is enforcing and
firewalld exposes SSH; no remote libvirt or Podman listener is enabled.

Credentials are never put in child command arguments. A mode-0600 tmpfs kickstart
holds the SHA-512 account password hash and base64 network and VPN configuration. **Base64 is
encoding, not encryption.** `--nosave=all` prevents Anaconda retaining the generated
kickstart on the target. Runtime NM keyfiles are deleted after activation; the
target receives one root-only persistent profile so it comes online by itself.
Audit upstream Anaconda/NM logs with test credentials during acceptance before
asserting that SEC-1 holds across those external components.

Installation ends in poweroff. Remove the stick after shutdown, then start the
server. The first target boot checks user access to `/dev/kvm`, `kvm` membership,
libvirt connectivity and rootless `podman info`. Failures stay visible in the
service journal and retry next boot; a successful check creates the marker above.

## VPN and maintenance

The **Always-on VPN** step accepts an HTTPS profile URL or opens Neovim or Helix
in the installer terminal. Type or paste the profile, then save and quit with
`:wq`. The installer identifies WireGuard and OpenVPN from the contents and asks
you to confirm the detected type. **No VPN** is preselected. Press Enter to go
straight to the update settings without enabling a VPN service on the target.

Use a self-contained client profile. WireGuard supports addresses, peers, routes
and DNS, with a 25-second keepalive for peers with endpoints unless the profile
specifies one. OpenVPN supports TUN profiles with inline certificates and
credentials and server verification. Use credentials that work unattended.
OpenVPN DNS pushes do not change the target resolver. The installer rejects
profile scripts, plugins and references to external credential files.

The profile is stored with mode 0600 under `/etc/wireguard/` or
`/etc/openvpn/client/`. Check the associated boot service with
`systemctl status wg-quick@kiosk-vpn` or
`systemctl status openvpn-client@kiosk-vpn`. The installer applies the profile
only to the target. Always-on means automatic startup and reconnect attempts.
The profile sets the routes. The server has no VPN kill switch.

**Automatic updates** defaults to every eight hours, with the first run eight
hours after boot and the interval measured from each run's completion. Enter
`0` for manual updates. Automatic reboot can be set to **Never** (default),
**When needed**, or **After any update**. The target uses
`/etc/dnf/automatic.conf` and the schedule drop-in in
`/etc/systemd/system/dnf5-automatic.timer.d/`. Inspect the timer with
`systemctl list-timers dnf5-automatic.timer`. [DNF5 automatic settings](https://dnf5.readthedocs.io/en/latest/dnf5_plugins/automatic.8.html)

## Change the installed server

The installed user's login shell is Fish. Interactive logins show
`Configure this server with sudo server-config`. Running that command lists its
subcommands and each subcommand accepts `--help`.

```sh
sudo server-config battery --start 45 --stop 55 --guard yes --shutdown-at 20
sudo server-config battery --mode full
sudo server-config ssh --edit-keys --password-login no
sudo server-config ssh --keys ~/server-authorized_keys
sudo server-config vpn --url
sudo server-config vpn --off
sudo server-config updates --hours 8 --reboot never
sudo server-config identity --hostname calm-fox --timezone Europe/Berlin --keymap de
sudo server-config network
sudo server-config password
```

Run `battery`, `ssh`, `vpn` or `updates` without flags to check the current
settings. SSH key changes validate each entry before replacing the file. The
utility checks the SSH daemon configuration before reloading and restores the
previous policy and keys if that fails. Keep a working key when disabling SSH
password login. Use the local console when changing the active network or VPN
routes if those routes carry your SSH session.

## Battery and UPS

```sh
sudo battery-limits status
sudo battery-limits full
sudo battery-limits normal
sudo battery-limits off
```

Configuration is `/etc/battery-limits.conf`: `START_PERCENT=45`, `STOP_PERCENT=55`,
`FULL_PERCENT=100`, `GUARD_ENABLED=yes`, `GUARD_SHUTDOWN_AT=20`, `BATTERY=auto`.
`auto` covers every present battery. A selected battery name restricts the policy.
Mode persists in `/var/lib/battery-limits/mode`; full and normal apply immediately
without reboot. `off` restores unrestricted charging; it does **not** disable UPS
shutdown, which is configured separately with `GUARD_ENABLED`.

The 45–55% band is a **corridor, not a charging cycle**. Use an adequately rated
adapter. On mains the pack should rest at the stop threshold, normally reporting
`Not charging`, while the adapter powers the machine through the charger power
path. The band matters on adapter loss or slow self-discharge. Continuous cycling
on mains is a fault symptom. `AC online=1` with `Discharging` is reported as an
undersized adapter or deliberate firmware discharge; investigate it under load.

Threshold writes require matching read-back. Upper-only hardware gets a 55%
ceiling; no forced discharge is used to reach it. Where only `charge_behaviour`
exists, a 60-second polling policy inhibits charging at 55 and releases at 45;
this is software policy and has polling overshoot, not a firmware threshold.
It never selects `force-discharge`.

Policy runs in the live system and is restored at boot; a udev rule requests
reapplication on power-supply events. TLP is installed as a fallback tool, not
enabled as a competing service. If native controls and a verifiable TLP attempt
fail, the installer warns and records the result in `/etc/battery-limits-decision`.
There is no generic API to establish whether a firmware charge-limit menu exists:
inspect firmware manually when instructed; software does not claim to have set it.
No battery is a logged skip. A smart plug with an independently validated local
controller is an optional external workaround, outside this product.

`battery-guard.timer` polls every 60 seconds. It watches power-supply **online**
attributes, logs adapter loss/restoration, and calls `systemctl poweroff` below
20% on confirmed adapter loss. It never hibernates. Unknown AC state does not
trigger a guessed shutdown. Guard events are in the system journal. A failed
threshold implementation must not disable this UPS reserve protection.

## Virtual machines on the installed server

q35 with KVM and the host CPU is the default example. Use an existing guest
disk configured for serial console, or add a display while installing a guest:

```sh
qemu-system-x86_64 -M q35 -enable-kvm -cpu host -m 2048 \
  -drive file=guest.qcow2,format=qcow2,if=virtio \
  -nic user,model=virtio-net-pci -display none -serial mon:stdio
```

`microvm` is a separate minimal-machine example, using your own guest kernel,
initramfs and root filesystem. Its devices use **virtio-mmio**, not PCI:

```sh
qemu-system-x86_64 -M microvm -enable-kvm -cpu host -m 1024 \
  -nodefaults -no-user-config -display none -serial mon:stdio \
  -kernel ./bzImage -initrd ./initramfs.img \
  -append 'console=ttyS0 root=/dev/vda rw' \
  -drive if=none,id=rootfs,file=guest.raw,format=raw \
  -device virtio-blk-device,drive=rootfs \
  -netdev user,id=net0 -device virtio-net-device,netdev=net0
```

Build the guest kernel/initramfs with serial console, `CONFIG_VIRTIO_MMIO`,
virtio block/network, and the root filesystem driver available at boot. Direct
kernel boot in this guest is distinct from the host's firmware Secure Boot
chain. Libvirt uses its local Unix socket; membership in `libvirt` grants
administration through that socket, without a remote listener.

## Secure Boot boundaries and extensions

The intended trust chain is firmware → Microsoft-signed Fedora shim →
Fedora-signed GRUB → Fedora kernel → Fedora kernel modules. Legacy BIOS cannot
offer this Secure Boot guarantee. The installed target relies on Anaconda's
standard Fedora shim/GRUB/kernel installation, which must be tested separately.

**Secure Boot does not verify the initramfs, kernel command line, squashfs,
kiosk scripts, or generated kickstart.** Someone who replaces kiosk userspace can
change the installer despite a valid boot chain. Kernel command-line recovery
also remains possible. No claim of complete USB tamper resistance is made.
dm-verity with an authenticated root hash and a signed UKI would be separate
work with an expanded signing design; no UKI or systemd-boot is used here.

With Secure Boot active Fedora enables kernel `lockdown=integrity`: `/dev/mem`
access is blocked, unsigned modules are refused, `kexec` is restricted, and parts
of tracing and BPF are unavailable. Ordinary Podman, KVM, and services do not
require bypassing those restrictions.

For later out-of-tree modules, akmods/DKMS must sign modules with a local key,
then import its public certificate with `mokutil --import`, reboot into
MokManager and enroll it. MokManager uses the firmware keyboard layout, usually
US: choose a plain alphanumeric enrollment password. This is a per-machine,
interactive extension and is outside the default image.

An own-key route can retain Fedora shim and use an enrolled MOK for compatible
signed downstream EFI binaries/modules; `sbctl` can help manage PE signing but
does not itself make firmware trust a MOK. Alternatively, provision your own
PK/KEK/db in firmware Setup Mode and sign the complete chosen chain. Back up
vendor keys and verify recovery first. Full db enrollment is per-device and can
make a machine unbootable, potentially without a recoverable firmware path.
Neither route is implemented by this installer.

## Abort and recovery

`kiosk-install → foot → cage → kiosk.service stop → ExecStopPost` is the shutdown
chain, including SIGKILL of the installer. INT/TERM/HUP additionally request
`sync`, `poweroff -i`, then `poweroff -f`. There is no service restart policy.
The supervisor launches the compositor exactly twice: probe, then installation.
Ctrl-Alt-Del targets poweroff, and a burst uses forced poweroff. All gettys are
masked. No lid suspension, idle lock, or sleep target is available.

An Anaconda failure displays `/tmp/anaconda.log`, `/tmp/ks.error`,
`/tmp/packaging.log`, and `/var/log/kiosk-install.log`, waits for Enter, then
powers off. Logs are live-only and disappear at shutdown. They are not silently
copied to the target. Copy needed diagnostics from recovery before ending that
recovery session. A frozen session needs a physical reset.

For recovery, edit the boot command line and replace
`systemd.unit=multi-user.target` with `systemd.unit=rescue.target`, appending
`systemd.mask=kiosk.service`. Since the live root password is locked, a root shell
may additionally require `rd.break` or `init=/bin/bash` on systems where rescue
authentication is required. These are physical-access escape hatches. Recovery
must be booted deliberately; no second terminal exists in the kiosk session.

## Verification record

Run the portable checks without root:

```sh
python3 -m pip install -r requirements-dev.txt
python3 build/test.py
python3 -m compileall -q image/usr/local/lib build
bash -n build/build-live.sh build/container-entrypoint.sh image/usr/local/bin/kiosk-session \
  image/usr/local/bin/kiosk-poweroff image/usr/local/bin/server-self-check
```

Workspace validation: **81 tests passed**, Python compilation and shell syntax
checks passed, and both the flattened live kickstart and a rendered target
kickstart passed pykickstart 3.78 with the Fedora 44 schema. Systemd unit
validation passed against a temporary image root, with dependency errors excluded
because that root is not a complete composed Fedora system. These checks neither
execute the units nor replace the timed shutdown tests.

The portable launcher is tested with simulated engine responses for Linux and
macOS, including source filtering, mount quoting, rootless rejection, cleanup
and exported checksums. Full portable composes have completed on the ARM64
OrbStack engine. The latest Fedora builder container exited successfully and
exported `out/server-config/kiosk-live.iso` (1,052,770,304 bytes), SHA-256
`d85f5cfefc547d7ad7b3ab9787b0e6b5ebf7797648a9d99b0f62d3c59c11c472`.
ISO creation, hybrid boot catalog, ESP contents, Microsoft/Fedora signature
metadata, RPM payload digests, size and the exported checksum passed. The compose
ran no tests. The embedded payload and target files match the reviewed source,
including the executable configuration command and Fish login snippet.
The compose log confirms both editors, Textual 4.0.0, Rich 14.3.2 and all thirteen split
WLAN firmware packages; development tests pin those same UI library versions.
The reported ThinkPad L13 Yoga Gen 3 regression needs a physical retest with
this image. No per-model detection rule is used. Other host engines and full
Anaconda installations remain unverified.

The separate Secure Boot smoke of `out/vpn-maintenance/kiosk-live.iso` reached the
Textual keyboard screen inside Cage/Foot, and emitted `KIOSK_READY SB=enabled`.
It booted the read-only ISO as a raw USB disk with q35, enrolled OVMF keys and
software rendering, without guest keypresses. Artifacts, including `screen.png`,
are in `out/vpn-maintenance/smoke/`. The current ISO passed the signature gates
and ships byte-identical shim, GRUB and kernel binaries. The checksum and
validation record are beside the current ISO. Full installation remains untested.

The SSH update is covered by eight additional tests: complete key preservation
with beginning/end previews, checkbox defaults and keyboard toggling, back/edit
state, keyless lockout prevention and both emitted target policies. Fedora 44's
real `sshd -t` and `sshd -T` also confirmed both policies against its packaged
defaults, without starting an SSH listener. Results and four reviewed 80x12
screens are in `out/ssh-policy-check/`.

Textual's Pilot drives the real widgets through the complete inert wizard,
including cursor editing, UTF-8 and long-key paste, validation, page boundaries,
resize, back navigation, settings edits, secret clearing and Ctrl-C during
blocking work, with immediate invocation of the live shutdown hook. Anaconda
success, timeout and cancellation are checked with a simulated subprocess.
The destructive confirmation rejects an empty or mismatched path.
Seventeen earlier preview frames were captured and visually reviewed at 100x30
and 80x12 terminal sizes in `out/tui-review-final/` and `out/tui-review-compact/`.
The updated review, VPN and maintenance screens were also checked at 80x12 in
`out/server-policy-check/screens/`. Fedora 44 packages verified the generated
Kickstarts and Systemd units. Terminal tests exercised saving a profile and
Ctrl-C in both editors. End-to-end connections to a VPN server and a full
installation remain pending.
The installed command was also exercised in Fedora 44 with a disposable test
account. Fish printed the configuration hint on interactive login and produced
clean output for noninteractive commands. The SSH key helper read and replaced
the account's key file with the correct owner and mode 0600. The command ran
with only the target payload, without Textual.
These screenshot exports include a renderer-supplied terminal frame; the live kiosk
itself has no window frame or title bar.

Unit coverage also includes physical disk ancestry and fail-closed protection,
rendering and file permissions, keymap metadata and AZERTY/QWERTZ scoring,
network keyfile escaping and connectivity gating, battery read-back, AC-based
shutdown, failed NetworkManager profile cleanup, and screen geometry. These
checks do not prove a successful installation on physical hardware.

| Scenarios | Required acceptance evidence | Current result |
|---|---|---|
| T1 | Real UEFI/SB laptop, Wi-Fi, German, key SSH | Pending hardware |
| T2 | Legacy BIOS, Ethernet, no battery | Pending VM/hardware |
| T3 | Incomplete KS fails, Enter, poweroff | Pending full VM |
| T4 | Actual AZERTY/QWERTZ raw input | Scoring tested; device test pending |
| T5 | USB UK country hint and probe | Pending USB hardware |
| T6 | Modifier and 30s timeout fallback | Pending input device test |
| T7–T8 | Bad Wi-Fi password; Ethernet without carrier | Pending network hardware |
| T9 | Unsupported thresholds, firmware/TLP reporting | Logic tested; hardware pending |
| T10–T11 | 24h corridor; inadequate adapter under load | Pending battery hardware |
| T12–T14 | Ctrl-C, terminal close, SIGKILL: poweroff ≤5s | Pending timed VM tests |
| T15 | Running raw USB refused as target | Logic tested; booted VM pending |
| T16 | Adapter loss, guard shutdown below 20% | Logic tested; hardware pending |
| T17 | Multiple GitHub keys, individual/all | Pending integration |
| T18–T20 | Secure Boot raw USB, signatures, target boot | Current ISO signatures passed. VPN and maintenance build boot smoke passed. Full installation and target boot pending. |
| T21 | Every screen visually reviewed against §8 | Preview frames reviewed at 100x30 and 80x12; physical review pending |

Before release record the ISO hash, package manifest, firmware version/enrolled
CA, measured boot and poweroff times, full-install transcript, secret-log audit,
every screen capture, and battery results. The <60s USB boot, <25min installation,
8 GB target fit, and sustained battery corridor remain unmeasured requirements.
