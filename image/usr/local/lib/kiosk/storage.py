import json
import os
from pathlib import Path
import re

from .common import Failure, log, output, run
from .ui import Back

MIN_DISK = 8_000_000_000


def flatten(nodes):
    for node in nodes:
        yield node
        yield from flatten(node.get('children', []))


def inventory():
    data = json.loads(output(['lsblk', '--json', '--bytes', '--paths', '--output',
                              'NAME,TYPE,SIZE,MODEL,TRAN,MOUNTPOINTS,PKNAME,PARTN,MAJ:MIN,SERIAL,DISK-SEQ,RO,FSTYPE']))
    result = {}
    for node in flatten(data['blockdevices']):
        node = dict(node, name=os.path.realpath(node['name']))
        if node.get('pkname'):
            node['pkname'] = os.path.realpath(node['pkname'])
        result[node['name']] = node
    return result


def physical(source, nodes, seen=None):
    seen = set() if seen is None else seen
    source = os.path.realpath(source.split('[')[0])
    if source in seen:
        return set()
    seen.add(source)
    node = nodes.get(source)
    if not node:
        return set()
    if node['type'] in ('disk', 'rom'):
        return {source}
    parents = set()
    if node.get('pkname'):
        parents.add(node['pkname'])
    for slave in Path('/sys/class/block', Path(source).name, 'slaves').glob('*'):
        parents.add('/dev/' + slave.name)
    if node['type'] == 'loop':
        backing = Path('/sys/class/block', Path(source).name, 'loop/backing_file')
        try:
            file = '/' + backing.read_text().strip().lstrip('/')
            parents.add(output(['findmnt', '-n', '-o', 'SOURCE', '--target', file]))
        except (OSError, Failure):
            pass
    result = set()
    for parent in parents:
        result.update(physical(parent, nodes, seen))
    return result


def live_disks(nodes):
    sources = []
    for mount in ('/', '/run/initramfs/live'):
        value = output(['findmnt', '-n', '-o', 'SOURCE', '--target', mount], check=False)
        if value:
            sources.append(value)
    mounts = json.loads(output(['findmnt', '--json', '--output', 'SOURCE,TARGET']))
    for node in flatten(mounts['filesystems']):
        if node.get('target', '').startswith('/run/initramfs/'):
            sources.append(node['source'])
    protected = set()
    for source in sources:
        protected.update(physical(source, nodes))
    if not protected:
        raise Failure('Live disk unresolved; use recovery to inspect storage')
    return protected


def is_target_disk(node):
    return (node['type'] == 'disk' and node['size'] >= MIN_DISK
            and not re.fullmatch(r'/dev/zram[0-9]+', node['name']))


def validate_disk(path, nodes, protected):
    path = os.path.realpath(path)
    node = nodes.get(path)
    if not node or not is_target_disk(node):
        raise Failure('Invalid disk; choose a whole disk of at least 8 GB')
    if path in protected:
        raise Failure('Live medium protected; choose another disk')
    if node.get('ro'):
        raise Failure('Disk is read-only; check the SSD or choose another disk')
    for child in nodes.values():
        parents = physical(child['name'], nodes)
        if path not in parents:
            continue
        if any(child.get('mountpoints') or []):
            raise Failure('Disk is mounted or swap is active; choose another disk')
        if parents - {path}:
            raise Failure('Disk is part of active multi-disk storage; choose another disk')
    return node


def fingerprint(node):
    return tuple(node.get(key) for key in ('name', 'size', 'maj:min', 'serial', 'disk-seq'))


def prepare_target(confirmed):
    # A hybrid image can leave independent ISO signatures on the whole disk
    # and on a partition. Refresh both views after each write before probing again.
    cleared = set()
    while True:
        nodes = inventory()
        disk = validate_disk(confirmed['name'], nodes, live_disks(nodes))
        if fingerprint(confirmed) != fingerprint(disk):
            raise Failure('Disk changed; select and confirm it again')
        partitions = [node for node in nodes.values()
                      if node['type'] == 'part' and node.get('pkname') == disk['name']]
        devices = [disk, *partitions]
        device = next((node for node in devices if node.get('fstype') == 'iso9660'), None)
        if device is None:
            return
        name = Path(device['name'])
        if name in cleared or device.get('ro'):
            raise Failure('Cannot clear old ISO signature; check the selected disk')
        suffix = f"-part{device['partn']}" if device['type'] == 'part' else ''
        target = Path('/dev/disk/by-diskseq', f"{disk['disk-seq']}{suffix}")
        try:
            if target.resolve(strict=True) != name:
                raise Failure('Disk changed; select and confirm it again')
        except (OSError, RuntimeError):
            raise Failure('Disk changed; select and confirm it again') from None
        try:
            run(['wipefs', '--all', '--types', 'iso9660', '--lock=yes', '--', str(target)], timeout=10)
            sysfs = [str(Path('/sys/class/block', Path(node['name']).name)) for node in devices]
            run(['udevadm', 'trigger', '--action=change', '--settle', '--', *sysfs], timeout=15)
        except Failure:
            raise Failure('Cannot clear old ISO signature; check the selected disk') from None
        cleared.add(name)
        log(f'storage: removed old ISO signature from {name}')


def log_target_layout(path):
    try:
        result = output(['lsblk', '--json', '--paths', '--output', 'NAME,TYPE,FSTYPE,PTTYPE,RO', '--', path],
                        timeout=4)
        devices = json.loads(result)['blockdevices']
    except (Failure, ValueError, KeyError):
        log('storage: target topology unavailable after failure')
        return
    for node in flatten(devices):
        fields = [str(node.get(key) or '-') for key in ('name', 'type', 'fstype', 'pttype')]
        if all(re.fullmatch(r'[A-Za-z0-9_./:-]+', field) for field in fields):
            log(f'storage: {fields[0]} type={fields[1]} fs={fields[2]} table={fields[3]} ro={int(bool(node.get("ro")))}')


class StorageBackend:
    @staticmethod
    def snapshot():
        nodes = inventory()
        return nodes, live_disks(nodes)

    validate = staticmethod(validate_disk)


async def choose_disk(ui, backend=None, previous=None):
    backend = backend or StorageBackend()
    while True:
        try:
            nodes, protected = await ui.busy('Disk', 'Inspecting disks', backend.snapshot)
            disks = [node for node in nodes.values() if is_target_disk(node)]
            labels = []
            for disk in disks:
                note = ' [live: locked]' if disk['name'] in protected else ' [read-only]' if disk.get('ro') else ''
                labels.append(f"{disk['name']} {disk['size'] / 1e9:.1f} GB{note} "
                              f"{disk.get('tran') or '-'} {disk.get('model') or '-'}")
            default = next((i for i, x in enumerate(disks) if x['name'] == (previous or {}).get('name')), None)
            if default is None:
                default = next((i for i, x in enumerate(disks) if x['name'] not in protected and not x.get('ro')),
                               len(disks))
            index = await ui.menu('Disk', labels + ['Device path'], context='Select the disk to erase.', default=default)
            if index < len(disks):
                path = disks[index]['name']
            else:
                try:
                    path = await ui.ask('Disk', 'Device', '/dev/sda')
                except Back:
                    continue
            return await ui.busy('Disk', 'Checking target', backend.validate, path, nodes, protected)
        except Failure as error:
            await ui.notice('Disk', str(error))
