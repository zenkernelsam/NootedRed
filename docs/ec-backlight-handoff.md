# R7000P 2021H 显示与背光交接调查

## 结论与证据范围

2026-10-21，完成下述直接依赖修正后，用户确认正常启动且亮度调节正常。以下失败记录是修正前的调查过程；长期运行和睡眠唤醒尚未验证。

本机导出的 ACPI 证实 AMD 原生背光、NVIDIA 显示 MUX 和 WMI EC 背光是不同接口。不能由“出现苹果进度条”推定 AMD 加速驱动已接管，也不能由 WMI 返回来源 2 推定面板控制权刚刚完成交接。

两版独立 EC 服务均被用户报告在苹果标志或进度条阶段反复重启；补充 `EXPORT` 后仍然失败，因此导出修正不足以解决启动问题。未取得这两次失败的 panic 回溯，不能声称已确定全部启动故障原因。

2026-10-21 发现并离线复现了另一项确定的链接错误：最终二进制导入 `__ZN20IOACPIPlatformDevice9metaClassE`，但 `OSBundleLibraries` 没有声明其提供者 `com.apple.iokit.IOACPIFamily`，而是声明了具体驱动 `com.apple.driver.AppleACPIPlatform`。从本机 `BootKernelExtensions.kc` 的文件集条目和公开符号表读取，该符号只属于 IOACPIFamily。OpenCore 的 `InternalSolveSymbolByName` 使用 `OcGetSymbolFirstLevel`，普通导入解析不会继续搜索间接依赖。这会使注入时的符号解析失败；NootedRed 无法正常加载本身也会令本机图形支持失效。

本轮生产修改仅将这项依赖替换为 `com.apple.iokit.IOACPIFamily = 1.0.0d1`。不更改 EC 初始化、调光、定时或 AMD 路由，用于独立验证加载问题。

## 机型资料

联想支持页面将 R7000P2021H 和 Legion 5 15ACH6H 列为 82JU 系列。PSREF 列出 Ryzen 7 5800H、RTX 3060，以及依面板配置提供的 Advanced Optimus 支持。这只能证明系列硬件能力，不能确定本机当前 BIOS 模式或面板布线。

- [联想机型列表](https://support.lenovo.com/jo/en/downloads/ds566091-patch-for-lenovo-vantage-gaming-feature-driver-for-windows-10-64bit-windows-11-64bit-ideapads)
- [Legion 5 15ACH6H PSREF](https://psref.lenovo.com/WDProduct/Legion/Legion_5_15ACH6H?tab=model)

## 本机固件的三条路径

证据来自先前已导出的 `build/ec-backlight-investigation-20261020/tables/` 与 `efi-acpi/`，本轮没有执行 AML 或亮度查询。

1. AMD 路径：DSDT 的 `\_SB.PCI0.GP17.VGA.LCD._BCM` 将百分比转换为 0–255，调用 `AFN7`。EFI 的 `SSDT-PNLF` 只在该 AMD 设备下增加 `APP0002` 匹配节点；它没有添加 EC 调光逻辑。
2. 显示 MUX：SSDT-5 的 NVIDIA `PEGP.EDP1` 包含 `MXDS` 与 `MXDM`。后者修改 `DSMF` 并请求固件处理。这里的面板信号路由不等于 WMI 背光来源。现有代码没有调用这些切换方法。
3. EC 路径：SSDT-5 的 `\_SB.PCI0.AWMI.WMAA`，方法 1 GET 读取 `PCBV`，SET 在取得 `LFCM` 后写 `PCBV` 并置 `SPBL=1`，最大值为 800。方法 2 GET 直接 `Return (0x02)`，没有读取 `PCBS`；SET 才写 `PCBS`。因此 GET 不能证明控制权实际状态，也不能验证 SET 生效。字段位于 EC 的 SystemMemory 操作区，不是通过传统 EC 端口的直接写入。

独立诊断模块此前通过 WMAA 完成 264 → 132 → 264，用户确认物理亮度改变。它证明这条 WMI 到 EC 的路径在当时的桌面状态有效，不证明早期启动可调用，也不证明系统滑块始终走相同路径。

## 上游关于控制路径变化的证据

[Linux 2023 年提交](https://git.zx2c4.com/wireguard-linux/commit/?id=0d9bdd8a550170306c2021b8d6766c5343b870c2) 记录部分 Legion 在不同时刻分别需要 `nvidia_wmi_ec_backlight` 或 `amdgpu_bl0`。补丁允许同时暴露两者，没有实现固定的“启动几秒后交接”规则。

[Linux 背光维护者 2026 年讨论](https://www.mail-archive.com/dri-devel@lists.freedesktop.org/msg604431.html) 说明部分机器来源报告错误，实际可用路径会随运行状态变化，并提出聚合两种路径的方案。涉及同代 Legion Slim 7；不能直接当作本机已经发生某种切换的证据。

[Linux WMI 驱动](https://github.com/torvalds/linux/blob/master/drivers/platform/x86/nvidia-wmi-ec-backlight.c) 将 EC 背光作为固件设备处理，并非把屏幕显示输出也交给 EC。

这些资料与用户观察到的“干净基线启动初期滑块有效，随后失效”相容，但无法确定本机失效点。实际图像输出、背光 PWM/EC、macOS 参数路由需要分别观察。后续若调查此问题，需要对应启动阶段的日志；不能依赖固定延时或来源常量判断交接。

## 加载检查与限制

- [Apple IOACPIFamily 实现](https://github.com/apple-oss-distributions/IOACPIFamily/blob/main/IOACPIPlatformDevice.cpp) 定义 `IOACPIPlatformDevice` 元类。
- [OpenCore Link.c](https://github.com/acidanthera/OpenCorePkg/blob/master/Library/OcAppleKernelLib/Link.c) 的 `InternalSolveSymbolByName` 和 `InternalOcGetSymbolWorkerName` 给出直接依赖搜索规则。
- `tools/test_ec_backlight_binary.py` 根据最终 kext 的实际未定义符号检查 ACPI 元类的直接依赖，并继续检查自定义 IOKit 类公开元数据。该检查对上一版 Release 失败。
- 本机内核集合符号核对、上一版失败结果及本轮构建结果保存在原工作区 `build/ec-backlight-handoff-20261021/`。

离线链接检查不能代替启动验证。本轮已有用户确认的正常启动及亮度调节反馈，长时间稳定性仍需后续使用结果。
