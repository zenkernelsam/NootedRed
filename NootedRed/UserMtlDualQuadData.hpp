// MTL dual-quad 补丁的固定 24H32 数据；逐字节核对自 pinned dyld 缓存。
#pragma once
#include <IOKit/IOTypes.h>
namespace UserMtlDualQuadData
{

    struct Patch
    {
        UInt64       offset;
        const UInt8* original;
        const UInt8* patched;
        UInt32       size;
    };

    // dyld_shared_cache_x86_64h magic "dyld_v1 x86_64h\0" + pinned 24H32 UUID。
    inline constexpr UInt8 CacheMagic[] = {0x64, 0x79, 0x6C, 0x64, 0x5F, 0x76, 0x31, 0x20,
                                           0x78, 0x38, 0x36, 0x5F, 0x36, 0x34, 0x68, 0x00};
    inline constexpr UInt8 CacheUUID[] = {0x42, 0xEC, 0x28, 0x90, 0xBC, 0x40, 0x31, 0x7E,
                                          0xBB, 0x27, 0x45, 0x14, 0x2B, 0xFF, 0x7F, 0x26};
    inline constexpr UInt64 CacheBase = 0x7FF800000000ULL;

    // AMD_render_cmd_encoder 管线建立里的 dual-quad 判定（条件式，不翻 HwInfo 位）：
    //   test byte [rax+0xa0], 2          HwInfo+0xa0 bit1 门（Renoir 上为 0）
    //   75 0b  jnz +0xb                  bit1 置位才评估条件；Renoir 直接 xor eax,eax 跳过
    //   ...    cmp [rbp-0x190],0 / setnz cl          → cl = dualSource
    //   ...    test [r11+rax+8], 0x400000000         → RB+ 能力位
    //   0f 94 c0  setz al                             → al = !allowRBPlus
    //   08 c8  or al,cl                               → DISABLE_DUAL_QUAD = dualSource || !RB+
    // 修复语义：P0 `75 0b -> eb 0b` 恒评估条件；P1 `0f 94 c0 -> 31 c0 90` 令 !RB+ 项为假，
    // 结果 DISABLE_DUAL_QUAD = dualSource——只关 dual-source 管线的 dual-quad，
    // 不翻 HwInfo+0xa0、不全局放行 RB+（避免 DeviceSettings bit34 广域副作用）。
    // 依据：xiaot-evo 源码级 `or ecx,0x4d -> 0x4f` 同款改动在 Chromium/Electron 出
    // 黑块/缩略图缺失并已于其 8d6b048 回退、恢复 gate/setz 条件式；本实现为该验证
    // 形态的 userspace 定点等价（独立核对 pinned 24H32 缓存字节，非直接移植数据）。
    // 站点 VA：P0 = 0x7FFB087B3164，P1 = 0x7FFB087B3198；offset = VA - CacheBase。
    inline constexpr UInt8 Original0[] = {0x75, 0x0B};
    inline constexpr UInt8 Patched0[]  = {0xEB, 0x0B};
    inline constexpr UInt8 Original1[] = {0x0F, 0x94, 0xC0};
    inline constexpr UInt8 Patched1[]  = {0x31, 0xC0, 0x90};
    inline constexpr Patch Patches[]   = {
        {0x3087B3164ULL, Original0, Patched0, sizeof(Original0)},
        {0x3087B3198ULL, Original1, Patched1, sizeof(Original1)},
    };

    constexpr UInt32 MaxPatchSize = 16;
    static_assert(sizeof(Original0) <= MaxPatchSize);
    static_assert(sizeof(Original1) <= MaxPatchSize);

    // 站点上下文窗口（VA 0x7FFB087B3150，96B，同页），覆盖完整判定序列：
    // HwInfo+0xa0 bit1 test、<P0 jnz>、xor/mov/jmp 跳过分支、dualSource cmp+setnz、
    // movabs 0x400000000 + test RB+、<P1 setz>、or al,cl 及前后寄存器重载。
    // P0 站点字节位于窗口 +0x14，P1 位于 +0x48；全窗除站点字节外必须逐字节匹配，
    // 站点字节允许 original 或 patched——已修补页按幂等跳过。
    inline constexpr UInt64 ContextOffset  = 0x3087B3150ULL;
    inline constexpr UInt8  Context[]      = {
        0x18, 0xE9, 0xA9, 0xFB, 0xFF, 0xFF, 0x48, 0x8B, 0x85, 0x28, 0xFE, 0xFF, 0xFF, 0xF6, 0x80,
        0xA0, 0x00, 0x00, 0x00, 0x02, 0x75, 0x0B, 0x31, 0xC0, 0x4C, 0x8B, 0x9D, 0x80, 0xFE, 0xFF,
        0xFF, 0xEB, 0x2C, 0x83, 0xBD, 0x70, 0xFE, 0xFF, 0xFF, 0x00, 0x0F, 0x95, 0xC1, 0x4C, 0x8B,
        0x9D, 0x80, 0xFE, 0xFF, 0xFF, 0x48, 0x8B, 0x85, 0x40, 0xFE, 0xFF, 0xFF, 0x48, 0xBA, 0x00,
        0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00, 0x49, 0x85, 0x54, 0x03, 0x08, 0x0F, 0x94, 0xC0,
        0x08, 0xC8, 0x4C, 0x8B, 0xB5, 0x88, 0xFE, 0xFF, 0xFF, 0x48, 0x8B, 0xB5, 0x50, 0xFE, 0xFF,
        0xFF, 0x4C, 0x8B, 0xA5, 0x68, 0xFE};
    static_assert(sizeof(Context) == 96);

    // 镜像 provenance：站点 VA 位于
    // /System/Library/Extensions/AMDRadeonX5000MTLDriver.bundle/Contents/MacOS/AMDRadeonX5000MTLDriver
    // __TEXT（镜像基址 VA 0x7FFB08757000，AMD_render_cmd_encoder 管线建立段）。
    // 该镜像 x86_64h LC_UUID 与 `dyld_info -uuid` 实测一致；
    // GL 参照镜像 AMDRadeonX5000GLDriver UUID = BAD9208B-B99B-3DAA-AA51-15D199498873。
    // 运行时门 = cache UUID（整 build pin）+ 固定 VA + 96B 上下文窗；
    // ImageUUID 为证据级绑定（离线核验"站点页字节∈该镜像"），不是运行时附加条件。
    inline constexpr UInt8 ImageUUID[] = {0x2F, 0x00, 0xBC, 0x70, 0xA1, 0x92, 0x30, 0xB3,
                                          0xB6, 0x2D, 0x00, 0x39, 0xB8, 0xA8, 0xFC, 0xF3};
    inline constexpr UInt64 ImageVA = 0x7FFB08757000ULL;

}    // namespace UserMtlDualQuadData
