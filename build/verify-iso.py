#!/usr/bin/python3
"""Fail closed on signature, embedded ESP, boot catalog or size violations.

Signer labels alone are NOT cryptographic verification. Compare boot files
against their Fedora RPM payload digests. The separate firmware smoke test
checks that the chain is accepted with enrolled keys and SecureBoot enabled.
"""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys


def run(args):
    try:
        return subprocess.check_output([str(x) for x in args], stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f'{args[0]} failed: {error.output.decode(errors="replace").strip()}') from None


def rpm_payload_match(package, names, artifact):
    """Check actual RPM-owned bytes, excluding ghost ESP copies and metadata.

    Modern Fedora stages EFI payloads in /usr/lib/efi, then copies them to the
    ESP from scriptlets. Those /boot files are RPM ghosts with zero digests;
    their filesystem modes are not an integrity check for the signed binary.
    """
    query = 'ALGO %{FILEDIGESTALGO}\\n[%{FILENAMES}\\t%{FILEDIGESTS}\\n]'
    records = run(['rpm', '-q', '--qf', query, package]).decode().splitlines()
    algorithms = {'8': 'sha256', '9': 'sha384', '10': 'sha512'}
    algorithm, digests = None, {}
    for line in records:
        if line.startswith('ALGO '):
            algorithm = algorithms.get(line.split()[1])
            continue
        path_text, separator, expected = line.partition('\t')
        path = Path(path_text)
        if not separator or not algorithm or not names(path.name) or not expected or set(expected) == {'0'}:
            continue
        if not path.is_file():
            continue
        if algorithm not in digests:
            digests[algorithm] = hashlib.new(algorithm, artifact.read_bytes()).hexdigest()
        digest = digests[algorithm]
        if digest == expected and hashlib.new(algorithm, path.read_bytes()).hexdigest() == expected:
            return dict(package=package, rpm_file=str(path), algorithm=algorithm, digest=expected)
    raise ValueError(f'{artifact.name} does not match a strong Fedora RPM payload digest ({package})')


def check_kernel_signature(sigs, crosscheck):
    # Fedora's leaf certificate may have the generic CN "kernel-signer".
    # sbverify exposes the Fedora CA issuer; pesign only prints the leaf CN.
    if 'fedora secure boot ca' not in sigs.lower() or not re.search(r"The signer's common name is\s+\S+", crosscheck):
        raise ValueError('Kernel lacks Fedora signature metadata')


def verify(iso, directory):
    if iso.stat().st_size >= 2_500_000_000:
        raise ValueError('ISO exceeds 2.5 GB')
    directory.mkdir(parents=True, exist_ok=True)
    report = run(['xorriso', '-indev', iso, '-report_el_torito', 'plain', '-report_system_area', 'plain']).decode()
    (directory / 'boot-catalog.txt').write_text(report)
    if 'UEFI' not in report or 'BIOS' not in report or not re.search(r'MBR|GPT', report):
        raise ValueError('Missing hybrid BIOS/UEFI boot entries')
    for name, source in [('shim.efi', '/EFI/BOOT/BOOTX64.EFI'),
                         ('grub.efi', '/EFI/BOOT/grubx64.efi')]:
        run(['xorriso', '-osirrox', 'on', '-indev', iso, '-extract', source, directory / name])
    for source in ('/isolinux/vmlinuz0', '/images/pxeboot/vmlinuz0'):
        try:
            run(['xorriso', '-osirrox', 'on', '-indev', iso, '-extract', source, directory / 'vmlinuz'])
            break
        except RuntimeError:
            continue
    else:
        raise ValueError('Live kernel is missing from the ISO')
    boot_images = directory / 'boot-images'
    run(['xorriso', '-osirrox', 'on', '-indev', iso, '-extract_boot_images', boot_images])
    images = list(boot_images.glob('eltorito_img*_uefi.img'))
    if len(images) != 1:
        raise ValueError('Expected exactly one extracted UEFI El Torito image')
    # The actual UEFI El Torito / hybrid ESP must agree with the ISO tree.
    for name, efi in [('shim.efi', 'BOOTX64.EFI'), ('grub.efi', 'grubx64.efi')]:
        destination = directory / ('esp-' + name)
        # mkefiboot accepts an EFI/BOOT source directory and puts its contents
        # under EFI/BOOT in the FAT image.
        run(['mcopy', '-i', images[0], '::/EFI/BOOT/' + efi, destination])
        if destination.read_bytes() != (directory / name).read_bytes():
            raise ValueError('EFI system partition differs from ISO tree')
    # Compare against RPM-recorded payload bytes, then inspect Authenticode
    # signatures. An unrelated package mode mismatch must not substitute for
    # (or prevent) checking the exact boot binary's integrity.
    provenance = {}
    trusted = {'shim.efi': ('shim-x64', 'shimx64.efi', 'Microsoft'),
               'grub.efi': ('grub2-efi-x64-cdboot', 'gcdx64.efi', 'Fedora')}
    for name, (package, basename, signer) in trusted.items():
        provenance[name] = rpm_payload_match(package, lambda value: value == basename, directory / name)
        signatures = run(['sbverify', '--list', directory / name]).decode()
        crosscheck = run(['pesign', '-S', '-i', directory / name]).decode()
        if signer.lower() not in signatures.lower() or signer.lower() not in crosscheck.lower():
            raise ValueError(f'{name}: unexpected signature')
        (directory / (name + '.signatures.txt')).write_text(signatures + crosscheck)
    kernel = directory / 'vmlinuz'
    provenance['vmlinuz'] = rpm_payload_match('kernel-core', lambda value: value == 'vmlinuz' or value.startswith('vmlinuz-'), kernel)
    sigs = run(['sbverify', '--list', kernel]).decode()
    cross = run(['pesign', '-S', '-i', kernel]).decode()
    check_kernel_signature(sigs, cross)
    (directory / 'kernel.signatures.txt').write_text(sigs + cross)
    (directory / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    # The separate QEMU test checks that firmware actually accepts this chain with
    # enrolled keys and SecureBoot enabled; listing signatures cannot do that.


if __name__ == '__main__':
    verify(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
