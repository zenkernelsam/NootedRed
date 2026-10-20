#!/usr/bin/env python3
"""UserMtlDualQuad 补丁数据的离线核验：pinned 24H32 缓存原字节、offset 数学、默认关门、单页边界。"""
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metal_cache import CACHE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SITE_VA = 0x7FFB087EF53E
CACHE_BASE = 0x7FF800000000
EXPECT_UUID = "42ec2890bc40317ebb2745142bff7f26"


def cache_reader():
    mappings = []
    files = []
    for path in sorted(CACHE.glob("dyld_shared_cache_x86_64h*")):
        if path.suffix in (".map", ".atlas"):
            continue
        handle = path.open("rb")
        files.append(handle)
        header = handle.read(104)
        if not header.startswith(b"dyld_v1"):
            continue
        off, cnt = struct.unpack_from("<II", header, 16)
        handle.seek(off)
        data = handle.read(cnt * 32)
        for i in range(cnt):
            a, s, f, _, _ = struct.unpack_from("<QQQII", data, i * 32)
            mappings.append((a, a + s, f, handle))
    uuid = None
    for path in files:
        path.seek(88)
        uuid = path.read(16).hex()
        break

    def read(addr, n):
        for s, e, f, h in mappings:
            if s <= addr and addr + n <= e:
                h.seek(f + addr - s)
                return h.read(n)
        raise ValueError(f"unmapped {addr:#x}")

    return read, uuid, files


def main():
    failures = []
    read, uuid, files = cache_reader()
    try:
        data_hpp = (ROOT / "NootedRed/UserMtlDualQuadData.hpp").read_text()
        module_cpp = (ROOT / "NootedRed/UserMtlDualQuad.cpp").read_text()
        nred_cpp = (ROOT / "NootedRed/NRed.cpp").read_text()
        pbxproj = (ROOT / "NootedRed.xcodeproj/project.pbxproj").read_text()

        # 1. pinned 缓存 UUID 与数据头一致
        if uuid != EXPECT_UUID:
            failures.append(f"cache uuid {uuid} != {EXPECT_UUID}")
        uuid_bytes = re.search(r"CacheUUID\[\] = \{([^}]+)\}", data_hpp).group(1)
        uuid_arr = bytes(int(x.strip(), 16) for x in uuid_bytes.split(","))
        if uuid_arr.hex() != EXPECT_UUID:
            failures.append("header CacheUUID != pinned 24H32 uuid")

        # 2. 站点原字节 = 83 c9 4d（分支 A or ecx,0x4d）
        site = read(SITE_VA, 3)
        if site != b"\x83\xc9\x4d":
            failures.append(f"site bytes {site.hex()} != 83c94d")

        # 3. offset 数学 = VA - base，且站点不跨页
        m = re.search(r"\{0x([0-9A-Fa-f]+)ULL, Original0, Patched0", data_hpp)
        off = int(m.group(1), 16)
        if off != SITE_VA - CACHE_BASE:
            failures.append(f"offset {off:#x} != {SITE_VA - CACHE_BASE:#x}")
        if (SITE_VA & 0xFFF) + 3 > 0x1000:
            failures.append("patch crosses page boundary")

        # 4. original/patched 字段与二进制一致
        if "Original0[] = {0x83, 0xC9, 0x4D}" not in data_hpp:
            failures.append("Original0 bytes wrong")
        if "Patched0[]  = {0x83, 0xC9, 0x4F}" not in data_hpp:
            failures.append("Patched0 bytes wrong")

        # 5. 默认关门：仅 -NRedMtlDualQuad 启动；isRenoir + 15.x 门；mismatch no-op
        for needle in [
            'checkKernelArgument("-NRedMtlDualQuad")',
            "majorMatches(MACOS_15_X)",
            "isRenoir()",
            "memcmp(header + 88, CacheUUID",
            "Stage::CacheMismatch",
            "Stage::CodeMismatch",
            "vm_protect(map, pages[p].address, PAGE_SIZE, FALSE, pages[p].protection)",
            "writeUser(saved[j]",
        ]:
            if needle not in module_cpp:
                failures.append(f"module missing: {needle}")
        if "UserMtlDualQuad::init(patcher)" not in nred_cpp:
            failures.append("NRed.cpp init call missing")
        for needle in ["UserMtlDualQuad.cpp in Sources", "UserMtlDualQuadData.hpp", "UserMtlDualQuad.hpp"]:
            if needle not in pbxproj:
                failures.append(f"pbxproj missing: {needle}")
    finally:
        for f in files:
            f.close()
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("PASS: MTL dual-quad patch data, gates, and single-page COW semantics verified on pinned 24H32 cache")
    return 0


if __name__ == "__main__":
    sys.exit(main())
