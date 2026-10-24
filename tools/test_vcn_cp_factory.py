"""执行原生内容保护工厂与 KE 分发器；先运行 build_video_decode_patch.py，不访问 GPU。"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from audit_video_driver import symbols
from metal_cache import CACHE, CacheReader
from test_video_driver_identify import byte_lines
from legacy_cp_factory import factory

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/vcn-cp-factory'
FACTORY = '__ZN11VACPFactory8createCPEP11_VAHWConfigP11VAIOConnectPP4VACp'
HASH = 'e63a2259ad470bd088d623edc778be34f9f6630fc6156bf6cac808c381b15aa6'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    cache = CacheReader()
    try:
        lines = (CACHE / 'dyld_shared_cache_x86_64h.map').read_text().splitlines()
        path = '/System/Library/Extensions/AMDRadeonVADriver2.bundle/Contents/MacOS/AMDRadeonVADriver2'
        base = int(lines[lines.index(path) + 1].split()[1], 16)
        names, _ = symbols(cache.read, base)
        address = names[FACTORY]
        native = cache.read(address, min(a for a in names.values() if a > address) - address)
        assert len(native) == 184 and hashlib.sha256(native).hexdigest() == HASH
        assert native[0x3F:0x44] == bytes.fromhex('83f9047317')
        candidate = bytearray(native)
        if not args.baseline:
            records = json.loads((ROOT / 'build/vcn/user-patches.json').read_text())['records']
            rows = [record for record in records if record['label'] == FACTORY]
            assert len(rows) == 1 and rows[0]['fairplayDecode']
            for record in rows:
                offset = record['address'] - address
                before, after = bytes.fromhex(record['original']), bytes.fromhex(record['patched'])
                assert candidate[offset:offset + len(before)] == before and len(before) == len(after)
                candidate[offset:offset + len(after)] = after
            assert [i for i, (old, new) in enumerate(zip(native, candidate)) if old != new] == [0x41]
            assert candidate[0x41] == 5
        constructors = {0x54: '__ZN7VACpPspC1Ev', 0x6F: '__ZN8VACpSamuC1Ev'}
        for offset, name in constructors.items():
            assert native[offset] == 0xE8
            assert address + offset + 5 + struct.unpack_from('<i', native, offset + 1)[0] == names[name]
        replacements = [(0x49, 'allocate'), (0x54, 'psp'), (0x64, 'allocate'), (0x6F, 'samu'),
                        (0xAA, 'deallocate'), (0xB2, 'rethrow')]
        assembly = '.text\n'
        for label, code in [('stockFactory', native), ('candidateFactory', candidate)]:
            assembly += f'.globl _{label}\n_{label}:\n'
            cursor = 0
            for offset, name in replacements:
                assert code[offset] == 0xE8
                assembly += byte_lines(code[cursor:offset]) + f'\ncallq _{name}\n'
                cursor = offset + 5
            assembly += byte_lines(code[cursor:]) + '\n'
        a = names['__ZN12AvdVAContext14getKEParameterEjPv']
        dispatch = cache.read(a, 48)
        assert hashlib.sha256(dispatch).hexdigest() == 'e24d593242ccf83e40f9a048db86d0c9f44323c9043503117ea2902c85608a09'
        assembly += '.globl _nativeGetKE\n_nativeGetKE:\n' + byte_lines(dispatch) + '\n'
    finally:
        cache.close()
    profiles = json.loads((ROOT / 'tools/fixtures/legacy-compat.json').read_text())['profiles']
    for profile in profiles:
        major = profile['major']
        _, original, patched, offset = factory(profile)
        assert [i for i, (a, b) in enumerate(zip(original, patched)) if a != b] == [offset]
        calls = [0x40, 0x4B, 0x5B, 0x66, 0xA9, 0xB1] if major == 23 else [0x49, 0x54, 0x64, 0x6F, 0xAA, 0xB2]
        replacements = list(zip(calls, ['allocate', 'psp', 'allocate', 'samu', 'deallocate', 'rethrow']))
        for label, code in [(f'stockFactory{major}', original),
                            (f'candidateFactory{major}', original if args.baseline else patched)]:
            assembly += f'.globl _{label}\n_{label}:\n'
            cursor = 0
            for offset, name in replacements:
                assert code[offset] == 0xE8
                assembly += byte_lines(code[cursor:offset]) + f'\ncallq _{name}\n'
                cursor = offset + 5
            assembly += byte_lines(code[cursor:]) + '\n'
    source = r'''
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
alignas(32) static std::array<uint8_t,0x1a8> object;
static unsigned selected,allocations,initializations,destructions;
static int initStatus;
extern "C" int stockFactory(void*,void*,void*,void**);
extern "C" int candidateFactory(void*,void*,void*,void**);
extern "C" int stockFactory23(void*,void*,void*,void**);
extern "C" int candidateFactory23(void*,void*,void*,void**);
extern "C" int stockFactory24(void*,void*,void*,void**);
extern "C" int candidateFactory24(void*,void*,void*,void**);
extern "C" int nativeGetKE(void*,unsigned,void*);
using Factory=int(*)(void*,void*,void*,void**);
static int init(void* p,void* connection){assert(p==object.data()&&connection==reinterpret_cast<void*>(0x1234));initializations++;return initStatus;}
static void destroy(void* p){assert(p==object.data());destructions++;}
static void* table[3]={nullptr,reinterpret_cast<void*>(destroy),reinterpret_cast<void*>(init)};
extern "C" void* allocate(size_t size){assert(size==object.size());allocations++;object.fill(0);return object.data();}
static void construct(void* p,unsigned type){assert(p==object.data());selected=type;void* v=table;memcpy(p,&v,sizeof(v));}
extern "C" void psp(void* p){construct(p,2);}
extern "C" void samu(void* p){construct(p,1);}
extern "C" void deallocate(void*){abort();}
extern "C" void rethrow(void*){abort();}
static int exchangeResult;static unsigned exchanges;static void* exchangeInput;
static int exchange(void* p,unsigned tag,void* bytes){assert(p==object.data()&&tag==8&&bytes==exchangeInput);exchanges++;return exchangeResult;}
int main(){
 std::array<uint8_t,16> config{};
 for(auto pair:{std::array<Factory,2>{stockFactory,candidateFactory},
                std::array<Factory,2>{stockFactory23,candidateFactory23},
                std::array<Factory,2>{stockFactory24,candidateFactory24}})
 for(auto function:pair)for(unsigned type=0;type<65536;type++){
  memcpy(config.data()+12,&type,4);selected=allocations=initializations=destructions=0;initStatus=0;void* output=reinterpret_cast<void*>(0x5678);
  unsigned expected=type==0?1:type<=4?2:0;
#ifndef BASELINE
  if(function==pair[1]&&type==5)expected=2;
#endif
  assert(function(nullptr,config.data(),reinterpret_cast<void*>(0x1234),&output)==0);
  assert(selected==expected&&output==(expected?object.data():nullptr));
  assert(allocations==unsigned(expected!=0)&&initializations==allocations&&destructions==0);
  if(expected){
   initStatus=42;destructions=0;output=reinterpret_cast<void*>(0x5678);
   assert(function(nullptr,config.data(),reinterpret_cast<void*>(0x1234),&output)==42);
   assert(output==nullptr&&destructions==1);
  }
 }
 for(auto function:{stockFactory,candidateFactory,stockFactory23,candidateFactory23,stockFactory24,candidateFactory24}){
  void* output=reinterpret_cast<void*>(0x5678);
  assert(function(nullptr,nullptr,reinterpret_cast<void*>(0x1234),&output)==5&&output==reinterpret_cast<void*>(0x5678));
  assert(function(nullptr,config.data(),nullptr,&output)==5&&output==reinterpret_cast<void*>(0x5678));
  assert(function(nullptr,config.data(),reinterpret_cast<void*>(0x1234),nullptr)==5);
 }
 // 未建立 CP 对象或未启用 CP 时，原生 KE 分发器拒绝；有效 CP 的返回码必须逐字透传。
 std::array<uint8_t,16> wrapper{};std::array<uint8_t,0x4e0> context{};
 void* pointer=context.data();memcpy(wrapper.data()+8,&pointer,8);
 std::array<void*,6> cpTable{};cpTable[5]=reinterpret_cast<void*>(exchange);pointer=cpTable.data();memcpy(object.data(),&pointer,8);
 int input{};exchangeInput=&input;exchanges=0;
 assert(nativeGetKE(wrapper.data(),8,&input)==1&&exchanges==0);
 pointer=object.data();memcpy(context.data()+0x4d8,&pointer,8);
 assert(nativeGetKE(wrapper.data(),8,&input)==1&&exchanges==0);
 context[0x20]=1;
 for(int status:{0,1,9,42}){exchangeResult=status;assert(nativeGetKE(wrapper.data(),8,&input)==status);}
 assert(exchanges==4);
#ifdef BASELINE
 puts("PASS baseline: native CP factory returns success with null for enum 5; KE requires a real CP object");
#else
 puts("PASS: Darwin 23/24/25, 65536 CP families each; only enum 5 adds native PSP; bad inputs and init cleanup preserved; native Tahoe KE errors preserved");
#endif
}
'''
    cpp, asm, exe = WORK / 'test.cpp', WORK / 'factory.s', WORK / 'test'
    cpp.write_text(source)
    asm.write_text(assembly)
    flags = ['-DBASELINE'] if args.baseline else []
    subprocess.run(['xcrun', 'clang++', '-std=c++23', '-O2', '-Wall', '-Wextra', '-Werror', *flags,
                    str(cpp), str(asm), '-o', str(exe)], check=True, timeout=60)
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    (WORK / ('baseline.log' if args.baseline else 'candidate.log')).write_text(result.stdout + result.stderr)
    print(result.stdout + result.stderr, end='')
    result.check_returncode()


if __name__ == '__main__':
    main()
