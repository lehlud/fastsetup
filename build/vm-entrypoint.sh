#!/bin/bash
set -euo pipefail
umask 077
work=$(mktemp -d /work/kiosk-vm.XXXXXXXX)
publication=
trap 'rm -rf -- "$work"; if [[ -n $publication ]]; then rm -rf -- "$publication"; fi' EXIT
cd "$work"

# Cloud-init starts the build in the guest. Use a separate disk for compose
# files and export so that the container can copy them without any mounts.
mkdir seed
cp /source/build/vm-guest.sh seed/user-data
printf 'instance-id: kiosk-builder\nlocal-hostname: kiosk-builder\n' > seed/meta-data
xorriso -as mkisofs -quiet -V cidata -J -r -o seed.iso seed
truncate -s 40G work.img
mke2fs -q -t ext4 -L kiosk-work -d /source -F work.img
qemu-img create -q -f qcow2 -F qcow2 -b /var/lib/kiosk-fedora.qcow2 fedora.qcow2 16G

vm_status=0
timeout 4h qemu-system-x86_64 -machine q35 -accel tcg,thread=multi -cpu max -m 4096 -smp 4 \
    -display none -serial stdio -monitor none -no-reboot \
    -drive if=virtio,format=qcow2,file=fedora.qcow2 \
    -drive if=virtio,format=raw,file=work.img \
    -drive media=cdrom,format=raw,readonly=on,file=seed.iso \
    -nic user,model=virtio-net-pci || vm_status=$?

debugfs -R 'ls -p /result' work.img > files.txt
for file in compose.log build-host.txt kiosk-live.flattened.ks; do
    if grep -Fq "/$file/" files.txt; then
        debugfs -R "dump /result/$file /output/$file" work.img
    fi
done
if grep -Fq '/verification/' files.txt; then
    debugfs -R 'rdump /result/verification /output' work.img
fi

if ((vm_status != 0)) || [[ $(debugfs -R 'cat /exit-status' work.img 2>/dev/null) != 0 ]]; then
    echo "Build VM failed (QEMU status $vm_status); inspect builder.log and compose.log" >&2
    exit 1
fi
publication=$(mktemp -d /output/.kiosk-export.XXXXXXXX)
for file in kiosk-live.iso kiosk-live.iso.sha256; do
    debugfs -R "dump /result/$file $publication/$file" work.img
done
(cd "$publication" && sha256sum -c kiosk-live.iso.sha256)
for file in kiosk-live.iso kiosk-live.iso.sha256; do
    mv "$publication/$file" "/output/$file"
done
