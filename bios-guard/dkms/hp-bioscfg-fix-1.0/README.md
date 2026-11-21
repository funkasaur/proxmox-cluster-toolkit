<!-- SPDX-License-Identifier: GPL-2.0 -->
# hp-bioscfg-fix (DKMS)

The Linux `hp-bioscfg` driver (`drivers/platform/x86/hp/hp-bioscfg/`, as in Linux 7.0) with one fix, packaged for
DKMS so it overrides the in-tree module. **This folder is GPL-2.0** (see `LICENSE`), like the kernel code it comes
from; the rest of the repository is MIT.

## The bug

With no BIOS setup password set, the stock driver can read every setting but every write fails: the BIOS returns
code 0x4 and the driver logs "Invalid command type".

`hp_calculate_security_buffer()` reserves 4 bytes when the password is empty, but `hp_populate_security_buffer()`
always writes the `<utf-16/>` prefix (2 + 18 bytes) unless the token is a BEAM signature. The buffer handed to the
BIOS is 16 bytes short, so the firmware's string parser sees a length running past the end and fails. The driver
also writes 16 bytes past its own kmalloc buffer.

The fix (`spmobj-attributes.c`) counts the prefix for an empty password too. `upstream-fix.patch` is the same change
as a kernel-tree patch.

Tested on HP EliteDesk 800 G3 Mini boards: a changed setting survives a reboot and reads back the same through the
stock module. Other HP business models that use `hp-bioscfg` probably have the same problem, but none were tested.

## Upstream

This fix belongs in the mainline kernel, and the patch is written for that. It has **not** been sent upstream yet.
Once it lands in a kernel you run, remove this DKMS module (`dkms remove hp-bioscfg-fix/1.0 --all`).

## Files

- `*.c`, `*.h`: the driver. Only `spmobj-attributes.c` (the fix) and `bioscfg.c` (include path for an out-of-tree
  build) differ from the kernel source. `firmware_attributes_class.h` is copied from `drivers/platform/x86/` for the
  same reason.
- `Makefile`, `dkms.conf`: the out-of-tree build.
- `upstream-fix.patch`: the fix alone, for a kernel tree (`patch -p1`).

Install with `../../install_module.sh root@<node>`.
