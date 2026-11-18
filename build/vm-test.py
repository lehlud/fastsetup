#!/usr/bin/python3
"""Interactive full install/boot acceptance harness; creates disposable disks."""
import argparse
from pathlib import Path
import shutil
import subprocess

from smoke import firmware

parser = argparse.ArgumentParser()
parser.add_argument('mode', choices=['install', 'boot'])
parser.add_argument('--iso', type=Path, default=Path('out/kiosk-live.iso'))
parser.add_argument('--directory', type=Path, default=Path('out/full-vm'))
parser.add_argument('--bios', action='store_true')
args = parser.parse_args()
args.directory.mkdir(parents=True, exist_ok=True)
disk = args.directory.resolve() / 'target.qcow2'
variables = args.directory.resolve() / 'OVMF_VARS.fd'
if args.mode == 'install':
    if disk.exists():
        parser.error('Target already exists; choose a new --directory for a fresh run')
    subprocess.run(['qemu-img', 'create', '-f', 'qcow2', str(disk), '32G'], check=True)
elif not disk.exists():
    parser.error('No target disk; run install first')
cmd = ['qemu-system-x86_64', '-machine', 'q35,smm=on', '-m', '4096', '-vga', 'std',
       '-nic', 'user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:2222-:22',
       '-drive', f'if=virtio,format=qcow2,file={disk}', '-no-reboot']
if Path('/dev/kvm').exists():
    cmd += ['-enable-kvm', '-cpu', 'host']
else:
    cmd += ['-accel', 'tcg']
if not args.bios:
    code, template = firmware()
    if not variables.exists():
        shutil.copyfile(template, variables)
    cmd += ['-global', 'driver=cfi.pflash01,property=secure,value=on',
            '-drive', f'if=pflash,format=raw,readonly=on,file={code}',
            '-drive', f'if=pflash,format=raw,file={variables}']
if args.mode == 'install':
    cmd += ['-device', 'qemu-xhci',
            '-drive', f'if=none,id=stick,format=raw,readonly=on,file={args.iso.resolve()}',
            '-device', 'usb-storage,drive=stick,bootindex=1']
subprocess.run(cmd, check=True)
