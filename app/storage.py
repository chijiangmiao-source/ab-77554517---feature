"""复核记录与证据封存账本的 SQLite 持久化。

表结构
------
- submissions：复核记录。合法提交（含不可行结论）即保存；非法输入
  在接口层直接拒绝，不产生记录。
- ledger：只追加账本。每条合法提交在**同一 SQLite 事务**内追加一叶
  (seq, review_id, leaf_hash)：seq 为服务端递增序号（从 1 开始、
  连续无断号），leaf_hash 为“排序后的输入、观测与结论”的规范字节
  摘要。任一插入失败则整体回滚，失败提交不占用账本序号。
- seals：证据封存。seal_id 为测试工程师给出的稳定封存标识，锚定
  目标复核提交瞬间的连续账本前缀 1..seq 的 Merkle 根。同一标识
  同一载荷重传返回原封存；复用标识却改变目标或摘要则拒绝，且
  不改变已封存根。

重启完整性
----------
init_db() 建表后为旧版记录补登账本，随后从持久叶重建前沿、逐叶
重算摘要并核对全部封存根；发现断号、叶摘要或根不一致时置不
健康标记（停止接受新复核与封存，既有读取仍可用），并记录最早
损坏序号。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid

try:
    from ledger import Frontier, leaf_digest
except ImportError:  # 作为 app 包导入时
    from .ledger import Frontier, leaf_digest

DB_PATH = os.environ.get("APP_DB", "/data/locator.db")

_lock = threading.Lock()
_frontier = Frontier()
# 完整性状态：init_db 重建核对时刷新；不健康则停止接受新复核/封存。
_integrity = {"healthy": True, "corrupt_seq": None, "detail": ""}

SEAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ROOT_RE = re.compile(r"^[0-9a-f]{64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    review_id   TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    payload     TEXT NOT NULL,
    conclusion  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    seq        INTEGER PRIMARY KEY,
    review_id  TEXT NOT NULL UNIQUE REFERENCES submissions(review_id),
    leaf_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS seals (
    seal_id    TEXT PRIMARY KEY,
    review_id  TEXT NOT NULL REFERENCES submissions(review_id),
    seq        INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    leaf_hash  TEXT NOT NULL,
    root       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class SealConflict(Exception):
    """封存标识冲突：已封存记录保持不变。

    existing 为已存在的封存（可能为 None，如新建时声明的摘要与
    账本复算根不一致）；computed 为账本侧复算根（可能为 None）。
    """

    def __init__(self, message, *, existing=None, computed=None):
        self.existing = existing
        self.computed = computed
        super().__init__(message)


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _mark_corrupt(seq, detail: str) -> None:
    """记录损坏位置，保留最早损坏序号。"""
    if _integrity["corrupt_seq"] is None or seq < _integrity["corrupt_seq"]:
        _integrity.update(healthy=False, corrupt_seq=seq, detail=detail)


def init_db() -> None:
    """建表、补登旧记录、从持久叶重建前沿并核对全部封存根。"""
    global _frontier
    with _lock:
        _integrity.update(healthy=True, corrupt_seq=None, detail="")
        with _connect() as conn:
            conn.executescript(_SCHEMA)
            _backfill_ledger(conn)
        _rebuild_and_verify()


def _backfill_ledger(conn) -> None:
    """为旧版（无账本行）的复核记录按提交先后补登账本叶。"""
    rows = conn.execute(
        """
        SELECT s.review_id, s.payload, s.conclusion
        FROM submissions s
        WHERE NOT EXISTS (SELECT 1 FROM ledger l WHERE l.review_id = s.review_id)
        ORDER BY s.rowid
        """
    ).fetchall()
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger").fetchone()[0]
    for row in rows:
        seq += 1
        try:
            digest = leaf_digest(
                json.loads(row["payload"]), json.loads(row["conclusion"])
            )
        except (ValueError, KeyError, TypeError) as exc:
            _mark_corrupt(seq, f"旧记录 {row['review_id']} 无法重算摘要: {exc}")
            continue  # 无法核对的叶不入账；留下的断号会被连续性核对定位
        conn.execute(
            "INSERT INTO ledger (seq, review_id, leaf_hash) VALUES (?, ?, ?)",
            (seq, row["review_id"], digest),
        )


def _rebuild_and_verify() -> None:
    """从持久叶重建前沿；逐叶重算摘要、逐封存重算根（须在锁内调用）。"""
    global _frontier
    leaves: list[bytes] = []
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT l.seq, l.review_id, l.leaf_hash, s.payload, s.conclusion
            FROM ledger l
            LEFT JOIN submissions s ON s.review_id = l.review_id
            ORDER BY l.seq
            """
        ).fetchall()
        for expect, row in enumerate(rows, start=1):
            if row["seq"] != expect:
                _mark_corrupt(
                    expect, f"账本断号：期望序号 {expect}，实际 {row['seq']}"
                )
            # 叶按持久内容入列，保持“序号 ↔ 位置”对齐；核对另行进行。
            try:
                leaves.append(bytes.fromhex(row["leaf_hash"]))
            except ValueError:
                leaves.append(b"\x00" * 32)
                _mark_corrupt(row["seq"], f"账本叶 {row['seq']} 摘要不是合法哈希")
            if row["payload"] is None:
                _mark_corrupt(row["seq"], f"账本叶 {row['seq']} 缺少对应复核记录")
                continue
            try:
                digest = leaf_digest(
                    json.loads(row["payload"]), json.loads(row["conclusion"])
                )
            except (ValueError, KeyError, TypeError) as exc:
                _mark_corrupt(row["seq"], f"复核记录无法解析: {exc}")
                continue
            if digest != row["leaf_hash"]:
                _mark_corrupt(
                    row["seq"], f"账本叶 {row['seq']} 摘要与复核记录不符"
                )
        # 即使个别叶损坏也用持久叶重建前沿（不健康状态会阻止新提交，
        # 既有读取与核对仍可进行）。
        _frontier = Frontier(leaves)
        seals = conn.execute(
            "SELECT seal_id, size, root FROM seals ORDER BY size"
        ).fetchall()
    for seal in seals:
        size, root = seal["size"], seal["root"]
        if size < 1 or size > len(leaves):
            _mark_corrupt(
                size, f"封存 {seal['seal_id']} 锚定前缀 {size} 超出账本范围"
            )
            continue
        actual = _frontier.root(size).hex()
        if actual != root:
            _mark_corrupt(
                size,
                f"封存 {seal['seal_id']} 根不一致：存 {root}，算 {actual}",
            )


