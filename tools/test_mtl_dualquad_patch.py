#!/usr/bin/env python3
"""UserMtlDualQuad 补丁数据的离线核验：pinned 24H32 缓存原字节、offset 数学、默认关门、单页边界。"""
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metal_cache import CACHE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# 条件式 dual-quad 修复的两处站点（AMD_render_cmd_encoder 管线建立段）：
# P0 `75 0b -> eb 0b`（恒评估 dual-quad 条件）、P1 `0f 94 c0 -> 31 c0 90`（!RB+ 项置假）。
SITES = [
    (0x7FFB087B3164, b"\x75\x0b", b"\xeb\x0b"),
    (0x7FFB087B3198, b"\x0f\x94\xc0", b"\x31\xc0\x90"),
]
SITE_VA = SITES[0][0]  # 镜像归属扫描用主站点
CACHE_BASE = 0x7FF800000000
CTX_OFF = 0x3087B3150
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
        if ctx_off != CTX_OFF:
            failures.append(f"ContextOffset {ctx_off:#x} != {CTX_OFF:#x}")
        if read(CACHE_BASE + ctx_off, len(ctx_arr)) != ctx_arr:
            failures.append("Context window != pinned cache bytes")
        # 两站点字节必须落在窗内 +0x14/+0x48 且原字节正确
        for site_va, orig, _ in SITES:
            idx = site_va - CACHE_BASE - ctx_off
            if not (0 <= idx <= len(ctx_arr) - len(orig)):
                failures.append(f"site {site_va:#x} outside context window")
            elif ctx_arr[idx : idx + len(orig)] != orig:
                failures.append(f"site {site_va:#x} bytes in context != original")

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

        # 2. 各站点原字节与 pinned 缓存逐字节一致，且不跨页
        for site_va, orig, _ in SITES:
            if read(site_va, len(orig)) != orig:
                failures.append(f"site {site_va:#x} bytes != {orig.hex()}")
            if (site_va & 0xFFF) + len(orig) > 0x1000:
                failures.append(f"patch at {site_va:#x} crosses page boundary")
        if SITES[0][0] >> 12 != SITES[1][0] >> 12:
            failures.append("sites not on the same page")

        # 3. offset 数学 = VA - base
        for i, (site_va, _, _) in enumerate(SITES):
            m = re.search(r"\{0x([0-9A-Fa-f]+)ULL, Original%d," % i, data_hpp)
            if not m or int(m.group(1), 16) != site_va - CACHE_BASE:
                failures.append(f"Patches[{i}] offset wrong for {site_va:#x}")

        # 4. original/patched 字段与二进制一致
        for needle in [
            "Original0[] = {0x75, 0x0B}",
            "Patched0[]  = {0xEB, 0x0B}",
            "Original1[] = {0x0F, 0x94, 0xC0}",
            "Patched1[]  = {0x31, 0xC0, 0x90}",
        ]:
            if needle not in data_hpp:
                failures.append(f"data missing: {needle}")

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
