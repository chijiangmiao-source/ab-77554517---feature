"""复核记录与证据封存账本的 SQLite 持久化。

提交被接受（输入合法）后，无论结论可行还是不可行，都在**同一事务**中：
  1. 写入复核记录（payload + conclusion）；
  2. 把“排序后的输入、观测与结论”的规范字节摘要作为新叶追加到账本，
     叶序号 = 服务端递增序号（连续、不重号）。
输入非法或求解失败的请求不产生记录，也不占用账本序号。

封存（seal）：以稳定封存标识锚定“目标复核及其此前全部复核”的连续
前缀批次，记录批次大小与根摘要。同一标识同一载荷重传返回原封存；
复用标识却改变目标或摘要则拒绝，且不改变已封存根。

重启时 init_db 从持久叶记录重建前沿，并逐条核对叶摘要与全部封存根；
发现断号、叶摘要或根不一致时进入停摆状态（定位最早损坏序号），
此后拒绝新的复核与封存，既有复核读取仍然可用。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid

import ledger

DB_PATH = os.environ.get("APP_DB", "/data/locator.db")

_lock = threading.Lock()
_frontier = ledger.Frontier()
_halted: dict | None = None  # {"corrupt_seq": int, "reason": str}


class LedgerHaltedError(RuntimeError):
    """账本已损坏（断号 / 叶摘要 / 封存根不一致），停止接受新复核。"""

    def __init__(self, corrupt_seq: int, reason: str):
        self.corrupt_seq = corrupt_seq
        self.reason = reason
        super().__init__(f"账本已损坏（序号 {corrupt_seq}）：{reason}")


class SealConflictError(RuntimeError):
    """封存标识已被不同载荷使用。"""

    def __init__(self, existing: dict):
        self.existing = existing
        super().__init__("封存标识已被不同载荷使用")


class RootMismatchError(RuntimeError):
    """请求携带的摘要与账本计算根不一致。"""

    def __init__(self, computed: str):
        self.computed = computed
        super().__init__("请求摘要与账本计算根不一致")


class UnknownReviewError(LookupError):
    """目标复核编号不存在。"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_inclusion_path(index: int, digests: list[str]) -> list[dict] | None:
    """计算包含路径；账本内容损坏时返回 None（读取不得因此失败）。"""
    try:
        return ledger.inclusion_path(index, digests)
    except (ValueError, TypeError):
        return None


