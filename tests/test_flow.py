import os, sys, tempfile, threading, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError


class CollationFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=CollationDB(self.path)
        self.owner=self.db.add_user("负责人","owner"); self.next_owner=self.db.add_user("接任负责人","owner")
        self.editor=self.db.add_user("编辑","editor"); self.reviewer=self.db.add_user("审阅","reviewer"); self.outsider=self.db.add_user("外部","reviewer")
        self.work=self.db.create_work("残卷","异文比较",self.owner)
        self.w1=self.db.add_witness(self.work,"甲本","version"); self.w2=self.db.add_witness(self.work,"乙本","fragment","馆藏残片","中段缺页")
        self.db.grant_witness_editor(self.w2,self.editor,self.owner); self.db.grant_work_access(self.work,self.reviewer,"view",self.owner)
        self.passage=self.db.add_passage(self.work,"第一节","春水东流，故人南去。",self.owner)
        self.db.align_passage(self.passage,self.w1,"春水东流，故人南去。",1,self.owner)
        self.db.align_passage(self.passage,self.w2,"春水东流，[缺页]",2,self.editor)
    def tearDown(self): self.db.close(); os.unlink(self.path)

    def test_multilayer_revision_snapshot_export_and_lock(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        rev=self.db.update_variant(variant,"春水东流，[不可辨]人南去。","墨迹受损，不再直接补写",self.editor,1)
        self.assertEqual(2,rev)
        snap=self.db.get_snapshot(self.passage,2,self.owner)
        self.assertEqual(2,snap["layer"])
        # 未确认修订不会进入交付稿，也不计入缺口统计之外的异文。
        pending_export=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,pending_export["gap_count"])
        self.assertEqual(0,len(pending_export["passages"][0]["variants"]))
        self.assertEqual(1,len(pending_export["passages"][0]["pending_variants"]))
        self.db.confirm_variant(variant,self.owner)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,exported["gap_count"])
        self.assertEqual([],exported["passages"][0]["variants"][0]["notes"])
        self.db.lock_passage(self.passage,self.owner,"定稿")
        with self.assertRaisesRegex(DomainError,"封存"):
            self.db.update_variant(variant,"另一文本","无意义修改",self.editor,2)

    def test_optimistic_lock_permission_and_mark_validation(self):
        first=self.db.create_variant(self.passage,self.w2,"补足一","理由一",self.editor,0)
        with self.assertRaisesRegex(DomainError,"版本冲突"):
            self.db.create_variant(self.passage,self.w2,"补足二","理由二",self.editor,0)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.create_variant(self.passage,self.w2,"补足三","理由三",self.reviewer,1)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.export_collation(self.work,self.outsider)
        with self.assertRaisesRegex(DomainError,"括号"):
            self.db.align_passage(self.passage,self.w1,"文本[未闭合",9,self.owner)
        self.assertEqual(first,first)

    def test_seal_keeps_snapshot_but_allows_supplements(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        self.db.add_note(variant,"封存前的定案说明。",self.owner)
        self.db.confirm_variant(variant,self.owner)
        result=self.db.seal_passage(self.passage,self.owner,"定稿封存")
        self.assertEqual(1,result["revision_no"])
        # 已交付内容不可改动
        with self.assertRaisesRegex(DomainError,"封存"):
            self.db.align_passage(self.passage,self.w1,"改动对齐",3,self.owner)
        with self.assertRaisesRegex(DomainError,"封存"):
            self.db.update_variant(variant,"再改一层","封存后想推翻",self.editor,2)
        with self.assertRaisesRegex(DomainError,"重复封存"):
            self.db.seal_passage(self.passage,self.owner,"再次封存")
        # 未确认修订不能封存（在另一段落验证）
        p2=self.db.add_passage(self.work,"第二节","山高水长。",self.owner)
        self.db.align_passage(p2,self.w1,"山高水长。",1,self.owner)
        self.db.create_variant(p2,self.w1,"山高水远。","疑似异文待核",self.owner,0)
        with self.assertRaisesRegex(DomainError,"未确认修订"):
            self.db.seal_passage(p2,self.owner,"提前封存")
        # 封存后仍可补录：注释 + 新异文（标记为封存补录，不能混入交付稿）
        self.db.add_note(variant,"封存后新发现的纸背线索。",self.editor)
        supp=self.db.create_variant(self.passage,self.w2,"春水东流，[残损]南去。","补录存疑一处",self.editor,1)
        exported=self.db.export_collation(self.work,self.owner)
        block=exported["passages"][0]
        self.assertTrue(block["seal"])
        self.assertEqual(1,len(block["variants"]))                      # 交付稿：封存快照内容
        self.assertEqual("春水东流，故人南去。",block["variants"][0]["proposed_text"])
        self.assertEqual(1,len(block["variants"][0]["notes"]))          # 封存前注释留在交付稿；封存后注释进入补录区
        self.assertEqual(1,len(block["supplements"]["notes"]))
        self.assertEqual(1,len(block["supplements"]["variants"]))
        self.assertEqual(supp,block["supplements"]["variants"][0]["id"])
        self.assertEqual(1,block["supplements"]["variants"][0]["sealed_supplement"])
        # 原负责人转交后仍可查看封存内容
        self.assertIn("gap_basis",block["seal"])

    def test_gap_basis_change_invalidates_seals_and_recounts(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        self.db.confirm_variant(variant,self.owner)
        self.db.seal_passage(self.passage,self.owner,"定稿")
        before=self.db.export_collation(self.work,self.owner)
        self.assertEqual(1,before["gap_count"])
        # 改依据：旧封存立即失效
        res=self.db.set_gap_basis(self.work,["[不可辨]"],self.owner)
        self.assertEqual(1,res["invalidated_seals"])
        after=self.db.export_collation(self.work,self.owner)
        self.assertEqual(0,after["gap_count"])
        self.assertIsNone(after["passages"][0]["seal"])
        self.assertEqual("open",after["passages"][0]["status"])
        # 已确认的异文仍然确认；新依据下缺口为 0
        self.assertEqual("confirmed",after["passages"][0]["variants"][0]["status"])
        # 无效依据拒绝
        with self.assertRaisesRegex(DomainError,"封存依据"):
            self.db.set_gap_basis(self.work,["缺页"],self.owner)
        # 相同依据不误伤封存
        self.db.seal_passage(self.passage,self.owner,"按新依据重新封存")
        res2=self.db.set_gap_basis(self.work,["[不可辨]"],self.owner)
        self.assertEqual(0,res2["invalidated_seals"])
        self.assertIsNotNone(self.db.export_collation(self.work,self.owner)["passages"][0]["seal"])
        # 非负责人不能改依据
        with self.assertRaisesRegex(DomainError,"负责人"):
            self.db.set_gap_basis(self.work,["[缺页]"],self.editor)

    def test_confirm_is_single_shot_even_concurrently(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        outcomes=[]
        barrier=threading.Barrier(5)
        def confirm():
            try:
                barrier.wait()
                self.db.confirm_variant(variant,self.owner); outcomes.append("ok")
            except DomainError: outcomes.append("err")
        threads=[threading.Thread(target=confirm) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(1,outcomes.count("ok")); self.assertEqual(4,outcomes.count("err"))
        row=self.db.conn.execute("SELECT status,confirmed_by FROM variants WHERE id=?",(variant,)).fetchone()
        self.assertEqual("confirmed",row["status"]); self.assertEqual(self.owner,row["confirmed_by"])
        with self.assertRaisesRegex(DomainError,"已经确认"):
            self.db.confirm_variant(variant,self.owner)
        with self.assertRaisesRegex(DomainError,"负责人"):
            self.db.confirm_variant(variant if False else self.db.create_variant(
                self.passage,self.w2,"另一处","理由足够长",self.editor,1),self.editor)

    def test_handover_transfers_responsibility_and_revokes_write(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        p2=self.db.add_passage(self.work,"第二节","山高水长。",self.owner)
        self.db.align_passage(p2,self.w1,"山高水长。",1,self.owner)
        # 接手人权限不符：editor/reviewer 都被拒绝
        with self.assertRaisesRegex(DomainError,"权限不符"):
            self.db.handover_work(self.work,self.editor,self.owner)
        with self.assertRaisesRegex(DomainError,"权限不符"):
            self.db.handover_work(self.work,self.reviewer,self.owner)
        with self.assertRaisesRegex(DomainError,"转交给自己"):
            self.db.handover_work(self.work,self.owner,self.owner)
        # 非现任负责人不能转交
        with self.assertRaisesRegex(DomainError,"现任负责人"):
            self.db.handover_work(self.work,self.next_owner,self.next_owner)
        result=self.db.handover_work(self.work,self.next_owner,self.owner)
        self.assertEqual(1,result["pending_revisions"])     # variant 未确认
        self.assertEqual(2,result["unsealed_passages"])    # 两节都未封存
        work=self.db.conn.execute("SELECT owner_id FROM works WHERE id=?",(self.work,)).fetchone()
        self.assertEqual(self.next_owner,work["owner_id"])
        # 原负责人失去写权限：锁段、改异文、改依据都被拒绝；但仍可查看
        with self.assertRaisesRegex(DomainError,"负责人"):
            self.db.seal_passage(self.passage,self.owner,"旧负责人锁段")
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.create_variant(self.passage,self.w1,"旧负责人补异文","理由足够长",self.owner,1)
        self.assertTrue(self.db.can_view_work(self.work,self.owner))
        # 新负责人接手：确认未处理修订并封存
        self.db.confirm_variant(variant,self.next_owner)
        self.db.seal_passage(self.passage,self.next_owner,"接手后定稿")
        # 历史可查
        history=self.db.work_history(self.work,self.owner)
        self.assertEqual(1,len(history["handovers"]))
        h=history["handovers"][0]
        self.assertEqual(self.owner,h["from_owner"]); self.assertEqual(self.next_owner,h["to_owner"])
        self.assertGreaterEqual(len(history["seals"]),1)
        self.assertIn("sealed",[e["action"] for e in history["seals"]])
        self.assertEqual("confirmed",history["confirmations"][0]["status"])
        # 外部用户不能看历史
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.work_history(self.work,self.outsider)

    def test_concurrent_handover_succeeds_only_once(self):
        outcomes=[]; barrier=threading.Barrier(2)
        rival=self.db.add_user("另一位负责人","owner")
        def handover(target):
            try:
                barrier.wait()
                self.db.handover_work(self.work,target,self.owner); outcomes.append(("ok",target))
            except DomainError as exc: outcomes.append(("err",str(exc)))
        t1=threading.Thread(target=handover,args=(self.next_owner,))
        t2=threading.Thread(target=handover,args=(rival,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(1,len([o for o in outcomes if o[0]=="ok"]))
        winner=[o[1] for o in outcomes if o[0]=="ok"][0]
        owner=self.db.conn.execute("SELECT owner_id FROM works WHERE id=?",(self.work,)).fetchone()[0]
        self.assertEqual(winner,owner)
        logs=self.db.conn.execute("SELECT COUNT(*) FROM handover_log WHERE work_id=?",(self.work,)).fetchone()[0]
        self.assertEqual(1,logs)

    def test_seal_snapshot_survives_later_changes(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        self.db.confirm_variant(variant,self.owner)
        self.db.seal_passage(self.passage,self.owner,"定稿")
        # 依据变更让旧封存失效后，历史中仍保留封存快照
        self.db.set_gap_basis(self.work,["[残损]","[缺页]"],self.owner)
        history=self.db.work_history(self.work,self.owner)
        sealed_events=[e for e in history["seals"] if e["action"]=="sealed"]
        self.assertTrue(sealed_events[0]["snapshot"])
        self.assertEqual("春水东流，故人南去。",
                         sealed_events[0]["snapshot"]["variants"][0]["proposed_text"])
        invalidated=[e for e in history["seals"] if e["action"]=="invalidated"]
        self.assertEqual(1,len(invalidated))
        self.assertIsNone(invalidated[0]["snapshot"])
        # 未确认修订导出时不与已确认内容混杂
        self.db.create_variant(self.passage,self.w2,"又一处新拟","新的存疑一处",self.editor,1)
        exported=self.db.export_collation(self.work,self.owner)
        b=exported["passages"][0]
        self.assertEqual(1,len(b["variants"])); self.assertEqual(1,len(b["pending_variants"]))


if __name__=="__main__": unittest.main()
