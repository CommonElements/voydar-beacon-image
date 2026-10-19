#!/bin/sh
# Voydar Beacon: a disk swap file as a safety net on boards with less than 1 GiB of RAM
# (Raspberry Pi Zero 2 W and 3A+: 512 MB). Raspberry Pi OS (rpi-swap) already gives every board a
# compressed zram swap; on 512 MB that alone can run short while Docker unpacks the preloaded
# receiver images and ultrafeeder (readsb, tar1090, graphs1090) starts next to AIS-catcher. This
# file sits below zram in priority, so it is only touched when zram is full. Runs every boot;
# creates the file once (only if the card has room), then just switches it on.
set -u

SWAP=/var/lib/voydar/swapfile
SIZE_MB=512
LIMIT_KB=1048576   # 1 GiB

mem_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
if [ "${mem_kb:-0}" -ge "$LIMIT_KB" ]; then
	echo "voydar-lowmem-swap: ${mem_kb} kB RAM, zram is enough; nothing to do"
	exit 0
fi
if awk -v f="$SWAP" '$1 == f {found=1} END {exit !found}' /proc/swaps; then
	exit 0
fi
mkdir -p "$(dirname "$SWAP")"
if [ ! -f "$SWAP" ]; then
	free_mb=$(df -Pm "$(dirname "$SWAP")" | awk 'NR==2 {print $4}')
	if [ "${free_mb:-0}" -lt $((SIZE_MB * 4)) ]; then
		echo "voydar-lowmem-swap: only ${free_mb} MB free on the card; not creating a swap file"
		exit 0
	fi
	rm -f "$SWAP.tmp"
	fallocate -l "${SIZE_MB}M" "$SWAP.tmp" 2>/dev/null || dd if=/dev/zero of="$SWAP.tmp" bs=1M count="$SIZE_MB" status=none
	chmod 600 "$SWAP.tmp"
	mkswap "$SWAP.tmp" >/dev/null || { rm -f "$SWAP.tmp"; echo "voydar-lowmem-swap: mkswap failed"; exit 0; }
	mv "$SWAP.tmp" "$SWAP"
	echo "voydar-lowmem-swap: created ${SIZE_MB} MB $SWAP"
fi
# zram (rpi-swap / zram-generator) runs at priority 100; this is the last resort.
swapon --priority 0 "$SWAP" && echo "voydar-lowmem-swap: $SWAP on (${mem_kb} kB RAM)"
exit 0
