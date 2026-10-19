"""生成指定缓存的 VCN2 核显视频补丁；包含设备识别与已验证的编码协议修正。"""
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess

from audit_video_driver import symbols
from metal_cache import CACHE, CacheReader, array, macho
from vcn_capabilities import devices
from cache_resolver_data import ResolverData

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build/vcn"
GVA_CHOICE_ADDRESS = 0x7FFA06692198
GVA_CHOICE_SIZE = 1704
GVA_CHOICE_SHA256 = "1836eb3109e90afac34824654063b2cab953dbb55903cb94f222850a1fa67523"
HASHES = {
    "__ZN17VAAcceleratorInfo8identifyEjj": "dd95fd9d1ea9740465bcefa6d68770a0ebbb6e8161e97939be19ff1c1671737e",
    "__ZN18VAAddrLibInterface4initEP11VAIOConnectP13sHardwareInfo":
        "abafc0170bb2bfa7c333371531e68cc01c237d6ddf4f297711f71c6f0773a264",
    "__ZN4Addr2V27Gfx9Lib20HwlConvertChipFamilyEjj":
        "4a4a1d2337e580de8dcd851ec686179bc375639441ebf7020dcca69ea780f8fa",
    "__ZN4Addr3Lib6CreateEPK18_ADDR_CREATE_INPUTP19_ADDR_CREATE_OUTPUT":
        "b0a123ffae9ff1b251077346fe12ba2640c007bdbb12ec15716855c5bf160063",
    "__ZN9VAFactory20createGraphicsEngineEP9VAContext":
        "65ac04e1da1d0c5445de83db24583ffbfb29f2851928dc44cddb885c0d7846fc",
    "__ZN9VAFactory14createImageBltEP9VAContextj":
        "7052a8a0a8a73e5aac554bfd00a395e48a30af2de23587f0986710bd034cd2a2",
    "__ZN9VAFactory17createDeblockerVPEi": "58fcd2efbaca0055df4e6dbb2bd54da7d6fc07e9beebbd216cb047eeb9c2271e",
    "__ZN9VAFactory17createDitheringVPEi": "7bbd8395c27103ac3c5a7188fa79ef50b03c95cacf3dc6d5edee6fa8a7b9e72b",
    "__ZN9VAFactory20createDeinterlacerVPEi": "3eeaf7d7db19379578209f019145ef116dc1526ee091b3e42e43255a07ac89ad",
    "__ZN9VAFactory29createColourFormatConverterVPEi":
        "8ed81834c8e63adc03f7714b2cf866a15db0d4ee444e6d8be66363cbb17369cd",
    "__ZN9VAFactory31createDynamicContrastEnhancerVPEi":
        "d9e9dc85b70ed7c1f9fa236ba967bfe7f5fee5aee3d3f9fb88a1b44b85fd184b",
    "__ZN14Vcn2EncCommand15getFeedbackSizeEv":
        "571d3ec0d74000c103203b60450b43b67d758b74b470d0082d45879723021c7a",
    "__ZN26AVDVcn2AvcEncDataProcessor19ProcessEncodeStatusEP19_VAEncodeStatusInfoPvP15VAVendorTexture":
        "fe78c5a8d4508c9a0eab2ba96830634b5f8f8a7982b06cd91c2bc1f6a1e2f493",
    "__ZN27AVDVcn2HevcEncDataProcessor19ProcessEncodeStatusEP19_VAEncodeStatusInfoPvP15VAVendorTexture":
        "0ff1d68ec316f8497278a3be223c2eabe642f68e894fa7ecc808e70168c5b2c2",
    "__ZN11Vcn2Encoder15setEncodeParamsEPvP15VAVendorTexture":
        "c8193ea2d2c7193012a5618b6918d41fffe30ad0c1389256fee983e66ac24347",
}
# 短 getter 需要至少 32 字节锚点；两个编码器的 pitch 函数完全相同，需要相邻完整函数区分。
# 相邻函数仅参与完整匹配和重定位核验，不修改其字节。
ENCODE_CONTEXTS = {
    "__ZN14Vcn2EncCommand15getFeedbackSizeEv": (
        "__ZN14Vcn2EncCommand20addSessionInitPacketEv", 36,
        "30f2bdbaf54a9b10c7fe58119b8f288b5118bfa069bc062e01c15a7d248171d7"),
    "__ZN11Vcn2Encoder15setEncodeParamsEPvP15VAVendorTexture": (
        "__ZN11Vcn2Encoder18setAvcEncodeParamsEPv", 292,
        "e7d4ca36567f06e784dda6aeb0238fa9947d04ec562518f28e8c98628e1bf1e7"),
}


