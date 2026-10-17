"""Static regression for the Renoir Dcn1->Dcn2 swizzle-mask patch count.

Darwin 24's AMDRadeonX5000 contains four Dcn1 mask sites (two bpp64 and two
non-bpp64). Lilu treats MaskedLookupPatch's count as a maximum, so a count of
two silently leaves the tiled surface-info path on Dcn1 masks. This test checks
the source count and, when a binary is supplied, that all four sites exist.
"""
from pathlib import Path
import argparse
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
DCN1_BPP64 = 0x6660661
DCN1_NONBPP64 = 0x2220221
DCN2_BPP64 = 0x6060601
DCN2_NONBPP64 = 0x2020201


def count_u32(data, value):
    pattern = struct.pack("<I", value)
    count = 0
    start = 0
    while True:
        index = data.find(pattern, start)
        if index < 0:
            return count
        count += 1
        start = index + 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path)
    args = parser.parse_args()

    source = (ROOT / "NootedRed/X5000.cpp").read_text()
    assert "MACOS_13_4 ? 2U : 4" not in source, "old partial swizzle count remains"
    assert source.count("sizeof(UInt32), 4U") == 2, "Renoir swizzle patches must use count 4"
    print("PASS: source uses four Renoir swizzle replacements")

    if args.binary:
        data = args.binary.read_bytes()
        bpp64 = count_u32(data, DCN1_BPP64)
        nonbpp64 = count_u32(data, DCN1_NONBPP64)
        assert bpp64 == 4, f"expected 4 Dcn1 bpp64 sites, found {bpp64}"
        assert nonbpp64 == 4, f"expected 4 Dcn1 non-bpp64 sites, found {nonbpp64}"
        # The replacement values already occur elsewhere; only assert presence.
        assert count_u32(data, DCN2_BPP64) >= 1
        assert count_u32(data, DCN2_NONBPP64) >= 1
        print("PASS: binary has 4/4 Dcn1 sites; count 4 covers every site")


if __name__ == "__main__":
    sys.exit(main())
