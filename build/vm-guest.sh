#!/bin/bash
set -euo pipefail
umask 077
systemctl stop serial-getty@ttyS0.service
exec > >(tee /dev/ttyS0) 2>&1

finish() {
    status=$?
    trap - EXIT
    set +e
    if mountpoint -q /mnt/kiosk; then
        printf '%s\n' "$status" > /mnt/kiosk/exit-status
        sync -f /mnt/kiosk
        umount /mnt/kiosk
    fi
    systemctl --no-block poweroff
    exit "$status"
}
trap finish EXIT

mkdir -p /mnt/kiosk
udevadm settle --timeout=30
mount -L kiosk-work /mnt/kiosk
repos=(--repo=fedora --repo=updates --setopt=timeout=30 --setopt=retries=3
       --setopt=fedora.metalink= --setopt=updates.metalink=
       --setopt=fedora.baseurl=https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os/
       --setopt=updates.baseurl=https://dl.fedoraproject.org/pub/fedora/linux/updates/44/Everything/x86_64/)
dnf -y "${repos[@]}" --refresh install \
    livecd-tools-31.0-20.fc44 pykickstart \
    shim-x64 grub2-efi-x64 grub2-efi-x64-cdboot kernel-core \
    sbsigntools pesign xorriso mtools edk2-ovmf
# Verification compares the ISO boot binaries to these installed RPM payloads.
dnf -y "${repos[@]}" upgrade shim-x64 grub2-efi-x64 grub2-efi-x64-cdboot kernel-core
# Allow setfiles to write contexts from the ISO's policy in this disposable VM.
if selinuxenabled; then
    setenforce 0
fi
echo 'Composing the live image; compose.log will be exported after the VM stops'
KIOSK_RPM_CACHE=/mnt/kiosk/rpms bash /mnt/kiosk/build/build-live.sh /mnt/kiosk/result
