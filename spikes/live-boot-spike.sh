#!/bin/bash
# Spike: does a minimal rootfs boot via live-boot from a grub-mkrescue ISO (BIOS + UEFI)?
# Usage (inside osp-builder): bash spikes/live-boot-spike.sh debian bookworm
#                             bash spikes/live-boot-spike.sh ubuntu noble
set -euo pipefail
flavor=$1 release=$2
work=/var/lib/osp/spike/$flavor-$release
rm -rf "$work"
mkdir -p "$work/iso/live" "$work/iso/boot/grub"

case $flavor in
  debian) mirror="deb http://deb.debian.org/debian $release main"; kernel=linux-image-amd64; keyring=() ;;
  ubuntu) mirror="deb http://archive.ubuntu.com/ubuntu $release main universe"; kernel=linux-image-generic
          keyring=(--keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg) ;;
  *) echo "unknown flavor $flavor" >&2; exit 2 ;;
esac

mmdebstrap --mode=root --variant=minbase --arch=amd64 "${keyring[@]}" \
  --include="systemd-sysv,udev,live-boot,initramfs-tools,$kernel" \
  --customize-hook='chroot "$1" sh -c "echo root:spike | chpasswd"' \
  "$release" "$work/rootfs" "$mirror"

cp "$work"/rootfs/boot/vmlinuz-* "$work/iso/live/vmlinuz"
cp "$work"/rootfs/boot/initrd.img-* "$work/iso/live/initrd.img"
mksquashfs "$work/rootfs" "$work/iso/live/filesystem.squashfs" -comp xz -noappend
cat > "$work/iso/boot/grub/grub.cfg" <<'EOF'
set timeout=1
serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial
menuentry "Live" {
  linux /live/vmlinuz boot=live console=tty0 console=ttyS0,115200
  initrd /live/initrd.img
}
EOF
grub-mkrescue -o "$work/spike.iso" "$work/iso"

for fw in bios uefi; do
  args=(-m 2048 -smp 2 -display none -serial stdio -monitor none -no-reboot -cdrom "$work/spike.iso")
  if [ -w /dev/kvm ]; then args+=(-enable-kvm -cpu host); else args+=(-cpu max); fi
  if [ "$fw" = uefi ]; then
    cp /usr/share/OVMF/OVMF_VARS_4M.fd "$work/vars.fd"
    args+=(-drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd
           -drive if=pflash,format=raw,file="$work/vars.fd")
  fi
  qemu-system-x86_64 "${args[@]}" < /dev/null > "$work/$fw.log" 2>&1 &
  pid=$!
  result=FAIL
  for _ in $(seq 1 600); do
    if grep -q "login:" "$work/$fw.log"; then result=PASS; break; fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  echo "SPIKE $flavor/$release/$fw: $result (log: $work/$fw.log)"
done
