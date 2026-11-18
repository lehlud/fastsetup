#!/usr/bin/python3
"""Flatten local source into a self-contained livecd-creator kickstart."""
import base64
import io
import json
from pathlib import Path
import tarfile
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGET = [
    'usr/local/lib/kiosk/__init__.py', 'usr/local/lib/kiosk/common.py',
    'usr/local/lib/kiosk/battery.py', 'usr/local/bin/battery-limits',
    'usr/local/lib/kiosk/vpn.py', 'usr/local/lib/kiosk/target_policy.py',
    'usr/local/lib/kiosk/server_config.py', 'usr/local/bin/server-config',
    'etc/fish/conf.d/80-server-config.fish',
    'usr/local/bin/server-self-check', 'etc/battery-limits.conf',
    'etc/systemd/system/battery-limits.service', 'etc/systemd/system/battery-limits.timer',
    'etc/systemd/system/battery-guard.service', 'etc/systemd/system/battery-guard.timer',
    'etc/systemd/system/server-self-check.service', 'etc/udev/rules.d/80-battery-limits.rules',
    'etc/systemd/logind.conf.d/server-power.conf',
]


def encoded(data):
    return base64.b64encode(data).decode()


def prepare(destination):
    manifest = {}
    for name in TARGET:
        path = ROOT / 'image' / name
        manifest[name] = dict(data=encoded(path.read_bytes()), mode=0o755 if '/bin/' in name else 0o644)
    payload = encoded(json.dumps(manifest, sort_keys=True).encode()).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        for path in sorted((ROOT / 'image').rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            name = str(path.relative_to(ROOT / 'image'))
            info = tarfile.TarInfo(name)
            data = path.read_bytes()
            info.size, info.mtime, info.uid, info.gid = len(data), 0, 0, 0
            info.mode = 0o755 if '/bin/' in name else 0o644
            tar.addfile(info, io.BytesIO(data))
        for name, data in [('usr/share/kiosk/target-payload.b64', payload),
                           ('usr/share/kiosk/generate-keymaps.py', (ROOT / 'build/generate-keymaps.py').read_bytes())]:
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    # GNU tar's implicit-parent extraction fails with ENOSYS on ext4 under
    # some x86_64 container emulators. Python's extractor works there and also
    # rejects archive members that escape the image root.
    install = ('%post --nochroot --interpreter=/usr/bin/python3 --erroronfail\n'
               'import base64, io, os, tarfile\n'
               'KIOSK_PAYLOAD = ' + repr(encoded(archive.getvalue())) + '\n'
               'with tarfile.open(fileobj=io.BytesIO(base64.b64decode(KIOSK_PAYLOAD)), mode="r:gz") as archive:\n'
               '    archive.extractall(os.environ["INSTALL_ROOT"], filter="data")\n%end')
    template = (ROOT / 'build/kiosk-live.ks').read_text()
    marker = '%post --nochroot --erroronfail\n# build-live.sh appends an embedded payload before this post runs.\n%end'
    if template.count(marker) != 1:
        raise ValueError('Live payload insertion point missing')
    Path(destination).write_text(template.replace(marker, install))


if __name__ == '__main__':
    prepare(sys.argv[1])
