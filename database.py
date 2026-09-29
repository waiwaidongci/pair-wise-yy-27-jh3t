from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


WITNESS_KINDS = {"version", "fragment", "transcription"}
SPECIAL_TOKENS = {"[缺页]", "[不可辨]", "[残损]", "[插入]", "[删除]"}


def validate_transcription(text: str) -> str:
    text = text.strip()
    if not text:
        raise DomainError("文本不能为空")
    unclosed = text.count("[") - text.count("]")
    if unclosed:
        raise DomainError("校勘标记括号不匹配")
    return text


class CollationDB:
    """SQLite-backed textual collation service with optimistic revisions."""

    def __init__(self, path: str = "collation.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('owner','editor','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS works (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              owner_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_access (
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              permission TEXT NOT NULL CHECK(permission IN ('view','review')),
              PRIMARY KEY(work_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS witnesses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              siglum TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('version','fragment','transcription')),
              source_note TEXT NOT NULL DEFAULT '',
              missing_sections TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(work_id,siglum)
            );
            CREATE TABLE IF NOT EXISTS witness_editors (
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              granted_by INTEGER NOT NULL REFERENCES users(id),
              PRIMARY KEY(witness_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS passages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              base_text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','locked')),
              revision INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(work_id,label)
            );
            CREATE TABLE IF NOT EXISTS alignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              aligned_text TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id,witness_id)
            );
            CREATE TABLE IF NOT EXISTS variants (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id),
              base_text TEXT NOT NULL,
              proposed_text TEXT NOT NULL,
              reason TEXT NOT NULL,
              layer INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              variant_id INTEGER REFERENCES variants(id) ON DELETE CASCADE,
              revision_no INTEGER NOT NULL,
              layer INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id, revision_no)
            );
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
              body TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passage_locks (
              passage_id INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
              locked_by INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL DEFAULT '',
              locked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seals (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              seal_no INTEGER NOT NULL,
              basis_hash TEXT NOT NULL,
              snapshot_json TEXT NOT NULL,
              sealed_by INTEGER NOT NULL REFERENCES users(id),
              sealed_at TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'sealed' CHECK(status IN ('sealed','invalid')),
              invalidated_at TEXT,
              invalidated_reason TEXT,
              UNIQUE(passage_id, seal_no)
            );
            CREATE TABLE IF NOT EXISTS handovers (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              from_user INTEGER NOT NULL REFERENCES users(id),
              to_user INTEGER NOT NULL REFERENCES users(id),
              idempotency_key TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL DEFAULT 'completed' CHECK(status IN ('completed','cancelled')),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revision_confirms (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
              revision_no INTEGER NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE,
              confirmed_by INTEGER NOT NULL REFERENCES users(id),
              confirmed_at TEXT NOT NULL,
              UNIQUE(variant_id, revision_no)
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        owner = self.add_user("项目负责人", "owner")
        editor = self.add_user("校勘编辑", "editor")
        work = self.create_work("一则残卷", "演示不同版本的校勘", owner)
        w1 = self.add_witness(work, "甲本", "version", "馆藏胶片", "")
        w2 = self.add_witness(work, "乙本", "fragment", "残片转录", "第二句残损")
        self.grant_witness_editor(w2, editor, owner)
        passage = self.add_passage(work, "第1节", "春水东流，故人南去。", owner)
        self.align_passage(passage, w1, "春水东流，故人南去。", 1, owner)
        self.align_passage(passage, w2, "春水东流，[不可辨][不可辨]。", 2, owner)
        variant = self.create_variant(passage, w2, "春水东流，故人南去。", "综合语义与行款补足", owner, 0)
        self.add_note(variant, "补字仍需参照纸背墨迹。", editor)
        self.confirm_revision(variant, 1, owner, "seed-confirm-1")
        self.seal_passage(passage, owner, "初稿定稿封存")

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"owner", "editor", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_work(self, title: str, description: str, owner_id: int) -> int:
        owner = self.conn.execute("SELECT role FROM users WHERE id=?", (owner_id,)).fetchone()
        if not owner or owner["role"] != "owner" or not title.strip():
            raise DomainError("作品标题或负责人无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO works(title,description,owner_id,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), owner_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def grant_work_access(self, work_id: int, user_id: int, permission: str, granted_by: int) -> None:
        if permission not in {"view", "review"}:
            raise DomainError("权限必须为 view 或 review")
        self._require_owner(work_id, granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO work_access(work_id,user_id,permission) VALUES(?,?,?) "
                "ON CONFLICT(work_id,user_id) DO UPDATE SET permission=excluded.permission",
                (work_id, user_id, permission),
            )

    def _require_owner(self, work_id: int, user_id: int) -> None:
        row = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone()
        if not row:
            raise DomainError("只有项目负责人可以执行此操作")

    def can_view_work(self, work_id: int, user_id: int) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM works WHERE id=? AND owner_id=? "
            "UNION ALL SELECT 1 FROM work_access WHERE work_id=? AND user_id=? "
            "UNION ALL SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id "
            "WHERE w.work_id=? AND e.user_id=? LIMIT 1",
            (work_id, user_id, work_id, user_id, work_id, user_id),
        ).fetchone())

    def can_edit_witness(self, witness_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT w.work_id,wa.permission FROM witnesses w LEFT JOIN work_access wa ON wa.work_id=w.work_id AND wa.user_id=? WHERE w.id=?",
            (user_id, witness_id),
        ).fetchone()
        if not row:
            return False
        owner = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (row["work_id"], user_id)).fetchone()
        editor = self.conn.execute("SELECT 1 FROM witness_editors WHERE witness_id=? AND user_id=?", (witness_id, user_id)).fetchone()
        return bool(owner or editor)

    def add_witness(self, work_id: int, siglum: str, kind: str, source_note: str = "", missing_sections: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
            raise DomainError("作品不存在")
        if not siglum.strip() or kind not in WITNESS_KINDS:
            raise DomainError("版本标识或类型无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO witnesses(work_id,siglum,kind,source_note,missing_sections,created_at) VALUES(?,?,?,?,?,?)",
                    (work_id, siglum.strip(), kind, source_note.strip(), missing_sections.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一作品中的版本标识不能重复") from exc
        return int(cur.lastrowid)

    def grant_witness_editor(self, witness_id: int, user_id: int, granted_by: int) -> None:
        witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness:
            raise DomainError("版本不存在")
        self._require_owner(witness["work_id"], granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO witness_editors(witness_id,user_id,granted_by) VALUES(?,?,?)",
                (witness_id, user_id, granted_by),
            )

    def add_passage(self, work_id: int, label: str, base_text: str, user_id: int) -> int:
        self._require_owner(work_id, user_id)
        text = validate_transcription(base_text)
        if not label.strip():
            raise DomainError("段落标签不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO passages(work_id,label,base_text,updated_by,updated_at) VALUES(?,?,?,?,?)",
                    (work_id, label.strip(), text, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("段落标签已存在") from exc
        return int(cur.lastrowid)

    def align_passage(self, passage_id: int, witness_id: int, aligned_text: str, sort_order: int, user_id: int) -> int:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if sort_order <= 0:
            raise DomainError("排序号必须大于0")
        text = validate_transcription(aligned_text)
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO alignments(passage_id,witness_id,aligned_text,sort_order,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (passage_id, witness_id, text, sort_order, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该版本已经对齐此段落") from exc
        return int(cur.lastrowid)

    def create_variant(self, passage_id: int, witness_id: int, proposed_text: str, reason: str,
                       user_id: int, expected_revision: int) -> int:
        with self.transaction():
            passage, lock = self._editable_passage(passage_id, witness_id, user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            if not self.conn.execute("SELECT 1 FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)).fetchone():
                raise DomainError("该版本尚未对齐此段落")
            cur = self.conn.execute(
                "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (passage_id, witness_id, passage["base_text"], text, reason.strip(), user_id, datetime.now().isoformat(), datetime.now().isoformat()),
            )
            variant_id = int(cur.lastrowid)
            revision = self._record_revision(passage_id, variant_id, 1, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), passage_id))
        return variant_id

    def update_variant(self, variant_id: int, proposed_text: str, reason: str, user_id: int,
                       expected_revision: int) -> int:
        with self.transaction():
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant:
                raise DomainError("异文记录不存在")
            passage, _ = self._editable_passage(variant["passage_id"], variant["witness_id"], user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            layer = int(self.conn.execute("SELECT COALESCE(MAX(layer),0)+1 FROM variants WHERE passage_id=? AND witness_id=?", (variant["passage_id"], variant["witness_id"])).fetchone()[0])
            self.conn.execute(
                "UPDATE variants SET proposed_text=?,reason=?,layer=?,updated_at=? WHERE id=?",
                (text, reason.strip(), layer, datetime.now().isoformat(), variant_id),
            )
            revision = self._record_revision(variant["passage_id"], variant_id, layer, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), variant["passage_id"]))
        return revision

    def _editable_passage(self, passage_id: int, witness_id: int, user_id: int, expected_revision: int):
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone():
            raise DomainError("段落已锁定，不能修改")
        if self._has_valid_seal(passage_id):
            raise DomainError("段落已封存，不能改动已交付内容")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if passage["revision"] != expected_revision:
            raise DomainError(f"版本冲突：当前修订为 {passage['revision']}，提交基于 {expected_revision}")
        return passage, None

    def _record_revision(self, passage_id: int, variant_id: int, layer: int, user_id: int) -> int:
        revision = int(self.conn.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM revisions WHERE passage_id=?", (passage_id,)).fetchone()[0])
        snapshot = {
            "passage": dict(self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()),
            "variant": dict(self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()),
            "alignments": [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id WHERE a.passage_id=? ORDER BY a.sort_order",
                (passage_id,),
            ).fetchall()],
        }
        self.conn.execute(
            "INSERT INTO revisions(passage_id,variant_id,revision_no,layer,snapshot_json,author_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (passage_id, variant_id, revision, layer, json.dumps(snapshot, ensure_ascii=False), user_id, datetime.now().isoformat()),
        )
        return revision

    def add_note(self, variant_id: int, body: str, author_id: int) -> int:
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant or not self.can_view_work(
            self.conn.execute("SELECT work_id FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()["work_id"], author_id
        ):
            raise DomainError("异文不存在或无权评论")
        if not body.strip():
            raise DomainError("注释不能为空")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO notes(variant_id,body,author_id,created_at) VALUES(?,?,?,?)",
                (variant_id, body.strip(), author_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def lock_passage(self, passage_id: int, user_id: int, reason: str = "") -> None:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='locked',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute(
                "INSERT OR REPLACE INTO passage_locks(passage_id,locked_by,reason,locked_at) VALUES(?,?,?,?)",
                (passage_id, user_id, reason.strip(), datetime.now().isoformat()),
            )

    # ---- 封存与授权移交闭环 ----

    def _basis_hash(self, passage_id: int) -> str:
        """封存依据 = 底本 + 全部对齐文本；任一变动都会使旧封存失效。"""
        passage = self.conn.execute("SELECT base_text FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        aligns = self.conn.execute(
            "SELECT witness_id,aligned_text,sort_order FROM alignments WHERE passage_id=? ORDER BY witness_id",
            (passage_id,),
        ).fetchall()
        h = hashlib.sha256()
        h.update(passage["base_text"].encode("utf-8"))
        for a in aligns:
            h.update(f"|{a['witness_id']}:{a['aligned_text']}:{a['sort_order']}".encode("utf-8"))
        return h.hexdigest()

    def _has_valid_seal(self, passage_id: int) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM seals WHERE passage_id=? AND status='sealed' AND basis_hash=? LIMIT 1",
            (passage_id, self._basis_hash(passage_id)),
        ).fetchone())

    def _variant_current_revision(self, variant_id: int) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(revision_no),0) FROM revisions WHERE variant_id=?", (variant_id,)).fetchone()[0]
        return int(row)

    def _variant_confirmed(self, variant_id: int) -> bool:
        current = self._variant_current_revision(variant_id)
        if current <= 0:
            return False
        return bool(self.conn.execute(
            "SELECT 1 FROM revision_confirms WHERE variant_id=? AND revision_no=?",
            (variant_id, current),
        ).fetchone())

    def _seal_snapshot(self, passage_id: int) -> dict:
        passage = dict(self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone())
        alignments = [dict(r) for r in self.conn.execute(
            "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
            "WHERE a.passage_id=? ORDER BY a.sort_order", (passage_id,),
        ).fetchall()]
        variants = []
        for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage_id,)).fetchall():
            variant = dict(row)
            variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
            variants.append(variant)
        return {"passage": passage, "alignments": alignments, "variants": variants}

    def seal_passage(self, passage_id: int, user_id: int, reason: str = "") -> dict:
        """封存段落：留存异文、注释与对齐快照。封存后已交付内容不可改动，注释仍可补录。"""
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        basis = self._basis_hash(passage_id)
        snapshot = self._seal_snapshot(passage_id)
        with self.transaction():
            seal_no = int(self.conn.execute("SELECT COALESCE(MAX(seal_no),0)+1 FROM seals WHERE passage_id=?", (passage_id,)).fetchone()[0])
            cur = self.conn.execute(
                "INSERT INTO seals(passage_id,seal_no,basis_hash,snapshot_json,sealed_by,sealed_at,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (passage_id, seal_no, basis, json.dumps(snapshot, ensure_ascii=False), user_id, datetime.now().isoformat(), "sealed"),
            )
        return {"seal_id": int(cur.lastrowid), "seal_no": seal_no, "basis_hash": basis, "sealed_at": datetime.now().isoformat()}

    def invalidate_seals(self, passage_id: int, reason: str) -> int:
        """封存依据变动后，旧封存立即失效。返回失效封存数。"""
        with self.transaction():
            cur = self.conn.execute(
                "UPDATE seals SET status='invalid',invalidated_at=?,invalidated_reason=? "
                "WHERE passage_id=? AND status='sealed'",
                (datetime.now().isoformat(), reason.strip(), passage_id),
            )
        return cur.rowcount

    def update_passage_basis(self, passage_id: int, base_text: str, user_id: int) -> None:
        """修改封存依据（底本）。旧封存立即失效，导出与缺口统计按新依据重算。"""
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        text = validate_transcription(base_text)
        with self.transaction():
            self.conn.execute("UPDATE passages SET base_text=?,updated_by=?,updated_at=? WHERE id=?", (text, user_id, datetime.now().isoformat(), passage_id))
        self.invalidate_seals(passage_id, "底本依据已修改")

    def update_alignment(self, passage_id: int, witness_id: int, aligned_text: str, sort_order: int, user_id: int) -> None:
        """改定对齐文本（封存依据之一）。旧封存立即失效。"""
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if sort_order <= 0:
            raise DomainError("排序号必须大于0")
        text = validate_transcription(aligned_text)
        with self.transaction():
            self.conn.execute(
                "INSERT INTO alignments(passage_id,witness_id,aligned_text,sort_order,created_by,created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(passage_id,witness_id) DO UPDATE SET aligned_text=excluded.aligned_text,sort_order=excluded.sort_order",
                (passage_id, witness_id, text, sort_order, user_id, datetime.now().isoformat()),
            )
        self.invalidate_seals(passage_id, "对齐依据已修改")

    def _can_take_over(self, work_id: int, user_id: int) -> bool:
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            return False
        if self.conn.execute("SELECT 1 FROM work_access WHERE work_id=? AND user_id=? AND permission='review'", (work_id, user_id)).fetchone():
            return True
        return bool(self.conn.execute(
            "SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id WHERE w.work_id=? AND e.user_id=? LIMIT 1",
            (work_id, user_id),
        ).fetchone())

    def handover_work(self, work_id: int, from_user_id: int, to_user_id: int, idempotency_key: str) -> dict:
        """负责人转交作品：未处理修订与封存责任一并交出，原负责人失去写权限。

        幂等：同一 idempotency_key 并发或重复提交只成功一次，重放原结果。
        """
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if not work:
            raise DomainError("作品不存在")
        if not idempotency_key.strip():
            raise DomainError("转交必须携带幂等键")
        if to_user_id == from_user_id:
            raise DomainError("不能转交给自己")
        # 幂等重放：同键直接返回首次结果
        existing = self.conn.execute("SELECT * FROM handovers WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing:
            return self._handover_result(existing)
        if work["owner_id"] != from_user_id:
            raise DomainError("只有当前负责人可以转交作品")
        if not self._can_take_over(work_id, to_user_id):
            raise DomainError("接手人权限不符，拒绝转交")
        now = datetime.now().isoformat()
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO handovers(work_id,from_user,to_user,idempotency_key,status,created_at) VALUES(?,?,?,?,?,?)",
                    (work_id, from_user_id, to_user_id, idempotency_key.strip(), "completed", now),
                )
            except sqlite3.IntegrityError:
                # 并发下唯一键冲突：重放已提交的结果
                row = self.conn.execute("SELECT * FROM handovers WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                return self._handover_result(row)
            self.conn.execute("UPDATE works SET owner_id=? WHERE id=?", (to_user_id, work_id))
            # 原负责人的审阅权限降为查看，失去写权限
            self.conn.execute(
                "UPDATE work_access SET permission='view' WHERE work_id=? AND user_id=? AND permission='review'",
                (work_id, from_user_id),
            )
            handover_id = int(cur.lastrowid)
        row = self.conn.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
        return self._handover_result(row)

    def _handover_result(self, row: sqlite3.Row) -> dict:
        return {
            "handover_id": row["id"], "work_id": row["work_id"],
            "from_user": row["from_user"], "to_user": row["to_user"],
            "status": row["status"], "created_at": row["created_at"],
        }

    def confirm_revision(self, variant_id: int, revision_no: int, user_id: int, idempotency_key: str) -> dict:
        """确认修订：未确认修订不能混入交付稿。同一修订并发确认只成功一次。"""
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant:
            raise DomainError("异文记录不存在")
        if not idempotency_key.strip():
            raise DomainError("修订确认必须携带幂等键")
        # 幂等重放：同键直接返回首次结果，不重复校验权限
        existing = self.conn.execute("SELECT * FROM revision_confirms WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing:
            return self._confirm_result(existing)
        already = self.conn.execute(
            "SELECT * FROM revision_confirms WHERE variant_id=? AND revision_no=?", (variant_id, revision_no),
        ).fetchone()
        if already:
            return self._confirm_result(already)
        work_id = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()["work_id"]
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if work["owner_id"] != user_id:
            raise DomainError("只有负责人可以确认修订")
        current = self._variant_current_revision(variant_id)
        if revision_no != current:
            raise DomainError(f"只能确认当前修订（当前为第 {current} 层）")
        now = datetime.now().isoformat()
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO revision_confirms(passage_id,variant_id,revision_no,idempotency_key,confirmed_by,confirmed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (variant["passage_id"], variant_id, revision_no, idempotency_key.strip(), user_id, now),
                )
            except sqlite3.IntegrityError:
                row = self.conn.execute("SELECT * FROM revision_confirms WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                return self._confirm_result(row)
            confirm_id = int(cur.lastrowid)
        row = self.conn.execute("SELECT * FROM revision_confirms WHERE id=?", (confirm_id,)).fetchone()
        return self._confirm_result(row)

    def _confirm_result(self, row: sqlite3.Row) -> dict:
        return {
            "confirm_id": row["id"], "variant_id": row["variant_id"], "revision_no": row["revision_no"],
            "confirmed_by": row["confirmed_by"], "confirmed_at": row["confirmed_at"],
        }

    def list_seals(self, work_id: int, user_id: int) -> list[dict]:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该作品的封存历史")
        out = []
        for row in self.conn.execute(
            "SELECT s.*,p.label AS passage_label,u.name AS sealed_by_name FROM seals s "
            "JOIN passages p ON p.id=s.passage_id JOIN users u ON u.id=s.sealed_by "
            "WHERE p.work_id=? ORDER BY s.id", (work_id,),
        ).fetchall():
            item = dict(row)
            item["valid"] = (row["status"] == "sealed" and row["basis_hash"] == self._basis_hash(row["passage_id"]))
            out.append(item)
        return out

    def list_handovers(self, work_id: int, user_id: int) -> list[dict]:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该作品的转交历史")
        return [dict(r) for r in self.conn.execute(
            "SELECT h.*,uf.name AS from_user_name,ut.name AS to_user_name FROM handovers h "
            "JOIN users uf ON uf.id=h.from_user JOIN users ut ON ut.id=h.to_user "
            "WHERE h.work_id=? ORDER BY h.id", (work_id,),
        ).fetchall()]

    def passage_history(self, passage_id: int, user_id: int) -> dict:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该段落的历史")
        revisions = [dict(r) for r in self.conn.execute(
            "SELECT r.*,u.name AS author_name FROM revisions r JOIN users u ON u.id=r.author_id "
            "WHERE r.passage_id=? ORDER BY r.revision_no", (passage_id,),
        ).fetchall()]
        seals = [dict(r) for r in self.conn.execute(
            "SELECT s.*,u.name AS sealed_by_name FROM seals s JOIN users u ON u.id=s.sealed_by "
            "WHERE s.passage_id=? ORDER BY s.seal_no", (passage_id,),
        ).fetchall()]
        confirms = [dict(r) for r in self.conn.execute(
            "SELECT c.*,u.name AS confirmed_by_name FROM revision_confirms c JOIN users u ON u.id=c.confirmed_by "
            "WHERE c.passage_id=? ORDER BY c.id", (passage_id,),
        ).fetchall()]
        return {"passage": dict(passage), "revisions": revisions, "seals": seals, "confirms": confirms}

    # ---- 快照与导出 ----

    def get_snapshot(self, passage_id: int, revision_no: int, user_id: int) -> dict:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该快照")
        row = self.conn.execute("SELECT * FROM revisions WHERE passage_id=? AND revision_no=?", (passage_id, revision_no)).fetchone()
        if not row:
            raise DomainError("快照不存在")
        return {"revision_no": row["revision_no"], "layer": row["layer"], "created_at": row["created_at"], "snapshot": json.loads(row["snapshot_json"])}

    def export_collation(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        witnesses = [dict(r) for r in self.conn.execute("SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (work_id,))]
        passages = []
        gaps = 0
        pending = 0
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            alignments = []
            for row in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind,w.missing_sections FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],)
            ).fetchall():
                item = dict(row)
                if "[缺页]" in item["aligned_text"] or "[残损]" in item["aligned_text"]:
                    item["has_gap"] = True
                    gaps += 1
                alignments.append(item)
            variants = []
            pending_variants = []
            for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)).fetchall():
                variant = dict(row)
                variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
                if self._variant_confirmed(row["id"]):
                    variants.append(variant)
                else:
                    variant["pending_revision"] = self._variant_current_revision(row["id"])
                    pending_variants.append(variant)
                    pending += 1
            valid_seal = self.conn.execute(
                "SELECT seal_no FROM seals WHERE passage_id=? AND status='sealed' AND basis_hash=? ORDER BY seal_no DESC LIMIT 1",
                (passage["id"], self._basis_hash(passage["id"])),
            ).fetchone()
            passages.append({
                **dict(passage), "alignments": alignments, "variants": variants,
                "pending_variants": pending_variants,
                "sealed": bool(valid_seal), "seal_no": valid_seal["seal_no"] if valid_seal else None,
            })
        return {
            "work": dict(work), "witnesses": witnesses, "passages": passages,
            "gap_count": gaps, "pending_count": pending,
            "delivered": {"gap_count": gaps, "sealed_passages": sum(1 for p in passages if p["sealed"])},
        }

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
            "seals": [dict(r) for r in self.conn.execute("SELECT * FROM seals ORDER BY id")],
            "handovers": [dict(r) for r in self.conn.execute("SELECT * FROM handovers ORDER BY id")],
            "revision_confirms": [dict(r) for r in self.conn.execute("SELECT * FROM revision_confirms ORDER BY id")],
        }
