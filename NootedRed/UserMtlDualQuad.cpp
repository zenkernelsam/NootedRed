// 在进程私有代码页应用已核验的 MTL dual-quad 补丁；默认关闭、逐字节匹配、失败回滚。
// 事务核见 UserMtlDualQuadTxn.hpp（宿主 fixture 以故障注入跑同一实现）。
#include <Headers/kern_util.hpp>
#include <IOKit/IOLib.h>
#include <NRed.hpp>
#include <PenguinWizardry/KernelVersion.hpp>
#include <UserMtlDualQuad.hpp>
#include <UserMtlDualQuadData.hpp>
#include <UserMtlDualQuadTxn.hpp>
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
    using SharedRegionCheck = int           (*)(proc_t, SharedRegionCheckArgs*, int*);
    using GetTaskMap        = vm_map_t      (*)(task_t);
    using RegionRecurse     = kern_return_t (*)(vm_map_t, mach_vm_address_t*, mach_vm_size_t*, natural_t*,
                                            vm_region_recurse_info_t, mach_msg_type_number_t*);
    using CopyIn            = int           (*)(user_addr_t, void*, size_t);
    using CopyOut           = int           (*)(const void*, user_addr_t, size_t);

    mach_vm_address_t originalCheck{0};
    GetTaskMap        getTaskMap{nullptr};
    RegionRecurse     queryRegion{nullptr};
    CopyIn            readUser{nullptr};
    CopyOut           writeUser{nullptr};
    UInt32            seenStages{0}, appliedCount{0}, errorCount{0};
    constexpr auto    PatchCount = arrsize(UserMtlDualQuadData::Patches);
    static_assert(PatchCount <= MtlDQTxn::kMaxPatches);

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

    // Env 绑定：u = 本次调用的 vm_map_t（每调用独立 ctx，并发共享区检查互不串图）。
    // read/write 走 _copyin/_copyout；protQuery 逐层下钻子映射到叶节点；
    // protSet 走 vm_protect（RWC=读|写|COPY，RX=读|执行）。
    int envRead(void*, uint64_t addr, void* buf, uint32_t n)
    {
        return readUser(addr, buf, n);
    }
    int envWrite(void*, const void* buf, uint64_t addr, uint32_t n)
    {
        return writeUser(buf, addr, n);
    }
    int envProtQuery(void* u, uint64_t page, int32_t* out)
    {
        const auto map = static_cast<vm_map_t>(u);
        natural_t depth{0};
        for (UInt32 attempt = 0; attempt < 8; ++attempt) {
            auto                            address = page;
            mach_vm_size_t                  size{0};
            vm_region_submap_short_info_data_64_t info{};
            mach_msg_type_number_t count = VM_REGION_SUBMAP_SHORT_INFO_COUNT_64;
            const auto                      result = queryRegion(
                map, &address, &size, &depth, reinterpret_cast<vm_region_recurse_info_t>(&info), &count);
            if (result != KERN_SUCCESS) {
                error(result);
                return 1;
            }
            if (count < VM_REGION_SUBMAP_SHORT_INFO_COUNT_64 || address > page
                || page - address >= size || size - (page - address) < PAGE_SIZE)
            {
                return 1;
            }
            if (!info.is_submap) {
                *out = (info.protection == (VM_PROT_READ | VM_PROT_EXECUTE)) ? MtlDQTxn::ProtRX : 0;
                return 0;
            }
            if (depth == ~0U) { break; }
            ++depth;
        }
        return 1;
    }
    int envProtSet(void* u, uint64_t page, int32_t prot)
    {
        const auto vprot = (prot == MtlDQTxn::ProtRWC)
            ? (VM_PROT_READ | VM_PROT_WRITE | VM_PROT_COPY)
            : (VM_PROT_READ | VM_PROT_EXECUTE);
        return vm_protect(static_cast<vm_map_t>(u), page, PAGE_SIZE, FALSE, vprot) == KERN_SUCCESS ? 0
                                                                                                : 1;
    }

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
        UInt64 base{0};
        UInt8  header[104];
        if (readUser(args->startAddress, &base, sizeof(base)) != 0 || base == 0
            || base >= UserSharedCache::UserLimit - sizeof(header) || (base & (PAGE_SIZE - 1)) != 0
            || readUser(base, header, sizeof(header)) != 0)
        {
            stage(Stage::CacheRead);
            return result;
        }
        // magic+UUID 任一不符即 no-op；不做动态解析。
        if (MtlDQTxn::checkHeader(header, sizeof(header), CacheMagic, sizeof(CacheMagic), CacheUUID, 88)
            != 0)
        {
            stage(Stage::CacheMismatch);
            return result;
        }
        static const MtlDQTxn::Patch kPatches[] = {
            {Patches[0].offset, Patches[0].original, Patches[0].patched, Patches[0].size}};
        static const MtlDQTxn::Spec kSpec = {
            ContextOffset, Context, sizeof(Context), kPatches, PatchCount};
        vm_map_t map = getTaskMap(current_task());
        if (map == nullptr) {
            stage(Stage::NoMap);
            return result;
        }
        const MtlDQTxn::Env env = {map, envRead, envWrite, envProtQuery, envProtSet};
        const auto          txn = MtlDQTxn::apply(env, base, kSpec);
        using S                 = MtlDQTxn::Status;
        switch (txn.status) {
            case S::Applied:
                stage(Stage::Applied);
                break;
            case S::Already:
                stage(Stage::Already);
                break;
            case S::ReadFail:
                stage(Stage::CodeRead);
                break;
            case S::ContextMismatch:
            case S::SiteUnknown:
            case S::BadSpec:
                stage(Stage::CodeMismatch);
                break;
            case S::QueryFail:
            case S::NotRX:
                stage(Stage::Region);
                break;
            case S::ProtectFail:
                stage(Stage::Protection);
                error(KERN_FAILURE);
                break;
            case S::WriteRolledBack:
            case S::WriteRollbackPartial:
                stage(Stage::Write);
                stage(Stage::Rollback);
                error(KERN_FAILURE);
                break;
            case S::RestoreRecovered:
                stage(Stage::Restore);
                break;
            case S::Unrecovered:
                stage(Stage::Rollback);
                error(KERN_FAILURE);
                break;
        }
        if (txn.unrecoveredMask) {
            NRed::singleton().setProp32("NRedMtlDQUnrecovered", txn.unrecoveredMask);
        }
        if (txn.status == S::Applied) {
            if (process != nullptr) {
                const auto pid = proc_pid(process);
                if (pid > 0) { NRed::singleton().setProp32("NRedMtlDQLastPID", static_cast<UInt32>(pid)); }
            }
            const auto count = __atomic_fetch_add(&appliedCount, 1U, __ATOMIC_RELAXED);
            if (count < 16) { NRed::singleton().setProp32("NRedMtlDQAppliedCount", count + 1); }
        }
        return result;
    }

}    // namespace

void UserMtlDualQuad::init(KernelPatcher& patcher)
{
    if (!MtlDQTxn::argEnabled(checkKernelArgument("-NRedMtlDualQuad"))) { return; }
    if (!MtlDQTxn::osSupported(
            currentKernelVersion().majorMatches(MACOS_15_X) ? 15 : 0))
    {
        // 站点仅在 15.8.1/24H32 上逐字节验证；其他系统交给 CacheMismatch no-op，但显式拒装。
        NRed::singleton().setProp32("NRedMtlDQRejected", 4);
        SYSLOG("UserMtlDualQuad", "rejected: unsupported kernel version");
        return;
    }
    if (!MtlDQTxn::deviceSupported(NRed::singleton().getAttributes().isRenoir())) {
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
