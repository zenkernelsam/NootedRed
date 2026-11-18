"""检查最终 kext 中 IOKit 类的公开元数据，覆盖隐藏可见性和 Release 裁剪。"""
from pathlib import Path
import argparse
import plistlib
import re
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('kext', type=Path)
    args = parser.parse_args()
    info = plistlib.loads((args.kext / 'Contents/Info.plist').read_bytes())
    binary = args.kext / 'Contents/MacOS' / info['CFBundleExecutable']
    symbols = subprocess.check_output(['nm', '-arch', 'x86_64', '-m', str(binary)], text=True, timeout=30)
    imports = set(re.findall(r'\(undefined\)\s+(?:weak\s+)?external\s+(\S+)', symbols))
    if '__ZN20IOACPIPlatformDevice9metaClassE' in imports:
        libraries = info.get('OSBundleLibraries_x86_64', info.get('OSBundleLibraries', {}))
        assert 'com.apple.iokit.IOACPIFamily' in libraries, (
            'IOACPIPlatformDevice::metaClass 由 IOACPIFamily 导出；'
            'OpenCore 普通符号解析只查直接依赖，不能通过 AppleACPIPlatform 间接引用')
    exported = set()
    for line in symbols.splitlines():
        match = re.match(r'^\S+\s+\((?!undefined)[^)]*\)\s+external\s+(\S+)$', line)
        if match:
            exported.add(match.group(1))
    classes = sorted({p['IOClass'] for p in info['IOKitPersonalities'].values()
                      if p.get('CFBundleIdentifier') == info['CFBundleIdentifier']})
    assert 'NRedECBacklight' in classes, '最终 Info.plist 缺少 EC 服务的匹配项'
    missing = []
    for name in classes:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise AssertionError(f'需要为带命名空间的 IOClass 增加名称解析：{name}')
        encoded = f'{len(name)}{name}'
        required = [f'__ZTV{encoded}', f'__ZTVN{encoded}9MetaClassE',
                    f'__ZN{encoded}9metaClassE', f'__ZN{encoded}10gMetaClassE',
                    f'__ZN{encoded}10superClassE']
        missing.extend(s for s in required if s not in exported)
    assert not missing, 'IOKit 类元数据未公开或已被裁剪：\n' + '\n'.join(missing)
    print('PASS: exported class/metaclass vtables and metadata:', ', '.join(classes))
    print('PASS: imported ACPI metaclass has a direct provider dependency')


if __name__ == '__main__':
    main()