def init_db() -> None:
    """建表、兼容补叶、重建前沿并核对全部封存根。"""
    global _halted, _frontier
    with _lock:
        _halted = None
        _frontier = ledger.Frontier()
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS submissions (
                    review_id   TEXT PRIMARY KEY,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    payload     TEXT NOT NULL,
                    conclusion  TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leaves (
                    seq         INTEGER PRIMARY KEY,
                    review_id   TEXT NOT NULL UNIQUE
                                REFERENCES submissions(review_id),
                    digest      TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seals (
                    seal_id         TEXT PRIMARY KEY,
                    review_id       TEXT NOT NULL,
                    size            INTEGER NOT NULL,
                    root            TEXT NOT NULL,
                    request_digest  TEXT NOT NULL,
                    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
        _backfill_leaves()
        _rebuild_and_verify()


def _backfill_leaves() -> None:
    """兼容旧库：为尚无叶子的复核记录按入库顺序补叶（单事务）。

    摘要由持久化的 payload + conclusion 重算，与提交时计算完全一致。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT s.review_id, s.payload, s.conclusion FROM submissions s "
            "LEFT JOIN leaves l ON l.review_id = s.review_id "
            "WHERE l.review_id IS NULL ORDER BY s.rowid"
        ).fetchall()
        if not rows:
            return
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM leaves"
        ).fetchone()[0]
        for row in rows:
            digest = ledger.record_digest(
                json.loads(row["payload"]), json.loads(row["conclusion"])
            )
            conn.execute(
                "INSERT INTO leaves (seq, review_id, digest) VALUES (?, ?, ?)",
                (seq, row["review_id"], digest),
            )
            seq += 1


def _halt(corrupt_seq: int, reason: str) -> None:
    global _halted
    _halted = {"corrupt_seq": corrupt_seq, "reason": reason}


def _rebuild_and_verify() -> None:
    """从持久叶记录重建前沿；核对序号连续性、叶摘要与全部封存根。

    发现不一致时置停摆状态并定位最早损坏序号；读取不受影响。
    """
    global _frontier
    with _connect() as conn:
        leaves = conn.execute(
            "SELECT seq, review_id, digest FROM leaves ORDER BY seq"
        ).fetchall()
        subs = {
            row["review_id"]: row
            for row in conn.execute(
                "SELECT review_id, payload, conclusion FROM submissions"
            )
        }
        seals = conn.execute(
            "SELECT seal_id, size, root FROM seals ORDER BY size, rowid"
        ).fetchall()

    digests: list[str] = []
    frontier = ledger.Frontier()
    corrupt: tuple[int, str] | None = None
    for expected, leaf in enumerate(leaves):
        if leaf["seq"] != expected:
            corrupt = (expected, f"账本断号：期望序号 {expected}，实际 {leaf['seq']}")
            break
        sub = subs.get(leaf["review_id"])
        if sub is None:
            corrupt = (leaf["seq"], "叶记录缺少对应复核")
            break
        recomputed = ledger.record_digest(
            json.loads(sub["payload"]), json.loads(sub["conclusion"])
        )
        if recomputed != leaf["digest"]:
            corrupt = (leaf["seq"], "叶摘要与复核记录不一致")
            break
        digests.append(leaf["digest"])
        frontier.append(leaf["digest"])

    if corrupt is None:
        for seal in seals:
            if seal["size"] < 1 or seal["size"] > len(digests):
                corrupt = (max(seal["size"] - 1, 0),
                           f"封存 {seal['seal_id']} 的批次超出账本")
                break
            if ledger.root_hex(digests[: seal["size"]]) != seal["root"]:
                corrupt = (seal["size"] - 1, f"封存 {seal['seal_id']} 的根摘要不一致")
                break

    # 停摆时前沿停留在最早损坏序号之前的连续有效前缀上。
    _frontier = frontier
    if corrupt is not None:
        _halt(*corrupt)


def halted_info() -> dict | None:
    """停摆信息（{"corrupt_seq", "reason"}）；未停摆为 None。"""
    return _halted


def ledger_status() -> dict:
    """账本前沿状态：大小、当前根、是否停摆及最早损坏序号。"""
    with _lock:
        return {
            "size": _frontier.size,
            "root": _frontier.root_hex(),
            "halted": _halted is not None,
            "corrupt_seq": None if _halted is None else _halted["corrupt_seq"],
            "reason": None if _halted is None else _halted["reason"],
        }


def save_submission(payload: dict, conclusion: dict) -> tuple[str, int, str]:
    """保存复核记录并在同一事务追加账本叶。

    返回 (复核编号, 叶序号, 叶摘要)。非法输入在接口层已被拒绝，
    不会进入本函数，因此不占用账本序号。
    """
    digest = ledger.record_digest(payload, conclusion)
    with _lock:
        if _halted is not None:
            raise LedgerHaltedError(_halted["corrupt_seq"], _halted["reason"])
        review_id = uuid.uuid4().hex[:12]
        with _connect() as conn:
            # 极小概率撞号时重试一次。
            while True:
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
                "SELECT COALESCE(MAX(seq) + 1, 0) FROM leaves"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO leaves (seq, review_id, digest) VALUES (?, ?, ?)",
                (seq, review_id, digest),
            )
        _frontier.append(digest)
    return review_id, seq, digest


def load_submission(review_id: str) -> dict | None:
    """取回复核记录，并附账本位置与封存信息（含包含路径）。

    封存选取：优先锚定于本复核的封存，否则取覆盖该叶的最新封存。
    """
    if not review_id or not all(c in "0123456789abcdef" for c in review_id):
        return None
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT review_id, created_at, payload, conclusion "
            "FROM submissions WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if row is None:
            return None
        record = {
            "review_id": row["review_id"],
            "created_at": row["created_at"],
            "input": json.loads(row["payload"]),
            "conclusion": json.loads(row["conclusion"]),
            "ledger": None,
            "seal": None,
        }
        leaf = conn.execute(
            "SELECT seq, digest FROM leaves WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if leaf is None:
            return record
        record["ledger"] = {"seq": leaf["seq"], "leaf_digest": leaf["digest"]}
        # 优先展示锚定于本复核的封存（工程师从本详情发起的那次）；
        # 否则展示覆盖该叶的最新封存。
        seal = conn.execute(
            "SELECT seal_id, size, root, created_at FROM seals "
            "WHERE size > ? ORDER BY (review_id = ?) DESC, rowid DESC LIMIT 1",
            (leaf["seq"], review_id),
        ).fetchone()
        if seal is not None:
            digests = [
                r["digest"]
                for r in conn.execute(
                    "SELECT digest FROM leaves WHERE seq < ? ORDER BY seq",
                    (seal["size"],),
                )
            ]
            path = None
            if len(digests) == seal["size"]:
                path = _safe_inclusion_path(leaf["seq"], digests)
            record["seal"] = {
                "seal_id": seal["seal_id"],
                "size": seal["size"],
                "last_seq": seal["size"] - 1,
                "root": seal["root"],
                "created_at": seal["created_at"],
                "leaf_index": leaf["seq"],
                "path": path,
            }
        return record


def _seal_response(conn, seal_row, replayed: bool) -> dict:
    """由封存行构造响应（含目标叶的包含路径）。"""
    leaf = conn.execute(
        "SELECT seq, digest FROM leaves WHERE review_id = ?",
        (seal_row["review_id"],),
    ).fetchone()
    digests = [
        r["digest"]
        for r in conn.execute(
            "SELECT digest FROM leaves WHERE seq < ? ORDER BY seq",
            (seal_row["size"],),
        )
    ]
    path = None
    leaf_index = None
    leaf_digest = None
    if leaf is not None:
        leaf_index = leaf["seq"]
        leaf_digest = leaf["digest"]
        if len(digests) == seal_row["size"]:
            path = _safe_inclusion_path(leaf["seq"], digests)
    return {
        "seal_id": seal_row["seal_id"],
        "review_id": seal_row["review_id"],
        "size": seal_row["size"],
        "last_seq": seal_row["size"] - 1,
        "root": seal_row["root"],
        "created_at": seal_row["created_at"],
        "replayed": replayed,
        "leaf_index": leaf_index,
        "leaf_digest": leaf_digest,
        "path": path,
    }


def create_seal(
    seal_id: str, review_id: str, expected_root: str | None = None
) -> dict:
    """以稳定封存标识锚定目标复核及其此前全部复核的连续前缀批次。

    - 同一标识同一载荷重传：返回原封存（replayed=True）；
    - 复用标识却改变目标或摘要：抛 SealConflictError，已封存根不变；
    - 请求自带摘要与计算根不一致：抛 RootMismatchError，不落库。
    """
    req_digest = ledger.seal_request_digest(seal_id, review_id, expected_root)
    with _lock:
        if _halted is not None:
            raise LedgerHaltedError(_halted["corrupt_seq"], _halted["reason"])
        with _connect() as conn:
            existing = conn.execute(
                "SELECT seal_id, review_id, size, root, request_digest, "
                "created_at FROM seals WHERE seal_id = ?",
                (seal_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] == req_digest:
                    return _seal_response(conn, existing, replayed=True)
                raise SealConflictError({
                    "seal_id": existing["seal_id"],
                    "review_id": existing["review_id"],
                    "size": existing["size"],
                    "root": existing["root"],
                    "created_at": existing["created_at"],
                })

            leaf = conn.execute(
                "SELECT seq, digest FROM leaves WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            if leaf is None:
                raise UnknownReviewError(review_id)

            # 封存只能锚定提交瞬间连续的账本前缀 [0, seq]。
            size = leaf["seq"] + 1
            digests = [
                r["digest"]
                for r in conn.execute(
                    "SELECT digest FROM leaves WHERE seq < ? ORDER BY seq",
                    (size,),
                )
            ]
            if len(digests) != size:
                raise LedgerHaltedError(size - 1, "账本前缀不连续")
            root = ledger.root_hex(digests)
            if expected_root is not None and expected_root != root:
                raise RootMismatchError(root)
            conn.execute(
                "INSERT INTO seals (seal_id, review_id, size, root, "
                "request_digest) VALUES (?, ?, ?, ?, ?)",
                (seal_id, review_id, size, root, req_digest),
            )
            created_at = conn.execute(
                "SELECT created_at FROM seals WHERE seal_id = ?", (seal_id,)
            ).fetchone()["created_at"]
        return {
            "seal_id": seal_id,
            "review_id": review_id,
            "size": size,
            "last_seq": size - 1,
            "root": root,
            "created_at": created_at,
            "replayed": False,
            "leaf_index": leaf["seq"],
            "leaf_digest": leaf["digest"],
            "path": ledger.inclusion_path(leaf["seq"], digests),
        }
