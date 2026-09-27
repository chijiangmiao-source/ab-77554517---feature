"""证据封存账本：规范字节摘要 + Merkle 前缀树（仅标准库）。

账本模型
--------
每条被接受的复核在保存时追加一个账本叶：

- 叶摘要 = SHA-256(规范字节)。规范字节是“排序后的输入、观测与结论”
  的确定性 JSON（字典键排序、紧凑分隔符、UTF-8），与提交时的
  键序、校验书写顺序无关。
- 全部叶按服务端递增序号（seq，从 1 开始、连续无断号）排成
  只追加序列；任意前缀 1..n 的 Merkle 根把该前缀固定为一个
  可验证批次。

哈希方案
--------
- 叶节点即叶摘要本身（已是 32 字节哈希）；
- 内部节点 = SHA-256(0x01 || 左子树根 || 右子树根)，0x01 前缀做
  域分隔；
- 树的形状与 RFC 6962 相同：n 片叶时按“小于 n 的最大 2 的幂”
  拆分，左右子树递归求根后合并。

前沿（frontier）
----------------
当前规模二进制分解对应的子树根列表（按规模递减），支持 O(log n)
追加；应用重启后从持久叶记录逐叶重放即可重建，并据此复算任意
前缀的根与任意叶的包含路径。
"""

from __future__ import annotations

import hashlib
import json

_NODE_PREFIX = b"\x01"


def canonical_bytes(payload: dict, conclusion: dict) -> bytes:
    """排序后的输入、观测与结论的规范字节（确定性 JSON）。

    输入通道升序；每条校验的成员升序，校验之间按 (成员, 奇偶值)
    排序；结论取最小故障结论的本质字段（可行性、重量、故障通道、
    按升序通道对齐的选择向量、逐校验复算），复算条目同样按
    (成员, 观测值) 排序。
    """
    channels = sorted(payload["channels"])
    checks = sorted(
        (
            {"channels": sorted(c["channels"]), "parity": int(c["parity"])}
            for c in payload["checks"]
        ),
        key=lambda c: (c["channels"], c["parity"]),
    )
    vector = conclusion.get("vector") or {}
    canon = {
        "input": {"channels": channels, "checks": checks},
        "conclusion": {
            "feasible": bool(conclusion["feasible"]),
            "weight": int(conclusion["weight"]),
            "faulty": sorted(conclusion.get("faulty") or []),
            "vector": (
                [int(vector[ch]) for ch in channels]
                if conclusion["feasible"] else []
            ),
            "recompute": sorted(
                (
                    {
                        "members": sorted(r["members"]),
                        "observed": int(r["observed"]),
                        "recomputed": int(r["recomputed"]),
                        "pass": bool(r["pass"]),
                    }
                    for r in (conclusion.get("recompute") or [])
                ),
                key=lambda r: (r["members"], r["observed"]),
            ),
        },
    }
    return json.dumps(
        canon, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def leaf_digest(payload: dict, conclusion: dict) -> str:
    """规范字节摘要（小写十六进制）。"""
    return hashlib.sha256(canonical_bytes(payload, conclusion)).hexdigest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


def _split(n: int) -> int:
    """严格小于 n 的最大 2 的幂（要求 n >= 2）。"""
    k = 1 << (n.bit_length() - 1)
    return k >> 1 if k == n else k


def root_of(leaves: list[bytes]) -> bytes:
    """leaves（至少 1 片）所成 Merkle 树的根。"""
    if len(leaves) == 1:
        return leaves[0]
    k = _split(len(leaves))
    return _node(root_of(leaves[:k]), root_of(leaves[k:]))


def path_of(leaves: list[bytes], index: int) -> list[tuple[str, bytes]]:
    """index（0 基）叶在 leaves 所成树中的包含路径，自叶向根。

    每步为 (side, sibling)：side 给出兄弟子树相对当前节点的方位
    （"left" 兄弟在左，"right" 兄弟在右）。
    """
    if not 0 <= index < len(leaves):
        raise IndexError("叶序号越界")
    if len(leaves) == 1:
        return []
    k = _split(len(leaves))
    if index < k:
        return path_of(leaves[:k], index) + [("right", root_of(leaves[k:]))]
    return path_of(leaves[k:], index - k) + [("left", root_of(leaves[:k]))]


def fold_path(leaf: bytes, steps) -> bytes:
    """自叶摘要沿包含路径折叠，返回复算根（应与批次根一致）。"""
    acc = leaf
    for side, sibling in steps:
        acc = _node(sibling, acc) if side == "left" else _node(acc, sibling)
    return acc


class Frontier:
    """只追加账本的前沿：规模二进制分解的子树根（按规模递减）。

    同时持有全部叶摘要（每叶 32 字节），用于复算任意前缀的根与
    任意叶的包含路径；重启后由持久叶记录逐叶重放重建。
    """

    def __init__(self, leaves=None):
        self.leaves: list[bytes] = []
        self._trees: list[tuple[int, bytes]] = []
        for leaf in leaves or []:
            self.append(leaf)

    def _push(self, leaf: bytes) -> None:
        size, acc = 1, leaf
        while self._trees and self._trees[-1][0] == size:
            s, left = self._trees.pop()
            size += s
            acc = _node(left, acc)
        self._trees.append((size, acc))

    def __len__(self) -> int:
        return len(self.leaves)

    def append(self, leaf: bytes) -> int:
        """追加一叶，返回其 1 基序号。"""
        self.leaves.append(leaf)
        self._push(leaf)
        return len(self.leaves)

    def root(self, size: int | None = None) -> bytes:
        """连续前缀 1..size 的 Merkle 根；缺省为当前全部叶。"""
        n = len(self.leaves) if size is None else size
        if not 1 <= n <= len(self.leaves):
            raise ValueError("前缀长度越界")
        if n == len(self.leaves):
            acc = None
            for _, h in reversed(self._trees):
                acc = h if acc is None else _node(h, acc)
            return acc
        return root_of(self.leaves[:n])

    def path(self, index: int, size: int | None = None) -> list[tuple[str, bytes]]:
        """index（0 基）叶在连续前缀 1..size 批次中的包含路径。"""
        n = len(self.leaves) if size is None else size
        if not 0 <= index < n <= len(self.leaves):
            raise ValueError("叶序号或前缀长度越界")
        return path_of(self.leaves[:n], index)
