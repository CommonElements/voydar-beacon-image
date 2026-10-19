#!/bin/bash -e
# Voydar Beacon stage: installs the first-boot setup page, the boot-partition Wi-Fi file
# (voydar-wifi.txt) and its import service, the embedded (sha256-verified) Beacon installer v7 and
# ADS-B feeder v3, and the digest-pinned container images. files/build/ is filled
# by scripts/beacon-image/build.sh before pi-gen runs; this script refuses to build without it.
# Every name on the device is Voydar (image 1.2.0, installer v7): /etc/voydar, /opt/voydar,
# /var/lib/voydar, voydar-* units, containers and commands, and the local user voydar.

B=files/build
FEEDER=adsb-feed-v3.sh
for f in "$B/voydar-install.sh" "$B/$FEEDER" "$B/BUILDINFO"; do
	[ -s "$f" ] || { echo "missing $f: run scripts/beacon-image/build.sh, not pi-gen directly" >&2; exit 1; }
done

install -v -d -m 755 "${ROOTFS_DIR}/usr/local/lib/voydar" "${ROOTFS_DIR}/opt/voydar" "${ROOTFS_DIR}/opt/voydar/images"
install -v -d -m 700 "${ROOTFS_DIR}/etc/voydar"
install -v -d -m 755 "${ROOTFS_DIR}/var/lib/voydar"

install -v -m 755 files/voydar-setup.py   "${ROOTFS_DIR}/usr/local/lib/voydar/voydar-setup.py"
install -v -m 755 files/voydar-preload.py "${ROOTFS_DIR}/usr/local/lib/voydar/voydar-preload.py"
install -v -m 755 files/voydar-lowmem-swap.sh "${ROOTFS_DIR}/usr/local/lib/voydar/voydar-lowmem-swap.sh"
# The blank Wi-Fi file: on the boot partition for the contributor to fill in before first boot, and
# next to the setup script, which writes it back (values emptied) after reading the card's copy.
install -v -m 644 files/voydar-wifi.txt "${ROOTFS_DIR}/usr/local/lib/voydar/voydar-wifi.txt"
install -v -d "${ROOTFS_DIR}/boot/firmware"
install -v -m 644 files/voydar-wifi.txt "${ROOTFS_DIR}/boot/firmware/voydar-wifi.txt"
install -v -m 644 files/voydar-setup.service files/voydar-reset.service files/voydar-preload.service \
	files/voydar-lowmem-swap.service files/voydar-wifi-import.service "${ROOTFS_DIR}/etc/systemd/system/"
install -v -m 644 files/voydar-rtl-sdr.conf "${ROOTFS_DIR}/etc/modprobe.d/voydar-rtl-sdr.conf"
install -v -d -m 755 "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d"
install -v -m 644 files/dnsmasq-voydar.conf "${ROOTFS_DIR}/etc/NetworkManager/dnsmasq-shared.d/voydar-beacon.conf"

install -v -m 755 "$B/voydar-install.sh" "${ROOTFS_DIR}/opt/voydar/voydar-install.sh"
install -v -m 755 "$B/$FEEDER"           "${ROOTFS_DIR}/opt/voydar/$FEEDER"
install -v -m 644 "$B/BUILDINFO"         "${ROOTFS_DIR}/opt/voydar/BUILDINFO"
if compgen -G "$B/images/*.tar" > /dev/null; then
	install -v -m 644 "$B"/images/*.tar "$B"/images/*.tar.json "${ROOTFS_DIR}/opt/voydar/images/"
fi

on_chroot << EOF
systemctl enable docker.service
systemctl enable voydar-lowmem-swap.service voydar-reset.service voydar-preload.service voydar-wifi-import.service voydar-setup.service
# No SSH and no usable password on a fresh card: the setup page can turn SSH on with a password
# the contributor chooses. The build-time password of ${FIRST_USER_NAME} is discarded here.
systemctl disable ssh.service || true
usermod --pass='*' "${FIRST_USER_NAME}"
usermod -aG docker "${FIRST_USER_NAME}"
EOF
