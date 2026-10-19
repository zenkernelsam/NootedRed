#pragma once
#import <VideoToolbox/VideoToolbox.h>

static inline BOOL NRedHardwareDecoderSelectionAccepted(const BOOL required, const OSStatus propertyStatus,
                                                       const CFTypeRef value)
{
    if (!required) { return YES; }
    return propertyStatus == noErr && value != NULL && CFGetTypeID(value) == CFBooleanGetTypeID()
        && CFBooleanGetValue((CFBooleanRef)value);
}
