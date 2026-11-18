#!/usr/bin/env python3
"""Run unit tests, and optionally a separate Secure Boot ISO smoke test."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

import build as builder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iso', type=Path, help='also boot this ISO with Secure Boot in the test container')
    parser.add_argument('--output', type=Path, help='empty smoke artifact directory (default: beside ISO, in smoke/)')
    parser.add_argument('--engine', choices=('docker', 'podman'))
    parser.add_argument('--timeout', type=int, default=900, help='smoke boot deadline in seconds (default: 900)')
    args = parser.parse_args(argv)
    try:
        import textual
    except ModuleNotFoundError:
        parser.error('Install test dependencies with: python3 -m pip install -r requirements-dev.txt')
    if not 30 <= args.timeout <= 3600:
        parser.error('--timeout must be between 30 and 3600 seconds')
    if args.output and not args.iso:
        parser.error('--output requires --iso')
    if args.iso and not args.iso.is_file():
        parser.error('--iso must name an existing ISO')
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(builder.ROOT / 'tests'), '-v'], check=True)
    if not args.iso:
        return 0
    engine = args.engine or next((name for name in ('docker', 'podman') if shutil.which(name)), None)
    if engine is None:
        raise builder.BuildError('Install Docker or Podman to run the ISO smoke test')
    # The build creates this tools image. The test does not compose another ISO.
    subprocess.run([engine, 'image', 'inspect', builder.BUILDER_IMAGE], stdout=subprocess.DEVNULL, check=True)
    iso = args.iso.expanduser().resolve()
    output = (args.output or iso.parent / 'smoke').expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise builder.BuildError('Use an empty output directory for this smoke test')
    output.mkdir(parents=True, exist_ok=True)
    uid, gid = builder.owner()
    name = 'fedora-kiosk-test-' + uuid.uuid4().hex[:16]
    with tempfile.TemporaryDirectory(prefix='.kiosk-source-', dir=output.parent) as temporary:
        snapshot = Path(temporary).resolve()
        builder.copy_source(builder.ROOT, snapshot)
        command = [engine, 'run', '--rm', '--name', name, '--platform', 'linux/amd64',
                   '--user', f'{uid}:{gid}', '--entrypoint', '/usr/bin/python3',
                   '--mount', builder.mount(snapshot, '/source', True),
                   '--mount', builder.mount(iso, '/input/kiosk-live.iso', True),
                   '--mount', builder.mount(output, '/output'),
                   '--env', f'KIOSK_SMOKE_TIMEOUT={args.timeout}', builder.BUILDER_IMAGE,
                   '/source/build/smoke.py', '/input/kiosk-live.iso', '/output']
        try:
            subprocess.run(command, check=True)
        finally:
            subprocess.run([engine, 'rm', '--force', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=30)
    print(f'Secure Boot smoke passed; artifacts: {output}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (builder.BuildError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f'Tests failed: {error}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