def ledger_status() -> dict:
    """账本完整性状态与当前规模。"""
    with _lock:
        return {
            "healthy": _integrity["healthy"],
            "corrupt_seq": _integrity["corrupt_seq"],
            "detail": _integrity["detail"],
            "size": len(_frontier),
            "root": _frontier.root().hex() if len(_frontier) else None,
        }


def save_submission(payload: dict, conclusion: dict) -> tuple[str, int, str]:
    """保存复核记录并在**同一事务**追加账本叶。

    返回 (复核编号, 账本序号, 叶摘要)。调用前输入必须已通过校验；
    本函数抛出的任何异常都会使事务整体回滚，失败不占用账本序号。
    """
    global _frontier
    digest = leaf_digest(payload, conclusion)
    with _lock:
        with _connect() as conn:
            review_id = uuid.uuid4().hex[:12]
            while True:  # 极小概率撞号时换号重试
                try:
                    conn.execute(
                        "INSERT INTO submissions (review_id, payload, conclusion) "
                        "VALUES (?, ?, ?)",
                        (
                            review_id,
                            json.dumps(payload, ensure_ascii=False),
                            json.dumps(conclusion, ensure_ascii=False),
                        ),
                    )
                    break
                except sqlite3.IntegrityError:
                    review_id = uuid.uuid4().hex[:12]
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM ledger"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO ledger (seq, review_id, leaf_hash) VALUES (?, ?, ?)",
                (seq, review_id, digest),
            )
        # 事务提交成功后再推进内存前沿，保持与持久叶一致。
        _frontier.append(bytes.fromhex(digest))
    return review_id, seq, digest


