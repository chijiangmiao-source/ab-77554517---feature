"""账本与封存测试：规范摘要、Merkle 前沿、同事务追加、幂等封存、
重启重建与损坏定位（断号 / 叶摘要 / 封存根）、损坏时接口停收。

运行：python tests/test_ledger.py
"""

import hashlib
import json
import os
import random
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 必须在导入应用模块前设置 DB 路径（storage 在导入时读取 APP_DB）。
_TMP = tempfile.TemporaryDirectory()
os.environ["APP_DB"] = os.path.join(_TMP.name, "ledger-test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import ledger  # noqa: E402
import server  # noqa: E402
import storage  # noqa: E402


def fresh_db():
    """指向一个全新的空库并初始化，返回库文件路径。"""
    fd, path = tempfile.mkstemp(suffix=".db", dir=_TMP.name)
    os.close(fd)
    os.remove(path)
    storage.DB_PATH = path
    storage.init_db()
    return path


def sample_payload(tag="x"):
    return {
        "channels": ["a", "b", "c"],
        "checks": [
            {"channels": ["a", "b"], "parity": 1},
            {"channels": ["b", "c"], "parity": 0},
        ],
        "tag": tag,  # 额外字段不进规范摘要，仅用于区分载荷
    }


def sample_conclusion(weight=1):
    return {
        "feasible": True,
        "weight": weight,
        "faulty": ["a"],
        "vector": {"a": 1, "b": 0, "c": 0},
        "recompute": [
            {"members": ["a", "b"], "observed": 1, "recomputed": 1, "pass": True},
            {"members": ["b", "c"], "observed": 0, "recomputed": 0, "pass": True},
        ],
    }


def save_n(n):
    """连续保存 n 条复核，返回 [(review_id, seq, leaf)]。"""
    out = []
    for i in range(n):
        payload = sample_payload(tag=f"r{i}")
        out.append(storage.save_submission(payload, sample_conclusion()))
    return out


class CanonicalDigestTests(unittest.TestCase):
    def test_digest_deterministic_under_reordering(self):
        # 校验书写顺序、成员顺序、键序不同，规范摘要必须一致。
        c1 = sample_conclusion()
        p1 = sample_payload()
        p2 = {
            "checks": [
                {"parity": 0, "channels": ["c", "b"]},
                {"channels": ["b", "a"], "parity": 1},
            ],
            "channels": ["c", "b", "a"],
        }
        c2 = dict(reversed(list(c1.items())))
        self.assertEqual(ledger.leaf_digest(p1, c1), ledger.leaf_digest(p2, c2))

    def test_digest_changes_with_conclusion(self):
        p = sample_payload()
        d1 = ledger.leaf_digest(p, sample_conclusion(weight=1))
        d2 = ledger.leaf_digest(p, sample_conclusion(weight=2))
        self.assertNotEqual(d1, d2)
        self.assertEqual(len(d1), 64)


class MerkleTests(unittest.TestCase):
    def test_frontier_root_path_and_prefix(self):
        rng = random.Random(20260926)
        for n in range(1, 13):
            leaves = [rng.randbytes(32) for _ in range(n)]
            frontier = ledger.Frontier(leaves)
            # 前沿根与直接求根一致
            self.assertEqual(frontier.root(), ledger.root_of(leaves))
            # 每个前缀根一致，且前缀内每片叶的路径可折叠到该前缀根
            for size in range(1, n + 1):
                prefix = leaves[:size]
                want = ledger.root_of(prefix)
                self.assertEqual(frontier.root(size), want)
                for idx in range(size):
                    steps = ledger.path_of(prefix, idx)
                    self.assertEqual(ledger.fold_path(prefix[idx], steps), want)
                    self.assertEqual(frontier.path(idx, size), steps)

    def test_rebuild_from_persistent_leaves_matches(self):
        rng = random.Random(7)
        leaves = [rng.randbytes(32) for _ in range(20)]
        live = ledger.Frontier()
        for leaf in leaves:
            live.append(leaf)
        rebuilt = ledger.Frontier(leaves)  # 模拟重启后从持久叶重放
        self.assertEqual(live.root(), rebuilt.root())
        self.assertEqual(live._trees, rebuilt._trees)

    def test_tampered_leaf_changes_root(self):
        rng = random.Random(3)
        leaves = [rng.randbytes(32) for _ in range(5)]
        root = ledger.root_of(leaves)
        bad = list(leaves)
        bad[2] = bytes(32)
        self.assertNotEqual(ledger.root_of(bad), root)


class StorageLedgerTests(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_save_appends_leaf_same_transaction(self):
        (rid, seq, leaf) = storage.save_submission(sample_payload(), sample_conclusion())
        self.assertEqual(seq, 1)
        self.assertEqual(leaf, ledger.leaf_digest(sample_payload(), sample_conclusion()))
        # 账本行与复核记录同事务落库
        with sqlite3.connect(storage.DB_PATH) as conn:
            lrow = conn.execute(
                "SELECT seq, review_id, leaf_hash FROM ledger"
            ).fetchall()
            srow = conn.execute(
                "SELECT review_id FROM submissions"
            ).fetchall()
        self.assertEqual(lrow, [(1, rid, leaf)])
        self.assertEqual(srow, [(rid,)])
        # 读取响应携带账本信息
        rec = storage.load_submission(rid)
        self.assertEqual(rec["ledger"], {"seq": 1, "leaf": leaf})
        self.assertEqual(rec["seals"], [])

    def test_seq_contiguous_across_saves(self):
        saved = save_n(4)
        self.assertEqual([s[1] for s in saved], [1, 2, 3, 4])
        self.assertEqual(storage.ledger_status()["size"], 4)
        self.assertTrue(storage.ledger_status()["healthy"])

    def test_failed_save_rolls_back_and_keeps_seq(self):
        save_n(1)
        # 模拟提交中途失败：删掉账本表使同事务的账本插入失败，
        # 复核记录必须随同事务回滚，且失败不占用账本序号。
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("DROP TABLE ledger")
        with self.assertRaises(sqlite3.OperationalError):
            storage.save_submission(sample_payload("bad"), sample_conclusion())
        storage.init_db()  # 重建表并补登核对
        with sqlite3.connect(storage.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 1)
        _, seq, _ = storage.save_submission(sample_payload("ok"), sample_conclusion())
        self.assertEqual(seq, 2)  # 失败提交未占用序号

    def test_seal_create_idempotent_conflict(self):
        saved = save_n(3)
        r1, r2, r3 = (s[0] for s in saved)
        # 创建：锚定目标复核提交瞬间的连续前缀
        seal = storage.create_seal("batch-1", r2)
        self.assertTrue(seal["created"])
        self.assertEqual(seal["size"], 2)
        self.assertEqual(seal["seq"], 2)
        self.assertEqual(len(seal["root"]), 64)
        # 幂等重传：同标识同载荷返回原封存
        again = storage.create_seal("batch-1", r2)
        self.assertFalse(again["created"])
        self.assertEqual(again["root"], seal["root"])
        self.assertEqual(again["size"], seal["size"])
        # 复用标识改目标：拒绝且已封存根不变
        with self.assertRaises(storage.SealConflict):
            storage.create_seal("batch-1", r3)
        self.assertEqual(storage.load_seal("batch-1")["root"], seal["root"])
        # 复用标识改摘要：拒绝
        with self.assertRaises(storage.SealConflict):
            storage.create_seal("batch-1", r2, expected_root="0" * 64)
        # 声明摘要一致：幂等返回
        ok = storage.create_seal("batch-1", r2, expected_root=seal["root"])
        self.assertFalse(ok["created"])
        # 新标识声明错误摘要：拒绝且不产生封存
        with self.assertRaises(storage.SealConflict):
            storage.create_seal("batch-2", r3, expected_root="0" * 64)
        self.assertIsNone(storage.load_seal("batch-2"))
        # 未知复核返回 None
        self.assertIsNone(storage.create_seal("batch-3", "deadbeefdead"))

    def test_seal_path_folds_to_root(self):
        saved = save_n(5)
        for rid, seq, leaf in saved:
            seal = storage.create_seal(f"s-{seq}", rid)
            steps = [(st["side"], bytes.fromhex(st["hash"])) for st in seal["path"]["steps"]]
            self.assertEqual(
                ledger.fold_path(bytes.fromhex(leaf), steps).hex(), seal["root"]
            )

    def test_restart_rebuilds_frontier_and_verifies_seals(self):
        saved = save_n(4)
        seal = storage.create_seal("keep", saved[1][0])
        root_before = storage.ledger_status()["root"]
        storage.init_db()  # 模拟应用重启：从持久叶重建前沿并核对封存根
        status = storage.ledger_status()
        self.assertTrue(status["healthy"], status)
        self.assertEqual(status["root"], root_before)
        # 重启后对封存前记录取得的路径仍复算到相同根
        reloaded = storage.load_seal("keep")
        self.assertEqual(reloaded["root"], seal["root"])
        steps = [(st["side"], bytes.fromhex(st["hash"])) for st in reloaded["path"]["steps"]]
        self.assertEqual(
            ledger.fold_path(bytes.fromhex(reloaded["leaf"]), steps).hex(),
            seal["root"],
        )

    def test_corrupt_leaf_located(self):
        save_n(4)
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("UPDATE ledger SET leaf_hash = ? WHERE seq = 2", ("0" * 64,))
        storage.init_db()
        status = storage.ledger_status()
        self.assertFalse(status["healthy"])
        self.assertEqual(status["corrupt_seq"], 2)

    def test_gap_located(self):
        save_n(4)
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("DELETE FROM ledger WHERE seq = 3")
        storage.init_db()
        status = storage.ledger_status()
        self.assertFalse(status["healthy"])
        self.assertEqual(status["corrupt_seq"], 3)

    def test_seal_root_mismatch_located(self):
        saved = save_n(3)
        storage.create_seal("sealed", saved[2][0])  # 锚定前缀 1..3
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("UPDATE seals SET root = ? WHERE seal_id = 'sealed'", ("0" * 64,))
        storage.init_db()
        status = storage.ledger_status()
        self.assertFalse(status["healthy"])
        self.assertEqual(status["corrupt_seq"], 3)

    def test_earliest_corrupt_seq_wins(self):
        saved = save_n(4)
        storage.create_seal("s2", saved[1][0])
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("UPDATE ledger SET leaf_hash = ? WHERE seq = 4", ("0" * 64,))
            conn.execute("UPDATE seals SET root = ? WHERE seal_id = 's2'", ("1" * 64,))
        storage.init_db()
        status = storage.ledger_status()
        self.assertFalse(status["healthy"])
        self.assertEqual(status["corrupt_seq"], 2)  # 封存根在序号 2 处更早损坏


class CorruptApiTests(unittest.TestCase):
    """账本损坏后：停止接受新复核与封存，既有读取仍可用。"""

    @classmethod
    def setUpClass(cls):
        fresh_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        fresh_db()  # 恢复干净状态，避免影响其它测试

    def _req(self, method, path, body=None):
        data = headers = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers or {}, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_unhealthy_ledger_refuses_writes_but_serves_reads(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [{"channels": ["a", "b"], "parity": 1}],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 200, data)
        rid = data["review_id"]
        # 篡改序号为 1 的叶摘要并重启核对
        with sqlite3.connect(storage.DB_PATH) as conn:
            conn.execute("UPDATE ledger SET leaf_hash = ? WHERE seq = 1", ("0" * 64,))
        storage.init_db()
        self.assertFalse(storage.ledger_status()["healthy"])
        # 新复核被拒（503，定位最早损坏序号）
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 503)
        self.assertEqual(data["corrupt_seq"], 1)
        # 新封存同样被拒
        status, data = self._req("POST", "/api/seal",
                                 {"seal_id": "late", "review_id": rid})
        self.assertEqual(status, 503)
        # 既有复核读取仍返回既有最小故障结论
        status, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status, 200)
        self.assertTrue(got["conclusion"]["feasible"])
        self.assertEqual(got["conclusion"]["faulty"], ["b"])  # 字典序裁决 (0,1,0)
        # 账本状态可查
        status, st = self._req("GET", "/api/ledger/status")
        self.assertEqual(status, 200)
        self.assertFalse(st["healthy"])
        self.assertEqual(st["corrupt_seq"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
