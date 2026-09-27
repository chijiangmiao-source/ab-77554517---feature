"""证据封存账本：规范字节摘要 + RFC 6962 风格 Merkle 树。

每条被接受的复核记录（排序后的输入、观测与结论）先算出规范字节摘要，
作为一片叶子按服务端递增序号追加到账本；序号连续、不重号、不占号
（非法或失败的提交不会产生叶子）。

- 规范字节：对排序后的通道、规范化并排序的校验以及结论做键排序、
  紧凑分隔符的 JSON 编码（UTF-8），取其 SHA-256；
- 叶/节点哈希带域分离：叶 = SHA256(0x00 || 叶摘要)，
  内部节点 = SHA256(0x01 || 左 || 右)；
- 任意连续前缀 [0, size) 的根即该封存批次的根摘要；叶子的包含路径
  （Merkle path）可由任何人独立复算，验证该叶确属某批次。
"""

from __future__ import annotations

import hashlib
import json

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"


def canonical_bytes(payload: dict, conclusion: dict) -> bytes:
    """排序后的输入、观测与结论的规范字节。

    通道按标识排序；每条校验的成员排序后，校验列表再按
    (成员, 奇偶值) 排序；结论按键排序的紧凑 JSON 表示。
    """
    canon = {
        "channels": sorted(payload["channels"]),
        "checks": sorted(
            (
                {"channels": sorted(ck["channels"]), "parity": int(ck["parity"])}
                for ck in payload["checks"]
            ),
            key=lambda c: (c["channels"], c["parity"]),
        ),
        "conclusion": conclusion,
    }
    return json.dumps(
        canon, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def record_digest(payload: dict, conclusion: dict) -> str:
    """复核记录的规范字节摘要（hex）。"""
    return hashlib.sha256(canonical_bytes(payload, conclusion)).hexdigest()


def seal_request_digest(seal_id: str, review_id: str, root: str | None) -> str:
    """封存请求载荷的规范摘要（hex），用于同标识重传的幂等判定。"""
    canon = {"review_id": review_id, "root": root, "seal_id": seal_id}
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def leaf_hash(digest_hex: str) -> bytes:
    return hashlib.sha256(LEAF_PREFIX + bytes.fromhex(digest_hex)).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def _split(n: int) -> int:
    """小于 n 的最大 2 的幂（要求 n >= 2）。"""
    k = 1
    while k < n:
        k <<= 1
    return k >> 1


def _mth(hashes: list[bytes]) -> bytes:
    """Merkle Tree Hash：叶哈希序列的根。"""
    if len(hashes) == 1:
        return hashes[0]
    k = _split(len(hashes))
    return node_hash(_mth(hashes[:k]), _mth(hashes[k:]))


def root_hex(digests: list[str]) -> str:
    """连续叶摘要序列（前缀批次）的根摘要（hex）。"""
    if not digests:
        raise ValueError("空批次没有根摘要")
    return _mth([leaf_hash(d) for d in digests]).hex()


def inclusion_path(index: int, digests: list[str]) -> list[dict]:
    """叶 index 在批次 digests 中的包含路径。

    返回 [{"pos": "left"|"right", "hash": hex}, ...]，自叶向根排列；
    pos 指该步兄弟子树相对于当前子树的位置。
    """
    if not 0 <= index < len(digests):
        raise ValueError("叶序号超出批次范围")
    return _path(index, [leaf_hash(d) for d in digests])


def _path(i: int, hashes: list[bytes]) -> list[dict]:
    if len(hashes) == 1:
        return []
    k = _split(len(hashes))
    if i < k:
        return _path(i, hashes[:k]) + [
            {"pos": "right", "hash": _mth(hashes[k:]).hex()}
        ]
    return _path(i - k, hashes[k:]) + [
        {"pos": "left", "hash": _mth(hashes[:k]).hex()}
    ]


def verify_path(digest_hex: str, path: list[dict], expected_root_hex: str) -> bool:
    """由叶摘要与包含路径复算根摘要，并与期望根比对。"""
    h = leaf_hash(digest_hex)
    for step in path:
        sibling = bytes.fromhex(step["hash"])
        if step["pos"] == "left":
            h = node_hash(sibling, h)
        else:
            h = node_hash(h, sibling)
    return h.hex() == expected_root_hex


class Frontier:
    """账本前沿：当前全部叶子对应的完美子树根集合（尺寸严格递减）。

    重启时由持久叶记录逐片重建；每追加一叶即时更新。前沿折叠根
    与对全部叶子直接求 MTH 一致。
    """

    def __init__(self) -> None:
        self._peaks: list[tuple[int, bytes]] = []  # (子树尺寸, 根)，尺寸递减

    @property
    def size(self) -> int:
        return sum(size for size, _ in self._peaks)

    def append(self, digest_hex: str) -> None:
        self._peaks.append((1, leaf_hash(digest_hex)))
        # 相邻两峰尺寸相等即合并，保持尺寸严格递减。
        while len(self._peaks) >= 2 and self._peaks[-1][0] == self._peaks[-2][0]:
            size1, hash1 = self._peaks.pop()
            size2, hash2 = self._peaks.pop()
            self._peaks.append((size1 + size2, node_hash(hash2, hash1)))

    def root_hex(self) -> str | None:
        """当前全部叶子的根摘要（hex）；账本为空时为 None。"""
        if not self._peaks:
            return None
        acc = self._peaks[0][1]
        for _, h in self._peaks[1:]:
            acc = node_hash(acc, h)
        return acc.hex()
