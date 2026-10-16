#!/usr/bin/env python3
"""Build in a disposable tree with byte-verified #embed lowering for Apple clang 14.

No runtime source or firmware in the Git checkout is rewritten. Retire this
adapter when a compiler supporting the source's #embed and c++23 is available.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    tree = out / 'xcode14-tree'
    if tree.exists():
        raise SystemExit('Use a new output directory; refusing to replace an existing build tree')
    tree.mkdir()
    for name in ['NootedRed', 'NootedRed.xcodeproj']:
        shutil.copytree(ROOT / name, tree / name)
    for name in ['Lilu', 'MacKernelSDK', 'README.md', 'LICENSE.md']:
        (tree / name).symlink_to(ROOT / name)
    # The retained pre-AppleForge Xcode archive phase expects this filename.
    (tree / 'LICENSE').symlink_to(ROOT / 'LICENSE.md')
    embeds = []
    sources = []
    declarations = []

    def lower(match):
        relative = match.group(1)
        suffix = match.group(2).strip()
        if suffix not in ['', r"suffix(, '\0')"]:
            raise ValueError('Unsupported embed suffix')
        path = (ROOT / 'NootedRed' / relative).resolve()
        if not any(path.is_relative_to((ROOT / 'NootedRed' / name).resolve())
                   for name in ['Firmware', 'Personalities']):
            raise ValueError('Unexpected embed input directory')
        data = path.read_bytes()
        if not data:
            raise ValueError('Unexpected empty firmware input')
        embeds.append({'path': relative, 'size': len(data), 'sha256': sha(data), 'null_suffix': bool(suffix)})
        if suffix:
            data += b'\0'
        # Character literals preserve each byte without signed-char narrowing.
        rows = [','.join("'\\x%02x'" % b for b in data[i:i + 24])
                for i in range(0, len(data), 24)]
        return ',\n'.join(rows)

    for source in sorted((ROOT / 'NootedRed').rglob('*.cpp')):
        original = source.read_bytes()
        if b'#embed' not in original:
            continue
        text = original.decode()
        lowered = re.sub(r'^#embed "([^"\n]+)"([^\n]*)$', lower, text, flags=re.M)
        if re.search(r'^\s*#embed\b', lowered, re.M):
            raise ValueError('Unsupported embed syntax')
        (tree / source.relative_to(ROOT)).write_text(lowered)
        sources.append({'path': str(source.relative_to(ROOT)), 'original_sha256': sha(original),
                        'lowered_sha256': sha(lowered.encode())})
        selected = dict(re.findall(r'static const char (\w+)\[\] = \{\n(.*?)\n\};', lowered, re.S))
        for name, relative, suffix in re.findall(
                r'static const char (\w+)\[\] = \{\n#embed "([^"\n]+)"([^\n]*)\n\};', text):
            declarations.append((name, relative, bool(suffix.strip()), selected[name]))
    if not embeds:
        raise ValueError('No embeds found')
    # Compile and read back the actual lowered declarations against every input.
    assert len(declarations) == len(embeds)
    harness = '#include <fstream>\n#include <vector>\n#include <cassert>\n#include <cstring>\n#include <cstdio>\n'
    for name, relative, suffix, body in declarations:
        harness += f'static const char {name}[] = {{\n{body}\n}};\n'
    harness += 'int main(){\n'
    for name, relative, suffix, body in declarations:
        path = str(ROOT / 'NootedRed' / relative)
        harness += ('{std::ifstream f(' + json.dumps(path) + ', std::ios::binary);'
                    'assert(f.good()); std::vector<char> b((std::istreambuf_iterator<char>(f)), {});'
                    + ('b.push_back(0);' if suffix else '') +
                    f'assert(b.size()==sizeof({name})); assert(!memcmp(b.data(),{name},b.size()));}}\n')
    harness += f'puts("PASS {len(embeds)} embedded arrays: exact size, suffix and byte readback");}}\n'
    (out / 'firmware-readback.cpp').write_text(harness)
    clang = subprocess.check_output(['xcrun', '--find', 'clang++'], text=True).strip()
    sdk = subprocess.check_output(['xcrun', '--show-sdk-path'], text=True).strip()
    subprocess.run([clang, '-isysroot', sdk, '-std=c++2b', '-O0', str(out / 'firmware-readback.cpp'),
                    '-o', str(out / 'firmware-readback')], check=True, timeout=120)
    subprocess.run([str(out / 'firmware-readback')], check=True, timeout=30)
    manifest = {'sources': sources, 'embedded_inputs': embeds,
                'compiler': subprocess.check_output([clang, '--version'], text=True),
                'transform': 'plain #embed to character literal array elements; exact byte/size validation',
                'retirement': 'Use compiler with native #embed support; remove disposable adapter tree'}
    (out / 'compat-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    command = ['xcodebuild', '-project', str(tree / 'NootedRed.xcodeproj'),
               '-target', 'NootedRed', '-configuration', 'Release', '-arch', 'x86_64', 'build',
               'CLANG_CXX_LANGUAGE_STANDARD=c++2b', 'GCC_C_LANGUAGE_STANDARD=c2x',
               'CONFIGURATION_BUILD_DIR=' + str(out / 'Release'),
               'OBJROOT=' + str(out / 'obj'), 'SYMROOT=' + str(out / 'sym'),
               'CODE_SIGNING_ALLOWED=NO']
    (out / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    with (out / 'build.log').open('w') as log:
        result = subprocess.run(command, cwd=tree, stdout=log, stderr=subprocess.STDOUT, timeout=240)
    (out / 'build.rc').write_text(str(result.returncode) + '\n')
    for line in (out / 'build.log').read_text().splitlines():
        if 'error:' in line or '** BUILD' in line:
            print(line)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
