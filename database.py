from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


WITNESS_KINDS = {"version", "fragment", "transcription"}
SPECIAL_TOKENS = {"[缺页]", "[不可辨]", "[残损]", "[插入]", "[删除]"}
DEFAULT_GAP_BASIS = ["[缺页]", "[残损]"]
TOKEN_RE = re.compile(r"^\[[^\[\]]{1,8}\]$")


def validate_transcription(text: str) -> str:
    text = text.strip()
    if not text:
        raise DomainError("文本不能为空")
    unclosed = text.count("[") - text.count("]")
    if unclosed:
        raise DomainError("校勘标记括号不匹配")
    return text


def normalize_basis(basis) -> list[str]:
    if isinstance(basis, str):
        basis = [b.strip() for b in basis.replace("，", ",").split(",") if b.strip()]
    if not isinstance(basis, (list, tuple)) or not basis:
        raise DomainError("封存依据至少包含一个缺口标记")
    cleaned = []
    for token in basis:
        token = str(token).strip()
        if not TOKEN_RE.match(token):
            raise DomainError(f"封存依据标记无效：{token}（应为 [缺页] 形式）")
        if token not in cleaned:
            cleaned.append(token)
    if len(cleaned) > 6:
        raise DomainError("封存依据最多包含 6 个缺口标记")
    return cleaned


