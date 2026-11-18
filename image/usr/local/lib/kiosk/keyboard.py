import csv
import fcntl
import json
import os
from pathlib import Path
import select
import struct
import time

from .common import Failure, RUN, SHARE, output, poweroff, private_write
from .ui import Back

MODIFIERS = {29, 42, 54, 56, 97, 100, 125, 126}
IGNORED = MODIFIERS | {58, 69, 70}
EVENT = struct.Struct('@llHHi')
EVIOCGRAB = 0x40044590


def layouts(share=SHARE):
    result = []
    for line in (share / 'layouts.tsv').read_text().splitlines():
        if line and not line.startswith('#'):
            ident, layout, variant, console, label = line.split('\t')
            result.append(dict(id=ident, layout=layout, variant='' if variant == '-' else variant,
                               vconsole=console, label=label))
    return result


def load_table(share=SHARE):
    table = {}
    with (share / 'keymap-table.tsv').open() as stream:
        for row in csv.DictReader(stream, delimiter='\t'):
            table.setdefault(row['layout'], {})[int(row['evdev'])] = row['character']
    return table


def load_digits(share=SHARE):
    digits = {}
    with (share / 'keymap-table.tsv').open() as stream:
        for row in csv.DictReader(stream, delimiter='\t'):
            if row.get('printed_digit'):
                digits.setdefault(row['layout'], {})[int(row['evdev'])] = row['printed_digit']
    return digits


def distance(left, right):
    row = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        new = [i]
        for j, other in enumerate(right, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (char != other)))
        row = new
    return row[-1]


def rank(table, rounds, digits=None):
    digits = digits or {}
    return sorted((sum(distance(''.join(digits.get(ident, {}).get(code, mapping.get(code, ''))
                                       if index < len(target) and target[index].isdigit()
                                       else mapping.get(code, '') for index, code in enumerate(codes)), target)
                       for target, codes in rounds), ident)
                  for ident, mapping in table.items())


def devices(root=Path('/sys/class/input')):
    found = []
    for node in sorted(root.glob('event*')):
        try:
            bits = int((node / 'device/capabilities/key').read_text().replace(' ', '').strip(), 16)
            if not all(bits & (1 << code) for code in (16, 30, 28, 57)):
                continue
            path = str((node / 'device').resolve())
            kind = 'internal' if '/serio' in path or 'i8042' in path else 'external'
            # USB HID country is in the HID descriptor, not report_descriptor.
            # Read the descriptor collection from the ancestor USB device.
            country = None
            for parent in (node / 'device').resolve().parents:
                descriptor = parent / 'descriptors'
                if not descriptor.exists():
                    continue
                data = descriptor.read_bytes()
                offset = 0
                while offset + 2 <= len(data) and data[offset] >= 2:
                    size = data[offset]
                    if data[offset + 1] == 0x21 and size >= 5:
                        country = data[offset + 4]
                        break
                    offset += size
                break
            found.append({'path': '/dev/input/' + node.name, 'kind': kind,
                          'name': (node / 'device/name').read_text().strip(), 'country': country})
        except (OSError, ValueError):
            continue
    return found


