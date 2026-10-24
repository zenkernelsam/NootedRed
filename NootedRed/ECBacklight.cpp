// EC 背光服务
// Licensed under the Thou Shalt Not Profit License version 1.5. See LICENSE for details.

#include <ECBacklight.hpp>
#include <Headers/kern_util.hpp>
#include <libkern/c++/OSData.h>
#include <libkern/c++/OSNumber.h>
#include <pexpert/pexpert.h>

OSDefineMetaClassAndStructors(NRedECBacklight, IOService)

namespace
{

    constexpr UInt32 kReady   = 1;
    constexpr UInt32 kFaulted = 2;
    constexpr UInt32 kPollMs  = 100;
    constexpr UInt8  kGuid[]  = {0x13, 0x96, 0x3E, 0x60, 0x25, 0xEF, 0x38, 0x43,
                                 0xA3, 0xD0, 0xC4, 0x61, 0x77, 0x51, 0x6D, 0xB7};

    struct GuidBlock
    {
        UInt8 guid[16];
        char  objectId[2];
        UInt8 instances;
        UInt8 flags;
    };
    static_assert(sizeof(GuidBlock) == 20);

    struct Arguments
    {
        UInt32 mode;
        UInt32 value;
        UInt32 result;
        UInt32 reserved[3];
    };
    static_assert(sizeof(Arguments) == 24);
    static_assert(__atomic_always_lock_free(sizeof(UInt32), nullptr));
    static_assert(__atomic_always_lock_free(sizeof(UInt64), nullptr));

}    // namespace

UInt32           NRedECBacklight::backendState = 0;
UInt32           NRedECBacklight::displayReady = 0;
UInt32           NRedECBacklight::pending      = 0;
UInt64           NRedECBacklight::desired      = 0;
NRedECBacklight* NRedECBacklight::owner        = nullptr;

IOService* NRedECBacklight::probe(IOService* provider, SInt32* score)
{
    int mode = 0;
    PE_parse_boot_argn("NRedECFix", &mode, sizeof(mode));
    bool enabled = true;
    PE_parse_boot_argn("AMDBacklight", &enabled, sizeof(enabled));
    if (!enabled || (mode != 1 && mode != 2) || checkKernelArgument("-NRedOff")
        || OSDynamicCast(IOACPIPlatformDevice, provider) == nullptr)
    {
        return nullptr;
    }
    this->forceEc = mode == 2;
    return IOService::probe(provider, score);
}

bool NRedECBacklight::start(IOService* provider)
{
    if (!IOService::start(provider)) { return false; }
    this->device = OSDynamicCast(IOACPIPlatformDevice, provider);
    if (this->device == nullptr) { return false; }
    this->device->retain();
    this->loop = IOWorkLoop::workLoop();
    if (this->loop != nullptr) {
        this->timer = IOTimerEventSource::timerEventSource(this, onTimer);
        if (this->timer != nullptr && this->loop->addEventSource(this->timer) == kIOReturnSuccess) {
            this->timerAdded = true;
            this->setProperty("State", "waiting");
            if (this->timer->setTimeoutMS(kPollMs) == kIOReturnSuccess) { return true; }
        }
    }
    this->releaseResources();
    return false;
}

void NRedECBacklight::releaseResources()
{
    if (this->timer != nullptr) {
        this->timer->cancelTimeout();
        if (this->timerAdded && this->loop != nullptr) { this->loop->removeEventSource(this->timer); }
        this->timerAdded = false;
        OSSafeReleaseNULL(this->timer);
    }
    if (this->ownsBackend) {
        __atomic_store_n(&backendState, 0, __ATOMIC_RELEASE);
        __atomic_store_n(&pending, 0, __ATOMIC_RELEASE);
        __atomic_store_n(&owner, nullptr, __ATOMIC_RELEASE);
        this->ownsBackend = false;
    }
    OSSafeReleaseNULL(this->loop);
    OSSafeReleaseNULL(this->device);
}

void NRedECBacklight::stop(IOService* provider)
{
    this->releaseResources();
    IOService::stop(provider);
}

void NRedECBacklight::free()
{
    this->releaseResources();
    IOService::free();
}

IOWorkLoop* NRedECBacklight::getWorkLoop() const { return this->loop; }

bool NRedECBacklight::hasBackend() { return __atomic_load_n(&backendState, __ATOMIC_ACQUIRE) != 0; }

void NRedECBacklight::setDisplayReady(const UInt32 level, const UInt32 maximum)
{
    if (maximum == 0 || level > maximum) { return; }
    if (!hasBackend()) { __atomic_store_n(&desired, (static_cast<UInt64>(maximum) << 32) | level, __ATOMIC_RELEASE); }
    __atomic_store_n(&displayReady, 1, __ATOMIC_RELEASE);
}

UInt32 NRedECBacklight::currentLevel() { return static_cast<UInt32>(__atomic_load_n(&desired, __ATOMIC_ACQUIRE)); }

IOReturn NRedECBacklight::queue(const UInt32 level, const UInt32 maximum)
{
    const auto state = __atomic_load_n(&backendState, __ATOMIC_ACQUIRE);
    if (state == 0) { return kIOReturnUnsupported; }
    if (state != kReady) { return kIOReturnIOError; }
    if (maximum == 0 || level > maximum) { return kIOReturnBadArgument; }
    __atomic_store_n(&desired, (static_cast<UInt64>(maximum) << 32) | level, __ATOMIC_RELEASE);
    __atomic_store_n(&pending, 1, __ATOMIC_RELEASE);
    return kIOReturnSuccess;
}

