"""在 CPU 执行 14/15 的完整原生 gate/store 块，核实策略及无关状态保留。"""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/legacy-blend-multi-os'


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    fixture = json.loads((ROOT / 'tools/fixtures/legacy-compat.json').read_text())
    definitions = ''
    for profile, size in zip(fixture['profiles'], [0x51, 0x73]):
        gate = profile['metal']['gate'] & 4095
        code = bytes.fromhex(profile['metal']['page'])[gate - 14:gate - 14 + size]
        assert code[14:16] == b'\x75\x0b'
        definitions += f'static const uint8_t code{profile["major"]}[]={{' + ','.join(map(str, code)) + '};\n'
    harness = r'''
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <sys/mman.h>
DEFINITIONS
extern "C" void invoke23(void*,void*,void*,unsigned,void*);
extern "C" void invoke24(void*,void*,void*,unsigned,void*);
asm(".text\n.globl _invoke23\n_invoke23:\npushq %rbp\nmovq %rsp,%rbp\npushq %rbx\npushq %r12\npushq %r14\nsubq $512,%rsp\n"
    "movq %rdi,%r12\nxorq %r14,%r14\nmovq %rsi,-440(%rbp)\nmovq %rdx,-392(%rbp)\nmovl %ecx,-384(%rbp)\nmovq $0,-400(%rbp)\n"
    "callq *%r8\naddq $512,%rsp\npopq %r14\npopq %r12\npopq %rbx\npopq %rbp\nretq\n");
asm(".text\n.globl _invoke24\n_invoke24:\npushq %rbp\nmovq %rsp,%rbp\npushq %rbx\npushq %r12\npushq %r14\nsubq $512,%rsp\n"
    "movq %rdi,-376(%rbp)\nmovq %rsi,-472(%rbp)\nmovq %rdx,-384(%rbp)\nmovl %ecx,-400(%rbp)\nmovq $0,-448(%rbp)\n"
    "movq $0,-432(%rbp)\nmovq $0,-408(%rbp)\nmovq $0,-416(%rbp)\ncallq *%r8\n"
    "addq $512,%rsp\npopq %r14\npopq %r12\npopq %rbx\npopq %rbp\nretq\n");
int main(){
 unsigned total=0,defects=0;uint32_t random=1;
 for(unsigned major:{23U,24U}){
  const auto* code=major==23?code23:code24;const auto size=major==23?sizeof(code23):sizeof(code24);
  auto* p=static_cast<uint8_t*>(mmap(nullptr,4096,3,MAP_PRIVATE|MAP_ANON,-1,0));assert(p!=MAP_FAILED);
  memcpy(p,code,size);p[size]=0xc3;memcpy(p+256,p,size+1);p[256+14]=0xEB;assert(mprotect(p,4096,5)==0);
  auto invoke=major==23?invoke23:invoke24;
  for(bool hardware:{false,true})for(bool dual:{false,true})for(bool enabled:{false,true})for(unsigned i=0;i<1024;i++){
   std::array<uint8_t,512> original,patched;std::array<uint8_t,180> info{};std::array<uint8_t,64> settings{};
   for(auto& b:original){random=random*1664525+1013904223;b=uint8_t(random>>24);}patched=original;
   info[0xA0]=hardware?2:0;uint64_t flags=uint64_t(enabled)<<34;memcpy(settings.data()+8,&flags,8);
   auto expectOriginal=original,expectPatched=patched;auto infoBefore=info;auto settingsBefore=settings;
   uint32_t value;memcpy(&value,original.data()+0x28,4);
   auto oldValue=(value&~1U)|unsigned(hardware&&(dual||!enabled));auto newValue=(value&~1U)|unsigned(dual||!enabled);
   memcpy(expectOriginal.data()+0x28,&oldValue,4);memcpy(expectPatched.data()+0x28,&newValue,4);
   invoke(original.data(),info.data(),settings.data(),dual,p);invoke(patched.data(),info.data(),settings.data(),dual,p+256);
   assert(original==expectOriginal&&patched==expectPatched&&info==infoBefore&&settings==settingsBefore);
   defects+=oldValue!=newValue;total++;
  }
  assert(munmap(p,4096)==0);
 }
 assert(defects>0);printf("PASS %u actual Sonoma/Sequoia gate/store cases; %u original defects reproduced; dual/RB+ conditions and other bits preserved\n",total,defects);
}
'''.replace('DEFINITIONS', definitions)
    path, exe = WORK / 'test.cpp', WORK / 'test'
    path.write_text(harness)
    subprocess.run(['xcrun', 'clang++', '-std=c++23', '-O2', '-Wall', '-Wextra', '-Werror', str(path), '-o', str(exe)],
                   check=True, timeout=60)
    subprocess.run([str(exe)], check=True, timeout=60)


if __name__ == '__main__':
    main()
