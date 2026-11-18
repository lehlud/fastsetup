import json
import os
from pathlib import Path

from .common import Failure, output
from .ui import Back

MIN_DISK = 8_000_000_000


def flatten(nodes):
    for node in nodes:
        yield node
        yield from flatten(node.get('children', []))


def inventory():
    data = json.loads(output(['lsblk', '--json', '--bytes', '--paths', '--output',
                              'NAME,TYPE,SIZE,MODEL,TRAN,MOUNTPOINTS,PKNAME,MAJ:MIN,SERIAL']))
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


def validate_disk(path, nodes, protected):
    path = os.path.realpath(path)
    node = nodes.get(path)
    if not node or node['type'] != 'disk' or node['size'] < MIN_DISK:
        raise Failure('Invalid disk; choose a whole disk of at least 8 GB')
    if path in protected:
        raise Failure('Live medium protected; choose another disk')
    for child in nodes.values():
        if path in physical(child['name'], nodes) and any(child.get('mountpoints') or []):
            raise Failure('Disk is mounted or swap is active; choose another disk')
    return node


def fingerprint(node):
    return tuple(node.get(key) for key in ('name', 'size', 'maj:min', 'serial'))


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
            disks = [x for x in nodes.values() if x['type'] == 'disk' and x['size'] >= MIN_DISK]
            labels = [f"{x['name']} {x['size'] / 1e9:.1f} GB {x.get('tran') or '-'} {x.get('model') or '-'}" +
                      (' [live: locked]' if x['name'] in protected else '') for x in disks]
            default = next((i for i, x in enumerate(disks) if x['name'] == (previous or {}).get('name')), None)
            if default is None:
                default = next((i for i, x in enumerate(disks) if x['name'] not in protected), len(disks))
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