class CollationDB:
    """SQLite-backed textual collation service with sealing, handover and optimistic revisions."""

    def __init__(self, path: str = "collation.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        # 所有写事务串行化：并发的转交 / 确认 / 封存提交在事务内复查状态，保证只有一次成功。
        self._txlock = threading.Lock()
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        self._txlock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._txlock.release()

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
              gap_basis TEXT NOT NULL DEFAULT '["[缺页]","[残损]"]',
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
              status TEXT NOT NULL DEFAULT 'open',
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
              status TEXT NOT NULL DEFAULT 'pending',
              sealed_supplement INTEGER NOT NULL DEFAULT 0,
              confirmed_by INTEGER REFERENCES users(id),
              confirmed_at TEXT,
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
            CREATE TABLE IF NOT EXISTS passage_seals (
              passage_id INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
              sealed_by INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL DEFAULT '',
              basis_json TEXT NOT NULL,
              snapshot_json TEXT NOT NULL,
              revision_no INTEGER NOT NULL,
              sealed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seal_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL,
              work_id INTEGER NOT NULL,
              action TEXT NOT NULL CHECK(action IN ('sealed','invalidated')),
              actor_id INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL DEFAULT '',
              basis_json TEXT NOT NULL DEFAULT '[]',
              snapshot_json TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS handover_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              from_owner INTEGER NOT NULL REFERENCES users(id),
              to_owner INTEGER NOT NULL REFERENCES users(id),
              pending_revisions INTEGER NOT NULL,
              unsealed_passages INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        self._migrate()
        self.conn.commit()

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        if "gap_basis" not in self._columns("works"):
            self.conn.execute("ALTER TABLE works ADD COLUMN gap_basis TEXT NOT NULL DEFAULT "
                              "'[\"[缺页]\",\"[残损]\"]'")
        variant_cols = self._columns("variants")
        if "status" not in variant_cols:
            self.conn.execute("ALTER TABLE variants ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        if "sealed_supplement" not in variant_cols:
            self.conn.execute("ALTER TABLE variants ADD COLUMN sealed_supplement INTEGER NOT NULL DEFAULT 0")
        if "confirmed_by" not in variant_cols:
            self.conn.execute("ALTER TABLE variants ADD COLUMN confirmed_by INTEGER REFERENCES users(id)")
        if "confirmed_at" not in variant_cols:
            self.conn.execute("ALTER TABLE variants ADD COLUMN confirmed_at TEXT")
        # 兼容旧版 locked 数据：迁移为封存并补留快照。
        if self._columns("passages"):
            legacy = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='passage_locks'"
            ).fetchone()
            if legacy:
                for row in self.conn.execute("SELECT * FROM passage_locks").fetchall():
                    pid = row["passage_id"]
                    exists = self.conn.execute(
                        "SELECT 1 FROM passage_seals WHERE passage_id=?", (pid,)
                    ).fetchone()
                    passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (pid,)).fetchone()
                    if passage and not exists:
                        snapshot = self._collect_passage(passage, DEFAULT_GAP_BASIS)
                        self.conn.execute(
                            "INSERT INTO passage_seals(passage_id,sealed_by,reason,basis_json,snapshot_json,"
                            "revision_no,sealed_at) VALUES(?,?,?,?,?,?,?)",
                            (pid, row["locked_by"], row["reason"], json.dumps(DEFAULT_GAP_BASIS, ensure_ascii=False),
                             json.dumps(snapshot, ensure_ascii=False), passage["revision"], row["locked_at"]),
                        )
                        self.conn.execute(
                            "INSERT INTO seal_events(passage_id,work_id,action,actor_id,reason,basis_json,"
                            "snapshot_json,created_at) VALUES(?,?, 'sealed', ?,?,?,?,?)",
                            (pid, passage["work_id"], row["locked_by"], row["reason"],
                             json.dumps(DEFAULT_GAP_BASIS, ensure_ascii=False),
                             json.dumps(snapshot, ensure_ascii=False), row["locked_at"]),
                        )
                self.conn.execute("UPDATE passages SET status='sealed' WHERE status='locked'")

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        owner = self.add_user("项目负责人", "owner")
        self.add_user("接任负责人", "owner")
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
        self.confirm_variant(variant, owner)

    # ---------- 用户 / 作品 / 授权 ----------

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
                "INSERT INTO works(title,description,owner_id,gap_basis,created_at) VALUES(?,?,?,?,?)",
                (title.strip(), description.strip(), owner_id,
                 json.dumps(DEFAULT_GAP_BASIS, ensure_ascii=False), datetime.now().isoformat()),
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
            raise DomainError("只有现任项目负责人可以执行此操作")

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
            "SELECT w.work_id FROM witnesses w WHERE w.id=?",
            (witness_id,),
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

    # ---------- 负责人转交 ----------

    def handover_work(self, work_id: int, to_user_id: int, user_id: int) -> dict:
        """负责人转交作品：未处理修订和封存责任一并移交，原负责人写权限立即收回。"""
        with self.transaction():
            work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
            if not work:
                raise DomainError("作品不存在")
            if work["owner_id"] != user_id:
                raise DomainError("只有现任负责人可以转交作品")
            if to_user_id == user_id:
                raise DomainError("不能转交给自己")
            target = self.conn.execute("SELECT * FROM users WHERE id=?", (to_user_id,)).fetchone()
            if not target or target["role"] != "owner":
                raise DomainError("接手人权限不符，转交被拒绝：接手人必须具备负责人(owner)身份")
            pending = int(self.conn.execute(
                "SELECT COUNT(*) FROM variants v JOIN passages p ON p.id=v.passage_id "
                "WHERE p.work_id=? AND v.status='pending'", (work_id,),
            ).fetchone()[0])
            unsealed = int(self.conn.execute(
                "SELECT COUNT(*) FROM passages WHERE work_id=? AND status!='sealed'", (work_id,),
            ).fetchone()[0])
            now = datetime.now().isoformat()
            self.conn.execute(
                "INSERT INTO handover_log(work_id,from_owner,to_owner,pending_revisions,unsealed_passages,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (work_id, user_id, to_user_id, pending, unsealed, now),
            )
            self.conn.execute("UPDATE works SET owner_id=? WHERE id=?", (to_user_id, work_id))
            # 原负责人失去该作品的一切写权限（负责人身份移交、版本编辑授权收回），保留查看权限。
            self.conn.execute(
                "DELETE FROM witness_editors WHERE user_id=? AND witness_id IN "
                "(SELECT id FROM witnesses WHERE work_id=?)",
                (user_id, work_id),
            )
            self.conn.execute(
                "INSERT INTO work_access(work_id,user_id,permission) VALUES(?,?,'view') "
                "ON CONFLICT(work_id,user_id) DO UPDATE SET permission='view'",
                (work_id, user_id),
            )
        return {"new_owner_id": to_user_id, "pending_revisions": pending, "unsealed_passages": unsealed}

    # ---------- 段落与对齐 ----------

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
        if self._is_sealed(passage):
            raise DomainError("段落已封存锁定，不能改动已交付的对齐内容")
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

    # ---------- 异文 / 修订 / 确认 ----------

    def create_variant(self, passage_id: int, witness_id: int, proposed_text: str, reason: str,
                       user_id: int, expected_revision: int) -> int:
        with self.transaction():
            passage, sealed = self._passage_context(passage_id, witness_id, user_id, expected_revision, sealed_ok=True)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            if not self.conn.execute("SELECT 1 FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)).fetchone():
                raise DomainError("该版本尚未对齐此段落")
            now = datetime.now().isoformat()
            cur = self.conn.execute(
                "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,status,sealed_supplement,"
                "created_by,created_at,updated_at) VALUES(?,?,?,?,?, 'pending', ?,?,?,?)",
                (passage_id, witness_id, passage["base_text"], text, reason.strip(),
                 1 if sealed else 0, user_id, now, now),
            )
            variant_id = int(cur.lastrowid)
            revision = self._record_revision(passage_id, variant_id, 1, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, now, passage_id))
        return variant_id

    def update_variant(self, variant_id: int, proposed_text: str, reason: str, user_id: int,
                       expected_revision: int) -> int:
        with self.transaction():
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant:
                raise DomainError("异文记录不存在")
            passage, _ = self._passage_context(variant["passage_id"], variant["witness_id"], user_id, expected_revision, sealed_ok=False)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            layer = int(self.conn.execute("SELECT COALESCE(MAX(layer),0)+1 FROM variants WHERE passage_id=? AND witness_id=?", (variant["passage_id"], variant["witness_id"])).fetchone()[0])
            now = datetime.now().isoformat()
            # 新层一律回到未确认状态，未经确认不能混入交付稿。
            self.conn.execute(
                "UPDATE variants SET proposed_text=?,reason=?,layer=?,status='pending',confirmed_by=NULL,"
                "confirmed_at=NULL,updated_at=? WHERE id=?",
                (text, reason.strip(), layer, now, variant_id),
            )
            revision = self._record_revision(variant["passage_id"], variant_id, layer, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, now, variant["passage_id"]))
        return revision

    def confirm_variant(self, variant_id: int, user_id: int) -> str:
        """确认修订：pending -> confirmed 只允许一次，并发提交只有第一次成功。"""
        with self.transaction():
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant:
                raise DomainError("异文记录不存在")
            passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()
            self._require_owner(passage["work_id"], user_id)
            if variant["status"] == "confirmed":
                raise DomainError("修订已经确认，重复提交无效")
            now = datetime.now().isoformat()
            self.conn.execute(
                "UPDATE variants SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE id=?",
                (user_id, now, variant_id),
            )
        return now

    def _is_sealed(self, passage: sqlite3.Row) -> bool:
        if passage["status"] == "sealed":
            return True
        return bool(self.conn.execute(
            "SELECT 1 FROM passage_seals WHERE passage_id=?", (passage["id"],)
        ).fetchone())

    def _passage_context(self, passage_id: int, witness_id: int, user_id: int, expected_revision: int,
                         sealed_ok: bool):
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        sealed = self._is_sealed(passage)
        if sealed and not sealed_ok:
            raise DomainError("段落已封存锁定，不能改动已交付内容")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if passage["revision"] != expected_revision:
            raise DomainError(f"版本冲突：当前修订为 {passage['revision']}，提交基于 {expected_revision}")
        return passage, sealed

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

    # ---------- 封存 / 封存依据 ----------

    def _collect_passage(self, passage: sqlite3.Row, basis: list) -> dict:
        alignments = []
        for row in self.conn.execute(
            "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
            "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],),
        ).fetchall():
            item = dict(row)
            item["has_gap"] = any(token in item["aligned_text"] for token in basis)
            alignments.append(item)
        variants = []
        for row in self.conn.execute(
            "SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)
        ).fetchall():
            variant = dict(row)
            variant["notes"] = [dict(r) for r in self.conn.execute(
                "SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
            variants.append(variant)
        return {"passage": dict(passage), "alignments": alignments, "variants": variants, "gap_basis": basis}

    def seal_passage(self, passage_id: int, user_id: int, reason: str = "") -> dict:
        """封存段落：留存异文、注释和对齐快照。封存后只能补录，不能改动已交付内容。"""
        with self.transaction():
            passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
            if not passage:
                raise DomainError("段落不存在")
            self._require_owner(passage["work_id"], user_id)
            if self._is_sealed(passage):
                raise DomainError("段落已经封存，不能重复封存")
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM variants WHERE passage_id=? AND status!='confirmed'", (passage_id,)
            ).fetchone()[0]
            if pending:
                raise DomainError("仍有未确认修订，确认后才能封存，未确认修订不能混入交付稿")
            work = self.conn.execute("SELECT gap_basis FROM works WHERE id=?", (passage["work_id"],)).fetchone()
            basis = json.loads(work["gap_basis"])
            snapshot = self._collect_passage(passage, basis)
            now = datetime.now().isoformat()
            snap_text = json.dumps(snapshot, ensure_ascii=False)
            basis_text = json.dumps(basis, ensure_ascii=False)
            self.conn.execute(
                "INSERT INTO passage_seals(passage_id,sealed_by,reason,basis_json,snapshot_json,revision_no,sealed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (passage_id, user_id, reason.strip(), basis_text, snap_text, passage["revision"], now),
            )
            self.conn.execute(
                "INSERT INTO seal_events(passage_id,work_id,action,actor_id,reason,basis_json,snapshot_json,created_at) "
                "VALUES(?,?,'sealed',?,?,?,?,?)",
                (passage_id, passage["work_id"], user_id, reason.strip(), basis_text, snap_text, now),
            )
            self.conn.execute(
                "UPDATE passages SET status='sealed',updated_by=?,updated_at=? WHERE id=?",
                (user_id, now, passage_id),
            )
        return {"passage_id": passage_id, "sealed_at": now, "revision_no": passage["revision"], "gap_basis": basis}

    def lock_passage(self, passage_id: int, user_id: int, reason: str = "") -> dict:
        # 旧接口保留：锁定即封存。
        return self.seal_passage(passage_id, user_id, reason)

    def set_gap_basis(self, work_id: int, basis, user_id: int) -> dict:
        """修改封存依据：旧封存立即失效，缺口统计和导出随后按新依据重算。"""
        new_basis = normalize_basis(basis)
        with self.transaction():
            work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
            if not work:
                raise DomainError("作品不存在")
            self._require_owner(work_id, user_id)
            old_basis = json.loads(work["gap_basis"])
            if old_basis == new_basis:
                return {"gap_basis": new_basis, "invalidated_seals": 0}
            now = datetime.now().isoformat()
            reason = f"封存依据变更：{'、'.join(old_basis)} → {'、'.join(new_basis)}"
            sealed_rows = self.conn.execute(
                "SELECT s.*,p.work_id FROM passage_seals s JOIN passages p ON p.id=s.passage_id "
                "WHERE p.work_id=?", (work_id,),
            ).fetchall()
            for row in sealed_rows:
                self.conn.execute(
                    "INSERT INTO seal_events(passage_id,work_id,action,actor_id,reason,basis_json,snapshot_json,created_at) "
                    "VALUES(?,?,'invalidated',?,?,?,?,?)",
                    (row["passage_id"], work_id, user_id, reason,
                     json.dumps(new_basis, ensure_ascii=False), None, now),
                )
                self.conn.execute("DELETE FROM passage_seals WHERE passage_id=?", (row["passage_id"],))
                self.conn.execute(
                    "UPDATE passages SET status='open',updated_by=?,updated_at=? WHERE id=?",
                    (user_id, now, row["passage_id"]),
                )
            self.conn.execute("UPDATE works SET gap_basis=? WHERE id=?",
                              (json.dumps(new_basis, ensure_ascii=False), work_id))
        return {"gap_basis": new_basis, "invalidated_seals": len(sealed_rows)}

    # ---------- 快照 / 历史 / 导出 ----------

    def get_snapshot(self, passage_id: int, revision_no: int, user_id: int) -> dict:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该快照")
        row = self.conn.execute("SELECT * FROM revisions WHERE passage_id=? AND revision_no=?", (passage_id, revision_no)).fetchone()
        if not row:
            raise DomainError("快照不存在")
        return {"revision_no": row["revision_no"], "layer": row["layer"], "created_at": row["created_at"], "snapshot": json.loads(row["snapshot_json"])}

    def _variant_notes(self, variant_id: int, note_ids=None) -> list:
        rows = self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (variant_id,)).fetchall()
        if note_ids is not None:
            rows = [r for r in rows if r["id"] in note_ids]
        return [dict(r) for r in rows]

    def export_collation(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        basis = json.loads(work["gap_basis"])
        witnesses = [dict(r) for r in self.conn.execute("SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (work_id,))]
        passages = []
        gaps = 0
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            alignments = []
            for row in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind,w.missing_sections FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],)
            ).fetchall():
                item = dict(row)
                if any(token in item["aligned_text"] for token in basis):
                    item["has_gap"] = True
                    gaps += 1
                alignments.append(item)
            all_variants = self.conn.execute(
                "SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)
            ).fetchall()
            seal_row = self.conn.execute(
                "SELECT * FROM passage_seals WHERE passage_id=?", (passage["id"],)
            ).fetchone()
            seal_info = None
            delivered, pending, supplements = [], [], []
            if seal_row:
                sealed_snapshot = json.loads(seal_row["snapshot_json"])
                sealed_at = seal_row["sealed_at"]
                delivered_ids = {v["id"] for v in sealed_snapshot["variants"]}
                extra_notes = []
                for row in all_variants:
                    variant = dict(row)
                    variant["notes"] = self._variant_notes(row["id"])
                    if row["id"] in delivered_ids:
                        # 交付稿只保留封存时点（含）之前的注释；封存后补录的注释单独列出，仅供查阅。
                        later = [n for n in variant["notes"] if n["created_at"] > sealed_at]
                        variant["notes"] = [n for n in variant["notes"] if n["created_at"] <= sealed_at]
                        delivered.append(variant)
                        for note in later:
                            note["variant_id"] = row["id"]
                            extra_notes.append(note)
                    else:
                        supplements.append(variant)
                seal_info = {
                    "sealed_by": seal_row["sealed_by"],
                    "reason": seal_row["reason"],
                    "sealed_at": seal_row["sealed_at"],
                    "revision_no": seal_row["revision_no"],
                    "gap_basis": json.loads(seal_row["basis_json"]),
                }
                pending_block = {"variants": supplements, "notes": extra_notes}
            else:
                for row in all_variants:
                    variant = dict(row)
                    variant["notes"] = self._variant_notes(row["id"])
                    (delivered if row["status"] == "confirmed" else pending).append(variant)
                pending_block = {"variants": pending, "notes": []}
            passages.append({
                **dict(passage),
                "alignments": alignments,
                "variants": delivered,
                "pending_variants": [] if seal_row else pending,
                "supplements": pending_block,
                "seal": seal_info,
            })
        return {
            "work": dict(work),
            "gap_basis": basis,
            "witnesses": witnesses,
            "passages": passages,
            "gap_count": gaps,
        }

    def work_history(self, work_id: int, user_id: int) -> dict:
        if not self.conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
            raise DomainError("作品不存在")
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该项目历史")
        handovers = [dict(r) for r in self.conn.execute(
            "SELECT h.*,f.name AS from_name,t.name AS to_name FROM handover_log h "
            "JOIN users f ON f.id=h.from_owner JOIN users t ON t.id=h.to_owner "
            "WHERE h.work_id=? ORDER BY h.id", (work_id,),
        ).fetchall()]
        seals = []
        for row in self.conn.execute(
            "SELECT e.id,e.passage_id,e.action,e.actor_id,e.reason,e.basis_json,e.snapshot_json,e.created_at,"
            "p.label,u.name AS actor_name FROM seal_events e "
            "JOIN passages p ON p.id=e.passage_id JOIN users u ON u.id=e.actor_id "
            "WHERE e.work_id=? ORDER BY e.id", (work_id,),
        ).fetchall():
            item = {k: row[k] for k in row.keys() if k != "snapshot_json"}
            item["basis"] = json.loads(row["basis_json"])
            item["snapshot"] = json.loads(row["snapshot_json"]) if row["snapshot_json"] else None
            seals.append(item)
        revisions = [dict(r) for r in self.conn.execute(
            "SELECT r.revision_no,r.passage_id,r.variant_id,r.layer,r.author_id,r.created_at,u.name AS author_name,p.label "
            "FROM revisions r JOIN users u ON u.id=r.author_id JOIN passages p ON p.id=r.passage_id "
            "WHERE p.work_id=? ORDER BY r.id", (work_id,),
        ).fetchall()]
        confirmations = [dict(r) for r in self.conn.execute(
            "SELECT v.id,v.passage_id,p.label,v.status,v.confirmed_by,v.confirmed_at,u.name AS confirmer_name "
            "FROM variants v JOIN passages p ON p.id=v.passage_id "
            "LEFT JOIN users u ON u.id=v.confirmed_by WHERE p.work_id=? ORDER BY v.id", (work_id,),
        ).fetchall()]
        return {"handovers": handovers, "seals": seals, "revisions": revisions, "confirmations": confirmations}

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
            "variants": [dict(r) for r in self.conn.execute("SELECT * FROM variants ORDER BY id")],
            "seals": [dict(r) for r in self.conn.execute("SELECT * FROM passage_seals ORDER BY passage_id")],
            "handovers": [dict(r) for r in self.conn.execute("SELECT * FROM handover_log ORDER BY id")],
        }
