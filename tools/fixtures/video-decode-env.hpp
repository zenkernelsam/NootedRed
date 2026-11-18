// 共享测试桩：steve 新架构（legacy 层移除后）在夹具中的最小环境。
// 只模拟"协调器未接管、阶段计时关闭、拒绝表就绪、VP9 停用"路径；
// 真实协调器/拒绝表/计时器由各自专项测试验证。
#pragma once
#include <cstring>
namespace UserPatchCoordinator { struct Participant; struct Transaction; }
namespace UserVP9DecodeResolver { struct Plan; }

#ifndef VIDEO_ENV_SKIP_COORDINATOR
namespace UserPatchCoordinator
{
    constexpr UInt32 HeaderSize = 104;
    enum class Outcome : uint8_t { Pending, Idle, Patched };
    struct Proof { UInt32 references{0}, siteCount{0}; };
    struct Participant
    {
        Outcome outcome{Outcome::Pending};
        Proof*  proof{nullptr};
    };
    struct Transaction
    {
        UInt64      base{0};
        UInt8       header[HeaderSize]{};
        bool        cacheRead{false};
        Participant display{}, video{};
        Participant vp9{Outcome::Idle, nullptr};
        bool sharedHeader(UInt64& outBase, UInt8* outHeader) const
        {
            if (!cacheRead) { return false; }
            outBase = base;
            memcpy(outHeader, header, HeaderSize);
            return true;
        }
    };
    using PatchCallback = int (*)(void*, void*, int, Transaction&);
    inline bool deferModulePatches() { return false; }
    inline bool processAllowed(void*) { return true; }
    inline void forgetMap(void*) {}
    inline void clearDisplay() {}
    inline void clearVideo() {}
    inline void registerDisplay(const PatchCallback) {}
    inline void registerVideo(const PatchCallback) {}
    template <typename F> inline void registerVideo(F&&) {}
    template <typename R> inline void registerReader(R&&) {}
    template <typename F> inline void registerDisplay(F&&) {}
    template <typename P> inline void settlePatched(Participant&, const P&) {}
}    // namespace UserPatchCoordinator
#endif

namespace UserRejectedCaches
{
    class Table
    {
    public:
        bool init() { return true; }
        bool shouldSkip(const UInt8*) { return false; }
        template <typename Input> void rejectMissingImages(const UInt8*, Input&) {}
        void accept(const UInt8*) {}
    };
}    // namespace UserRejectedCaches

namespace UserPhaseTiming
{
    enum class Phase : UInt32 { CoordinateFull, CoordinateSkipped, Header, SkipCheck,
                                Display, DisplayResolve, DisplayPrepare, DisplayWrite,
                                Video, VideoResolve, VideoPrepare, VideoWrite, Count };
    inline void setEnabled(bool) {}
    struct Timer
    {
        explicit Timer(Phase) {}
        void stop() {}
    };
}    // namespace UserPhaseTiming

#ifndef VIDEO_ENV_SKIP_VP9PLAN
namespace UserVP9DecodeResolver { struct Plan { UInt32 reserved; }; }
#endif

namespace VP9
{
    inline bool                          enabled{false};
    inline void*                        lock{nullptr};
    inline UserVP9DecodeResolver::Plan*  plan{nullptr};
    inline bool                          traceEnabled{false};
    inline void publishState(UInt32) {}
    inline bool trace(void*, UInt64) { return false; }
    inline void install(void*, UInt64, const UInt8*, UserPatchCoordinator::Participant*) {}
    inline void settle(UserPatchCoordinator::Participant*, UInt64) {}
}    // namespace VP9
