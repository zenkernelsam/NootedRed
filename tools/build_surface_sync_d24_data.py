#!/usr/bin/env python3
"""Generate the Darwin 24 ImmediateSync static patch from the release 4 data.

The release 4 `ImmediateInit15E7` body is a 215-byte replacement for
`InitDeviceSettings`.  Its fixed code is retained; only the RIP-relative 3.0f
load, the NativeOverride call and the NativeDump jump are relocated to the
current AMDRadeonX5000MTLDriver addresses.  The original 215/10-byte guards and
the second (tail) patch are read from the live cache.
"""
from pathlib import Path
import hashlib
import re
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from metal_cache import CacheReader  # noqa: E402

HEADER = ROOT / 'NootedRed/UserSurfaceSyncData.hpp'
SOURCE = ROOT / 'NootedRed/UserSurfaceSync.cpp'
CACHE_BASE = 0x7FF800000000
INIT = 0x7FFB0886C85C
OVERRIDE = 0x7FFB0886650F
DUMP = 0x7FFB087A8829
CONST3 = 0x7FFB08AE1048
TAIL = 0xD7


def array_bytes(text, name):
    match = re.search(rf'inline constexpr UInt8 {name}\[\] = \{{(.*?)\n\}};', text, re.S)
    assert match, name
    return bytes(int(value, 16) for value in re.findall(r'0x[0-9A-Fa-f]+', match.group(1)))


def format_array(name, data):
    lines = []
    for index in range(0, len(data), 16):
        chunk = data[index:index + 16]
        lines.append('    ' + ', '.join(f'0x{byte:02X}' for byte in chunk) + ',')
    body = '\n'.join(lines)
    if body.endswith(','):
        body = body[:-1]
    return f'inline constexpr UInt8 {name}[] = {{\n{body}\n}};'


def replace_array(text, name, data):
    pattern = re.compile(rf'inline constexpr UInt8 {name}\[\] = \{{.*?\n\}};', re.S)
    assert pattern.search(text), name
    return pattern.sub(lambda _: format_array(name, data), text, count=1)


def set_u32(buffer, index, value):
    buffer[index:index + 4] = struct.pack('<i', value)


def main():
    cache = CacheReader()
    try:
        original0 = cache.read(INIT, 215)
        original1 = cache.read(INIT + TAIL, 10)
        uuid = cache.uuid
        assert cache.base == CACHE_BASE
    finally:
        cache.close()

    header = HEADER.read_text()
    source = SOURCE.read_text()
    immediate = bytearray(array_bytes(header, 'ImmediateInit15E7'))
    assert len(immediate) == 215
    release_original0 = array_bytes(header, 'Original0')
    release_original1 = array_bytes(header, 'Original1')
    assert len(release_original0) == 215 and len(release_original1) == 10

    # 3.0f RIP-relative load: C4 E2 79 18 05 + disp32.
    probe = immediate.find(bytes.fromhex('c4e2791805'))
    assert probe >= 0
    field = probe + 5
    set_u32(immediate, field, CONST3 - (INIT + field + 4))

    # Main-path jump to the native dump (the only E9 in the body).
    jump = immediate.find(b'\xe9')
    assert jump >= 0
    set_u32(immediate, jump + 1, DUMP - (INIT + jump + 5))

    # Loverride's call to the native override (after offset 166).
    call = immediate.find(b'\xe8', 166)
    assert call >= 0
    set_u32(immediate, call + 1, OVERRIDE - (INIT + call + 5))

    patched1 = bytes.fromhex('e9caffffff9090909090')
    offset0 = INIT - CACHE_BASE
    offset1 = INIT + TAIL - CACHE_BASE

    header = replace_array(header, 'Original0', original0)
    header = replace_array(header, 'Patched0', immediate)
    header = replace_array(header, 'ImmediateInit15E7', immediate)
    header = replace_array(header, 'Original1', original1)
    header = replace_array(header, 'Patched1', patched1)
    header = replace_array(header, 'SrdSharedPatched1', patched1)
    header = replace_array(header, 'CacheUUID', uuid)

    patches = (
        'inline constexpr Patch Patches[] = {\n'
        f'    {{0x{offset0:X}ULL, Original0, Patched0, sizeof(Original0)}},\n'
        f'    {{0x{offset1:X}ULL, Original1, Patched1, sizeof(Original1)}}\n'
        '};'
    )
    header, count = re.subn(r'inline constexpr Patch Patches\[\] = \{.*?\n\};', patches,
                            header, count=1, flags=re.S)
    assert count == 1

    # The ImmediateSync-only guard must not read a Tahoe ComputeScratch offset.
    header, count = re.subn(r'(inline constexpr Patch ComputeScratchPatches\[\] = \{\n'
                            r'    LegacyBlendPatches\[0\],\n'
                            r'    LegacyBlendPatches\[1\],\n)'
                            r'    LegacyBlendPatches\[2\]',
                            r'\1    {0, nullptr, nullptr, 0}', header, count=1)
    assert count == 1

    guard_old = ('            const auto& extra = ComputeScratchPatches[i];\n'
                 '            if (readUser(')
    guard_new = ('            const auto& extra = ComputeScratchPatches[i];\n'
                 '            if (extra.size == 0) { continue; }\n'
                 '            if (readUser(')
    assert guard_old in source
    source = source.replace(guard_old, guard_new, 1)

    HEADER.write_text(header)
    SOURCE.write_text(source)

    print('cache UUID', uuid.hex())
    print('offset0', hex(offset0), 'offset1', hex(offset1))
    print('original0 sha', hashlib.sha256(original0).hexdigest())
    print('patched sha ', hashlib.sha256(immediate).hexdigest())
    print('original1', original1.hex())
    print('patched1 ', patched1.hex())
    print('release original0 diff', sum(a != b for a, b in zip(original0, release_original0)))
    print('release original1 diff', sum(a != b for a, b in zip(original1, release_original1)))
    print('3.0f field', hex(field), 'call', hex(call), 'jump', hex(jump))


if __name__ == '__main__':
    main()
