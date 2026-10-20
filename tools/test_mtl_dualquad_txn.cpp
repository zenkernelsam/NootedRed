// UserMtlDualQuadTxn 的宿主侧故障注入 fixture：直接编译内核共用的事务头。
// clang++ -std=c++17 -fsanitize=address,undefined -fno-omit-frame-pointer \
//   -I NootedRed tools/test_mtl_dualquad_txn.cpp -o /tmp/mtldq-txn && /tmp/mtldq-txn
#include <cstdio>
#include <cstring>
#include <IOKit/IOTypes.h>
#include <UserMtlDualQuadData.hpp>
#include <UserMtlDualQuadTxn.hpp>

using namespace MtlDQTxn;

static int failures = 0;
#define CHECK(cond, name)                                                                              \
    do {                                                                                               \
        if (!(cond)) {                                                                                 \
            ++failures;                                                                                \
            printf("FAIL %s\n", name);                                                                 \
        }                                                                                              \
    } while (0)

// 模拟进程地址空间：所有 env 回调用 (addr - shift) 做稀疏平移索引，
// shift = applyBase + 窗口起始 offset - 期望窗口落点。prot 按页同样平移。
struct Sim
{
    uint64_t applyBase{0}, shift{0};
    uint8_t  mem[0x3000]{};
    int32_t  prot[8]{};
    int      readCalls{0}, writeCalls{0}, protSetCalls{0};
    int      failReadN{-1};         // 第 N 次 read 失败
    int      failWriteN{-1};        // 第 N 次 write 失败（0 字节落盘）
    int      partialWriteN{-1};     // 第 N 次 write 写 1 字节后失败
    int      failProtSetN{-1};      // 第 N 次 protSet 失败
    int      failProtSetSeq{-1};    // 第 N 次 protSet 失败（之后恢复）
    int      failWriteFrom{-1};     // 第 N 次起所有 write 失败
};

static int sRead(void* u, uint64_t addr, void* buf, uint32_t n)
{
    auto* s = static_cast<Sim*>(u);
    if (s->readCalls++ == s->failReadN) { return 1; }
    if (addr < s->shift || addr + n - s->shift > sizeof(s->mem)) { return 1; }
    memcpy(buf, s->mem + (addr - s->shift), n);
    return 0;
}
static int sWrite(void* u, const void* buf, uint64_t addr, uint32_t n)
{
    auto* s   = static_cast<Sim*>(u);
    int   cur = s->writeCalls++;
    if (cur == s->failWriteN || (s->failWriteFrom >= 0 && cur >= s->failWriteFrom)) { return 1; }
    if (addr < s->shift || addr + n - s->shift > sizeof(s->mem)) { return 1; }
    if (cur == s->partialWriteN) {
        s->mem[addr - s->shift] = static_cast<const uint8_t*>(buf)[0];
        return 1;
    }
    memcpy(s->mem + (addr - s->shift), buf, n);
    return 0;
}
static int sQuery(void* u, uint64_t page, int32_t* out)
{
    auto* s = static_cast<Sim*>(u);
    *out    = s->prot[(page - s->shift) / kPageSize];
    return 0;
}
static int sProtSet(void* u, uint64_t page, int32_t p)
{
    auto* s   = static_cast<Sim*>(u);
    int   cur = s->protSetCalls++;
    if (cur == s->failProtSetN || cur == s->failProtSetSeq) { return 1; }
    s->prot[(page - s->shift) / kPageSize] = p;
    return 0;
}
static Env simEnv(Sim& s) { return {&s, sRead, sWrite, sQuery, sProtSet}; }

// ---- 真实数据头 Spec：窗口铺到 mem[0x800]，applyBase 用真 cache base ----
static Spec realSpec()
{
    static const Patch kP[] = {{UserMtlDualQuadData::Patches[0].offset,
                                UserMtlDualQuadData::Patches[0].original,
                                UserMtlDualQuadData::Patches[0].patched,
                                UserMtlDualQuadData::Patches[0].size}};
    return {UserMtlDualQuadData::ContextOffset, UserMtlDualQuadData::Context,
            sizeof(UserMtlDualQuadData::Context), kP, 1};
}
static void initRealSim(Sim& s, bool patchedSite)
{
    auto spec     = realSpec();
    s.applyBase   = 0x7FF800000000ULL;
    s.shift       = s.applyBase + spec.contextOffset - 0x800;
    const auto site = spec.patches[0].offset - spec.contextOffset;
    memcpy(s.mem + 0x800, spec.context, spec.contextSize);
    memcpy(s.mem + 0x800 + site, patchedSite ? spec.patches[0].patched : spec.patches[0].original,
           spec.patches[0].size);
    const auto pageIdx = (s.applyBase + (spec.patches[0].offset & ~0xFFFULL) - s.shift) / kPageSize;
    for (auto& p : s.prot) { p = ProtRX; }
    s.prot[pageIdx] = ProtRX;
}
static size_t realMemIdx(const Spec& spec)
{
    return 0x800 + spec.patches[0].offset - spec.contextOffset;
}

