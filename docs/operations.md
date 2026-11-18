# Operations

## Flash the USB

Verify the image from its output directory with `sha256sum -c kiosk-live.iso.sha256` on Linux or `shasum -a 256 -c kiosk-live.iso.sha256` on macOS.

**Writing the image erases the USB drive.** Identify the whole USB device by size and model, then unmount its partitions. You can use a disk image writer or write from the shell. On Linux, use `lsblk` to identify the device and replace `/dev/sdX` below.

```sh
sudo dd if=out/kiosk-live.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

On macOS, identify the USB drive with `diskutil list`, run `diskutil unmountDisk /dev/diskN` for that drive, then write to the matching raw device.

```sh
sudo dd if=out/kiosk-live.iso of=/dev/rdiskN bs=4m
sync
```

## Test a full installation

The interactive harness uses QEMU on the test machine and creates a disposable 32 GB target disk. On UEFI runs it also needs OVMF firmware with enrolled Secure Boot keys. The ISO build and the containerized boot smoke test supply their own tools.

```sh
python3 build/vm-test.py install
python3 build/vm-test.py boot
ssh -p 2222 server@127.0.0.1
```

Select `/dev/vda` in the VM and wait for power-off before running the boot command. Use the account name and key configured during installation. For legacy BIOS pass `--bios --directory out/bios-vm` to both VM commands. After login, check `sudo mokutil --sb-state` on UEFI and `systemctl status server-self-check.service` on the installed server.

## Recover the installer

Closing or interrupting the kiosk powers off the live machine. An installation failure lists the logs and waits for Enter before shutdown. The live logs disappear at power-off.

For a separate recovery boot, replace `systemd.unit=multi-user.target` with `systemd.unit=rescue.target` in the boot command line and add `systemd.mask=kiosk.service`. The live root password is locked, so `rd.break` or `init=/bin/bash` may be needed for a root shell. A frozen machine needs a physical reset.

## Battery and VPN

Use `sudo server-config battery --mode full` to charge to 100% and `sudo server-config battery --mode normal` to restore the corridor. Use `--start`, `--stop`, and `--shutdown-at` to change the thresholds. `--mode off` lifts charge limits while the UPS guard remains independently configurable. If the hardware cannot verify a charge limit, check firmware settings. `AC online: 1` with `Discharging` can indicate an undersized adapter or a firmware discharge setting.

OpenVPN requires a TUN client profile with inline credentials and server verification. Profile scripts, plugins, and external credential files are rejected. DNS settings pushed by OpenVPN do not change the target resolver. Use the local console to change VPN or network settings when those connections carry your SSH session.

## Check laptop power policy

The firmware can turn off the laptop display when the lid closes. In an SSH session, run `while true; date; sleep 1; end` in Fish and check that timestamps keep arriving with the lid closed. Repeat on AC and battery while the battery is above the UPS reserve.

```sh
systemd-analyze cat-config systemd/logind.conf
systemd-analyze cat-config systemd/sleep.conf
systemctl is-enabled sleep.target suspend.target hibernate.target \
  hybrid-sleep.target suspend-then-hibernate.target
```

Check for `ignore` on all three lid settings and `no` on all four `Allow…` sleep settings. All five targets should report `masked`.

## Run guests on the installed server

For a guest disk configured for a serial console, use q35 with KVM and the host CPU.

```sh
qemu-system-x86_64 -M q35 -enable-kvm -cpu host -m 2048 \
  -drive file=guest.qcow2,format=qcow2,if=virtio \
  -nic user,model=virtio-net-pci -display none -serial mon:stdio
```

For `microvm`, provide a guest kernel and initramfs with serial console, virtio-mmio, and the guest filesystem driver.

```sh
qemu-system-x86_64 -M microvm -enable-kvm -cpu host -m 1024 \
  -nodefaults -no-user-config -display none -serial mon:stdio \
  -kernel ./bzImage -initrd ./initramfs.img \
  -append 'console=ttyS0 root=/dev/vda rw' \
  -drive if=none,id=rootfs,file=guest.raw,format=raw \
  -device virtio-blk-device,drive=rootfs \
  -netdev user,id=net0 -device virtio-net-device,netdev=net0
```

## Extend Secure Boot

The default chain uses Fedora shim, GRUB, kernel, and modules. For an out-of-tree module, sign it with a local akmods or DKMS key, import the certificate with `mokutil --import`, then reboot and enroll it in MokManager. Use an alphanumeric enrollment password because MokManager uses the firmware keyboard layout, usually US.

For custom boot binaries, `sbctl` can help sign EFI binaries while retaining Fedora shim and enrolling a compatible Machine Owner Key. Another option is enrolling your own PK, KEK, and db in firmware Setup Mode and signing the entire chosen chain. Back up the vendor keys and verify recovery before changing firmware trust. This is a per-machine operation and a mistake can leave the device unbootable.
