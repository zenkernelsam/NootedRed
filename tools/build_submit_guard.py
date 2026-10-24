"""生成提交守卫数据：核验 AMD Metal 驱动方法入口，汇编守卫并嵌入头文件。"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build/submit-guard"
OUT = ROOT / "NootedRed/UserSubmitGuardData.hpp"
sys.path.insert(0, str(ROOT / "tools"))

from metal_cache import CacheReader, macho  # noqa: E402

# -[GFX9AMD_MtlCmdQueue submitCommandBuffers:count:] 的缓存内偏移与入口前 15 字节。
METHOD_OFFSET = 0x310CE7240
METHOD_ORIGINAL = bytes.fromhex("554889e54883ec10488d45f0488938")
METHOD_PATCH_SIZE = len(METHOD_ORIGINAL)
PLACEHOLDER = bytes.fromhex("49bb8877665544332211")  # movabs $0x1122334455667788,%r11


def array(name: str, data: bytes) -> str:
    lines = [f"inline constexpr UInt8 {name}[] = {{"]
    for i in range(0, len(data), 12):
        lines.append("    " + ", ".join(f"0x{b:02X}" for b in data[i:i + 12]) + ",")
    lines.append("};")
    return "\n".join(lines) + "\n"


def main() -> None:
    cache = CacheReader()
    original = cache.read(0x7FF800000000 + METHOD_OFFSET, METHOD_PATCH_SIZE)
    if original != METHOD_ORIGINAL:
        raise SystemExit("AMD Metal 提交方法入口未核实，拒绝生成")

    WORK.mkdir(parents=True, exist_ok=True)
    obj = WORK / "submit_guard.o"
    subprocess.run(["as", "-arch", "x86_64", str(ROOT / "tools/submit_guard.s"), "-o", str(obj)],
                   check=True, timeout=60)
    data = obj.read_bytes()
    _, sections, _ = macho(lambda a, n: data[a:a + n], 0)
    text = next(s for s in sections if s[0] == "__text")
    payload = data[text[3]:text[3] + text[2]]
    if len(payload) >= 4096:
        raise SystemExit("守卫超过一页")
    if payload.count(PLACEHOLDER) != 1:
        raise SystemExit("守卫占位符数量不为 1")
    jump_offset = payload.index(PLACEHOLDER) + 2

    header = "// 由 tools/build_submit_guard.py 生成，不手工编辑。\n#pragma once\n#include <IOKit/IOTypes.h>\n"
    header += "namespace UserSubmitGuardData {\n"
    header += f"inline constexpr UInt64 MethodOffset = 0x{METHOD_OFFSET:X}ULL;\n"
    header += array("MethodOriginal", METHOD_ORIGINAL)
    header += f"inline constexpr UInt32 MethodPatchSize = {METHOD_PATCH_SIZE};\n"
    header += f"inline constexpr UInt32 PayloadJumpOffset = {jump_offset};\n"
    header += array("Payload", payload)
    header += "} // namespace UserSubmitGuardData\n"
    OUT.write_text(header)
    print(f"submit guard: method 0x{METHOD_OFFSET:X} payload {len(payload)} bytes "
          f"jump offset {jump_offset} -> {OUT}")


if __name__ == "__main__":
    main()
