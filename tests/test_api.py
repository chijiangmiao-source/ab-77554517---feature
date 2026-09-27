"""接口冒烟测试：提交、复核取回、非法输入拒绝、不可行结论持久化、健康检查。

以 Python 标准库 urllib 起真实 HTTP 线程，无需第三方依赖。
另含证据封存账本测试：序号与摘要追加、封存幂等与冲突、包含路径复算、
重启后重建核对、损坏停摆定位。
"""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 让测试可以独立运行；且必须在导入应用模块前设置 DB 路径
# （storage 在导入时读取 APP_DB）。
import sys

_TMP = tempfile.TemporaryDirectory()
os.environ["APP_DB"] = os.path.join(_TMP.name, "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import ledger  # noqa: E402
import server  # noqa: E402
import storage  # noqa: E402


def http_req(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def valid_body(tag):
    """构造一个合法提交（唯一故障解：仅 f"{tag}1" 失效）。"""
    return {
        "channels": [f"{tag}0", f"{tag}1", f"{tag}2"],
        "checks": [
            {"channels": [f"{tag}0", f"{tag}1"], "parity": 1},
            {"channels": [f"{tag}1", f"{tag}2"], "parity": 1},
            {"channels": [f"{tag}0", f"{tag}2"], "parity": 0},
        ],
    }


class HttpTestCase(unittest.TestCase):
    """自带独立临时库与 HTTP 服务的基类。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old_db = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls._tmp.name, "test.db")
        server.init_db()
        cls._start_server()

    @classmethod
    def _start_server(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def _stop_server(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    @classmethod
    def tearDownClass(cls):
        cls._stop_server()
        storage.DB_PATH = cls._old_db
        server.init_db()  # 恢复默认库并清除可能的停摆状态
        cls._tmp.cleanup()

    def _req(self, method, path, body=None):
        return http_req(self.base, method, path, body)


class ApiTests(HttpTestCase):
    def test_healthz(self):
        status, body = self._req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers["Content-Type"])
            self.assertIn("故障定位", resp.read().decode("utf-8"))

    def test_submit_unique_fault_and_review(self):
        body = {
            "channels": ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"],
            "checks": [
                {"channels": ["CH0", "CH1", "CH3"], "parity": 1},
                {"channels": ["CH2", "CH3", "CH4"], "parity": 1},
                {"channels": ["CH3", "CH5"], "parity": 1},
                {"channels": ["CH0", "CH2", "CH4"], "parity": 0},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 200, data)
        rid = data["review_id"]
        self.assertTrue(rid)
        self.assertEqual(data["conclusion"]["faulty"], ["CH3"])
        self.assertEqual(data["conclusion"]["weight"], 1)
        self.assertTrue(all(r["pass"] for r in data["conclusion"]["recompute"]))

        # 刷新后凭编号取回
        status2, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status2, 200)
        self.assertEqual(got["review_id"], rid)
        self.assertEqual(got["conclusion"]["faulty"], ["CH3"])

    def test_infeasible_is_persisted(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["a", "b", "c"], "parity": 0},
                {"channels": ["c"], "parity": 1},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 200)
        self.assertFalse(data["conclusion"]["feasible"])
        rid = data["review_id"]
        status2, got = self._req("GET", f"/api/review/{rid}")
        self.assertEqual(status2, 200)
        self.assertFalse(got["conclusion"]["feasible"])
        self.assertEqual(got["conclusion"]["faulty"], [])

    def test_invalid_input_rejected_with_location(self):
        # 重复通道 + 空校验集合 + 非法奇偶
        body = {
            "channels": ["a", "b", "a"],
            "checks": [{"channels": [], "parity": 9}],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 400)
        fields = {e["field"] for e in data["errors"]}
        self.assertIn("channels[2]", fields)
        self.assertTrue(any(".channels" in f for f in fields))
        self.assertTrue(any(".parity" in f for f in fields))

    def test_duplicate_check_set_rejected(self):
        body = {
            "channels": ["a", "b", "c"],
            "checks": [
                {"channels": ["a", "b"], "parity": 0},
                {"channels": ["b", "a"], "parity": 1},
            ],
        }
        status, data = self._req("POST", "/api/submit", body)
        self.assertEqual(status, 400)
        self.assertTrue(any("重复" in e["message"] for e in data["errors"]))

    def test_bad_json_rejected(self):
        req = urllib.request.Request(
            self.base + "/api/submit",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            data = json.loads(e.read().decode("utf-8"))
            self.assertEqual(data["field"], "body")

    def test_unknown_review_id_404(self):
        status, data = self._req("GET", "/api/review/deadbeefdead")
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "review_id")


class LedgerApiTests(HttpTestCase):
    """封存账本：序号与摘要追加、封存幂等与冲突、包含路径复算。"""

    def test_submit_assigns_seq_and_leaf_digest(self):
        _, before = self._req("GET", "/api/ledger")
        status, data = self._req("POST", "/api/submit", valid_body("s"))
        self.assertEqual(status, 200, data)
        self.assertEqual(data["ledger"]["seq"], before["size"])
        self.assertEqual(len(data["ledger"]["leaf_digest"]), 64)
        _, after = self._req("GET", "/api/ledger")
        self.assertEqual(after["size"], before["size"] + 1)
        self.assertFalse(after["halted"])

    def test_invalid_submit_does_not_consume_seq(self):
        _, before = self._req("GET", "/api/ledger")
        status, _ = self._req("POST", "/api/submit", {
            "channels": ["a", "b", "a"],
            "checks": [{"channels": [], "parity": 9}],
        })
        self.assertEqual(status, 400)
        _, after = self._req("GET", "/api/ledger")
        self.assertEqual(after["size"], before["size"], "非法提交不得占用账本序号")
        # 随后的合法提交拿到紧邻的下一个序号（无断号）。
        status, data = self._req("POST", "/api/submit", valid_body("g"))
        self.assertEqual(status, 200)
        self.assertEqual(data["ledger"]["seq"], before["size"])

    def test_seal_create_replay_and_conflict(self):
        ids = []
        for tag in ("a", "b", "c"):
            status, data = self._req("POST", "/api/submit", valid_body(tag))
            self.assertEqual(status, 200)
            ids.append((data["review_id"], data["ledger"]["seq"],
                        data["ledger"]["leaf_digest"]))
        rid1, seq1, leaf1 = ids[1]

        # 创建封存：锚定 [0, seq1] 连续前缀。
        body = {"seal_id": "t-seal-1", "review_id": rid1}
        status, seal = self._req("POST", "/api/seal", body)
        self.assertEqual(status, 200, seal)
        self.assertFalse(seal["replayed"])
        self.assertEqual(seal["size"], seq1 + 1)
        self.assertEqual(seal["last_seq"], seq1)
        self.assertEqual(len(seal["root"]), 64)
        self.assertEqual(seal["leaf_index"], seq1)
        self.assertEqual(seal["leaf_digest"], leaf1)
        self.assertTrue(
            ledger.verify_path(leaf1, seal["path"], seal["root"]),
            "包含路径应能复算到根摘要",
        )

        # 同一标识同一载荷重传 → 返回原封存。
        status, again = self._req("POST", "/api/seal", body)
        self.assertEqual(status, 200)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["root"], seal["root"])
        self.assertEqual(again["created_at"], seal["created_at"])

        # 复用标识却改变目标 → 拒绝，且已封存根不变。
        status, conflict = self._req("POST", "/api/seal", {
            "seal_id": "t-seal-1", "review_id": ids[2][0],
        })
        self.assertEqual(status, 409)
        self.assertEqual(conflict["existing"]["root"], seal["root"])
        status, again2 = self._req("POST", "/api/seal", body)
        self.assertEqual(status, 200)
        self.assertEqual(again2["root"], seal["root"], "冲突不得改变已封存根")

        # 复核详情：根摘要与本条复核的包含路径可复算。
        status, got = self._req("GET", f"/api/review/{rid1}")
        self.assertEqual(status, 200)
        self.assertEqual(got["ledger"]["seq"], seq1)
        self.assertEqual(got["seal"]["root"], seal["root"])
        self.assertTrue(ledger.verify_path(
            got["ledger"]["leaf_digest"], got["seal"]["path"], got["seal"]["root"]))

    def test_seal_root_mismatch_rejected_without_creating(self):
        status, data = self._req("POST", "/api/submit", valid_body("m"))
        rid = data["review_id"]
        status, resp = self._req("POST", "/api/seal", {
            "seal_id": "t-seal-2", "review_id": rid, "root": "0" * 64,
        })
        self.assertEqual(status, 400)
        self.assertEqual(resp["field"], "root")
        # 未创建：随后正确请求仍可成功。
        status, seal = self._req("POST", "/api/seal", {
            "seal_id": "t-seal-2", "review_id": rid,
        })
        self.assertEqual(status, 200)
        # 复用标识却改变摘要 → 拒绝。
        status, _ = self._req("POST", "/api/seal", {
            "seal_id": "t-seal-2", "review_id": rid, "root": "0" * 64,
        })
        self.assertEqual(status, 409)

    def test_seal_unknown_review_404(self):
        status, data = self._req("POST", "/api/seal", {
            "seal_id": "t-seal-3", "review_id": "deadbeefdead",
        })
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "review_id")

    def test_seal_validation_400(self):
        status, data = self._req("POST", "/api/seal", {"review_id": "abc"})
        self.assertEqual(status, 400)
        fields = {e["field"] for e in data["errors"]}
        self.assertIn("seal_id", fields)
        status, data = self._req("POST", "/api/seal", {
            "seal_id": "t-seal-4", "review_id": "abc", "root": "xyz",
        })
        self.assertEqual(status, 400)
        fields = {e["field"] for e in data["errors"]}
        self.assertIn("root", fields)


class LedgerRestartTests(HttpTestCase):
    """重启后从持久叶记录重建前沿；封存前记录的路径仍复算到相同根。"""

    @classmethod
    def _restart(cls):
        cls._stop_server()
        server.init_db()  # 同一数据库文件：重建前沿并核对全部封存根
        cls._start_server()

    def test_restart_recomputes_same_root(self):
        ids, leaves = [], []
        for tag in ("r0", "r1", "r2"):
            status, data = self._req("POST", "/api/submit", valid_body(tag))
            self.assertEqual(status, 200)
            ids.append(data["review_id"])
            leaves.append(data["ledger"]["leaf_digest"])

        # 封存前两条（序号 0..1）为一个批次。
        status, seal = self._req("POST", "/api/seal", {
            "seal_id": "restart-seal", "review_id": ids[1],
        })
        self.assertEqual(status, 200)
        self.assertEqual(seal["size"], 2)
        root = seal["root"]
        self.assertEqual(root, ledger.root_hex(leaves[:2]))

        # 重启前取一次路径。
        _, before = self._req("GET", f"/api/review/{ids[0]}")
        self.assertEqual(before["seal"]["root"], root)

        # 重启：重建前沿并核对全部封存根。
        self._restart()

        _, led = self._req("GET", "/api/ledger")
        self.assertFalse(led["halted"])
        self.assertEqual(led["size"], 3)
        self.assertEqual(led["root"], ledger.root_hex(leaves))
        # 重启后对封存前记录取得的路径仍复算到相同根。
        for rid, leaf in zip(ids[:2], leaves[:2]):
            status, got = self._req("GET", f"/api/review/{rid}")
            self.assertEqual(status, 200)
            self.assertEqual(got["seal"]["root"], root)
            self.assertTrue(ledger.verify_path(
                got["ledger"]["leaf_digest"], got["seal"]["path"], root))
        _, after = self._req("GET", f"/api/review/{ids[0]}")
        self.assertEqual(after["seal"]["path"], before["seal"]["path"],
                         "包含路径应确定不变")

        # 封存幂等性跨重启保持；账本仍可接受新复核。
        status, replay = self._req("POST", "/api/seal", {
            "seal_id": "restart-seal", "review_id": ids[1],
        })
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["root"], root)
        status, data = self._req("POST", "/api/submit", valid_body("r3"))
        self.assertEqual(status, 200)
        self.assertEqual(data["ledger"]["seq"], 3)


class LedgerCorruptionTests(HttpTestCase):
    """断号 / 叶摘要 / 封存根损坏：停摆并定位最早损坏序号。"""

    def setUp(self):
        # 每个用例一套全新库：三条复核 + 一个覆盖前两条的封存。
        storage.DB_PATH = os.path.join(
            self._tmp.name, f"{self._testMethodName}.db")
        server.init_db()
        self.ids = []
        for tag in ("x0", "x1", "x2"):
            status, data = self._req("POST", "/api/submit", valid_body(tag))
            self.assertEqual(status, 200)
            self.ids.append(data["review_id"])
        status, seal = self._req("POST", "/api/seal", {
            "seal_id": "corrupt-seal", "review_id": self.ids[1],
        })
        self.assertEqual(status, 200)
        self.seal = seal

    def _tamper(self, sql, params=()):
        conn = sqlite3.connect(storage.DB_PATH)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
        server.init_db()  # 重启重建：应发现损坏

    def _assert_halted(self, expect_seq):
        _, led = self._req("GET", "/api/ledger")
        self.assertTrue(led["halted"])
        self.assertEqual(led["corrupt_seq"], expect_seq)
        # 停止接受新复核与新封存。
        status, body = self._req("POST", "/api/submit", valid_body("z"))
        self.assertEqual(status, 503)
        self.assertEqual(body["corrupt_seq"], expect_seq)
        status, body = self._req("POST", "/api/seal", {
            "seal_id": "late-seal", "review_id": self.ids[0],
        })
        self.assertEqual(status, 503)
        # 既有复核读取仍返回最小故障结论。
        status, got = self._req("GET", f"/api/review/{self.ids[0]}")
        self.assertEqual(status, 200)
        self.assertEqual(got["conclusion"]["faulty"], ["x01"])
        self.assertTrue(got["conclusion"]["feasible"])

    def test_gap_halts_and_locates(self):
        self._tamper("DELETE FROM leaves WHERE seq = 1")
        self._assert_halted(1)

    def test_leaf_digest_tamper_halts(self):
        self._tamper("UPDATE leaves SET digest = ? WHERE seq = 1", ("0" * 64,))
        self._assert_halted(1)

    def test_malformed_leaf_digest_halts_and_reads_survive(self):
        # 非十六进制的损坏摘要：停摆定位不变，复核读取不得崩溃。
        self._tamper("UPDATE leaves SET digest = 'zz' WHERE seq = 1")
        self._assert_halted(1)

    def test_seal_root_tamper_halts(self):
        # 封存覆盖序号 0..1，根被篡改 → 定位到该批次末尾序号 1。
        self._tamper("UPDATE seals SET root = ? WHERE seal_id = 'corrupt-seal'",
                     ("f" * 64,))
        self._assert_halted(1)

    def test_first_leaf_tamper_locates_earliest(self):
        self._tamper("UPDATE leaves SET digest = ? WHERE seq = 0", ("0" * 64,))
        self._assert_halted(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
