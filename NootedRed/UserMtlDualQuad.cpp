// 在进程私有代码页应用已核验的 MTL dual-quad 补丁；默认关闭、逐字节匹配、失败回滚。
#include <Headers/kern_util.hpp>
#include <IOKit/IOLib.h>
#include <NRed.hpp>
#include <PenguinWizardry/KernelVersion.hpp>
#include <UserMtlDualQuad.hpp>
#include <UserMtlDualQuadData.hpp>
#include <UserSharedCache.hpp>
#include <kern/task.h>
#include <mach/i386/vm_param.h>
#include <mach/vm_map.h>
#include <mach/vm_region.h>
#include <sys/proc.h>

namespace
{

    struct SharedRegionCheckArgs
    {
        user_addr_t startAddress;
    };
    static_assert(sizeof(SharedRegionCheckArgs) == 8);
    using SharedRegionCheck = int       (*)(proc_t, SharedRegionCheckArgs*, int*);
    using GetTaskMap        = vm_map_t         (*)(task_t);
    using RegionRecurse     = kern_return_t (*)(vm_map_t, mach_vm_address_t*, mach_vm_size_t*, natural_t*,
                                            vm_region_recurse_info_t, mach_msg_type_number_t*);
    using CopyIn            = int                  (*)(user_addr_t, void*, size_t);
    using CopyOut           = int                 (*)(const void*, user_addr_t, size_t);

    mach_vm_address_t originalCheck{0};
    GetTaskMap        getTaskMap{nullptr};
    RegionRecurse     queryRegion{nullptr};
    CopyIn            readUser{nullptr};
    CopyOut           writeUser{nullptr};
    UInt32            seenStages{0}, appliedCount{0}, errorCount{0};
    constexpr auto    PatchCount = arrsize(UserMtlDualQuadData::Patches);
    static_assert(PatchCount <= 4);

    enum class Stage : UInt32
    {
        Called,
        OriginalError,
        Argument,
        CacheRead,
        CacheMismatch,
        CodeRead,
        CodeMismatch,
        Already,
        Mixed,
        NoMap,
        Region,
        Protection,
        Write,
        Restore,
        Rollback,
        Applied
    };

    void stage(const Stage value)
    {
        static const char* const keys[] = {"NRedMtlDQD00", "NRedMtlDQD01", "NRedMtlDQD02", "NRedMtlDQD03",
                                           "NRedMtlDQD04", "NRedMtlDQD05", "NRedMtlDQD06", "NRedMtlDQD07",
                                           "NRedMtlDQD08", "NRedMtlDQD09", "NRedMtlDQD10", "NRedMtlDQD11",
                                           "NRedMtlDQD12", "NRedMtlDQD13", "NRedMtlDQD14", "NRedMtlDQD15"};
        const auto               index  = static_cast<UInt32>(value);
        if ((__atomic_fetch_or(&seenStages, 1U << index, __ATOMIC_RELAXED) & (1U << index)) == 0) {
            NRed::singleton().setProp32(keys[index], 1);
            if (value == Stage::Applied) { NRed::singleton().setProp32("NRedMtlDQApplied", 1); }
            SYSLOG("UserMtlDualQuad", "stage=%u", index);
        }
    }

    void error(const kern_return_t value)
    {
        auto count = __atomic_load_n(&errorCount, __ATOMIC_RELAXED);
        while (count < 8) {
            if (__atomic_compare_exchange_n(&errorCount, &count, count + 1, false, __ATOMIC_RELAXED, __ATOMIC_RELAXED))
            {
                NRed::singleton().setProp32("NRedMtlDQLastError", static_cast<UInt32>(value));
                SYSLOG("UserMtlDualQuad", "rejected: error=0x%X", value);
                break;
            }
        }
    }

