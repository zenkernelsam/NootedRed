"""硬件回归的安装前置检查；安装证据不代表编解码功能已通过。"""
from pathlib import Path
import plistlib
import subprocess


def guarded_service(registry, processes, service='Encoder'):
    """核验核显已纳入补丁计划、用户态管线已启用且无拒绝记录，
    并且本 boot 至少有一个被筛选进程完成过补丁安装。

    新架构（steve 合并后）属性语义：NRedVCNUserEnabled 只在
    shared_region_check 钩子成功路由后置 1；NRedVCNUserApplied /
    NRedVCNUserDynamicApplied / NRedVCNGVASelectionFix 只在补丁
    成功写入被筛选进程后发布，任一非零即证明本 boot 补丁链曾完整
    执行。这些标记驻留在驱动服务对象上，目标 XPC 进程退出后仍保留，
    不存在旧 Guarded*PID 的进程存活竞态。NRedVCNUserRejected 非零
    或 VPF 隔离/动态拒绝即视为前置条件不满足。
    """
    if service not in ('Encoder', 'Decoder'):
        raise ValueError('未知视频服务')
    stack = list(registry)
    device_seen = enabled_seen = applied_seen = False
    device_id = 0
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        stack.extend(item.get('IORegistryEntryChildren', []))
        candidate = item.get('NRedVCNDeviceID')
        if candidate not in (0x15e7, 0x1636, 0x1638, 0x164c):
            continue
        device_seen = True
        device_id = candidate
        rejected = item.get('NRedVCNUserRejected')
        if rejected:
            raise RuntimeError(f'驱动报告补丁管线拒绝 NRedVCNUserRejected={rejected}，停止硬件测试')
        if item.get('NRedVCNVPFQuarantined') or item.get('NRedVCNUserDynamicRejected'):
            raise RuntimeError('驱动报告补丁被隔离或动态解析拒绝，停止硬件测试并保留现场日志')
        if item.get('NRedVCNUserEnabled') == 1:
            enabled_seen = True
        if (item.get('NRedVCNUserApplied') or item.get('NRedVCNUserDynamicApplied')
                or item.get('NRedVCNGVASelectionFix')):
            applied_seen = True
    if not device_seen:
        raise RuntimeError('未在 IORegistry 找到受支持的 VCN 设备记录，停止硬件测试')
    if not enabled_seen:
        raise RuntimeError('用户态视频补丁管线未启用（NRedVCNUserEnabled!=1），停止硬件测试')
    if not applied_seen:
        raise RuntimeError('本 boot 尚无被筛选进程完成补丁安装，停止硬件测试')
    return device_id


def require_guard(service='Encoder'):
    output=subprocess.run(['/usr/sbin/ioreg','-a','-l','-w','0','-r','-c','IOPCIDevice'],
                          capture_output=True,check=True,timeout=10).stdout
    registry=plistlib.loads(output)
    output=subprocess.run(['/bin/ps','-axo','pid=,comm='],capture_output=True,text=True,
                          check=True,timeout=10).stdout
    processes={}
    for line in output.splitlines():
        fields=line.strip().split(None,1)
        if len(fields)==2 and fields[0].isdigit():processes[int(fields[0])]=fields[1]
    return guarded_service(registry,processes,service)


class HardwareFault(RuntimeError):
    pass


class GPUMonitor:
    """实时保存 GPU/桌面异常；监视器失效也停止后续硬件用例。"""
    def __init__(self,work):
        self.path=work/'gpu-events.jsonl'
        self.output=self.path.open('w')
        self.errors=(work/'gpu-monitor.stderr.txt').open('w')
        predicate='process == "kernel" AND (eventMessage CONTAINS[c] "gpuRestart" OR eventMessage CONTAINS[c] "GPU Reset") OR eventMessage CONTAINS[c] "WindowServer watchdog"'
        self.process=subprocess.Popen(['/usr/bin/log','stream','--style','json','--predicate',predicate],
                                      stdout=self.output,stderr=self.errors)

    def check(self):
        import re
        if self.process.poll() is not None:
            raise HardwareFault('GPU 日志监视器退出，停止硬件测试')
        text=self.path.read_text(errors='replace')
        for message in re.findall(r'"eventMessage"\s*:\s*"([^"\n]*)',text):
            if any(word in message.lower() for word in ('gpurestart','gpu reset','windowserver watchdog')):
                raise HardwareFault('检测到新增 GPU 重置或桌面看门狗异常，停止全部后续测试')

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=5)
        self.output.close();self.errors.close()


def wait_for_case(process,timeout,monitor=None):
    import time
    deadline=time.monotonic()+timeout
    while True:
        if monitor:monitor.check()
        remaining=deadline-time.monotonic()
        if remaining<=0:raise subprocess.TimeoutExpired(process.args,timeout)
        try:
            result=process.wait(timeout=min(0.25,remaining))
            if monitor:monitor.check()
            return result
        except subprocess.TimeoutExpired:
            continue
