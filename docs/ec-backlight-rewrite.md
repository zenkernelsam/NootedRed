# EC 背光重写

基线提交：`25b60d79f6892fd5cf0a1d5d07519b3227d58605`。本实现从干净工作区编写，未包含先前的 EC 补丁或原工作区中未提交的渲染修改。

2026-10-21，用户确认依赖修正版可以启动且系统亮度调节正常。长期稳定性、睡眠唤醒及不同供电状态尚未验证。旧版与当前实现的差异和归因边界见 [版本对比](ec-backlight-comparison.md)，本机固件及显示路径资料见 [交接调查](ec-backlight-handoff.md)。

## 已知情况

- 本机为 Lenovo Legion R7000P 2021H、Ryzen 7 5800H，macOS 26.7.1（25G241）。
- 独立诊断模块通过 `IOACPIPlane:/_SB/PCI0@0/AWMI` 的 `WMAA`，完成了亮度 264 → 132 → 264；用户确认实际变暗并恢复。这里验证的是 WMI 到 EC 的路径，没有直接写裸 EC 地址。
- 用户报告：未修改的基线提交也会在启动初期短暂正常，随后滑块失效。控制权发生变化的具体位置尚未证实。
- 先前多版集成仍被报告卡死；WMI 是本次优先调查的假设，不是已证实的死机根因。

## 实现

`NRedECBacklight` 作为独立 IOKit 服务匹配 `PNP0C14`，使用自己的匹配类别，与现有 WMI 驱动共存。显示对象就绪后，服务在自己的工作循环中验证 `_WDG` 的 GUID、读取来源与量程。模式 1 初始化只读，不修改启动亮度。

显卡回调通过无锁原子变量提交一份完整的“亮度、量程”数据；回调中不调用 ACPI、不取得互斥锁、不唤醒或等待工作线程。服务每 100 ms 消费最新请求，空闲时不查询或写入 WMI。新请求即使数值不变也会执行，避免唤醒后的恢复请求被永久去重。

EC 已接管时，亮度读写都跳过 AMD 的原处理函数。未找到支持接口时保留基线行为。固件调用返回错误后停止写入，后续请求返回错误，不在后台反复重试，也不静默改走 AMD 背光。

`NRedECBacklight` 的 `State`、`Maximum` 和 `AppliedLevel` 属性仅反映已完成的状态与写入。读取这些属性不触发 WMI。内核不写阶段文件。

## 参数

- `NRedECFix=0` 或省略：不启动 EC 服务。
- `NRedECFix=1`：使用固件报告的 EC 来源。
- `NRedECFix=2`：请求切换到 EC，检查固件调用结果和报告来源后启用。本机来源读取固定返回 2，不能据此确认实际控制权切换。
- `AMDBacklight=0` 或 `-NRedOff`：不启动 EC 服务。

旧版 `NRedECBacklight`、`NRedECBacklightProbe`、`-NRedECBacklightTrace` 不再读取。
三项修复的统一入口见 [启动参数](boot-arguments.md)。

## 验证与边界

`python3 tools/test_ec_backlight_service.py` 对完整生产服务和显卡回调执行宿主测试，启用 AddressSanitizer 与 UndefinedBehaviorSanitizer；加 `--thread-sanitizer` 验证并发提交与读取。测试覆盖初始化不写亮度、显示未就绪不访问 ACPI、工作循环隔离、最新请求合并、读写绕过 AMD、错误停止、多个设备的归属和资源释放。

`python3 tools/test_ec_backlight_binary.py path/to/NootedRed.kext` 检查最终产物中的 IOKit 类元数据及 ACPI 直接依赖。两类测试已接入 CI，二进制检查覆盖 Debug、Research Release 和 Release。

构建与静态分析覆盖 Debug、Research Release 和 Release。首次重写及后续修正的验证日志分别保存在原工作区 `build/ec-backlight-rewrite-20261020/validation/` 和 `build/ec-backlight-handoff-20261021/validation/`。提交前整理的检查日志保存在 `build/ec-backlight-commit-20261021/validation/`。

这些离线检查不能证明实机死机已消除。内核没有为 `evaluateObject` 提供可安全强制中止 AML 的超时机制，本实现也不声称能够中止已经卡住的固件调用。

## 启动构建检查

首版重写的 `NRedECBacklight` 缺少项目内 IOKit 类使用的 `EXPORT`。Debug 中仍可看到该类的元数据，Release 的隐藏可见性及符号裁剪则使最终 kext 丢失其公开虚表、元类虚表、`metaClass`、`gMetaClass` 和 `superClass` 符号。现已显式导出类，并增加 `tools/test_ec_backlight_binary.py path/to/NootedRed.kext` 对三种配置的最终二进制逐项检查。

该导出检查在原 Release 上失败、原 Debug 上通过。源码模拟测试和静态分析未覆盖这项构建差异。用户报告首版重写在苹果标志或进度条阶段反复重启；没有取得对应 panic 回溯，只修正符号导出仍不能正常启动。

进一步发现 ACPI 元类导入缺少其实际提供者 IOACPIFamily 的直接依赖，现已修正。该依赖检查对之前的产物失败，对当前三种构建通过；修正后的产物已获得正常启动与调光的用户反馈。
