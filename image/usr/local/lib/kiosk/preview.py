"""Exercise the production wizard with an inert backend on Linux or macOS."""
import base64

from . import install, keyboard, network, storage, vpn
from .common import SHARE


class DemoNetwork(network.NetworkBackend):
    devices = staticmethod(lambda: (['wlan0'], ['eth0']))
    hard_blocked = staticmethod(lambda: False)
    carrier = staticmethod(lambda device: True)
    scan = staticmethod(lambda device: [['Home network', '92', 'WPA2'], ['Guest', '68', '--']])

    @staticmethod
    def activate(device, ssid='', psk='', security='wpa-psk', hidden=False):
        _, profile = network.profile(device, ssid, psk, security, hidden)
        return dict(device=device, ssid=ssid, psk=psk, security=security, hidden=hidden,
                    profile=profile, path='Wi-Fi' if ssid else 'Ethernet')


class DemoStorage(storage.StorageBackend):
    @staticmethod
    def snapshot():
        nodes = {name: dict(name=name, type='disk', size=size, tran=bus, model=model,
                            mountpoints=[], **{'maj:min': ident, 'serial': ident})
                 for name, size, bus, model, ident in [
                     ('/dev/sda', 16_000_000_000, 'usb', 'Installer USB', '8:0'),
                     ('/dev/nvme0n1', 512_000_000_000, 'nvme', 'Example SSD', '259:0')]}
        return nodes, {'/dev/sda'}


class DemoServices(install.LiveServices):
    """Only dummy data: no commands, network, disks, hashing, or power-off."""
    installed = False

    @staticmethod
    async def keyboard(ui):
        choices = keyboard.layouts(SHARE)
        selected = choices[await ui.menu('Keyboard', [x['label'] for x in choices], back=False)]
        return selected

    @staticmethod
    async def identity(ui, previous=None, draft=None):
        return await install.identity(ui, previous, draft,
                                      defaults=lambda: ('calm-fox', {'Europe/Berlin', 'UTC', 'America/New_York', 'Asia/Tokyo'}))

    @staticmethod
    async def network(ui):
        return await network.configure(ui, DemoNetwork())

    @staticmethod
    async def disk(ui, previous=None):
        return await storage.choose_disk(ui, DemoStorage(), previous)

    @staticmethod
    async def account(ui, previous=None, draft=None):
        return await install.account(ui, previous, draft, hasher=lambda password: '$6$preview$notarealhash')

    @staticmethod
    async def keys(ui):
        return await install.ssh_keys(ui,
            fetch=lambda user: ['ssh-ed25519 PREVIEW-KEY-ONE', 'ssh-ed25519 PREVIEW-KEY-TWO'],
            validate=lambda key: key)

    @staticmethod
    async def vpn(ui, previous=None):
        def profile(url):
            return (b'[Interface]\nAddress = 192.0.2.2/24\nPrivateKey = ' + base64.b64encode(b'A' * 32) + b'\n'
                    b'[Peer]\nEndpoint = vpn.example.test:51820\nAllowedIPs = 192.0.2.0/24\nPublicKey = '
                    + base64.b64encode(b'B' * 32) + b'\n')
        async def editor(ui, choice):
            return profile('')
        return await vpn.configure(ui, previous, fetch=profile, edit=editor)

    @staticmethod
    async def battery(ui):
        return '45-55%, verified (preview)'

    boot_state = staticmethod(lambda: 'enabled (preview)')
    connected = staticmethod(lambda net: True)

    async def commit(self, ui, data):
        self.installed = True
        await ui.notice('Preview complete', 'No installation was performed', warning=True)
        return True