def read_round(path, count, timeout=30):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    grabbed = False
    try:
        fcntl.ioctl(fd, EVIOCGRAB, 1)
        grabbed = True
        held = bytearray(96)
        fcntl.ioctl(fd, 0x80604518, held, True)  # EVIOCGKEY(96)
        modified = any(held[c // 8] & (1 << (c % 8)) for c in MODIFIERS)
        pressed = {code for code in MODIFIERS if held[code // 8] & (1 << (code % 8))}
        # Drop queued menu keystrokes before sampling the round.
        while select.select([fd], [], [], 0)[0]:
            os.read(fd, EVENT.size * 64)
        codes = []
        deadline = time.monotonic() + timeout
        while len(codes) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise Failure('Probe timed out; select a layout')
            data = os.read(fd, EVENT.size * 64)
            for offset in range(0, len(data), EVENT.size):
                _, _, kind, code, value = EVENT.unpack_from(data, offset)
                if kind != 1:
                    continue
                if value == 0:
                    pressed.discard(code)
                elif value == 1:
                    pressed.add(code)
                if value != 1:
                    continue
                if code == 46 and pressed & {29, 97}:  # Ctrl-C is grabbed too.
                    poweroff()
                if code == 111 and pressed & {29, 97} and pressed & {56, 100}:
                    poweroff()  # Ctrl-Alt-Del must work during the grab as well.
                if code in MODIFIERS:
                    modified = True
                if code not in IGNORED and len(codes) < count:
                    codes.append(code)
        return None if modified else codes
    finally:
        if grabbed:
            fcntl.ioctl(fd, EVIOCGRAB, 0)
        os.close(fd)


async def shortlist(ui, options, country=None):
    # Hardware is only a hint. Never filter out a printed layout.
    hint = {7: 'de', 8: 'fr', 32: 'gb', 33: 'us'}.get(country)
    if hint is None:
        # Firmware language is a weak ordering hint, never a decision.
        for path in Path('/sys/firmware/efi/efivars').glob('PlatformLang-*'):
            try:
                language = path.read_bytes()[4:].decode().strip('\x00').lower()
                hint = {'en-gb': 'gb', 'en-us': 'us', 'de-at': 'at', 'de-ch': 'ch-de',
                        'fr-ch': 'ch-fr'}.get(language, language.split('-')[0])
            except (OSError, UnicodeError):
                pass
    options = sorted(options, key=lambda x: (x['id'] != hint, x['id'] != 'us'))
    return options[await ui.menu('Keyboard', [x['label'] for x in options],
                                 context='Select the layout printed on the keycaps.')]


async def identify_layout(ui, options):
    choice = await ui.menu('Keyboard', ['Identify keycaps', 'Choose layout'], back=False)
    found, rounds, selected = devices(), [], None
    device = None
    if choice == 0 and found:
        device = found[0] if len(found) == 1 else found[await ui.menu(
            'Keyboard', [f"{x['kind']}  {x['name']}" for x in found],
            context='Select the keyboard to identify.')]
        table = load_table()
        try:
            for target in ('yzqwam,1', 'aqzymw;2'):
                codes = await ui.busy('Keyboard', 'Reading keys', read_round, device['path'], len(target),
                                      context='Type the keys labeled with these characters.', rows=[target])
                if codes is None:
                    continue  # A discarded round consumes one of the two attempts.
                rounds.append((target, codes))
            if rounds:
                scores = rank(table, rounds, load_digits())
                if scores[0][0] <= 1 and scores[0][0] < scores[1][0]:
                    selected = next(x for x in options if x['id'] == scores[0][1])
        except (OSError, Failure):
            pass
    if selected is None:
        selected = await shortlist(ui, options, device['country'] if device else None)
    return selected


async def probe(ui):
    options = layouts()
    while True:
        try:
            selected = await identify_layout(ui, options)
            break
        except Back:
            continue
    selected.update(json.loads((SHARE / 'keymap-metadata.json').read_text())[selected['id']])
    RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_write(RUN / 'layout', json.dumps(selected))
    private_write(RUN / 'vconsole-keymap', selected['vconsole'] + '\n')


async def confirm(ui):
    selected = json.loads((RUN / 'layout').read_text())
    # Use actual unshifted glyphs from this map, including non-Latin scripts.
    mapping = load_table()[selected['id']]
    target = ''.join(mapping.get(code, '') for code in (16, 30, 44, 21))
    if not target or len(target) < 3:
        target = '1234'
    ui.screen('Keyboard', [target], context='Type this text and check the visible echo.')
    await ui.read('Echo', '', valid=lambda value: value == target,
                  error='Echo differs; retry or restart the stick to change layout', back=False)
    return selected