// ---- 合成跨页 Spec：contextOffset=0xF40、size=0xE8；shift=applyBase ----
static const uint8_t kO0[] = {0x83, 0xC9, 0x4D};
static const uint8_t kN0[] = {0x83, 0xC9, 0x4F};
static const uint8_t kO1[] = {0xC7, 0x47, 0x78, 0x04};
static const uint8_t kN1[] = {0xC7, 0x47, 0x78, 0x01};
static uint8_t       gCtx[0xE8];
static const Patch   gP2[] = {{0xFFE, kO0, kN0, sizeof(kO0)},     // 跨页 0xFFE..0x1001
                              {0x1010, kO1, kN1, sizeof(kO1)}};   // 页 1
static Spec crossSpec()
{
    memset(gCtx, 0xAA, sizeof(gCtx));
    memcpy(gCtx + (0xFFE - 0xF40), kO0, sizeof(kO0));
    memcpy(gCtx + (0x1010 - 0xF40), kO1, sizeof(kO1));
    return {0xF40, gCtx, sizeof(gCtx), gP2, 2};
}
static void initCrossSim(Sim& s)
{
    s.applyBase = 0x7FF800000000ULL;
    s.shift     = s.applyBase;
    memcpy(s.mem + 0xF40, gCtx, sizeof(gCtx));
    for (auto& p : s.prot) { p = ProtRX; }
}

