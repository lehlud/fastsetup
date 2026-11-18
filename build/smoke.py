#!/usr/bin/python3
"""Boot ISO as a raw USB disk under Secure Boot, without GPU acceleration."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time


def firmware(root=Path('/usr/share/qemu/firmware')):
    for path in sorted(root.glob('*.json')):
        data = json.loads(path.read_text())
        if {'secure-boot', 'enrolled-keys'} <= set(data.get('features', [])):
            mapping = data.get('mapping', {})
            if (mapping.get('device') == 'flash' and 'x86_64' in json.dumps(data.get('targets'))
                    and mapping.get('executable', {}).get('format') == 'raw'
                    and mapping.get('nvram-template', {}).get('format') == 'raw'):
                return mapping['executable']['filename'], mapping['nvram-template']['filename']
    raise RuntimeError('No raw OVMF firmware descriptor with secure-boot and enrolled-keys')


def capture_screen(monitor, destination):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(monitor))
        with connection.makefile('rb') as stream:
            if not json.loads(stream.readline()).get('QMP'):
                raise RuntimeError('Invalid QMP greeting')
            for request in ({'execute': 'qmp_capabilities'},
                            {'execute': 'screendump', 'arguments': {'filename': str(destination), 'format': 'png'}}):
                connection.sendall(json.dumps(request).encode() + b'\n')
                while True:
                    response = json.loads(stream.readline())
                    if 'error' in response:
                        raise RuntimeError(str(response['error']))
                    if 'return' in response:
                        break


def smoke(iso, directory):
    directory.mkdir(parents=True, exist_ok=True)
    code, template = firmware()
    variables = directory / 'OVMF_VARS.fd'
    shutil.copyfile(template, variables)
    report = subprocess.check_output(['virt-fw-vars', '--input', str(variables), '--print'])
    (directory / 'enrolled-vars.txt').write_bytes(report)
    marker = directory / 'ready.txt'
    marker.unlink(missing_ok=True)
    monitor_dir = tempfile.TemporaryDirectory(prefix='kiosk-qmp-')
    monitor = Path(monitor_dir.name) / 'monitor.sock'
    command = ['qemu-system-x86_64', '-machine', 'q35,smm=on', '-accel', 'tcg', '-cpu', 'max', '-m', '4096',
               '-global', 'driver=cfi.pflash01,property=secure,value=on',
               '-drive', f'if=pflash,format=raw,readonly=on,file={code}',
               '-drive', f'if=pflash,format=raw,file={variables}',
               '-device', 'qemu-xhci', '-drive', f'if=none,id=stick,format=raw,readonly=on,file={iso}',
               '-device', 'usb-storage,drive=stick,bootindex=1', '-vga', 'std', '-display', 'none',
               '-serial', f'file:{directory / "serial.log"}', '-device', 'virtio-serial-pci',
               '-chardev', f'file,id=proof,path={marker}',
               '-device', 'virtserialport,chardev=proof,name=org.kiosk.smoke', '-no-reboot',
               '-qmp', f'unix:{monitor},server=on,wait=off',
               '-netdev', 'user,id=net0', '-device', 'virtio-net-pci,netdev=net0']
    (directory / 'command.json').write_text(json.dumps(command, indent=2))
    with (directory / 'qemu.log').open('wb') as log:
        proc = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + int(os.environ.get('KIOSK_SMOKE_TIMEOUT', '180'))
            next_capture = time.monotonic() + 30
            while proc.poll() is None and time.monotonic() < deadline:
                if marker.exists() and 'KIOSK_READY SB=enabled' in marker.read_text():
                    return
                if time.monotonic() >= next_capture:
                    try:
                        capture_screen(monitor, directory / 'screen.png')
                    except (OSError, ValueError, RuntimeError) as error:
                        (directory / 'screen-error.txt').write_text(str(error) + '\n')
                    next_capture = time.monotonic() + 30
                time.sleep(0.5)
            raise RuntimeError('Secure Boot kiosk smoke failed; inspect VM artifacts')
        finally:
            if proc.poll() is None:
                try:
                    capture_screen(monitor, directory / 'screen.png')
                except (OSError, ValueError, RuntimeError) as error:
                    (directory / 'screen-error.txt').write_text(str(error) + '\n')
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            monitor_dir.cleanup()


if __name__ == '__main__':
    smoke(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
