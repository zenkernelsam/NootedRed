"""以宿主 IOKit 模型执行完整 EC 服务和生产显卡回调，不访问本机硬件。"""
from pathlib import Path
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]


def extract(source, name):
    start = source.index(name)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def without_includes(source):
    return '\n'.join(line for line in source.splitlines()
                     if not line.startswith(('#include', '#pragma once')))


def main():
    fixture = (ROOT / 'tools/fixtures/ec-backlight-service.cpp.in').read_text()
    backlight = (ROOT / 'NootedRed/Backlight.cpp').read_text()
    source = fixture.replace('SERVICE_HEADER', without_includes((ROOT / 'NootedRed/ECBacklight.hpp').read_text()))
    source = source.replace('SERVICE_SOURCE', without_includes((ROOT / 'NootedRed/ECBacklight.cpp').read_text()))
    source = source.replace('SET_CALLBACK', extract(backlight, 'IOReturn Backlight::wrapSetAttributeForConnection('))
    source = source.replace('GET_CALLBACK', extract(backlight, 'IOReturn Backlight::wrapGetAttributeForConnection('))
    with tempfile.TemporaryDirectory(prefix='nred-ec-service-') as work:
        cpp, exe = Path(work) / 'test.cpp', Path(work) / 'test'
        cpp.write_text(source)
        sanitizer = 'thread' if '--thread-sanitizer' in sys.argv else 'address,undefined'
        subprocess.run(['xcrun', 'clang++', '-std=c++23', '-Wall', '-Wextra', '-Werror',
                        f'-fsanitize={sanitizer}', str(cpp), '-o', str(exe)], check=True, timeout=60)
        subprocess.run([str(exe)], check=True, timeout=60)


if __name__ == '__main__':
    main()
