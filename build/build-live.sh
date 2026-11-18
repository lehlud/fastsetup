#!/bin/bash
set -euo pipefail
umask 077
project_dir=$(cd "$(dirname "$0")/.." && pwd)
build_dir=${1:-"$project_dir/out"}
mkdir -p "$build_dir"
build_dir=$(cd "$build_dir" && pwd)
test ! -e "$build_dir/kiosk-live.iso" || { echo 'Use a fresh output directory for each compose' >&2; exit 1; }
test "$(id -u)" = 0 || { echo 'Internal builder requires root; use python3 build/build.py on the host' >&2; exit 1; }
. /etc/os-release
test "$ID" = fedora && test "$VERSION_ID" = 44 || { echo 'Internal builder requires Fedora 44; use python3 build/build.py on the host' >&2; exit 1; }
test "$(rpm -q --qf '%{VERSION}-%{RELEASE}' livecd-tools)" = 31.0-20.fc44 || {
    echo 'Expected livecd-tools-31.0-20.fc44; review changes before updating the pin' >&2; exit 1;
}
for tool in livecd-creator python3 ksvalidator sbverify pesign xorriso mcopy; do
    command -v "$tool" >/dev/null || { echo "Missing build dependency: $tool" >&2; exit 1; }
done
python3 "$project_dir/build/prepare.py" "$build_dir/kiosk-live.flattened.ks"
ksvalidator -v F44 "$build_dir/kiosk-live.flattened.ks"
rpm -q livecd-tools shim-x64 grub2-efi-x64 kernel-core edk2-ovmf > "$build_dir/build-host.txt"
cd "$build_dir"
mkdir -p "$build_dir/tmp"
python3 -u "$project_dir/build/create-live.py" --config="$build_dir/kiosk-live.flattened.ks" --fslabel=kiosk-live \
    --releasever=44 --cache="${KIOSK_RPM_CACHE:-$build_dir/cache}" --tmpdir="$build_dir/tmp" 2>&1 | tee "$build_dir/compose.log"
python3 "$project_dir/build/verify-iso.py" "$build_dir/kiosk-live.iso" "$build_dir/verification"
sha256sum kiosk-live.iso > "$build_dir/kiosk-live.iso.sha256"
echo "$build_dir/kiosk-live.iso"
