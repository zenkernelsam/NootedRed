// 按 Darwin 主版本选择 ABI 和调用点；目标代码仍须逐项核验。
#pragma once
#include <VCNKernelData.hpp>
#include <VCNLegacyKernelData.hpp>

namespace VCNKernelProfiles
{

    inline const VCNKernelData::SlotGuard* guards(UInt32 major, bool old)
    {
        if (major == 23 || major == 24) {
            return old ? VCNLegacyKernelData::Guards5000 : VCNLegacyKernelData::Guards6000;
        }
        if (major == 25) { return old ? VCNKernelData::Guards5000 : VCNKernelData::Guards6000; }
        return nullptr;
    }

    inline const VCNKernelData::Patch* patches(UInt32 major, size_t& count)
    {
        count = 0;
        if (major == 23) {
            count = sizeof(VCNLegacyKernelData::Patches23) / sizeof(VCNLegacyKernelData::Patches23[0]);
            return VCNLegacyKernelData::Patches23;
        }
        if (major == 24) {
            count = sizeof(VCNLegacyKernelData::Patches24) / sizeof(VCNLegacyKernelData::Patches24[0]);
            return VCNLegacyKernelData::Patches24;
        }
        if (major == 25) {
            count = sizeof(VCNKernelData::Patches) / sizeof(VCNKernelData::Patches[0]);
            return VCNKernelData::Patches;
        }
        return nullptr;
    }

}    // namespace VCNKernelProfiles
