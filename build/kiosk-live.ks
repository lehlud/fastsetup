lang en_US.UTF-8
keyboard us
timezone UTC --utc
rootpw --lock
selinux --enforcing
firewall --enabled
network --bootproto=dhcp --device=link --activate
bootloader --timeout=0 --append="quiet loglevel=3 rd.systemd.show_status=false systemd.show_status=false systemd.unit=multi-user.target"
zerombr
clearpart --all
part / --size=8192 --fstype=ext4
services --enabled=NetworkManager --disabled=sshd
repo --name=fedora --baseurl=https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os/
repo --name=updates --baseurl=https://dl.fedoraproject.org/pub/fedora/linux/updates/44/Everything/x86_64/

%packages --exclude-weakdeps
@core
fedora-release
kernel
kernel-modules
kernel-modules-extra
linux-firmware
# Wireless firmware is split into separate packages; weak deps are disabled.
atheros-firmware
atmel-firmware
brcmfmac-firmware
iwlegacy-firmware
iwlwifi-dvm-firmware
iwlwifi-mld-firmware
iwlwifi-mvm-firmware
libertas-firmware
mt7xxx-firmware
nxpwireless-firmware
realtek-firmware
tiwilink-firmware
zd1211-firmware
dracut-live
syslinux
syslinux-extlinux
grub2-efi-x64
grub2-efi-x64-cdboot
grub2-efi-x64-modules
grub2-pc
grub2-pc-modules
shim-x64
efibootmgr
mokutil
libselinux-utils
cage
foot
dejavu-sans-mono-fonts
mesa-dri-drivers
libxkbcommon
xkeyboard-config
kbd
kbd-misc
kbd-legacy
terminus-fonts-console
systemd-pam
NetworkManager
NetworkManager-wifi
NetworkManager-tui
wpa_supplicant
iputils
util-linux
curl
openssl
openssh-clients
python3
python3-textual
bat
less
neovim
helix
pykickstart
anaconda-core
anaconda-tui
anaconda-install-env-deps
anaconda-live
btrfs-progs
upower
tlp
-systemd-boot-unsigned
-systemd-boot
-plymouth
%end

%post --nochroot --erroronfail
# build-live.sh appends an embedded payload before this post runs.
%end

%post --erroronfail
set -eu
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
python3 /usr/share/kiosk/generate-keymaps.py /usr/share/kiosk --check-console
chmod 755 /usr/local/bin/*
systemctl set-default multi-user.target
systemctl enable kiosk.service battery-limits.timer battery-guard.timer
systemctl add-wants local-fs.target tmp.mount
systemctl disable tlp.service
systemctl mask getty@.service serial-getty@.service console-getty.service getty@tty1.service
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
ln -sf /usr/lib/systemd/system/poweroff.target /etc/systemd/system/ctrl-alt-del.target
mkdir -p /etc/systemd/system.conf.d /etc/dracut.conf.d
cat > /etc/systemd/system.conf.d/kiosk.conf <<'EOF'
[Manager]
CtrlAltDelBurstAction=poweroff-force
ShowStatus=no
EOF
cat > /etc/dracut.conf.d/kiosk.conf <<'EOF'
add_dracutmodules+=" dmsquash-live "
hostonly="no"
EOF
dracut --regenerate-all --force
# Audit package provenance as a build gate, before the root is squashed.
rpm -qa --qf '%{NAME} %{VENDOR}\n' > /usr/share/kiosk/package-vendors.txt
if grep -Ev ' Fedora Project$' /usr/share/kiosk/package-vendors.txt; then
    exit 1
fi
if rpm -qa --qf '%{NAME}\n' | grep -E '^(akmod-|dkms|systemd-boot)'; then
    exit 1
fi
rpm -qa --qf '%{NEVRA}\n' | sort > /usr/share/kiosk/package-manifest.txt
restorecon -RF /usr/local /etc/systemd /etc/udev /usr/share/kiosk
%end
