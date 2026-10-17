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
    assert len(report["records"]) == 17
    assert report["pages"] == 8
    assert "AppleGVA_AMDOnlyFallback" not in {r["label"] for r in report["records"]}

    cache = CacheReader()
    try:
        lines = (CACHE / "dyld_shared_cache_x86_64h.map").read_text().splitlines()
        image_index = lines.index(IMAGE)
        base = int(next(line for line in lines[image_index + 1:] if "__TEXT" in line).split("0x")[1].split()[0], 16)
        names, _ = symbols(cache.read, base)
        identify = names[IDENTIFY]
        record = next(r for r in report["records"] if r["label"] == IDENTIFY)
        assert record["address"] == identify + 0x11
        assert bytes.fromhex(record["original"]) == bytes.fromhex("bb0b000000")
        assert bytes.fromhex(record["patched"]) == bytes.fromhex("bb05000000")
        assert cache.uuid.hex() == UUID
    finally:
        cache.close()

    source = (ROOT / "NootedRed/UserVideoDecode.cpp").read_text()
    assert 'checkKernelArgument("-NRedDarwin24")' in source
    assert "rejection(deviceID, currentKernelVersion(), darwin24)" in source
    print("PASS: current Darwin 24.6 cache, 17 gated decode patches, and exact userland gate")


if __name__ == "__main__":
    main()
