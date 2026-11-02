#!/usr/bin/env python3
"""Prepare the pinned arm64 Mach-O runtime using only the Python standard library.

The universal WeType.app binary carries one x86-64 and one arm64 slice of the
same engine build. Everything the upstream tool does after slice selection —
segment map, section table, dyld chained-fixup bind entries, symbol list — is
architecture independent, so this wrapper only replaces the two CPU constants
and calls the same implementation.
"""
import importlib.util
import pathlib
import sys

CPU_TYPE_ARM64 = 0x0100000C


def load_upstream():
    here = pathlib.Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "prepare_macos_runtime", here / "prepare_macos_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    module = load_upstream()
    module.CPU_TYPE_X86_64 = CPU_TYPE_ARM64
    module.main()


if __name__ == "__main__":
    sys.exit(main())
