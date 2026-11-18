#!/usr/bin/env python3
"""Preview the installer with dummy hardware; no installation or power-off."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'image/usr/local/lib'))
try:
    from kiosk import common
    from kiosk import keyboard, install, preview
    from kiosk.ui import KioskApp
except ModuleNotFoundError as error:
    if error.name in ('textual', 'rich'):
        raise SystemExit('Install the preview dependencies: python3 -m pip install -r requirements-dev.txt') from None
    raise

if __name__ == '__main__':
    common.SHARE = keyboard.SHARE = preview.SHARE = ROOT / 'image/usr/share/kiosk'
    services = preview.DemoServices()
    app = KioskApp(lambda ui: install.wizard(ui, services))
    app.run(mouse=False)