def compile_payload(source, label):
    asm, obj = WORK / f"{label}.s", WORK / f"{label}.o"
    asm.write_text(source)
    subprocess.run(["xcrun", "clang", "-c", str(asm), "-o", str(obj)], check=True, timeout=60)
    data = obj.read_bytes()
    _, sections, _ = macho(lambda a, n: data[a:a + n], 0)
    section = next(s for s in sections if s[0] == "__text")
    return data[section[3]:section[3] + section[2]]


def identify_payload(native, device_id, va_family):
    code = compile_payload(".text\n.globl _dump\n_dump:\n.byte " + ",".join(hex(v) for v in native), "identify-dump")
    assert code == native
    output = subprocess.run(["xcrun", "llvm-objdump", "-d", str(WORK / "identify-dump.o")],
                            capture_output=True, text=True, check=True, timeout=60).stdout
    result = ".text\n.globl _payload\n_payload:\n"
    for line in output.splitlines():
        m = re.match(r"\s*([0-9a-f]+): ((?:[0-9a-f]{2}[ \t]+)+)([^#]+)", line)
        if not m:
            continue
        offset = int(m[1], 16)
        raw = bytes.fromhex(m[2])
        instruction = m[3].strip()
        result += f"L{offset:x}:\n"
        if offset in (0x24, 0x48, 0x5A):
            # 唯一外部调用与两处 RIP 数据访问使用原地址差，不产生外部重定位。
            prefix, displacement_offset = ((b"\x48\x8d\x05", 3) if offset == 0x24 else
                                           (b"\xe8", 1) if offset == 0x48 else (b"\x0f\xb7\x05", 3))
            target = offset + len(raw) + struct.unpack_from("<i", raw, displacement_offset)[0]
            result += ".byte " + ",".join(hex(v) for v in prefix) + "\n"
            result += f".long {target} - (. - _payload) - 4\n"
        elif instruction.startswith("j"):
            branch = re.match(r"(\w+)\s+0x([0-9a-f]+)", instruction)
            if not branch:
                raise ValueError("无法核实的识别分支")
            result += f"{branch[1]} L{branch[2]}\n"
        elif re.match(r"movl\s+\$0x[0-9a-f]+, %ebx", instruction):
            value = int(re.search(r"\$0x([0-9a-f]+)", instruction)[1], 16)
            result += f"pushq ${value}\npopq %rbx\n"
        else:
            result += ".byte " + ",".join(hex(v) for v in raw) + "\n"
        if offset == 0x5A:
            result += (f"cmpw ${device_id:#x}, %ax\njne Loriginal\npushq ${va_family}\npopq %rbx\n"
                       "jmp L51\nLoriginal:\n")
    result += ".org 448, 0x90\n"
    return compile_payload(result, f"identify-{device_id:04X}")


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    capabilities = devices()
    if (0x1638, 5) not in capabilities:
        raise ValueError("缺少既有 Cezanne 配置")
    cache = CacheReader()
    records = []
    full_functions = {}
    try:
        lines = (CACHE / "dyld_shared_cache_x86_64h.map").read_text().splitlines()
        image = "/System/Library/Extensions/AMDRadeonVADriver2.bundle/Contents/MacOS/AMDRadeonVADriver2"
        base = int(lines[lines.index(image) + 1].split()[1], 16)
        names, sections = symbols(cache.read, base)

        def function(name):
            address = names[name]
            end = min(a for a in names.values() if a > address)
            code = cache.read(address, end - address)
            if hashlib.sha256(code).hexdigest() != HASHES[name]:
                raise ValueError(f"原生函数未核实：{name}")
            context = code
            if name in ENCODE_CONTEXTS:
                following, size, digest = ENCODE_CONTEXTS[name]
                if end != names[following] or min(a for a in names.values() if a > end) != address + size:
                    raise ValueError(f"编码锚点边界未核实：{name}")
                context = cache.read(address, size)
                if hashlib.sha256(context).hexdigest() != digest:
                    raise ValueError(f"编码锚点未核实：{name}")
            full_functions[name] = (address, context)
            return address, code

        def add(name, address, original, patched):
            if len(original) != len(patched):
                raise ValueError("补丁长度不相等")
            records.append({"label": name, "address": address, "original": original.hex(), "patched": patched.hex()})

        # Build 24.6 has a different board-id path from the historical
        # MacBook-only fallback. Profile the selector, but do not patch it until
        # a board-specific current branch is proven.
        gva_choice = cache.read(GVA_CHOICE_ADDRESS, GVA_CHOICE_SIZE)
        if hashlib.sha256(gva_choice).hexdigest() != GVA_CHOICE_SHA256:
            raise ValueError("AppleGVA 选择函数未核实，拒绝生成")

        name = "__ZN17VAAcceleratorInfo8identifyEjj"
        address, original = function(name)
        # Darwin 24.6 has no range entry for Renoir/Cezanne IDs; unmatched
        # devices fall through to the final default `mov ebx,0xB` at +0x1B6.
        # Return the VCN 2.2 family (5) there instead. The earlier +0x11 default
        # only covers the helper-hit/arg!=1 path.
        # _AVD_CreateAVDAccelerator's family gate needs NO patch: {0xB,0xC}
        # take the release+return path, while every other family — including 5 —
        # falls through `ja` into the real init path (object creation +
        # initDriverCapability + result struct). Widening the gate would send
        # family 5 to release+return and nullify this patch.
        assert original[0x1B6:0x1BB] == bytes.fromhex("bb0b000000")
        add(name, address + 0x1B6, original[0x1B6:0x1BB], bytes.fromhex("bb05000000"))
        for name, offset in [("__ZN9VAFactory20createGraphicsEngineEP9VAContext", 10),
                             ("__ZN9VAFactory14createImageBltEP9VAContextj", 11)]:
            address, code = function(name)
            assert code[offset:offset + 10] == bytes.fromhex("488b86600400008b400c")
            add(name, address + offset, code[offset:offset + 10], bytes.fromhex("b8020000009090909090"))
        for name in HASHES:
            if "VPEi" not in name:
                continue
            address, code = function(name)
            if bytes.fromhex("8d46ff83f80272") not in code:
                continue  # 原生没有 GFX9 实现的可选处理器保持原样。
            offset = code.index(bytes.fromhex("8d46ff83f80272"))
            assert code[offset + 7] < 0x80
            target = offset + 8 + code[offset + 7]
            patched = b"\xbe\x02\x00\x00\x00\xeb" + bytes([target - offset - 7]) + b"\x90"
            add(name, address + offset, code[offset:offset + 8], patched)
        name = "__ZN18VAAddrLibInterface4initEP11VAIOConnectP13sHardwareInfo"
        address, code = function(name)
        assert code[0x8B:0x92] == bytes.fromhex("4181fc8d000000")
        add(name, address + 0x8E, b"\x8d", b"\x8e")
        name = "__ZN4Addr3Lib6CreateEPK18_ADDR_CREATE_INPUTP19_ADDR_CREATE_OUTPUT"
        address, code = function(name)
        assert code[0xB5:0xBA] == bytes.fromhex("3d8d000000")
        add(name, address + 0xB6, b"\x8d", b"\x8e")
        name = "__ZN4Addr2V27Gfx9Lib20HwlConvertChipFamilyEjj"
        address, code = function(name)
        add(name, address, code, compile_payload((ROOT / "tools/video_addr_convert.s").read_text(), "addr-convert"))
        # 本机固件写入 48 字节反馈；原生 Vcn2 使用 164 字节，后续帧因此读到零或无关数据。
        name = "__ZN14Vcn2EncCommand15getFeedbackSizeEv"
        address, code = function(name)
        assert code[4:9] == bytes.fromhex("b8a4000000")
        add(name, address + 4, code[4:9], bytes.fromhex("b830000000"))
        for name in [
            "__ZN26AVDVcn2AvcEncDataProcessor19ProcessEncodeStatusEP19_VAEncodeStatusInfoPvP15VAVendorTexture",
            "__ZN27AVDVcn2HevcEncDataProcessor19ProcessEncodeStatusEP19_VAEncodeStatusInfoPvP15VAVendorTexture",
        ]:
            address, code = function(name)
            assert code[0x2A:0x31] == bytes.fromhex("4869c0a4000000")
            add(name, address + 0x2A, code[0x2A:0x31], bytes.fromhex("4869c030000000"))
        # 输入纹理的实际 pitch 可大于编码宽度；保留原生的字节到元素换算及半宽色度布局。
        name = "__ZN11Vcn2Encoder15setEncodeParamsEPvP15VAVendorTexture"
        address, code = function(name)
        assert code[0x31:0x56] == bytes.fromhex(
            "8b8a840000008b87240400000fafc105ff0000002500ffffff31d2f7f1894318d1e889431c")
        pitch = compile_payload(".text\n.globl _payload\n_payload:\n"
            "movl 0x80(%rdx), %eax\nmovl 0x84(%rdx), %ecx\nxorl %edx, %edx\ndivl %ecx\n"
            "movl %eax, 0x18(%rbx)\nshrl %eax\nmovl %eax, 0x1c(%rbx)\n.org 37, 0x90\n", "encode-pitch")
        add(name, address + 0x31, code[0x31:0x56], pitch)
        code_section = next(s for s in sections if s[0] == "__text")
        text = cache.read(code_section[1], code_section[2])
        for old, new in [(0x2220221, 0x2020201), (0x6660661, 0x6060601)]:
            cursor, count = 0, 0
            while (offset := text.find(struct.pack("<I", old), cursor)) >= 0:
                address = code_section[1] + offset
                parent = max((a, n) for n, a in names.items() if a <= address)
                if not any(s in parent[1] for s in ["HwlGetPreferredSurfaceSetting", "HwlComputeSurfaceInfoSanityCheck",
                                                   "IsValidDisplaySwizzleMode", "ValidateSwModeParams"]):
                    raise ValueError("swizzle mask 位于未核实的消费者")
                add(parent[1], address, struct.pack("<I", old), struct.pack("<I", new))
                count += 1
                cursor = offset + 1
            if count != 4:
                raise ValueError("swizzle mask 数量不匹配")
        records.sort(key=lambda r: r["address"])
        resolver = ResolverData(cache, WORK)
        functions = {**full_functions}
        for record in records:
            label = record["label"]
            if label not in functions:
                address = names[label]
                end = min(a for a in names.values() if a > address)
                functions[label] = (address, cache.read(address, end-address))
        indices = {}
        for label, (address, code) in functions.items():
            if not any(r["label"] == label for r in records):
                continue
            patched = bytearray(code)
            for record in records:
                if record["label"] == label:
                    offset = record["address"]-address
                    patch = bytes.fromhex(record["patched"])
                    patched[offset:offset+len(patch)] = patch
            indices[label] = resolver.add(address, code, bytes(patched))
        locations = [(indices[r["label"]], r["address"]-functions[r["label"]][0]) for r in records]
        header = "// 由 tools/build_video_decode_patch.py 生成，不手工编辑。\n#pragma once\n#include <IOKit/IOTypes.h>\n#include <UserCacheResolver.hpp>\n"
        header += "namespace UserVideoDecodeData {\n" + array("CacheMagic", cache.magic) + array("CacheUUID", cache.uuid)
        header += f"inline constexpr UInt64 CacheBase = 0x{cache.base:X}ULL;\n"
        header += "struct Patch { UInt64 offset; const UInt8* original; const UInt8* patched; UInt32 size; };\n"
        for i, record in enumerate(records):
            address, size = record["address"], len(bytes.fromhex(record["original"]))
            if address // 4096 != (address + size - 1) // 4096:
                raise ValueError("补丁跨页")
            if i and records[i - 1]["address"] + len(bytes.fromhex(records[i - 1]["original"])) > address:
                raise ValueError("补丁重叠")
            header += array(f"Original{i}", bytes.fromhex(record["original"]))
            header += array(f"Patched{i}", bytes.fromhex(record["patched"]))
        header += "inline constexpr Patch Patches[] = {\n"
        for i, record in enumerate(records):
            header += f'    {{0x{record["address"] - cache.base:X}ULL, Original{i}, Patched{i}, sizeof(Original{i})}},\n'
        pages = len({r["address"] // 4096 for r in records})
        header += "};\n"
        device_records = []
        for device, _ in capabilities:
            variants = [dict(record) for record in records]
            device_records.append({'device_id': device, 'records': variants})
        header += 'struct DevicePatchSet { UInt32 deviceID; const Patch* patches; };\n'
        header += 'inline constexpr DevicePatchSet DevicePatches[] = {\n'
        for device, _ in capabilities:
            header += f'    {{0x{device:04X}, Patches}},\n'
        header += '};\n'
        header += ('inline constexpr const Patch* findDevice(UInt32 deviceID) {\n'
                   '    for (const auto& entry : DevicePatches) { if (entry.deviceID == deviceID) { return entry.patches; } }\n'
                   '    return nullptr;\n}\n')
        header += resolver.header(locations)
        header += f"inline constexpr UInt32 MaxPatchSize = 448;\ninline constexpr UInt32 PageCount = {pages};\n}}\n"
        (ROOT / "NootedRed/UserVideoDecodeData.hpp").write_text(header)
        (WORK / "user-patches.json").write_text(json.dumps({"records": records, "pages": pages,
                                                           "device_records": device_records,
                                                           "cacheUUID": cache.uuid.hex()}, indent=2) + "\n")
        print("Generated video patches:", len(records), "pages:", pages, "VCN2 devices:", len(capabilities))
    finally:
        cache.close()


if __name__ == "__main__":
    main()
