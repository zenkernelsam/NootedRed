"""从固定参考页面核验旧系统 CP 工厂，仅扩展既有 PSP family 范围。"""
import hashlib


# 原始字节来自 legacy-compat.json 的已记录参考提交和页面。
FACTORIES = {
    23: (0x7FFB033A69C2, 182, 0x38, 'b1c45d251edb628dfb6e182f94c469ca8d11ec70f528273b8477f0536c12de7a'),
    24: (0x7FFB0860FE86, 183, 0x41, '4d6e96cf8b2b6ad3fb7aaa17b0b5a93c7649fed9308912caeaf851dbf3c19b84'),
}


def factory(profile):
    address, size, offset, digest = FACTORIES[profile['major']]
    page = next(p for p in profile['pages'] if p['address'] <= address
                and address + size <= p['address'] + 4096)
    start = address - page['address']
    original = bytes.fromhex(page['bytes'])[start:start + size]
    if hashlib.sha256(original).hexdigest() != digest:
        raise ValueError('旧系统 CP 工厂完整函数不匹配')
    if original[offset - 2:offset + 3] != bytes.fromhex('83f9047317'):
        raise ValueError('旧系统 PSP family 分支不匹配')
    patched = bytearray(original)
    patched[offset] = 5
    return address, original, bytes(patched), offset