bool NRedECBacklight::evaluate(const UInt32 methodId, const UInt32 mode, const UInt32 value, UInt32& result)
{
    const Arguments args{mode, mode == 1 ? value : 0, 0, {0, 0, 0}};
    auto            instance = OSNumber::withNumber(0ULL, 32);
    auto            method   = OSNumber::withNumber(static_cast<UInt64>(methodId), 32);
    auto            input    = OSData::withBytes(&args, sizeof(args));
    if (instance == nullptr || method == nullptr || input == nullptr) {
        OSSafeReleaseNULL(instance);
        OSSafeReleaseNULL(method);
        OSSafeReleaseNULL(input);
        return false;
    }
    OSObject* parameters[] = {instance, method, input};
    OSObject* output       = nullptr;
    auto      success      = this->device->evaluateObject(this->methodName, &output, parameters, 3) == kIOReturnSuccess;
    if (success) {
        if (const auto number = OSDynamicCast(OSNumber, output)) {
            const auto returned = number->unsigned64BitValue();
            success             = returned <= 0xFFFFFFFF && (mode != 1 || returned == 0);
            result              = static_cast<UInt32>(returned);
        }
        else if (const auto data = OSDynamicCast(OSData, output)) {
            const auto bytes = data->getBytesNoCopy(8, sizeof(result));
            if (bytes != nullptr) { memcpy(&result, bytes, sizeof(result)); }
            else {
                success = false;
            }
        }
        else {
            success = false;
        }
    }
    OSSafeReleaseNULL(output);
    OSSafeReleaseNULL(instance);
    OSSafeReleaseNULL(method);
    OSSafeReleaseNULL(input);
    return success;
}

bool NRedECBacklight::prepare()
{
    OSObject*  object = nullptr;
    const auto status = this->device->evaluateObject("_WDG", &object);
    const auto data   = OSDynamicCast(OSData, object);
    if (status == kIOReturnSuccess && data != nullptr && data->getLength() % sizeof(GuidBlock) == 0) {
        const auto blocks = static_cast<const GuidBlock*>(data->getBytesNoCopy());
        for (unsigned i = 0; i < data->getLength() / sizeof(GuidBlock); i++) {
            if (memcmp(blocks[i].guid, kGuid, sizeof(kGuid)) == 0 && (blocks[i].flags & 2) != 0
                && blocks[i].instances != 0)
            {
                this->methodName[0] = 'W';
                this->methodName[1] = 'M';
                this->methodName[2] = blocks[i].objectId[0];
                this->methodName[3] = blocks[i].objectId[1];
                break;
            }
        }
    }
    OSSafeReleaseNULL(object);
    if (this->methodName[0] == 0) { return false; }

    NRedECBacklight* expected = nullptr;
    if (!__atomic_compare_exchange_n(&owner, &expected, this, false, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        return false;
    }
    this->ownsBackend = true;
    UInt32 source     = 0;
    UInt32 result     = 0;
    if (this->forceEc && !this->evaluate(2, 1, 2, result)) { return false; }
    if (!this->evaluate(2, 0, 0, source) || source != 2 || !this->evaluate(1, 2, 0, this->maximum)
        || this->maximum == 0)
    {
        return false;
    }
    this->setProperty("Maximum", static_cast<UInt64>(this->maximum), 32);
    this->setProperty("State", "ready");
    __atomic_store_n(&pending, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&backendState, kReady, __ATOMIC_RELEASE);
    this->initialized = true;
    return true;
}

void NRedECBacklight::fail()
{
    this->setProperty("State", "faulted");
    if (this->ownsBackend) {
        __atomic_store_n(&backendState, kFaulted, __ATOMIC_RELEASE);
        __atomic_store_n(&pending, 0, __ATOMIC_RELEASE);
    }
    SYSLOG("ECBacklight", "WMI failed; further writes stopped");
}

void NRedECBacklight::onTimer(OSObject* ownerObject, IOTimerEventSource* sender)
{
    const auto self = OSDynamicCast(NRedECBacklight, ownerObject);
    if (self == nullptr) { return; }
    if (__atomic_load_n(&displayReady, __ATOMIC_ACQUIRE) == 0) {
        if (sender->setTimeoutMS(kPollMs) != kIOReturnSuccess) { self->fail(); }
        return;
    }
    if (!self->initialized) {
        if (!self->prepare()) {
            self->setProperty("State", "unavailable");
            if (self->ownsBackend) {
                __atomic_store_n(&owner, nullptr, __ATOMIC_RELEASE);
                self->ownsBackend = false;
            }
            return;
        }
    }
    if (__atomic_exchange_n(&pending, 0, __ATOMIC_ACQ_REL) != 0) {
        const auto request   = __atomic_load_n(&desired, __ATOMIC_ACQUIRE);
        const auto requested = static_cast<UInt32>(request);
        const auto range     = static_cast<UInt32>(request >> 32);
        if (range == 0 || requested > range) {
            self->fail();
            return;
        }
        auto level = static_cast<UInt32>(static_cast<UInt64>(requested) * self->maximum / range);
        if (requested != 0 && level == 0) { level = 1; }
        UInt32 result = 0;
        if (!self->evaluate(1, 1, level, result)) {
            self->fail();
            return;
        }
        self->setProperty("AppliedLevel", static_cast<UInt64>(level), 32);
    }
    if (sender->setTimeoutMS(kPollMs) != kIOReturnSuccess) { self->fail(); }
}