int main()
{
    // ---- 头部校验：真实 24H32 magic/uuid 常量 ----
    {
        uint8_t hdr[104]{};
        memcpy(hdr, UserMtlDualQuadData::CacheMagic, sizeof(UserMtlDualQuadData::CacheMagic));
        memcpy(hdr + 88, UserMtlDualQuadData::CacheUUID, 16);
        CHECK(checkHeader(hdr, sizeof(hdr), UserMtlDualQuadData::CacheMagic,
                          sizeof(UserMtlDualQuadData::CacheMagic), UserMtlDualQuadData::CacheUUID, 88)
                  == 0,
              "header-ok");
        hdr[5] ^= 1;
        CHECK(checkHeader(hdr, sizeof(hdr), UserMtlDualQuadData::CacheMagic,
                          sizeof(UserMtlDualQuadData::CacheMagic), UserMtlDualQuadData::CacheUUID, 88)
                  == 1,
              "header-magic-mismatch");
        memcpy(hdr, UserMtlDualQuadData::CacheMagic, sizeof(UserMtlDualQuadData::CacheMagic));
        hdr[95] ^= 1;
        CHECK(checkHeader(hdr, sizeof(hdr), UserMtlDualQuadData::CacheMagic,
                          sizeof(UserMtlDualQuadData::CacheMagic), UserMtlDualQuadData::CacheUUID, 88)
                  == 2,
              "header-uuid-mismatch");
    }
    // ---- 安装门谓词 ----
    CHECK(!argEnabled(false) && argEnabled(true), "gate-arg");
    CHECK(osSupported(15) && !osSupported(14) && !osSupported(26), "gate-os");
    CHECK(deviceSupported(true) && !deviceSupported(false), "gate-device");

    auto spec = realSpec();
    // ---- 成功 + 幂等 ----
    {
        Sim s;
        initRealSim(s, false);
        Env e  = simEnv(s);
        auto r = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::Applied, "apply-status");
        CHECK(memcmp(s.mem + realMemIdx(spec), spec.patches[0].patched, 3) == 0, "apply-bytes");
        CHECK(s.prot[((s.applyBase + (spec.patches[0].offset & ~0xFFFULL)) - s.shift) / kPageSize]
                  == ProtRX,
              "apply-rx");
        r = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::Already, "apply-idempotent");
    }
    // ---- 身份失败 ----
    {
        Sim s;
        initRealSim(s, false);
        s.mem[0x800 + 5] ^= 1;
        Env e  = simEnv(s);
        auto r = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::ContextMismatch && r.detail == 5, "ctx-mismatch");
        CHECK(s.writeCalls == 0, "ctx-mismatch-nowrite");
    }
    {
        Sim s;
        initRealSim(s, false);
        s.mem[realMemIdx(spec)] = 0x90;
        Env e  = simEnv(s);
        auto r = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::SiteUnknown, "site-unknown");
    }
    {
        Sim s;
        initRealSim(s, false);
        Env e        = simEnv(s);
        s.failReadN  = 0;
        auto r       = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::ReadFail, "read-fail");
    }
    // ---- 保护失败 ----
    {
        Sim s;
        initRealSim(s, false);
        Env e          = simEnv(s);
        s.failProtSetN = 0;
        auto r         = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::ProtectFail, "protect-fail");
        CHECK(s.writeCalls == 0, "protect-fail-nowrite");
    }
    // ---- 写失败 + 完整回滚（含部分 copyout） ----
    {
        Sim s;
        initRealSim(s, false);
        Env e         = simEnv(s);
        s.failWriteN  = 0;    // 首个 write 即补丁写
        auto r        = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::WriteRolledBack, "write-fail-rolledback");
        CHECK(memcmp(s.mem + realMemIdx(spec), spec.patches[0].original, 3) == 0,
              "write-fail-original");
        CHECK(s.prot[((s.applyBase + (spec.patches[0].offset & ~0xFFFULL)) - s.shift) / kPageSize]
                  == ProtRX,
              "write-fail-rx");
    }
    {
        Sim s;
        initRealSim(s, false);
        Env e           = simEnv(s);
        s.partialWriteN = 0;
        auto r          = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::WriteRolledBack, "partial-write-rolledback");
        CHECK(memcmp(s.mem + realMemIdx(spec), spec.patches[0].original, 3) == 0,
              "partial-write-original");
    }
    // ---- RX 恢复失败一次：字节还原 + 重试成功 → RestoreRecovered ----
    {
        Sim s;
        initRealSim(s, false);
        Env e            = simEnv(s);
        s.failProtSetSeq = 1;    // 0=置RW ok，1=首次恢复 RX 失败，2=重试 ok
        auto r           = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::RestoreRecovered, "restore-recovered");
        CHECK(r.unrecoveredMask == 0, "restore-recovered-mask");
        CHECK(s.prot[((s.applyBase + (spec.patches[0].offset & ~0xFFFULL)) - s.shift) / kPageSize]
                  == ProtRX,
              "restore-recovered-rx");
        CHECK(memcmp(s.mem + realMemIdx(spec), spec.patches[0].original, 3) == 0,
              "restore-recovered-bytes");
    }
    // ---- RX 恢复永久失败 → Unrecovered + 页位 + 字节已还原 ----
    {
        Sim s;
        initRealSim(s, false);
        Env e            = simEnv(s);
        s.failProtSetSeq = 1;    // 首次恢复失败
        s.failProtSetN   = 2;    // 重试也失败
        auto r           = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::Unrecovered, "unrecovered-status");
        CHECK(r.unrecoveredMask & 1, "unrecovered-mask");
        CHECK(s.prot[((s.applyBase + (spec.patches[0].offset & ~0xFFFULL)) - s.shift) / kPageSize]
                  == ProtRWC,
              "unrecovered-still-rw");
        CHECK(memcmp(s.mem + realMemIdx(spec), spec.patches[0].original, 3) == 0,
              "unrecovered-bytes-restored");
    }
    // ---- 跨页多补丁 ----
    {
        Sim s;
        auto cs = crossSpec();
        initCrossSim(s);
        Env e  = simEnv(s);
        auto r = apply(e, s.applyBase, cs);
        CHECK(r.status == Status::Applied, "cross-apply");
        CHECK(s.prot[0] == ProtRX && s.prot[1] == ProtRX, "cross-rx");
        CHECK(memcmp(s.mem + 0xFFE, kN0, 3) == 0 && memcmp(s.mem + 0x1010, kN1, 4) == 0,
              "cross-bytes");
    }
    {
        Sim s;
        auto cs = crossSpec();
        initCrossSim(s);
        Env e        = simEnv(s);
        s.failWriteN = 1;    // patch0 ok、patch1 失败 → 回滚 patch0
        auto r       = apply(e, s.applyBase, cs);
        CHECK(r.status == Status::WriteRolledBack, "cross-write-rollback");
        CHECK(memcmp(s.mem + 0xFFE, kO0, 3) == 0, "cross-rollback-bytes");
    }
    {
        Sim s;
        auto cs = crossSpec();
        initCrossSim(s);
        Env e           = simEnv(s);
        s.failWriteFrom = 1;    // patch1 写失败 + patch0 回滚也失败
        auto r          = apply(e, s.applyBase, cs);
        CHECK(r.status == Status::WriteRollbackPartial, "cross-rollback-partial");
        CHECK(s.prot[0] == ProtRX && s.prot[1] == ProtRX, "cross-partial-rx");
        CHECK(memcmp(s.mem + 0xFFE, kN0, 3) == 0, "cross-partial-left-patched");
    }
    {
        Sim s;
        auto cs = crossSpec();
        initCrossSim(s);
        Env e          = simEnv(s);
        s.failProtSetN = 1;    // 页 1 置 RW 失败 → 页 0 还原
        auto r         = apply(e, s.applyBase, cs);
        CHECK(r.status == Status::ProtectFail, "cross-protect-fail");
        CHECK(r.unrecoveredMask == 0 && s.prot[0] == ProtRX, "cross-protect-restored");
        CHECK(s.writeCalls == 0, "cross-protect-nowrite");
    }
    // ---- NotRX 拒绝 ----
    {
        Sim s;
        initRealSim(s, false);
        s.prot[((s.applyBase + (spec.patches[0].offset & ~0xFFFULL)) - s.shift) / kPageSize] = 0;
        Env e  = simEnv(s);
        auto r = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::NotRX, "not-rx");
    }

    if (failures == 0) { printf("PASS: MtlDQTxn all transaction/failure-injection cases\n"); }
    return failures ? 1 : 0;
}
