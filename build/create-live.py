#!/usr/bin/python3
"""Stage the final initramfs and omit obsolete HFS with pinned livecd-tools 31."""
import runpy
import shutil
import sys


def main():
    import imgcreate
    from imgcreate.live import LiveImageCreatorBase

    class KioskImageCreator(imgcreate.LiveImageCreator):
        # The architecture override unconditionally adds an HFS image. Its base
        # implementation creates the normal FAT UEFI image, which is all we use.
        _generate_efiboot = LiveImageCreatorBase._generate_efiboot

        def _create_bootconfig(self):
            # configure() below stages boot files after %post regenerates the
            # live initramfs, instead of copying the earlier package-script one.
            pass

        def configure(self):
            super().configure()
            super()._create_bootconfig()

    imgcreate.LiveImageCreator = KioskImageCreator
    command = shutil.which('livecd-creator')
    if not command:
        raise SystemExit('livecd-creator is unavailable')
    sys.argv[0] = command
    runpy.run_path(command, run_name='__main__')


if __name__ == '__main__':
    main()
