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

        # 1b. 数据头 CacheMagic 与实际 dyld 头前 16 字节逐字节一致
        files[0].seek(0)
        real_magic = files[0].read(16)
        magic_arr = bytes(
            int(x.strip(), 16)
            for x in re.search(r"CacheMagic\[\] = \{([^}]+)\}", data_hpp).group(1).split(",")
        )
        if magic_arr != real_magic:
            failures.append(f"CacheMagic {magic_arr.hex()} != real header {real_magic.hex()}")

        # 1c. 数据头 Context 窗口与实际缓存逐字节一致（站点字节也在窗内）
        ctx_arr = bytes(
            int(x.strip(), 16)
            for x in re.search(r"Context\[\]      = \{([^}]+)\}", data_hpp).group(1).split(",")
        )
        ctx_off = int(re.search(r"ContextOffset  = 0x([0-9A-Fa-f]+)ULL", data_hpp).group(1), 16)
        if ctx_off != SITE_VA - CACHE_BASE - 48:
            failures.append(f"ContextOffset {ctx_off:#x} wrong")
        if read(CACHE_BASE + ctx_off, len(ctx_arr)) != ctx_arr:
            failures.append("Context window != pinned cache bytes")
        site_idx = ctx_arr.find(b"\x83\xc9\x4d")
        if site_idx != 48 or ctx_arr[48:51] != b"\x83\xc9\x4d":
            failures.append("site position inside context wrong")
        if ctx_arr.count(b"\x83\xc9") != 1:
            failures.append("context contains second 83c9 site")

        # 1d. 站点所属镜像 LC_UUID == 数据头 ImageUUID（dyld_info 实测 2F00BC70…）
        img_arr = bytes(
            int(x.strip(), 16)
            for x in re.search(r"ImageUUID\[\] = \{([^}]+)\}", data_hpp).group(1).split(",")
        )
        found_uuid = None
        for fh in files:
            fh.seek(0)
            hd = fh.read(104)
            if not hd.startswith(b"dyld_v1"):
                continue
            moff, mcnt = struct.unpack_from("<II", hd, 16)
            fh.seek(moff)
            for i in range(mcnt):
                a, sz, fo, _, _ = struct.unpack_from("<QQQII", fh.read(32))
                if not (a <= SITE_VA < a + sz):
                    continue
                import mmap

                mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                pos = fo
                fend = fo + sz
                while True:
                    pos = mm.find(b"\xcf\xfa\xed\xfe", pos, fend)
                    if pos < 0:
                        break
                    ncmd = struct.unpack_from("<I", mm, pos + 16)[0]
                    p, iu, tv, ts = pos + 32, None, 0, 0
                    for _ in range(min(ncmd, 40)):
                        if p + 8 > fend:
                            break
                        cmd, csz = struct.unpack_from("<II", mm, p)
                        if csz < 8 or p + csz > fend:
                            break
                        if cmd == 0x1B:
                            iu = mm[p + 8 : p + 24]
                        if cmd == 0x19 and mm[p + 8 : p + 14] == b"__TEXT":
                            tv, ts = struct.unpack_from("<QQ", mm, p + 24)
                        p += csz
                    if iu and tv <= SITE_VA < tv + ts:
                        found_uuid = iu.hex()
                        break
                    pos += 4
        if found_uuid != img_arr.hex():
            failures.append(f"site image uuid {found_uuid} != ImageUUID {img_arr.hex()}")

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

        # 5. 默认关门：仅 -NRedMtlDualQuad 启动；isRenoir + 15.x 门；mismatch no-op；
        #    事务核（身份窗 + 已检查回滚 + 未恢复记录）走 UserMtlDualQuadTxn.hpp。
        txn_hpp = (ROOT / "NootedRed/UserMtlDualQuadTxn.hpp").read_text()
        for needle in [
            'checkKernelArgument("-NRedMtlDualQuad")',
            "majorMatches(MACOS_15_X)",
            "isRenoir()",
            "MtlDQTxn::checkHeader",
            "MtlDQTxn::apply",
            "Stage::CacheMismatch",
            "Stage::CodeMismatch",
            "NRedMtlDQUnrecovered",
            "UserMtlDualQuadTxn.hpp",
        ]:
            if needle not in module_cpp:
                failures.append(f"module missing: {needle}")
        for needle in [
            "Status::Unrecovered",
            "Status::WriteRollbackPartial",
            "unrecoveredMask",
            "env.protSet(env.u, pages[q], ProtRX)",
            "env.write(env.u, saved[j]",
        ]:
            if needle not in txn_hpp:
                failures.append(f"txn header missing: {needle}")
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
