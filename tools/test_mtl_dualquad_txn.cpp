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
    int      failWriteN2{-1};       // 第二个单点 write 失败
    int      failWriteRangeLo{-1}, failWriteRangeHi{-1};  // write 序号区间全失败
    int      wrongCtx{0};           // 回调见到非本 Sim 的 ctx 计数
    Sim*     reenter{nullptr};      // protSet 首次调用时对另一 Sim 重入 apply
    int      reenterDone{0};
};

static Spec crossSpec();
static Env simEnv(Sim& s);

static int sRead(void* u, uint64_t addr, void* buf, uint32_t n)
{
    auto* s = static_cast<Sim*>(u);
    if (u != s) { ++s->wrongCtx; }
    if (s->readCalls++ == s->failReadN) { return 1; }
    if (addr < s->shift || addr + n - s->shift > sizeof(s->mem)) { return 1; }
    memcpy(buf, s->mem + (addr - s->shift), n);
    return 0;
}
static int sWrite(void* u, const void* buf, uint64_t addr, uint32_t n)
{
    auto* s   = static_cast<Sim*>(u);
    if (u != s) { ++s->wrongCtx; }
    int   cur = s->writeCalls++;
    if (cur == s->failWriteN || cur == s->failWriteN2
        || (s->failWriteFrom >= 0 && cur >= s->failWriteFrom)
        || (s->failWriteRangeLo >= 0 && cur >= s->failWriteRangeLo && cur <= s->failWriteRangeHi))
    {
        return 1;
    }
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
    if (u != s) { ++s->wrongCtx; }
    *out    = s->prot[(page - s->shift) / kPageSize];
    return 0;
}
static int sProtSet(void* u, uint64_t page, int32_t p)
{
    auto* s   = static_cast<Sim*>(u);
    if (u != s) { ++s->wrongCtx; }
    int   cur = s->protSetCalls++;
    if (s->reenter != nullptr && !s->reenterDone) {
        s->reenterDone = 1;
        Sim* other   = s->reenter;
        Env  otherE  = simEnv(*other);
        apply(otherE, other->applyBase, crossSpec());
    }
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

    // ---- ctx 隔离 + 重入交错：s1 事务中重入 s2 完整事务，互不串图 ----
    {
        Sim s1, s2;
        auto cs = crossSpec();
        initCrossSim(s1);
        initCrossSim(s2);
        Env e1       = simEnv(s1);
        s1.reenter   = &s2;    // s1 首次 protSet 时跑完 s2 的整个 apply
        auto r1      = apply(e1, s1.applyBase, cs);
        CHECK(r1.status == Status::Applied, "reenter-s1-applied");
        CHECK(s1.reenterDone == 1, "reenter-fired");
        CHECK(s1.wrongCtx == 0 && s2.wrongCtx == 0, "reenter-ctx-isolated");
        CHECK(memcmp(s1.mem + 0xFFE, kN0, 3) == 0 && memcmp(s2.mem + 0xFFE, kN0, 3) == 0,
              "reenter-both-patched");
        CHECK(s1.prot[0] == ProtRX && s2.prot[0] == ProtRX, "reenter-both-rx");
    }
    // ---- 失败补丁本身已部分落盘 → 回滚必须覆盖它（逐字节原样） ----
    {
        Sim s;
        initCrossSim(s);
        Env e           = simEnv(s);
        s.partialWriteN = 1;    // patch1 写 1 字节后失败（0x01 覆盖原字节 0x04——原/修不同位）
        auto r          = apply(e, s.applyBase, crossSpec());
        CHECK(r.status == Status::WriteRolledBack, "partial-cross-rolledback");
        CHECK(memcmp(s.mem + 0xFFE, kO0, sizeof(kO0)) == 0, "partial-cross-p0-orig");
        CHECK(memcmp(s.mem + 0x1010, kO1, sizeof(kO1)) == 0, "partial-cross-p1-orig");
        CHECK(s.prot[0] == ProtRX && s.prot[1] == ProtRX, "partial-cross-rx");
    }
    // ---- 写失败且失败补丁的回滚也失败 → WriteRollbackPartial ----
    {
        Sim s;
        initCrossSim(s);
        Env e               = simEnv(s);
        s.failWriteN        = 1;    // patch1 写失败
        s.failWriteN2       = 3;    // patch1 自身回滚写也失败（patch0 回滚成功）
        auto r              = apply(e, s.applyBase, crossSpec());
        CHECK(r.status == Status::WriteRollbackPartial, "rollback-partial-status");
        CHECK((r.detail >> 8) == 1, "rollback-partial-count");
        CHECK(memcmp(s.mem + 0xFFE, kO0, 3) == 0, "rollback-partial-p0-restored");
    }
    // ---- 字节恢复失败 + RX 重试成功 → 绝不 RestoreRecovered，记 unrecovered ----
    {
        Sim s;
        initRealSim(s, false);
        Env e            = simEnv(s);
        s.failProtSetSeq = 1;     // 0=置RW ok，1=首次 RX 恢复失败
        // 恢复路径的字节回写是第 1 次 write（0=补丁写成功，1=字节恢复写失败）
        s.failWriteN     = 1;
        auto r           = apply(e, s.applyBase, spec);
        CHECK(r.status == Status::Unrecovered, "bytrec-unrecovered");
        CHECK(r.unrecoveredMask & 1, "bytrec-mask");
        CHECK((r.detail & 0x10000U) != 0, "bytrec-detail");
    }

    if (failures == 0) { printf("PASS: MtlDQTxn all transaction/failure-injection cases\n"); }
    return failures ? 1 : 0;
}
