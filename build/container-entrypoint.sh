#!/bin/bash
set -euo pipefail
umask 022
probe_loop=
native_output=/work/out

finish() {
    result=$?
    trap - EXIT
    set +e
    if test -n "$probe_loop"; then
        losetup --detach "$probe_loop"
    fi
    # Return diagnostics on failure as well. Temporary roots stay on the
    # anonymous Linux volume; the separate RPM cache can be reused on retry.
    for item in compose.log build-host.txt kiosk-live.flattened.ks verification; do
        if test -e "$native_output/$item"; then
            cp -a "$native_output/$item" /output/ || result=1
        fi
    done
    if test "$result" != 0 && test -f "$native_output/kiosk-live.iso"; then
        cp "$native_output/kiosk-live.iso" /output/kiosk-live.unverified.iso || :
    fi
    if test "$result" = 0; then
        if (cd "$native_output" && sha256sum -c kiosk-live.iso.sha256) \
            && cp "$native_output/kiosk-live.iso" /output/kiosk-live.iso.part \
            && cp "$native_output/kiosk-live.iso.sha256" /output/kiosk-live.iso.sha256.part \
            && mv /output/kiosk-live.iso.part /output/kiosk-live.iso \
            && mv /output/kiosk-live.iso.sha256.part /output/kiosk-live.iso.sha256; then
            :
        else
            result=1
            rm -f /output/kiosk-live.iso.part /output/kiosk-live.iso.sha256.part
        fi
    fi
    # Docker Desktop may map ownership itself; do not mask the build result if
    # its shared filesystem does not implement chown.
    chown -R --no-dereference "$KIOSK_OUTPUT_UID:$KIOSK_OUTPUT_GID" /output 2>/dev/null || :
    exit "$result"
}

[[ ${KIOSK_OUTPUT_UID:-} =~ ^[0-9]+$ && ${KIOSK_OUTPUT_GID:-} =~ ^[0-9]+$ ]] || {
    echo 'Output ownership is missing; use python3 build/build.py' >&2
    exit 1
}
trap finish EXIT
test "$(id -u)" = 0 || { echo 'The builder container must run as root' >&2; exit 1; }
mkdir -p "$native_output"

# Keep the reference RPMs aligned with the live compose's updates repository,
# including when the container image's install layer was cached.
dnf -y --setopt=timeout=30 --setopt=retries=3 upgrade --refresh

# Privileged containers expose the engine's kernel, not their own installed
# kernel. Do not load Fedora modules into a different host kernel.
test -e /dev/loop-control || mknod /dev/loop-control c 10 237
for minor in {0..63}; do
    test -e "/dev/loop$minor" || mknod "/dev/loop$minor" b 7 "$minor"
done
truncate -s 1M /work/loop-check.img
if ! probe_loop=$(losetup --find --show /work/loop-check.img); then
    echo 'Loop devices unavailable; enable loop support in the container engine host' >&2
    exit 1
fi
losetup --detach "$probe_loop"
probe_loop=
rm /work/loop-check.img

bash /source/build/build-live.sh "$native_output"
