"""在 CPU 模拟缓冲上执行真实提交守卫，不创建 Metal 设备或提交 GPU 命令。"""
import ctypes
import mmap
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(sys.argv.pop(1)) if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else Path(
    __file__).with_name('submit_guard.s')


def assemble(source):
    with tempfile.TemporaryDirectory(prefix='nred-guard-test-') as work:
        obj = Path(work) / 'guard.o'
        subprocess.run(['as', '-arch', 'x86_64', str(source), '-o', str(obj)], check=True, timeout=60)
        data = obj.read_bytes()
    assert struct.unpack_from('<I', data)[0] == 0xFEEDFACF
    cursor = 32
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        command, size = struct.unpack_from('<II', data, cursor)
        if command == 0x19:
            for index in range(struct.unpack_from('<I', data, cursor + 64)[0]):
                section = cursor + 72 + index * 80
                if data[section:section + 16].rstrip(b'\0') == b'__text':
                    length, offset = struct.unpack_from('<QI', data, section + 40)
                    return data[offset:offset + length]
        cursor += size
    raise AssertionError('缺少 __text')


class Guard:
    def __init__(self, payload):
        placeholder = struct.pack('<Q', 0x1122334455667788)
        assert payload.count(placeholder) == 1
        self.mapping = mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
        self.address = ctypes.addressof(ctypes.c_char.from_buffer(self.mapping))
        continuation = self.address + len(payload)
        self.mapping.write(payload.replace(placeholder, struct.pack('<Q', continuation)) + b'\xc9\xc3')
        self.call = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint64)(self.address)

    def run(self, words, used=None, streams=None):
        memory = (ctypes.c_uint32 * len(words))(*words)
        start = ctypes.addressof(memory)
        used = len(words) if used is None else used
        specifications = [(0, used, len(words))] if streams is None else streams
        records, objects = [], []
        for begin, end, limit in specifications:
            record = (ctypes.c_uint64 * 8)(0, start + begin * 4, start + limit * 4, start + end * 4)
            obj = ctypes.create_string_buffer(784)
            struct.pack_into('<Q', obj, 776, ctypes.addressof(record))
            records.append(record)
            objects.append(obj)
        buffers = (ctypes.c_void_p * len(objects))(*(ctypes.addressof(obj) for obj in objects))
        self.call(None, None, buffers, len(objects))
        return list(memory)


class SafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = Guard(assemble(SOURCE))

    def test_unknown_header_preserves_following_data(self):
        words = [0x40000000, 0, 0, 0x3F800000, 0xC0016900, 7, 0]
        self.assertEqual(self.guard.run(words), words)

    def test_unknown_zero_terminator_preserves_long_zero_run(self):
        words = [0] * 8 + [0x40000000, 0xC0016900, 7, 0]
        self.assertEqual(self.guard.run(words), words)

    def test_type0_shaped_zero_terminator_preserves_data(self):
        words = [0, 0, 0x00000004, 0x3F800000]
        self.assertEqual(self.guard.run(words), words)

    def test_truncated_type3_terminator_preserves_data(self):
        words = [0, 0, 0xC1006900, 7, 0]
        self.assertEqual(self.guard.run(words), words)

    def test_used_zeros_before_valid_type3_are_untouched(self):
        words = [0, 0, 0xC0016900, 7, 0]
        self.assertEqual(self.guard.run(words), words)

    def test_valid_type3_and_type0_payloads_are_untouched(self):
        words = [0xC0086900, 7] + [0] * 8 + [0x00010004, 0, 0]
        self.assertEqual(self.guard.run(words), words)

    def test_type2_padding_then_used_zeros_are_untouched(self):
        words = [0x80000001, 0, 0xC0016900, 7, 0]
        self.assertEqual(self.guard.run(words), words)

    def test_used_trailing_zeros_are_untouched(self):
        words = [0xC0016900, 7, 0] + [0] * 10
        self.assertEqual(self.guard.run(words), words)

    def test_used_prefix_is_preserved_while_tail_is_filled(self):
        words = [0, 0, 0xC0016900, 7, 0, 0xDEADBEEF, 0]
        self.assertEqual(self.guard.run(words, used=5), words[:5] + [0x80000000] * 2)

    def test_tail_fill_is_retained(self):
        words = [0xC0016900, 7, 0, 0xDEADBEEF, 0, 0x12345678]
        self.assertEqual(self.guard.run(words, used=3), words[:3] + [0x80000000] * 3)

    def test_same_batch_next_stream_is_preserved(self):
        words = [0xC0016900, 7, 0, 0, 0, 0xC0016900, 8, 0]
        self.assertEqual(self.guard.run(words, streams=[(0, 3, 8), (5, 8, 8)]),
                         words[:3] + [0x80000000] * 2 + words[5:])


if __name__ == '__main__':
    unittest.main(verbosity=2)
