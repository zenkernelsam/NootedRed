"""Verify the current Darwin 24.6 userland profile and gated patch record."""
import json
from pathlib import Path

from audit_video_driver import CACHE, symbols
from metal_cache import CacheReader

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "/System/Library/Extensions/AMDRadeonVADriver2.bundle/Contents/MacOS/AMDRadeonVADriver2"
UUID = "42ec2890bc40317ebb2745142bff7f26"
IDENTIFY = "__ZN17VAAcceleratorInfo8identifyEjj"


def main():
    report = json.loads((ROOT / "build/vcn/user-patches.json").read_text())
    assert report["cacheUUID"] == UUID
    # 23 站点/13 页：含 AppleGVA_AMDOnlyFallback（24H32 已核验站点 +0x283，steve profile 提供）。
    assert len(report["records"]) == 23
    assert report["pages"] == 13
    labels = {r["label"] for r in report["records"]}
    assert "AppleGVA_AMDOnlyFallback" in labels
    assert not any("_AVD_CreateAVDAccelerator" in label for label in labels)

    # Pin the intended VCN2 admission surface. In particular, Barcelo 15E7
    # must be present while unsupported VCN1 IDs stay outside these patches.
    expected_devices = {0x15E7, 0x1636, 0x1638, 0x164C}
    device_records = report["device_records"]
    assert {entry["device_id"] for entry in device_records} == expected_devices
    assert all(len(entry["records"]) == len(report["records"]) for entry in device_records)
    # 各设备仅 identify 载荷不同（per-device family payload），其余记录必须一致。
    assert all(
        all(a == b or a["label"] == b["label"] == IDENTIFY for a, b in zip(entry["records"], report["records"]))
        for entry in device_records
    )

    cache = CacheReader()
    try:
        lines = (CACHE / "dyld_shared_cache_x86_64h.map").read_text().splitlines()
        image_index = lines.index(IMAGE)
        base = int(next(line for line in lines[image_index + 1:] if "__TEXT" in line).split("0x")[1].split()[0], 16)
        names, _ = symbols(cache.read, base)
        identify = names[IDENTIFY]
        identify_records = [r for r in report["records"] if r["label"] == IDENTIFY]
        assert len(identify_records) == 1
        record = identify_records[0]
        # 新生成器重写整个 identify 函数（多站点 payload）：记录覆盖函数起点，
        # patched 内嵌 push-imm8 family-5 返回序列（6a 05 5b）。
        assert record["address"] == identify
        original = bytes.fromhex(record["original"])
        assert len(original) == 448
        assert cache.read(record["address"], len(original)) == original
        patched = bytes.fromhex(record["patched"])
        assert patched != original and b"\x6a\x05\x5b" in patched
        barcelo = next(d for d in report["device_records"] if d["device_id"] == 0x15E7)
        barcelo_identify = next(r for r in barcelo["records"] if r["label"] == IDENTIFY)
        assert barcelo_identify["patched"] != record["patched"]
        assert b"\x6a\x05\x5b" in bytes.fromhex(barcelo_identify["patched"])
        assert cache.uuid.hex() == UUID
    finally:
        cache.close()

    source = (ROOT / "NootedRed/UserVideoDecode.cpp").read_text()
    # 新架构不再用 -NRedDarwin24 门（Sequoia profile 自动选择）；保留 -NRedVCN 兼容别名。
    assert 'checkKernelArgument("-NRedVideoFix")' in source
    assert 'checkKernelArgument("-NRedVCN")' in source
    assert "rejection(deviceID, currentKernelVersion())" in source
    capability_source = (ROOT / "NootedRed/VCNCapabilities.hpp").read_text()
    nred_source = (ROOT / "NootedRed/NRed.cpp").read_text()
    assert all(f"0x{device:04X}" in capability_source for device in expected_devices)
    switch = nred_source[nred_source.index("switch (this->deviceID)"):]
    assert all(f"case 0x{device:04X}" in switch for device in expected_devices)
    print("PASS: current Darwin 24.6 cache, 21 gated video patches, and family-5 admission gate")


if __name__ == "__main__":
    main()
