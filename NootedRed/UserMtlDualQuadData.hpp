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

    // dyld_shared_cache_x86_64h magic + pinned 24H32 UUID。
    inline constexpr UInt8 CacheMagic[] = {0x64, 0x79, 0x6C, 0x64, 0x5F, 0x76, 0x31, 0x20,
                                           0x20, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20};
    inline constexpr UInt8 CacheUUID[] = {0x42, 0xEC, 0x28, 0x90, 0xBC, 0x40, 0x31, 0x7E,
                                          0xBB, 0x27, 0x45, 0x14, 0x2B, 0xFF, 0x7F, 0x26};
    inline constexpr UInt64 CacheBase = 0x7FF800000000ULL;

    // gfx9_HwInfoInitData 分支 A（src+8 <= 0x13）里 `or ecx, 0x4d`：
    // 置 HwInfo+0xa0 bit1，使 dual-quad 关闭位按 Vega12 分支语义生效。
    // 站点 VA = 0x7FFB087EF53E，offset = VA - CacheBase。
    inline constexpr UInt8 Original0[] = {0x83, 0xC9, 0x4D};
    inline constexpr UInt8 Patched0[]  = {0x83, 0xC9, 0x4F};
    inline constexpr Patch Patches[]   = {{0x3087EF53EULL, Original0, Patched0, sizeof(Original0)}};

    constexpr UInt32 MaxPatchSize = 16;
    static_assert(sizeof(Original0) <= MaxPatchSize);

}    // namespace UserMtlDualQuadData
