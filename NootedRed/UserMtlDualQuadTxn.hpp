// UserMtlDualQuad 的可移植事务核：身份校验 + 页级 COW 补丁 + 已检查回滚。
// 不依赖内核头——Env 回调由内核模块绑定 _copyin/_copyout/vm_protect，
// 由宿主 fixture 绑定模拟内存做故障注入；两侧跑同一份实现。
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace MtlDQTxn
{

    constexpr uint64_t kPageSize = 0x1000;
    constexpr uint32_t kMaxPatches = 4;
    constexpr uint32_t kMaxContext = 256;

    enum Prot : int32_t
    {
        ProtRX = 1,    // VM_PROT_READ|EXECUTE
        ProtRWC = 2    // VM_PROT_READ|WRITE|COPY
    };

    struct Patch
    {
        uint64_t       offset;
        const uint8_t* original;
        const uint8_t* patched;
        uint32_t       size;
    };

    // 身份规格：缓存头 + 站点上下文窗口。Context 窗口内除 siteIdx..+siteSize
    // 外逐字节相等；站点区允许 original 或 patched（幂等）。
    struct Spec
    {
        uint64_t       contextOffset;
        const uint8_t* context;
        uint32_t       contextSize;
        const Patch*   patches;
        uint32_t       patchCount;
    };

    struct Env
    {
        void* u;
        // 读/写用户地址；返回 0 成功。
        int (*read)(void*, uint64_t addr, void* buf, uint32_t n);
        int (*write)(void*, const void* buf, uint64_t addr, uint32_t n);
        // 页对齐地址的叶保护查询/设置；*out 填 Prot 值。
        int (*protQuery)(void*, uint64_t page, int32_t* out);
        int (*protSet)(void*, uint64_t page, int32_t prot);
    };

    enum class Status : int32_t
    {
        Applied = 0,
        Already,                // 站点已是 patched → 幂等跳过
        BadSpec,                // 规格自洽性失败（跨窗、空洞等）
        ReadFail,               // 上下文/站点读取失败
        ContextMismatch,        // 窗口非站点区不等 → 非本镜像
        SiteUnknown,            // 站点既非 original 也非 patched
        QueryFail,              // protQuery 失败
        NotRX,                  // 叶保护不是 RX → 拒绝
        ProtectFail,            // 置 RW 失败（已恢复页已尽力还原）
        WriteRolledBack,        // copyout 失败，原字节已完整回写
        WriteRollbackPartial,   // copyout 失败且回写也有失败 → 未恢复
        RestoreRecovered,       // RX 恢复一度失败但字节+权限均已救回
        Unrecovered             // 权限/字节未完全恢复 → unrecoveredMask 记录页位
    };

    struct Result
    {
        Status   status;
        uint32_t unrecoveredMask;    // 位 i = pages[i] 未恢复 RX/原字节
        uint32_t detail;             // 失败页/补丁序号（视 status）
    };

    inline int32_t checkHeader(const uint8_t* hdr, uint32_t n, const uint8_t* magic, uint32_t magicSize,
                               const uint8_t* uuid, uint32_t uuidOff)
    {
        if (hdr == nullptr || n < uuidOff + 16 || memcmp(hdr, magic, magicSize) != 0) { return 1; }
        if (memcmp(hdr + uuidOff, uuid, 16) != 0) { return 2; }
        return 0;
    }

    // 安装门（纯谓词，宿主可直接测）：boot-arg、OS 主版本、设备类。
    inline bool argEnabled(bool hasArg) { return hasArg; }
    inline bool osSupported(uint32_t kernelMajor) { return kernelMajor == 15; }
    inline bool deviceSupported(bool isRenoirClass) { return isRenoirClass; }

    inline Result apply(const Env& env, uint64_t base, const Spec& spec)
    {
        Result r{Status::Applied, 0, 0};
        if (spec.patches == nullptr || spec.patchCount == 0 || spec.patchCount > kMaxPatches
            || spec.context == nullptr || spec.contextSize == 0 || spec.contextSize > kMaxContext)
        {
            r.status = Status::BadSpec;
            return r;
        }
        bool inSite[kMaxContext] = {};
        for (uint32_t i = 0; i < spec.patchCount; ++i) {
            const auto& p   = spec.patches[i];
            const auto  idx = p.offset - spec.contextOffset;
            if (p.size == 0 || p.size > 64 || p.offset < spec.contextOffset
                || idx + p.size > spec.contextSize)
            {
                r.status = Status::BadSpec;
                return r;
            }
            for (uint32_t k = 0; k < p.size; ++k) { inSite[idx + k] = true; }
        }

        // 1) 身份：整窗读入；非站点区逐字节比对；每补丁独立判原/已修/未知。
        uint8_t buf[kMaxContext];
        if (env.read(env.u, base + spec.contextOffset, buf, spec.contextSize) != 0) {
            r.status = Status::ReadFail;
            return r;
        }
        for (uint32_t i = 0; i < spec.contextSize; ++i) {
            if (inSite[i]) { continue; }
            if (buf[i] != spec.context[i]) {
                r.status = Status::ContextMismatch;
                r.detail = i;
                return r;
            }
        }
        uint32_t originals = 0, patched = 0;
        for (uint32_t i = 0; i < spec.patchCount; ++i) {
            const auto& p   = spec.patches[i];
            const auto  idx = p.offset - spec.contextOffset;
            if (memcmp(buf + idx, p.original, p.size) == 0) { ++originals; }
            else if (memcmp(buf + idx, p.patched, p.size) == 0) { ++patched; }
            else {
                r.status = Status::SiteUnknown;
                r.detail = i;
                return r;
            }
        }
        if (patched == spec.patchCount) {
            r.status = Status::Already;
            return r;
        }
        if (originals != spec.patchCount) {
            r.status = Status::SiteUnknown;
            r.detail = 0xFFFF;    // 原/修混杂
            return r;
        }

        // 2) 页集（补丁可能跨页；每补丁已限定单窗口内）。
        uint64_t pages[kMaxPatches * 2];
        int32_t  pageProt[kMaxPatches * 2];
        uint32_t pageCount = 0;
        for (uint32_t i = 0; i < spec.patchCount; ++i) {
            const auto& p   = spec.patches[i];
            uint64_t    a0  = (base + p.offset) & ~(kPageSize - 1);
            uint64_t    a1  = (base + p.offset + p.size - 1) & ~(kPageSize - 1);
            for (uint64_t pg = a0; pg <= a1; pg += kPageSize) {
                bool found = false;
                for (uint32_t q = 0; q < pageCount; ++q) {
                    if (pages[q] == pg) { found = true; }
                }
                if (!found) {
                    if (pageCount >= kMaxPatches * 2) { r.status = Status::BadSpec; return r; }
                    pages[pageCount++] = pg;
                }
            }
        }
        for (uint32_t q = 0; q < pageCount; ++q) {
            if (env.protQuery(env.u, pages[q], &pageProt[q]) != 0) {
                r.status = Status::QueryFail;
                r.detail = q;
                return r;
            }
            if (pageProt[q] != ProtRX) {
                r.status = Status::NotRX;
                r.detail = q;
                return r;
            }
        }

        // 3) 置 RW：失败时把已改页尽力还原 RX，逐页记录未恢复位。
        bool writable[kMaxPatches * 2] = {};
        for (uint32_t q = 0; q < pageCount; ++q) {
            if (env.protSet(env.u, pages[q], ProtRWC) != 0) {
                for (uint32_t k = 0; k < q; ++k) {
                    if (writable[k] && env.protSet(env.u, pages[k], ProtRX) != 0) {
                        r.unrecoveredMask |= 1U << k;
                    }
                }
                r.status = Status::ProtectFail;
                r.detail = q;
                return r;
            }
            writable[q] = true;
        }

        // 4) 写入：失败则对已写补丁逐条回写原字节并检查结果，再统一恢复 RX。
        uint8_t saved[kMaxPatches][64];
        for (uint32_t i = 0; i < spec.patchCount; ++i) {
            const auto& p = spec.patches[i];
            if (env.read(env.u, base + p.offset, saved[i], p.size) != 0
                || memcmp(saved[i], p.original, p.size) != 0)
            {
                // 读回校验失败：视同站点漂移，走恢复路径。
                r.status = Status::SiteUnknown;
                r.detail = i;
                for (uint32_t q = 0; q < pageCount; ++q) {
                    if (writable[q] && env.protSet(env.u, pages[q], ProtRX) != 0) {
                        r.unrecoveredMask |= 1U << q;
                    }
                    writable[q] = false;
                }
                if (r.unrecoveredMask) { r.status = Status::Unrecovered; }
                return r;
            }
        }
        uint32_t written = 0;
        for (; written < spec.patchCount; ++written) {
            const auto& p = spec.patches[written];
            if (env.write(env.u, p.patched, base + p.offset, p.size) != 0) { break; }
        }
        const bool writeOK = (written == spec.patchCount);
        if (!writeOK) {
            uint32_t rollbackFail = 0;
            for (uint32_t j = 0; j < written; ++j) {
                const auto& p = spec.patches[j];
                if (env.write(env.u, saved[j], base + p.offset, p.size) != 0) { ++rollbackFail; }
            }
            r.detail = written | (rollbackFail << 8);
        }

        // 5) 恢复 RX：单页失败 → 页仍 RW 时先回写该页内补丁原字节、重试 RX 一次；
        //    仍失败置 unrecovered 位。与字节回滚分开记录。
        bool restoreFail = false;
        for (uint32_t q = 0; q < pageCount; ++q) {
            if (!writable[q]) { continue; }
            if (env.protSet(env.u, pages[q], ProtRX) == 0) {
                writable[q] = false;
                continue;
            }
            // 页仍 RW：尽力把落在该页上的补丁原字节写回，再重试。
            for (uint32_t j = 0; j < spec.patchCount; ++j) {
                const auto& p  = spec.patches[j];
                uint64_t    a0 = (base + p.offset) & ~(kPageSize - 1);
                uint64_t    a1 = (base + p.offset + p.size - 1) & ~(kPageSize - 1);
                if (pages[q] >= a0 && pages[q] <= a1) {
                    env.write(env.u, saved[j], base + p.offset, p.size);
                }
            }
            if (env.protSet(env.u, pages[q], ProtRX) == 0) {
                writable[q] = false;
                restoreFail = true;    // 救回了，但记录曾失败
            }
            else {
                r.unrecoveredMask |= 1U << q;
                restoreFail = true;
            }
        }

        if (r.unrecoveredMask) {
            r.status = Status::Unrecovered;
        }
        else if (!writeOK) {
            r.status = (r.detail >> 8) ? Status::WriteRollbackPartial : Status::WriteRolledBack;
        }
        else if (restoreFail) {
            r.status = Status::RestoreRecovered;
        }
        else {
            r.status = Status::Applied;
        }
        return r;
    }

}    // namespace MtlDQTxn
