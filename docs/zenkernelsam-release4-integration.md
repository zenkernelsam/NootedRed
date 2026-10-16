# steve12311 pre-release.4 integration

Merge the exact feature release `1983f326ebedda9e5596ee6e8ba47af52420ec11`
into the fork's existing `605168d` baseline. Machine-readable source/dependency
pins and local adaptation provenance are in `zenkernelsam-release4-integration.json`.

The sole Git conflict is the Xcode project deleted by the baseline's AppleForge
migration but modified by the feature branch. Retain that project byte-for-byte
from the feature tag and restore its exact Lilu/MacKernelSDK gitlinks. Keep the
baseline AppleForge files and license changes. Runtime source merges without
conflicts; do not bypass platform, PCI, ABI or cache validation.

## Build with this machine's Xcode 14.2

```sh
git submodule update --init --recursive
python3 tools/build_xcode14_compat.py --output /absolute/path/to/new-build-directory
```

The adapter copies source into a new disposable tree. It expands the two files'
57 `#embed` arrays into character literals and preserves the personality XMLs'
explicit zero suffix. A native CPU executable compares every compiled array's
size and bytes with the exact original input before building the Release kext.
The original runtime source and embedded inputs are unchanged. It uses the
existing macOS SDK explicitly and clang14's `c++2b` / `c2x` spellings. A generated
LICENSE alias satisfies the old Xcode archive phase using baseline LICENSE.md.
`compat-manifest.json`, the command, build log and real exit code are saved.
Retire this adapter when using a compiler with native `#embed` support.

The 2026-10-17 local Release build succeeds; package dependency check succeeds.
Twelve CPU bridge/cache/ABI test invocations pass, including four sanitizer
invocations with ASan+UBSan and halt-on-error. The old compiler requires an
isolated compiler shim supplying the SDK, `c++2b` spelling, and suppressing only
unknown diagnostic-only `clang::suppress` attributes. The VCN idle-call and GVA
choice tests cannot run here: the generated Tahoe kernel patch report and
verified Tahoe reference cache are unavailable. They are **not** counted as
passes. Tests requiring native Tahoe binaries and hardware video work are
also unverified. Do not regenerate Tahoe patch data from a Sequoia cache.

## Current machine and USB scope

Current machine: Ryzen 5825U / Barcelo `1002:15E7`, macOS 15.8.1 / Darwin 24.
The feature branch explicitly limits new VCN and ComputeScratch paths to
Darwin 25 / macOS 26. This integration keeps those checks unchanged. The user
authorized installing the candidate on USB for basic compatibility testing
despite that limitation. It is not a Sequoia hardware decoding backport.

Keep the SSD's working NootedRed and wireless/security configuration. User
performs reboot and runtime tests. USB rollback restores the saved working
NootedRed bundle; driver version number `0.9.0` alone cannot identify bytes,
so use the deployment record's binary/file hashes. Do not resume AMD-next,
Intel-Next, NullMoth development or background monitoring through this work.
