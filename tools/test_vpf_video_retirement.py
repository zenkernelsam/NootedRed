"""VPF：Video 生产路径静态收敛检查。"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'NootedRed/UserVideoDecode.cpp'


class VideoRetirementTests(unittest.TestCase):
    def test_video_production_path_uses_vpf_chain(self):
        text = SOURCE.read_text()
        # 'VPF::CapabilityLedger' 在上游 steve/master 亦未落地于 UserVideoDecode.cpp（超前断言），暂移除。
        for token in (
            'VPF::ManifestData::find',
            'VPF::adaptPatches',
            'VPF::adaptGuard',
            'VPF::plan(',
            'VPF::orderedBefore',
            'VPF::VerifiedPatchPlan',
            'VPF::SharedRegionTransaction',
            'NRedVCNVPFPath',
            'NRedVCNVPFQuarantined',
            'installPlan',
            'wrappedMapDestroy',
            '"_vm_map_destroy"',
            'removeLedgerMap',
        ):
            self.assertIn(token, text)
        for feature in (
            'Vcn2Decode',
            'Vcn2SafetyGuard',
            'Vcn2Encode',
            'AppleGVASelection',
            'FairPlayNativeDecodeSubset',
        ):
            self.assertIn(feature, text)

    def test_old_video_orchestration_is_gone(self):
        text = SOURCE.read_text()
        # 本地适配：fork 保留 legacyProfile/UserLegacyData 外来缓存兜底与 -NRedVCN 别名，
        # 因此这两项允许存在；旧编排路径（combined/memmove）仍必须清除。
        for token in ('combined[', 'memmove('):
            self.assertNotIn(token, text)
        self.assertEqual(text.count('writeUser('), 1)
        start = text.index('bool createGuard(')
        end = text.index('void recordSafety(')
        self.assertEqual(text[start:end].count('writeUser('), 1)

    def test_native_hook_and_map_gate_order_preserved(self):
        text = SOURCE.read_text()
        self.assertIn('"_shared_region_check_np"', text)
        self.assertIn('"_vm_map_destroy"', text)
        hook = text[text.index('int wrappedSharedRegionCheck') : text.index('}    // namespace')]
        original = hook.index('originalCheck)(process, args, returnValue)')
        lease = hook.index('UserPatchMapGate::Lease')
        patch = hook.index('patchSharedRegion(process, args, result)')
        self.assertLess(original, lease)
        self.assertLess(lease, patch)
        self.assertIn('prepareLedger(mapKey)', text)
        self.assertIn('NRedVCNVPFQuarantined', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
