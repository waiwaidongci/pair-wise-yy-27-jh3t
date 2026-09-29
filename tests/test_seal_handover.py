import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError


class SealHandoverClosedLoopTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = CollationDB(self.path)
        self.owner = self.db.add_user("负责人", "owner")
        self.editor = self.db.add_user("编辑", "editor")
        self.reviewer = self.db.add_user("审阅", "reviewer")
        self.outsider = self.db.add_user("外部", "reviewer")
        self.work = self.db.create_work("残卷", "异文比较", self.owner)
        self.w1 = self.db.add_witness(self.work, "甲本", "version")
        self.w2 = self.db.add_witness(self.work, "乙本", "fragment", "馆藏残片", "中段缺页")
        self.db.grant_witness_editor(self.w2, self.editor, self.owner)
        self.db.grant_work_access(self.work, self.reviewer, "review", self.owner)
        self.passage = self.db.add_passage(self.work, "第一节", "春水东流，故人南去。", self.owner)
        self.db.align_passage(self.passage, self.w1, "春水东流，故人南去。", 1, self.owner)
        self.db.align_passage(self.passage, self.w2, "春水东流，[缺页]", 2, self.editor)
        self.variant = self.db.create_variant(self.passage, self.w2, "春水东流，故人南去。", "按语义补足", self.editor, 0)
        self.db.add_note(self.variant, "补字仍需参照纸背墨迹。", self.editor)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _confirm_and_seal(self):
        self.db.confirm_revision(self.variant, 1, self.owner, "c1")
        return self.db.seal_passage(self.passage, self.owner, "定稿")

    def test_seal_retains_snapshot_and_blocks_delivered_edits(self):
        seal = self._confirm_and_seal()
        self.assertEqual(1, seal["seal_no"])
        # 快照留存异文、注释、对齐
        history = self.db.passage_history(self.passage, self.owner)
        snap = history["seals"][0]
        import json
        content = json.loads(snap["snapshot_json"])
        self.assertEqual(1, len(content["variants"]))
        self.assertEqual(1, len(content["variants"][0]["notes"]))
        self.assertEqual(2, len(content["alignments"]))
        # 封存后已交付内容不可改动
        with self.assertRaisesRegex(DomainError, "封存"):
            self.db.update_variant(self.variant, "另一文本", "无意义", self.editor, 1)
        with self.assertRaisesRegex(DomainError, "封存"):
            self.db.create_variant(self.passage, self.w2, "新异文", "理由", self.editor, 1)
        # 封存后注释仍可补录
        nid = self.db.add_note(self.variant, "封存后补录", self.editor)
        self.assertTrue(nid)

    def test_basis_change_invalidates_seal_and_recalculates_export(self):
        self._confirm_and_seal()
        # 修改底本依据 -> 旧封存立即失效
        self.db.update_passage_basis(self.passage, "春水东流，故人南去。（订补）", self.owner)
        seals = self.db.list_seals(self.work, self.owner)
        self.assertEqual("invalid", seals[0]["status"])
        self.assertFalse(seals[0]["valid"])
        # 导出按新依据重算：不再标记封存
        exported = self.db.export_collation(self.work, self.reviewer)
        p = exported["passages"][0]
        self.assertFalse(p["sealed"])
        self.assertIsNone(p["seal_no"])
        # 改定对齐依据同样使旧封存失效
        self.db.update_alignment(self.passage, self.w2, "春水东流，[残损]", 2, self.editor)
        seals = self.db.list_seals(self.work, self.owner)
        self.assertFalse(seals[0]["valid"])

    def test_handover_transfers_duty_and_revokes_write(self):
        # 不预先封存，直接转交；未处理修订仍在
        handover = self.db.handover_work(self.work, self.owner, self.reviewer, "h1")
        self.assertEqual(self.reviewer, handover["to_user"])
        # 原负责人失去写权限：不能封存
        with self.assertRaisesRegex(DomainError, "负责人"):
            self.db.seal_passage(self.passage, self.owner, "x")
        # 未确认修订仍在，原负责人不能确认
        with self.assertRaisesRegex(DomainError, "负责人"):
            self.db.confirm_revision(self.variant, 1, self.owner, "c9")
        # 接手人获得封存责任与确认权，未处理修订一并交出
        self.db.confirm_revision(self.variant, 1, self.reviewer, "c1")
        s2 = self.db.seal_passage(self.passage, self.reviewer, "新负责人封存")
        self.assertEqual(1, s2["seal_no"])
        # 接手人权限不符（无审阅/编辑关系）拒绝转交
        with self.assertRaisesRegex(DomainError, "接手人"):
            self.db.handover_work(self.work, self.reviewer, self.outsider, "h2")

    def test_handover_idempotent_concurrent_only_once(self):
        h1 = self.db.handover_work(self.work, self.owner, self.reviewer, "same-key")
        # 同键并发/重复提交只成功一次，重放原结果
        h2 = self.db.handover_work(self.work, self.owner, self.reviewer, "same-key")
        self.assertEqual(h1, h2)
        # 同 parties 不同键：作品已转交，原负责人不再是负责人
        with self.assertRaisesRegex(DomainError, "当前负责人"):
            self.db.handover_work(self.work, self.owner, self.reviewer, "other-key")

    def test_unconfirmed_revision_excluded_from_delivered_manuscript(self):
        # 未确认修订不进入交付稿
        exported = self.db.export_collation(self.work, self.reviewer)
        self.assertEqual(0, len(exported["passages"][0]["variants"]))
        self.assertEqual(1, exported["pending_count"])
        # 确认后进入交付稿
        self.db.confirm_revision(self.variant, 1, self.owner, "c1")
        exported = self.db.export_collation(self.work, self.reviewer)
        self.assertEqual(1, len(exported["passages"][0]["variants"]))
        self.assertEqual(0, exported["pending_count"])
        # 只能确认当前修订
        with self.assertRaisesRegex(DomainError, "当前修订"):
            self.db.confirm_revision(self.variant, 99, self.owner, "c2")
        # 只有负责人可以确认：用一个未确认的新修订验证
        v2 = self.db.create_variant(self.passage, self.w2, "另一异文", "理由二", self.editor, 1)
        with self.assertRaisesRegex(DomainError, "负责人"):
            self.db.confirm_revision(v2, 2, self.editor, "c3")

    def test_confirm_idempotent_concurrent_only_once(self):
        c1 = self.db.confirm_revision(self.variant, 1, self.owner, "ckey")
        # 同键重放
        c2 = self.db.confirm_revision(self.variant, 1, self.owner, "ckey")
        self.assertEqual(c1, c2)
        # 同修订不同键：已确认，返回原结果
        c3 = self.db.confirm_revision(self.variant, 1, self.owner, "ckey2")
        self.assertEqual(c1["confirm_id"], c3["confirm_id"])

    def test_history_lists_seals_handovers_revisions(self):
        self._confirm_and_seal()
        self.db.handover_work(self.work, self.owner, self.reviewer, "h1")
        seals = self.db.list_seals(self.work, self.reviewer)
        self.assertEqual(1, len(seals))
        handovers = self.db.list_handovers(self.work, self.reviewer)
        self.assertEqual(1, len(handovers))
        self.assertEqual(self.owner, handovers[0]["from_user"])
        history = self.db.passage_history(self.passage, self.reviewer)
        self.assertEqual(1, len(history["seals"]))
        self.assertEqual(1, len(history["confirms"]))
        # 无查看权限的用户被拒绝
        with self.assertRaisesRegex(DomainError, "无权"):
            self.db.list_seals(self.work, self.outsider)


if __name__ == "__main__":
    unittest.main()