def load_submission(review_id: str) -> dict | None:
    """按复核编号取回提交内容、结论与账本/封存信息。"""
    if not review_id or not all(c in "0123456789abcdef" for c in review_id):
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT review_id, created_at, payload, conclusion "
            "FROM submissions WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if row is None:
            return None
        lrow = conn.execute(
            "SELECT seq, leaf_hash FROM ledger WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        seals = conn.execute(
            "SELECT seal_id, size, root, created_at FROM seals "
            "WHERE review_id = ? ORDER BY created_at, seal_id",
            (review_id,),
        ).fetchall()
    record = {
        "review_id": row["review_id"],
        "created_at": row["created_at"],
        "input": json.loads(row["payload"]),
        "conclusion": json.loads(row["conclusion"]),
        "seals": [dict(s) for s in seals],
    }
    if lrow is not None:
        record["ledger"] = {"seq": lrow["seq"], "leaf": lrow["leaf_hash"]}
    return record


def _seal_dict(row, *, created: bool | None) -> dict:
    """封存记录 + 目标复核在锚定批次中的包含路径。

    created 为 None 时（读取场景）不在响应中携带该字段。
    """
    try:
        steps = _frontier.path(row["seq"] - 1, row["size"])
        path = {
            "index": row["seq"] - 1,
            "size": row["size"],
            "steps": [{"side": side, "hash": h.hex()} for side, h in steps],
        }
    except ValueError:
        path = None  # 账本损坏时路径不可信（此时已停止接受新复核）
    seal = {
        "seal_id": row["seal_id"],
        "review_id": row["review_id"],
        "seq": row["seq"],
        "size": row["size"],
        "leaf": row["leaf_hash"],
        "root": row["root"],
        "created_at": row["created_at"],
        "path": path,
    }
    if created is not None:
        seal["created"] = created
    return seal


def create_seal(
    seal_id: str, review_id: str, expected_root: str | None = None
) -> dict | None:
    """锚定目标复核提交瞬间的连续账本前缀，返回封存（幂等）。

    - 目标复核不存在：返回 None；
    - 同标识同载荷（目标一致且声明摘要一致或未声明）：返回原封存，
      created=False；
    - 复用标识却改变目标或摘要：抛 SealConflict，已封存根不变。
    """
    with _lock:
        with _connect() as conn:
            lrow = conn.execute(
                "SELECT seq, leaf_hash FROM ledger WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            if lrow is None:
                return None
            seq, leaf = lrow["seq"], lrow["leaf_hash"]
            root = _frontier.root(seq).hex()
            existing = conn.execute(
                "SELECT seal_id, review_id, seq, size, leaf_hash, root, "
                "created_at FROM seals WHERE seal_id = ?",
                (seal_id,),
            ).fetchone()
            if existing is not None:
                if existing["review_id"] == review_id and (
                    expected_root is None or expected_root == existing["root"]
                ):
                    return _seal_dict(existing, created=False)
                raise SealConflict(
                    "封存标识已被占用，且目标或摘要与已封存记录不一致",
                    existing=_seal_dict(existing, created=None),
                )
            if expected_root is not None and expected_root != root:
                raise SealConflict(
                    "声明的摘要与账本复算根不一致", computed=root
                )
            conn.execute(
                "INSERT INTO seals (seal_id, review_id, seq, size, leaf_hash, "
                "root) VALUES (?, ?, ?, ?, ?, ?)",
                (seal_id, review_id, seq, seq, leaf, root),
            )
            row = conn.execute(
                "SELECT seal_id, review_id, seq, size, leaf_hash, root, "
                "created_at FROM seals WHERE seal_id = ?",
                (seal_id,),
            ).fetchone()
            return _seal_dict(row, created=True)


def load_seal(seal_id: str) -> dict | None:
    """按封存标识取回封存与包含路径。"""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT seal_id, review_id, seq, size, leaf_hash, root, created_at "
            "FROM seals WHERE seal_id = ?",
            (seal_id,),
        ).fetchone()
        if row is None:
            return None
        return _seal_dict(row, created=None)
