// EC 背光服务：显卡回调只提交数值，ACPI 调用仅在独立工作循环中执行。
// Licensed under the Thou Shalt Not Profit License version 1.5. See LICENSE for details.

#pragma once
#include <Headers/kern_util.hpp>
#include <IOKit/IOService.h>
#include <IOKit/IOTimerEventSource.h>
#include <IOKit/IOWorkLoop.h>
#include <IOKit/acpi/IOACPIPlatformDevice.h>

class EXPORT NRedECBacklight : public IOService
{
    OSDeclareDefaultStructors(NRedECBacklight)

    IOACPIPlatformDevice* device{nullptr};
    IOWorkLoop*           loop{nullptr};
    IOTimerEventSource*   timer{nullptr};
    char                  methodName[5]{};
    UInt32                maximum{0};
    bool                  initialized{false};
    bool                  ownsBackend{false};
    bool                  timerAdded{false};
    bool                  forceEc{false};

    static UInt32           backendState;
    static UInt32           displayReady;
    static UInt32           pending;
    static UInt64           desired;
    static NRedECBacklight* owner;

    bool        prepare();
    bool        evaluate(UInt32 method, UInt32 mode, UInt32 input, UInt32& output);
    void        fail();
    void        releaseResources();
    static void onTimer(OSObject* owner, IOTimerEventSource* sender);

public:
    IOService*  probe(IOService* provider, SInt32* score) override;
    bool        start(IOService* provider) override;
    void        stop(IOService* provider) override;
    void        free() override;
    IOWorkLoop* getWorkLoop() const override;

    static bool     hasBackend();
    static void     setDisplayReady(UInt32 level, UInt32 maximum);
    static UInt32   currentLevel();
    static IOReturn queue(UInt32 level, UInt32 maximum);
};
