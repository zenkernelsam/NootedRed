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
    assert len(report["records"]) == 21
    assert report["pages"] == 12
    labels = {r["label"] for r in report["records"]}
    assert "AppleGVA_AMDOnlyFallback" not in labels
    assert not any("_AVD_CreateAVDAccelerator" in label for label in labels)

    # Pin the intended VCN2 admission surface. In particular, Barcelo 15E7
    # must be present while unsupported VCN1 IDs stay outside these patches.
    expected_devices = {0x15E7, 0x1636, 0x1638, 0x164C}
    device_records = report["device_records"]
    assert {entry["device_id"] for entry in device_records} == expected_devices
    assert all(len(entry["records"]) == len(report["records"]) for entry in device_records)
    assert all(entry["records"] == report["records"] for entry in device_records)

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
        # The entry default at +0x11 is dead for the normal device-classifier
        # path; unmatched Barcelo IDs reach the final default at +0x1B6.
        assert record["address"] == identify + 0x1B6
        original = bytes.fromhex(record["original"])
        assert original == bytes.fromhex("bb0b000000")
        assert cache.read(record["address"], len(original)) == original
        assert bytes.fromhex(record["patched"]) == bytes.fromhex("bb05000000")
        assert cache.uuid.hex() == UUID
    finally:
        cache.close()

    source = (ROOT / "NootedRed/UserVideoDecode.cpp").read_text()
    assert 'checkKernelArgument("-NRedDarwin24")' in source
    assert "rejection(deviceID, currentKernelVersion(), darwin24)" in source
    capability_source = (ROOT / "NootedRed/VCNCapabilities.hpp").read_text()
    nred_source = (ROOT / "NootedRed/NRed.cpp").read_text()
    assert all(f"0x{device:04X}" in capability_source for device in expected_devices)
    switch = nred_source[nred_source.index("switch (this->deviceID)"):]
    assert all(f"case 0x{device:04X}" in switch for device in expected_devices)
    print("PASS: current Darwin 24.6 cache, 21 gated video patches, and family-5 admission gate")


if __name__ == "__main__":
    main()
