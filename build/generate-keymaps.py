#!/usr/bin/python3
"""Build the level-zero evdev matrix using the image's own libxkbcommon."""
import ctypes as C
import ctypes.util
import csv
import hashlib
import json
from pathlib import Path
import sys


class Names(C.Structure):
    _fields_ = [(name, C.c_char_p) for name in ('rules', 'model', 'layout', 'variant', 'options')]


def generate(share, check_console=False):
    lib = C.CDLL(ctypes.util.find_library('xkbcommon'))
    signatures = {
        'xkb_context_new': (C.c_void_p, [C.c_int]),
        'xkb_keymap_new_from_names': (C.c_void_p, [C.c_void_p, C.POINTER(Names), C.c_int]),
        'xkb_keymap_key_get_syms_by_level': (C.c_int, [C.c_void_p, C.c_uint, C.c_uint, C.c_uint, C.POINTER(C.POINTER(C.c_uint))]),
        'xkb_keysym_to_utf32': (C.c_uint, [C.c_uint]),
        'xkb_keysym_get_name': (C.c_int, [C.c_uint, C.c_char_p, C.c_size_t]),
        'xkb_keymap_unref': (None, [C.c_void_p]),
        'xkb_context_unref': (None, [C.c_void_p]),
    }
    for name, (ret, args) in signatures.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = ret, args
    context = lib.xkb_context_new(0)
    if not context:
        raise RuntimeError('XKB context failed')
    metadata = {}
    try:
        with (share / 'keymap-table.tsv').open('w') as stream:
            writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
            writer.writerow(['layout', 'evdev', 'character', 'keysym', 'printed_digit'])
            for line in (share / 'layouts.tsv').read_text().splitlines():
                if not line or line.startswith('#'):
                    continue
                ident, layout, variant, console, label = line.split('\t')
                if check_console and not any(Path('/usr/lib/kbd/keymaps').rglob(console + '.map*')):
                    raise RuntimeError(f'Console keymap missing: {console}')
                names = Names(b'evdev', b'pc105', layout.encode(), b'' if variant == '-' else variant.encode(), b'')
                keymap = lib.xkb_keymap_new_from_names(context, C.byref(names), 0)
                if not keymap:
                    raise RuntimeError(f'Invalid XKB layout: {ident}')
                dead, compose = False, False
                try:
                    for code in range(1, 248):
                        syms = C.POINTER(C.c_uint)()
                        n = lib.xkb_keymap_key_get_syms_by_level(keymap, code + 8, 0, 0, C.byref(syms))
                        if n != 1:
                            continue
                        name = C.create_string_buffer(128)
                        lib.xkb_keysym_get_name(syms[0], name, len(name))
                        sym = name.value.decode()
                        dead |= sym.startswith('dead_')
                        compose |= sym == 'Multi_key'
                        point = lib.xkb_keysym_to_utf32(syms[0])
                        char = chr(point) if point and chr(point).isprintable() else ''
                        digit = char if char in '0123456789' and char else ''
                        if not digit:
                            shifted = C.POINTER(C.c_uint)()
                            count = lib.xkb_keymap_key_get_syms_by_level(keymap, code + 8, 0, 1, C.byref(shifted))
                            if count == 1:
                                point = lib.xkb_keysym_to_utf32(shifted[0])
                                if point and chr(point) in '0123456789':
                                    digit = chr(point)
                        writer.writerow([ident, code, char, sym, digit])
                finally:
                    lib.xkb_keymap_unref(keymap)
                metadata[ident] = {'dead_keys': dead, 'compose': compose}
    finally:
        lib.xkb_context_unref(context)
    metadata['_sha256'] = hashlib.sha256((share / 'keymap-table.tsv').read_bytes()).hexdigest()
    (share / 'keymap-metadata.json').write_text(json.dumps(metadata, sort_keys=True) + '\n')


if __name__ == '__main__':
    generate(Path(sys.argv[1] if len(sys.argv) > 1 else 'image/usr/share/kiosk'), '--check-console' in sys.argv)