    bool leafProtection(vm_map_t map, const mach_vm_address_t page, vm_prot_t& protection)
    {
        natural_t depth{0};
        for (UInt32 attempt = 0; attempt < 8; ++attempt) {
            auto                            address = page;
            mach_vm_size_t                  size{0};
            vm_region_submap_short_info_data_64_t info{};
            mach_msg_type_number_t count = VM_REGION_SUBMAP_SHORT_INFO_COUNT_64;
            const auto                      result =
                queryRegion(map, &address, &size, &depth, reinterpret_cast<vm_region_recurse_info_t>(&info), &count);
            if (result != KERN_SUCCESS) {
                error(result);
                return false;
            }
            if (count < VM_REGION_SUBMAP_SHORT_INFO_COUNT_64 || address > page || page - address >= size
                || size - (page - address) < PAGE_SIZE)
            {
                return false;
            }
            if (!info.is_submap) {
                protection = info.protection;
                return true;
            }
            if (depth == ~0U) { break; }
            ++depth;
        }
        return false;
    }

    struct Page
    {
        mach_vm_address_t address{0};
        vm_prot_t         protection{VM_PROT_NONE};
        bool              writable{false};
    };

    int wrappedSharedRegionCheck(proc_t process, SharedRegionCheckArgs* args, int* returnValue)
    {
        stage(Stage::Called);
        const auto result = reinterpret_cast<SharedRegionCheck>(originalCheck)(process, args, returnValue);
        if (result != 0) {
            stage(Stage::OriginalError);
            return result;
        }
        if (args == nullptr || args->startAddress == 0) {
            stage(Stage::Argument);
            return result;
        }
        using namespace UserMtlDualQuadData;
        UInt64            base{0};
        UInt8             header[104];
        if (readUser(args->startAddress, &base, sizeof(base)) != 0 || base == 0
            || base >= UserSharedCache::UserLimit - sizeof(header) || (base & (PAGE_SIZE - 1)) != 0
            || readUser(base, header, sizeof(header)) != 0)
        {
            stage(Stage::CacheRead);
            return result;
        }
        if (memcmp(header, CacheMagic, sizeof(CacheMagic)) != 0
            || memcmp(header + 88, CacheUUID, sizeof(CacheUUID)) != 0)
        {
            // 非 pinned 24H32 缓存一律 no-op；不做动态解析。
            stage(Stage::CacheMismatch);
            return result;
        }

        Page   pages[PatchCount];
        UInt32 pageCount{0}, originals{0}, patched{0};
        UInt8  saved[PatchCount][MaxPatchSize];
        for (UInt32 i = 0; i < PatchCount; ++i) {
            const auto address = base + Patches[i].offset;
            const auto page    = address & ~static_cast<UInt64>(PAGE_SIZE - 1);
            if (Patches[i].size == 0 || Patches[i].size > MaxPatchSize
                || Patches[i].offset >= UserSharedCache::UserLimit - base
                || Patches[i].size >= UserSharedCache::UserLimit - base - Patches[i].offset
                || readUser(address, saved[i], Patches[i].size) != 0)
            {
                stage(Stage::CodeRead);
                return result;
            }
            if (memcmp(saved[i], Patches[i].original, Patches[i].size) == 0) { ++originals; }
            else if (memcmp(saved[i], Patches[i].patched, Patches[i].size) == 0) {
                ++patched;
            }
            else {
                stage(Stage::CodeMismatch);
                error(KERN_INVALID_ARGUMENT);
                return result;
            }
            const auto last = (address + Patches[i].size - 1) & ~static_cast<UInt64>(PAGE_SIZE - 1);
            for (auto pageAddress = page; pageAddress <= last; pageAddress += PAGE_SIZE) {
                bool found = false;
                for (UInt32 p = 0; p < pageCount; ++p) {
                    if (pages[p].address == pageAddress) {
                        found = true;
                        break;
                    }
                }
                if (!found) {
                    if (pageCount == PatchCount) {
                        stage(Stage::CodeMismatch);
                        return result;
                    }
                    pages[pageCount++].address = pageAddress;
                }
            }
        }
        if (patched == PatchCount) {
            stage(Stage::Already);
            return result;
        }
        if (originals != PatchCount) {
            stage(Stage::Mixed);
            return result;
        }
        vm_map_t map = getTaskMap(current_task());
        if (map == nullptr) {
            stage(Stage::NoMap);
            return result;
        }
        for (UInt32 p = 0; p < pageCount; ++p) {
            if (!leafProtection(map, pages[p].address, pages[p].protection)
                || pages[p].protection != (VM_PROT_READ | VM_PROT_EXECUTE))
            {
                stage(Stage::Region);
                return result;
            }
        }
        bool ok = true;
        for (UInt32 p = 0; p < pageCount; ++p) {
            const auto r =
                vm_protect(map, pages[p].address, PAGE_SIZE, FALSE, VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY);
            if (r != KERN_SUCCESS) {
                stage(Stage::Protection);
                error(r);
                ok = false;
                break;
            }
            pages[p].writable = true;
        }
        if (ok) {
            for (UInt32 i = 0; i < PatchCount; ++i) {
                if (writeUser(Patches[i].patched, base + Patches[i].offset, Patches[i].size) != 0) {
                    stage(Stage::Write);
                    error(KERN_FAILURE);
                    for (UInt32 j = 0; j <= i; ++j) { writeUser(saved[j], base + Patches[j].offset, Patches[j].size); }
                    ok = false;
                    break;
                }
            }
        }
        for (UInt32 p = 0; p < pageCount; ++p) {
            if (!pages[p].writable) { continue; }
            if (vm_protect(map, pages[p].address, PAGE_SIZE, FALSE, pages[p].protection) != KERN_SUCCESS) {
                stage(Stage::Restore);
                ok = false;
            }
            else {
                pages[p].writable = false;
            }
        }
        if (!ok) {
            stage(Stage::Rollback);
            return result;
        }
        stage(Stage::Applied);
        if (process != nullptr) {
            const auto pid = proc_pid(process);
            if (pid > 0) { NRed::singleton().setProp32("NRedMtlDQLastPID", static_cast<UInt32>(pid)); }
        }
        const auto count = __atomic_fetch_add(&appliedCount, 1U, __ATOMIC_RELAXED);
        if (count < 16) { NRed::singleton().setProp32("NRedMtlDQAppliedCount", count + 1); }
        return result;
    }

}    // namespace

