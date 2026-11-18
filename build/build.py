#!/usr/bin/env python3
"""Build in a Fedora container on Linux/macOS; no host Fedora or QEMU needed."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ('build', 'image')
BUILDER_IMAGE = 'localhost/fedora-kiosk-builder:44'
VM_BUILDER_IMAGE = 'localhost/fedora-kiosk-vm-builder:44'


class BuildError(Exception):
    pass


def copy_source(root, destination):
    """Snapshot only build inputs, without host credentials, Git data or outputs."""
    for name in SOURCE_DIRS:
        for path in sorted((root / name).rglob('*')):
            if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                continue
            if path.is_symlink():
                raise BuildError(f'Source symlinks are unsupported: {path.relative_to(root)}')
            if path.is_file():
                target = destination / path.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    for name in ('README.md', '.gitignore', '.dockerignore'):
        shutil.copy2(root / name, destination / name)


def mount(source, target, readonly=False):
    # --mount uses CSV syntax; shell quoting does not escape its commas.
    text = io.StringIO()
    fields = ['type=bind', f'source={source}', f'target={target}']
    if readonly:
        fields.append('readonly')
    csv.writer(text, lineterminator='').writerow(fields)
    return text.getvalue()


def run_command(engine, source, output, uid, gid, name):
    return [engine, 'run', '--rm', '--name', name, '--privileged', '--userns=host',
            '--user', '0:0', '--platform', 'linux/amd64',
            '--mount', mount(source, '/source', True),
            '--mount', mount(output, '/output'),
            # An anonymous volume keeps loop files on a Linux filesystem even
            # on macOS. --rm also removes the anonymous volume on exit.
            '--volume', '/work',
            '--volume', 'fedora-kiosk-rpms-44-amd64:/var/cache/kiosk-live',
            '--env', 'KIOSK_RPM_CACHE=/var/cache/kiosk-live',
            '--env', f'KIOSK_OUTPUT_UID={uid}', '--env', f'KIOSK_OUTPUT_GID={gid}',
            BUILDER_IMAGE]


def vm_commands(engine, source, output, name):
    # Copy over the engine connection, including remote Podman machines whose
    # users and filesystems differ from those of the calling process.
    return dict(create=[engine, 'create', '--name', name, '--cap-drop=all',
                        '--security-opt', 'no-new-privileges', '--volume', '/work', VM_BUILDER_IMAGE],
                upload=[engine, 'cp', f'{source}/.', f'{name}:/source'],
                start=[engine, 'start', '--attach', name],
                download=[engine, 'cp', f'{name}:/output/.', str(output)])


def engine_is_rootless(engine):
    if engine == 'podman':
        query = [engine, 'info', '--format', '{{.Host.Security.Rootless}}']
    else:
        query = [engine, 'info', '--format', '{{json .SecurityOptions}}']
    try:
        info = subprocess.check_output(query, stderr=subprocess.STDOUT, timeout=30).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise BuildError(f'{engine} unavailable; start its Linux engine and retry') from None
    return info.strip().lower() == 'true' if engine == 'podman' else 'rootless' in info.lower()


def owner():
    # Preserve the invoking user's ownership with `sudo ... --engine podman`.
    uid = os.environ.get('SUDO_UID', str(os.getuid()))
    gid = os.environ.get('SUDO_GID', str(os.getgid()))
    if not uid.isdigit() or not gid.isdigit():
        raise BuildError('Invalid invoking user ID')
    return uid, gid


def verify_copy(output):
    iso = output / 'kiosk-live.iso'
    checksum = output / 'kiosk-live.iso.sha256'
    if not iso.is_file() or not checksum.is_file():
        raise BuildError('Build returned no verified ISO; inspect builder.log')
    fields = checksum.read_text().split()
    if len(fields) != 2 or fields[1] != 'kiosk-live.iso':
        raise BuildError('Unexpected ISO checksum format; inspect builder.log')
    digest = hashlib.sha256()
    with iso.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != fields[0]:
        raise BuildError('Returned ISO checksum mismatch; inspect builder.log')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=ROOT / 'out',
                        help='empty artifact directory (default: out)')
    parser.add_argument('--engine', choices=('docker', 'podman'),
                        help='container CLI (default: first available, Docker preferred)')
    parser.add_argument('--vm', action='store_true', help='use a disposable build VM (automatic with rootless engines)')
    parser.add_argument('--dry-run', action='store_true', help='show commands without downloading or running anything')
    args = parser.parse_args(argv)
    if platform.system() not in ('Linux', 'Darwin'):
        raise BuildError('Supported build hosts are Linux and macOS; Windows is out of scope')
    engine = args.engine or next((name for name in ('docker', 'podman') if shutil.which(name)), None)
    if engine is None:
        if args.dry_run:
            engine = 'docker'
        else:
            raise BuildError('Install Docker or rootful Podman, then rerun this command')
    output = args.output.expanduser().resolve()
    if output == ROOT or any(output.is_relative_to(ROOT / name) for name in SOURCE_DIRS + ('tests',)):
        raise BuildError('Choose an output directory outside build/, image/ and tests/')
    uid, gid = owner()
    if not args.dry_run and output.exists() and any(output.iterdir()):
        raise BuildError('Use an empty output directory for each build')
    rootless = not args.dry_run and engine_is_rootless(engine)
    use_vm = args.vm or rootless
    image = VM_BUILDER_IMAGE if use_vm else BUILDER_IMAGE
    containerfile = 'Containerfile.vm' if use_vm else 'Containerfile'
    architecture = [] if use_vm else ['--platform', 'linux/amd64']
    build = [engine, 'build', '--pull', *architecture, '--tag', image,
             '--file', str(ROOT / 'build' / containerfile), str(ROOT)]

    if args.dry_run:
        snapshot, name = '<source snapshot>', '<unique build name>'
        commands = vm_commands(engine, snapshot, output, name) if use_vm else {
            'run': run_command(engine, snapshot, output, uid, gid, name)}
        print(json.dumps(dict(build=build, **commands), indent=2))
        return 0
    output.mkdir(parents=True, exist_ok=True)
    print('Pulling the Fedora 44 VM builder and its tools' if use_vm else
          'Pulling the Fedora 44 builder and its tools', flush=True)
    subprocess.run(build, check=True)
    print(f'Building the ISO; follow {output / "builder.log"}', flush=True)
    name = 'fedora-kiosk-build-' + uuid.uuid4().hex[:16]
    # Keep the snapshot in the engine-shared tree. macOS Podman machines need
    # not share the operating system's default temporary directory.
    with tempfile.TemporaryDirectory(prefix='.kiosk-source-', dir=output.parent) as temporary:
        snapshot = Path(temporary).resolve()
        copy_source(ROOT, snapshot)
        with (output / 'builder.log').open('wb') as log:
            capture = dict(stdout=log, stderr=subprocess.STDOUT)
            try:
                if use_vm:
                    commands = vm_commands(engine, snapshot, output, name)
                    for step in ('create', 'upload'):
                        subprocess.run(commands[step], check=True, **capture)
                    result = subprocess.run(commands['start'], **capture)
                    exported = subprocess.run(commands['download'], **capture)
                    if exported.returncode and not result.returncode:
                        result = exported
                else:
                    result = subprocess.run(run_command(engine, snapshot, output, uid, gid, name), **capture)
            except subprocess.CalledProcessError as error:
                raise BuildError(f'Could not prepare the build container; inspect {log.name}') from error
            finally:
                # Remove only this invocation's container after interruption.
                # It may already have been removed by --rm.
                subprocess.run([engine, 'rm', '--force', '--volumes', name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False, timeout=30)
        if result.returncode:
            from collections import deque
            for diagnostic in (output / 'compose.log', output / 'builder.log'):
                try:
                    with diagnostic.open(errors='replace') as stream:
                        lines = deque(stream, maxlen=12)
                    if lines:
                        print(f'{diagnostic.name}:', file=sys.stderr)
                        print(''.join(line[:500].rstrip('\n') + '\n' for line in lines), file=sys.stderr)
                except OSError:
                    pass
            raise BuildError(f'Container build failed; inspect {output / "builder.log"}')
    verify_copy(output)
    print(output / 'kiosk-live.iso')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (BuildError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f'Build failed: {error}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('Build interrupted', file=sys.stderr)
        sys.exit(130)
