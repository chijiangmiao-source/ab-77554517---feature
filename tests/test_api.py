"""接口冒烟测试：提交、复核取回、非法输入拒绝、不可行结论持久化、健康检查、
账本叶追加与证据封存（创建 / 幂等 / 冲突 / 包含路径复算）。

以 Python 标准库 urllib 起真实 HTTP 线程，无需第三方依赖。
"""

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 让测试可以独立运行；且必须在导入应用模块前设置 DB 路径
# （storage 在导入时读取 APP_DB）。
import sys
import tempfile

_TMP = tempfile.TemporaryDirectory()
os.environ["APP_DB"] = os.path.join(_TMP.name, "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import server  # noqa: E402


def fold_path(leaf_hex, steps):
    """独立复算：叶摘要沿包含路径折叠（节点 = SHA-256(0x01||左||右)）。"""
    acc = bytes.fromhex(leaf_hex)
    for st in steps:
        sib = bytes.fromhex(st["hash"])
        if st["side"] == "left":
            acc = hashlib.sha256(b"\x01" + sib + acc).digest()
        else:
            acc = hashlib.sha256(b"\x01" + acc + sib).digest()
    return acc.hex()


VALID_BODY = {
    "channels": ["CH0", "CH1", "CH2", "CH3"],
    "checks": [
        {"channels": ["CH0", "CH1"], "parity": 1},
        {"channels": ["CH1", "CH2", "CH3"], "parity": 0},
    ],
}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _req(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

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

    # ---- 账本与证据封存 ----
    def test_submit_returns_ledger_leaf(self):
        status, data = self._req("POST", "/api/submit", VALID_BODY)
        self.assertEqual(status, 200, data)
        self.assertGreaterEqual(data["ledger"]["seq"], 1)
        self.assertEqual(len(data["ledger"]["leaf"]), 64)
        # 取回复核时携带同一账本信息与空封存列表
        status, got = self._req("GET", f"/api/review/{data['review_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(got["ledger"], data["ledger"])
        self.assertEqual(got["seals"], [])

    def test_invalid_submit_does_not_consume_seq(self):
        _, d1 = self._req("POST", "/api/submit", VALID_BODY)
        seq1 = d1["ledger"]["seq"]
        # 非法提交：被拒且不产生记录
        status, _ = self._req("POST", "/api/submit",
                              {"channels": ["a", "a"], "checks": []})
        self.assertEqual(status, 400)
        _, d2 = self._req("POST", "/api/submit", VALID_BODY)
        self.assertEqual(d2["ledger"]["seq"], seq1 + 1)

    def test_seal_flow_and_proof(self):
        _, d1 = self._req("POST", "/api/submit", VALID_BODY)
        _, d2 = self._req("POST", "/api/submit", VALID_BODY)
        r1, r2 = d1["review_id"], d2["review_id"]

        # 创建封存：锚定 r1 提交瞬间的连续前缀
        status, seal = self._req("POST", "/api/seal",
                                 {"seal_id": "api-seal-1", "review_id": r1})
        self.assertEqual(status, 200, seal)
        self.assertTrue(seal["created"])
        self.assertEqual(seal["size"], d1["ledger"]["seq"])
        self.assertEqual(seal["leaf"], d1["ledger"]["leaf"])
        self.assertEqual(len(seal["root"]), 64)
        # 包含路径复算到根
        self.assertEqual(
            fold_path(seal["leaf"], seal["path"]["steps"]), seal["root"]
        )
        self.assertEqual(seal["path"]["index"], d1["ledger"]["seq"] - 1)
        self.assertEqual(seal["path"]["size"], seal["size"])

        # 幂等重传：同标识同载荷返回原封存
        status, again = self._req("POST", "/api/seal",
                                  {"seal_id": "api-seal-1", "review_id": r1})
        self.assertEqual(status, 200)
        self.assertFalse(again["created"])
        self.assertEqual(again["root"], seal["root"])

        # 复用标识改目标：409 且已封存根不变
        status, conflict = self._req("POST", "/api/seal",
                                     {"seal_id": "api-seal-1", "review_id": r2})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["existing"]["root"], seal["root"])
        status, got = self._req("GET", "/api/seal/api-seal-1")
        self.assertEqual(status, 200)
        self.assertEqual(got["root"], seal["root"])
        self.assertEqual(
            fold_path(got["leaf"], got["path"]["steps"]), got["root"]
        )

        # 复用标识改摘要：409
        status, _ = self._req("POST", "/api/seal", {
            "seal_id": "api-seal-1", "review_id": r1,
            "expected_root": "0" * 64,
        })
        self.assertEqual(status, 409)
        # 声明摘要一致：幂等返回原封存
        status, ok = self._req("POST", "/api/seal", {
            "seal_id": "api-seal-1", "review_id": r1,
            "expected_root": seal["root"],
        })
        self.assertEqual(status, 200)
        self.assertFalse(ok["created"])

        # 复核详情携带封存列表
        status, rec = self._req("GET", f"/api/review/{r1}")
        self.assertEqual(status, 200)
        self.assertEqual([s["seal_id"] for s in rec["seals"]], ["api-seal-1"])

    def test_seal_validation_and_unknowns(self):
        _, d = self._req("POST", "/api/submit", VALID_BODY)
        rid = d["review_id"]
        # 非法标识 / 非法编号 / 非法摘要格式
        status, data = self._req("POST", "/api/seal",
                                 {"seal_id": "bad id!", "review_id": rid})
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["field"], "seal_id")
        status, data = self._req("POST", "/api/seal",
                                 {"seal_id": "ok-1", "review_id": "zz"})
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["field"], "review_id")
        status, data = self._req("POST", "/api/seal", {
            "seal_id": "ok-1", "review_id": rid, "expected_root": "XYZ",
        })
        self.assertEqual(status, 400)
        self.assertEqual(data["errors"][0]["field"], "expected_root")
        # 不存在的复核 / 封存标识
        status, data = self._req("POST", "/api/seal",
                                 {"seal_id": "ok-1", "review_id": "deadbeefdead"})
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "review_id")
        status, data = self._req("GET", "/api/seal/no-such-seal")
        self.assertEqual(status, 404)
        self.assertEqual(data["field"], "seal_id")

    def test_ledger_status_endpoint(self):
        status, data = self._req("GET", "/api/ledger/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["healthy"])
        self.assertGreaterEqual(data["size"], 1)
        self.assertEqual(len(data["root"]), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
