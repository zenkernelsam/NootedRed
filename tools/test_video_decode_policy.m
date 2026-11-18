#import <Foundation/Foundation.h>
#import "videotoolbox_test_policy.h"

#define CHECK(condition) do { \
    if (!(condition)) { \
        fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #condition); \
        return 1; \
    } \
} while (0)

int main(void)
{
    CHECK(NRedHardwareDecoderSelectionAccepted(YES, noErr, kCFBooleanTrue));
    CHECK(!NRedHardwareDecoderSelectionAccepted(YES, noErr, kCFBooleanFalse));
    CHECK(!NRedHardwareDecoderSelectionAccepted(YES, noErr, NULL));
    CHECK(!NRedHardwareDecoderSelectionAccepted(YES, -12900, kCFBooleanTrue));
    CHECK(!NRedHardwareDecoderSelectionAccepted(YES, noErr, CFSTR("true")));
    CHECK(NRedHardwareDecoderSelectionAccepted(NO, -12900, NULL));
    puts("PASS: required decoder demands readable Boolean true; null, false, wrong type, and errors refuse");
    return 0;
}
