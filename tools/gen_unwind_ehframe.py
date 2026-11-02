#!/usr/bin/env python3
"""Translate the engine's compact-unwind table into a standard .eh_frame.

Apple describes arm64 frames with `__unwind_info`: a two-level, page-indexed
table of compact unwind encodings. libgcc understands none of it, so without a
translation every C++ exception thrown inside the engine runs off the top of the
stack and aborts the process.

`llvm-objdump --macho --unwind-info` decodes that table for us; this script turns
its output into plain DWARF CFI, with one FDE per function:

  * FRAME      - frame-pointer based. CFA = x29 + 16, x29/x30 and each saved
                 register pair at fixed CFA offsets.
  * FRAMELESS  - CFA = sp + (encoding >> 12), x30 at CFA - 8.
  * DWARF      - points at a real FDE in the image's own __eh_frame; skipped,
                 those entries are already covered by that section.

Functions that carry a landing pad get an LSDA pointer, which is what makes
try/catch work at all. The generated table is mapped at a fixed address
(0x103A00000) because profile-relative pointers are resolved against it, and the
personality slot it points at lives just past the end of the table.

Usage:
    llvm-objdump --macho --unwind-info image.macho > unwind.txt
    gen_unwind_ehframe.py unwind.txt runtime/unwind.ehframe
"""
import argparse
import re
import struct

IMAGE_BASE = 0x100000000   # preferred load address of the Mach-O image
EH_BASE = 0x103A00000      # where the generated table is mapped at run time
PERSONALITY_SLOT = 0x103F00000  # one pointer, filled in by the host

MODE_MASK = 0x0F000000
MODE_FRAME = 0x04000000
MODE_FRAMELESS = 0x02000000
MODE_DWARF = 0x03000000
FRAMELESS_SIZE_MASK = 0x00FFF000

# Saved register pairs, in the order the compact encoding numbers them.
REGISTER_PAIRS = [(0x01, 19, 20), (0x02, 21, 22), (0x04, 23, 24),
                  (0x08, 25, 26), (0x10, 27, 28)]

ENCODING_RE = re.compile(
    r"\[(\d+)\]: function offset=0x([0-9a-f]+), encoding\[\d+\]=0x([0-9a-f]+)")
LSDA_RE = re.compile(
    r"\[(\d+)\]: function offset=0x([0-9a-f]+), LSDA offset=0x([0-9a-f]+)")


def uleb(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def cfi_frame(encoding):
    """CFI for a frame-pointer function: stp x29, x30, [sp, #-N]!; ...; add x29, sp, #N-16."""
    cfi = bytearray([0x0C, 29, 16])          # DW_CFA_def_cfa x29, 16
    cfi += bytes([0x80 | 29]) + uleb(2)      # x29 at CFA - 16
    cfi += bytes([0x80 | 30]) + uleb(1)      # x30 at CFA - 8
    slot = 0
    for bit, first, second in REGISTER_PAIRS:
        if encoding & bit:
            cfi += bytes([0x80 | first]) + uleb(3 + 2 * slot)
            cfi += bytes([0x80 | second]) + uleb(4 + 2 * slot)
            slot += 1
    return bytes(cfi)


def cfi_frameless(encoding):
    """CFI for a leaf function that only subtracts a constant from sp."""
    size = (encoding & FRAMELESS_SIZE_MASK) >> 12
    cfi = bytearray([0x0C, 31, 0])           # DW_CFA_def_cfa sp, 0
    cfi += uleb(size)
    cfi += bytes([0x80 | 30]) + uleb(1)      # x30 at CFA - 8
    return bytes(cfi)


def build_cie():
    cie = bytearray(b"\x00\x00\x00\x00")     # CIE id, meaning "this is a CIE"
    cie += b"\x01zPLR\x00"                   # version 1, augmentation "zPLR"
    cie += uleb(1) + b"\x78" + uleb(30)      # code_align 1, data_align -8, RA x30
    # The personality pointer is pcrel|indirect|sdata4 and resolves against the
    # address of its own field: 4 bytes of record length, then the CIE id, the
    # augmentation length uleb, and the one-byte encoding field.
    field_offset = 4 + len(cie) + 2
    aug = bytearray(b"\x9b")
    aug += struct.pack("<i", PERSONALITY_SLOT - (EH_BASE + field_offset))
    aug += b"\x1b\x1b"                       # LSDA and FDE pointers: pcrel|sdata4
    cie += uleb(len(aug)) + bytes(aug) + b"\x00"
    return cie


class Builder:
    def __init__(self):
        self.out = bytearray()
        self.cie = build_cie()
        self.out += struct.pack("<I", len(self.cie)) + self.cie
        self.frames = 0
        self.frameless = 0

    def add(self, func, encoding, next_func, lsda_offset):
        # Everything below is the FDE payload; the four-byte length prefix is
        # prepended when the record is written, so field offsets here are four
        # bytes shorter than they look in the finished table.
        payload = bytearray(struct.pack("<i", 0))       # cie_pointer
        payload += struct.pack("<i", 0)                 # initial_location
        payload += struct.pack("<I", next_func - func)  # address_range
        if lsda_offset:
            payload += uleb(4) + struct.pack("<i", 0)
        else:
            payload += uleb(0)
        mode = encoding & MODE_MASK
        payload += cfi_frame(encoding) if mode == MODE_FRAME else cfi_frameless(encoding)
        while (4 + len(payload)) % 4:
            payload.append(0)

        start = len(self.out)          # offset of the length prefix
        # All three pointers are resolved relative to their own field.
        payload[0:4] = struct.pack("<I", start + 4)
        payload[4:8] = struct.pack(
            "<i", (IMAGE_BASE + func) - (EH_BASE + start + 8))
        if lsda_offset:
            payload[13:17] = struct.pack(
                "<i", (IMAGE_BASE + lsda_offset) - (EH_BASE + start + 17))
        self.out += struct.pack("<I", len(payload)) + bytes(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unwind_info", help="llvm-objdump --macho --unwind-info output")
    parser.add_argument("output", help="path of the .eh_frame to generate")
    args = parser.parse_args()

    encodings, lsdas = {}, {}
    with open(args.unwind_info) as handle:
        for line in handle:
            match = ENCODING_RE.search(line)
            if match:
                encodings[int(match.group(2), 16)] = int(match.group(3), 16)
                continue
            match = LSDA_RE.search(line)
            if match:
                lsdas[int(match.group(2), 16)] = int(match.group(3), 16)

    functions = sorted(encodings.items())
    builder = Builder()
    for index, (func, encoding) in enumerate(functions):
        mode = encoding & MODE_MASK
        if mode not in (MODE_FRAME, MODE_FRAMELESS):
            continue
        next_func = functions[index + 1][0] if index + 1 < len(functions) else func + 0x100
        builder.add(func, encoding, next_func, lsdas.get(func))
        if mode == MODE_FRAME:
            builder.frames += 1
        else:
            builder.frameless += 1
    builder.out += b"\x00\x00\x00\x00"

    with open(args.output, "wb") as handle:
        handle.write(builder.out)
    print("frames=%d frameless=%d functions=%d lsda=%d bytes=%d" % (
        builder.frames, builder.frameless, len(functions), len(lsdas),
        len(builder.out)))


if __name__ == "__main__":
    main()
