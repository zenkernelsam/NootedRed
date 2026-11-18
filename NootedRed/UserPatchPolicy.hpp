// 按签名权限识别 FairPlay 客户端，具体补丁策略由调用模块决定。
#pragma once
#include <IOKit/IOUserClient.h>
#include <libkern/c++/OSBoolean.h>

namespace UserPatchPolicy
{

    inline bool isFairPlayClient(const task_t task)
    {
        if (task == nullptr) { return false; }
        auto*      entitlement = IOUserClient::copyClientEntitlement(task, "com.apple.private.fpsd.client");
        const bool client      = entitlement == kOSBooleanTrue;
        if (entitlement != nullptr) { entitlement->release(); }
        return client;
    }

}    // namespace UserPatchPolicy