void UserMtlDualQuad::init(KernelPatcher& patcher)
{
    if (!checkKernelArgument("-NRedMtlDualQuad")) { return; }
    if (!currentKernelVersion().majorMatches(MACOS_15_X)) {
        // 站点仅在 15.8.1/24H32 上逐字节验证；其他系统交给 CacheMismatch no-op，但显式拒装。
        NRed::singleton().setProp32("NRedMtlDQRejected", 4);
        SYSLOG("UserMtlDualQuad", "rejected: unsupported kernel version");
        return;
    }
    if (!NRed::singleton().getAttributes().isRenoir()) {
        // src+8<=0x13 的分支 A 路径目前只在 Renoir 系（含 Barcelo/Green Sardine）核实。
        NRed::singleton().setProp32("NRedMtlDQRejected", 5);
        return;
    }
    NRed::singleton().setProp32("NRedMtlDQRequested", 1);
    NRed::singleton().setProp32("NRedMtlDQDeviceID", NRed::singleton().getDeviceID());
    getTaskMap = reinterpret_cast<GetTaskMap>(patcher.solveSymbol(KernelPatcher::KernelID, "_get_task_map"));
    patcher.clearError();
    queryRegion =
        reinterpret_cast<RegionRecurse>(patcher.solveSymbol(KernelPatcher::KernelID, "_mach_vm_region_recurse"));
    patcher.clearError();
    readUser = reinterpret_cast<CopyIn>(patcher.solveSymbol(KernelPatcher::KernelID, "_copyin"));
    patcher.clearError();
    writeUser = reinterpret_cast<CopyOut>(patcher.solveSymbol(KernelPatcher::KernelID, "_copyout"));
    patcher.clearError();
    if (!getTaskMap || !queryRegion || !readUser || !writeUser) {
        NRed::singleton().setProp32("NRedMtlDQRejected", 1);
        return;
    }
    KernelPatcher::RouteRequest request{"_shared_region_check_np", wrappedSharedRegionCheck, originalCheck};
    if (!patcher.routeMultipleLong(KernelPatcher::KernelID, &request, 1)) {
        patcher.clearError();
        NRed::singleton().setProp32("NRedMtlDQRejected", 2);
        return;
    }
    NRed::singleton().setProp32("NRedMtlDQEnabled", 1);
}
