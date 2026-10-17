# Vega 核显 VCN 解码接入

## 范围

实验性接入按 `VCNCapabilities.hpp` 的能力表选择设备，内核与用户态共用这一判定。
当前开放 PCI `1002:15E7 / 1636 / 1638 / 164C` 的 VCN 2.2 路径；
`1002:15D8 / 15DD` 保留分类但不接入 VCN1。能力表必须与 NRed 原有设备分类一致，
生成器发现重复、遗漏或未经核实的 VA 分类时拒绝生成。

| PCI ID | NRed 家族 | 视频代际 | 本次接入 |
| --- | --- | --- | --- |
| `15D8 / 15DD` | Picasso / Raven（含 Raven2） | VCN 1.x | 拒绝，尚未实现 VCN1 桥接 |
| `1636 / 164C` | Renoir 系列 | VCN 2.2 | 显式启用的实验路径，待实机验收 |
| `1638 / 15E7` | Cezanne / Barcelo 系列 | VCN 2.2 | 保留 Cezanne 路径，新增设备待实机验收 |

代际依据 [Linux 内核 AMD 硬件组件表](https://www.kernel.org/doc/html/v6.17/gpu/amdgpu/amd-hardware-list-info.html)。
同一视频代际是复用现有桥接的依据，不代表 Apple 驱动已经支持或新设备已经硬解成功。

版本入口使用作者的 `KernelVersion::majorMatches(MACOS_26)`，接受 Tahoe 的 Darwin `25.x`，
不限制内核小版本。VCN 启用前还必须通过已核实的内核 ABI：两侧硬件虚表的六个槽位
分别核对具名方法身份或完整 getter 内容，包含 getter 访问的字段偏移；不兼容时不安装桥接。
引擎写入复用 X5000 的 PM4 引擎数组基址与 `kAMDHWEngineTypeVCN0`，不另设 `0x3F8` 常量。
这些检查验证当前桥接依赖的槽位与 getter，不能自动迁移任意新驱动布局。

用户态保留参考 `x86_64h` 缓存 UUID 的静态快路径；其他 UUID 进入动态解析，
按映像路径、完整函数和实际相对引用生成补丁计划。具体约束见 [动态缓存匹配](user-cache-dynamic.md)。
使用 `-NRedVCN` 显式启用；保留已验证渲染基线的启动参数。
默认关闭，未增加独显或其他 macOS 版本的支持。

## Darwin 24.6 只读 ABI 校验（不是硬件激活候选）

当前 fork 保留一组 **macOS 15.8.1 / Darwin 24.6** 的 kernel patch 数据，必须同时
使用 `-NRedVCN -NRedDarwin24` 才会选择。普通 `-NRedVCN` 仍走 Tahoe 数据；没有
`-NRedDarwin24` 时 Darwin 24 继续拒绝，避免误把候选加载到其它 15.x 小版本。

该数据来自本机 Build 24.6 的 `SystemKernelExtensions.kc`（collection UUID
`32F6A74A-0627-D0F9-6048-58AF2F562E12`）中的 `AMDRadeonX5000/X6000` fileset，
不是从 Tahoe 偏移猜测。20 个 X6000 调用点中 16 个逐字节匹配，另外四个按当前
函数边界重新核对：`setSuspendResumeState` 为 `0x28`，`writeDiagnosisReport`
为 `0x52B/0x7AD/0x9C3`。Darwin 24 的第一个硬件 getter 去掉了 Tahoe 版本的
`and 1`，因此使用独立的 ABI guard 表。

但在 Darwin 24 下这条路径**只做只读核验**：`processKext` 在两侧 ABI 通过后立即
返回，不安装 X5000/X6000 hook、不写任何调用点、不分配 VCN 引擎、不设置
`hasVCN0`、不替换 `AMDAccelShared`/`AMDAccelSharedUserClient`、不切换
SML/`safeCast`，也不安装 `UserVideoDecode` 的 shared-region hook。因此
`-NRedVCN -NRedDarwin24` 只发布诊断属性，不会在 VCN0 没有提交任何命令时改变
GFX engine/channel setup。

原因是硬件激活会无条件改变共享加速器对象和 GFX 引擎数组；本机 `.gpuRestart`
证据显示 stock release 4（没有 VCN 参数）同样在 QQ/Discord 的 GFX/compute 通道
上 reset，且 reset 报告中 VCN0 为 `Disabled`、VCN 命令数为 0。VCN 不是该 GFX
故障的唯一原因；在 X5000/X6000 共享类兼容性和基线 GFX/surface 问题未解决前，
不应开放 Darwin 24 硬件激活。

当前 USB 实验参数保持 `-NRedVCN -NRedDarwin24`，但 kext 只验证 ABI。期望属性：
`NRedVCNDarwin24DiagnosticOnly=1`、`NRedVCNEngineAllocated=0`、
`NRedVCNUserDiagnosticOnly=1`、两侧 `ABIValidated=1`、两侧 `Prepared=0`、
`NativeLoadReady` 缺失或为 0。VCN0 不创建/不启动，用户态解码补丁不安装。这不等于硬解、
Metal 或 WindowServer 稳定性证明。

## 实现

以下引擎、通道、SML 和用户态接入只在 `VCN::activate()` 为真时执行；Darwin 24 的
只读路径不会进入。Tahoe 行为保持不变。

X5000 加速器启动前请求可选加载 X6000；只有同步加载、两侧 hook 准备成功才接入
原生 VCN2 引擎、视频上下文与客户端。加载失败／延迟时不在既有实例中途启用。
启动 hook 还核对真实 PCI vendor 和设备 ID 必须与 NRed 检测的核显一致；
另一设备即使在能力表中，也不能触发本机接入。
插件库依赖保持不变，跨驱动虚调用按本机虚表修正，包含 GFXOFF 等待配对。

四种 VCN2 设备复用当前 X5000/X6000 的引擎槽位、VCN 通道、GFX9 SML 与 AddrLib 路径。
VA identify 载荷分别按本机 PCI 生成，只将该设备映射到已有的视频分类 5；
该分类用于选择 VCN2 解码能力，不把核显的 GFX/AddrLib 家族改成 Navi。
其他 PCI 分类、IOKit 错误出口和错误 type 请求保留原生行为。
视频工厂、RV AddrCreate、swizzle 修正与 AppleGVA 选择沿用既有实现。

用户态补丁仅写入布局与代码核验通过、具有 RX 保护的进程私有 COW 页；支持重排后跨页的补丁。
失败时回滚已写字节并恢复保护；保留原生返回值。生成头文件不手工修改。
AppleGVA 在无 Intel、仅 AMD 可用的机型回退出口选择已有的 AMD 解码器，
保留混合 GPU、已知机型、显式设备请求及无可用 GPU 的原生选择。

## 生成与验证

在当前适配的 macOS 主机执行；不匹配的驱动／缓存不得直接复用补丁：

```sh
python3 tools/build_vcn_kernel_patch.py
python3 tools/build_video_decode_patch.py
python3 tools/test_vcn_candidate.py
python3 tools/test_vcn_addr_profile.py
python3 tools/test_vcn_user_bridge.py
python3 tools/test_video_cache_dynamic.py
python3 tools/test_video_cache_dynamic.py --sanitize
python3 tools/test_compute_cache_dynamic.py
python3 tools/test_compute_cache_dynamic.py --sanitize
python3 tools/test_cache_resolver_reference.py
python3 tools/test_vcn_native_load.py
python3 tools/test_vcn_engine_bridge.py
python3 tools/test_vcn_kernel_abi.py
python3 tools/test_vcn_kernel_abi.py --sanitize
python3 tools/test_vcn_idle_calls.py
python3 tools/test_vcn_gva_choice.py
python3 tools/test_surface_sync_bridge.py
python3 tools/test_compute_vtable.py
```

中间文件位于 `build/vcn/`，子进程限时 60 秒。
VA 回归对每份载荷穷举完整 16 位 PCI 空间，并验证 IOKit 失败和错误请求；
启动回归直接编译实际 capability gate，覆盖真实 provider 不符、加载延迟与失败。
引擎回归编译实际 X5000 槽位写入、分配失败、跨驱动 cast 尺寸检查和 SML/GFX access 路由；
COW 回归在四种设备上覆盖每个部分写入、保护失败、缓存与混合补丁拒绝及返回值保留。
版本回归使用作者的真实版本类，覆盖不同 Tahoe 小版本及相邻系统主版本拒绝；
ABI 回归编译实际核验函数，覆盖两侧所有槽位、getter 尾部变化、方法不符和地址边界，含 ASan/UBSan。
这些是 CPU 模拟，不能代替硬件初始化、视频码流与长期运行测试。
另执行 Debug、Research Release、Release 的构建与静态分析，
并以 `tools/test_vcn_package_dependencies.py <构建后的 Info.plist>` 检查加载依赖。

## 实机验收与限制

既有 Cezanne `1638` 的正常尺寸 H.264／HEVC 短视频已观察到硬解与软件像素摘要一致；
64×64 H.264 仍失败。新增三种设备尚无等价实机结果。
Hackintool 检查曾触发 VCN0Dec 未完成与 WindowServer 看门狗；GFXOFF 调用修正已通过
离线回归，消除挂起的实机验证尚未完成，不能声明稳定或完整解码支持。

验收覆盖 Hackintool 创建／销毁解码器、至少五分钟空闲后再启动、持续播放与桌面响应。
保留有次数上限的诊断字段／日志；记录触发时间、硬解属性及 `.gpuRestart`／WindowServer 报告。
`NRedVCNGVASelectionFix=1` 表示新选择补丁已随完整用户态事务写入并恢复 RX。
Apple TV 在线观看的密钥请求及系统网络／音频 panic 需要独立验收，renderer 选择修正
尚不能证明这些故障已解决。

## 诊断与回退

用 `ioreg -l -w0 -r -n IGPU | rg 'NRedVCN'` 读取诊断。
`NRedVCNRequested=1`、`CapabilityVersion=2`、`DeviceID` 和 `Generation` 表示能力判定已执行；
`Generation=0x0202` 表示 VCN 2.2，`0x0100` 只表示 VCN1 家族（不是精确的 IP 小版本）。
`NRedVCNRejected` / `UserRejected` 的 3/4/5 分别表示设备未知、非 Tahoe、视频代际未实现；
用户态的 1/2 仍表示符号缺失 / hook 失败。
`NRedVCNKernelABIRejected=1` 表示桥接依赖的虚表或 getter 不符；
`NRedVCNX5000ABIValidated` / `X6000ABIValidated` 表示对应驱动核验通过，不能代替真实硬解结果。
Darwin 24 只读路径还应看到 `NRedVCNDarwin24DiagnosticOnly=1`、
`NRedVCNEngineAllocated=0`、两侧 `Prepared=0`、`NativeLoadReady` 缺失或为 0 和
`NRedVCNUserDiagnosticOnly=1`；这些表示桥接已核验但未激活，不是失败。
`NRedVCNEngineSkippedDiagnostic` 只在代码尝试分配引擎时出现，正常只读启动不会设置。

`NativeLoadReady`、`EngineAllocated`、`LastHardwareInit`、`UserApplied` 和 `DecoderPID`
分别记录内核准备、引擎分配、真实硬件初始化、用户态事务和解码进程。
`UserCacheUUID0..3` 记录实际缓存 UUID 的四个原始 32 位字；
`UserDynamicMatched` / `UserDynamicApplied` 分别表示动态解析成功／事务完成并恢复 RX，
`UserDynamicRejected` 表示内容或布局核验失败，`UserDynamicReads` 记录有上限的读取次数。
`UserD04=1` 表示缓存不匹配，`UserD06=1` 表示代码不匹配；
`UserEnabled` 只表示 hook 注册，不能作为硬解成功证据。

验收使用 `-NRedVCN`，记录 CPU/PCI、`sw_vers`、`uname -r`、诊断字段及解码器的
`UsingHardwareAcceleratedVideoDecoder` 属性，并对 H.264/HEVC 输出与软件解码做像素比较。
保留可启动的 EFI；发生异常时恢复旧 kext、移除 `-NRedVCN` 并重启。
