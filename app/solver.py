"""
硅像素读出板噪声故障定位 —— 最小汉明重量异或求解器。

问题模型
========
工程师录入 n 个唯一通道 (2 <= n <= 36)，以及 m 条奇偶校验
(1 <= m <= 28)。每条校验引用一个非空、互不重复的通道集合，
并给出观测奇偶值 b_i ∈ {0, 1}。

故障向量 x ∈ {0,1}^n 为每个通道是否失效；校验 i 要求
    XOR_{j in 通道集合_i} x_j = b_i。

需求要求在所有可行解中寻找汉明重量最小者；重量相同时，按通道标识
升序排列形成的选择向量（即有序通道上的 0/1 向量）做标准字典序
裁决——从最低序号通道起逐位比较，先出现 0 的向量更小，等价于
“优先让标识更小的通道保持健康”。

算法（规定实现，禁止枚举完整向量 / 随机搜索 / 高斯消元）
=======================================================
按排序后的通道折半（meet-in-the-middle）：

1. 左半通道数 l = n // 2，右半 r = n - l。
2. 枚举左半的全部 2^l 个部分向量（n <= 36 时至多 2^18 = 262144，
   且每条校验至多 28 位，综合征可用一个 Python int 表示），
   建立“左半综合征 -> 该综合征下最优左半候选”的索引：
   同综合征取重量最小者，同重量取选择向量字典序最小者。
3. 右半按重量层枚举；右半综合征 sR 必须匹配目标综合征中的
   左半贡献缺口，即需要 sL = b XOR sR。在索引中精确查找，
   命中时合并重量 wL + wR；按总重量、再按完整选择向量字典序
   裁决全局最优。右半重量层一旦不低于已知最优总重量即剪枝。

两侧枚举的都是折半后的 *部分* 向量（总迭代 2^l + 2^r 量级），
算法从不枚举任何完整 2^n 故障向量，也不使用随机搜索或高斯消元。
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_CHANNELS = 2
MAX_CHANNELS = 36
MAX_CHECKS = 28


class ValidationError(ValueError):
    """输入非法；errors 为可定位的拒绝信息列表 (field, message)。"""

    def __init__(self, errors: list[tuple[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{f}: {msg}" for f, msg in errors))


@dataclass(frozen=True)
class SolveResult:
    feasible: bool
    channels: tuple[str, ...]          # 排序后的全部通道
    faulty: tuple[str, ...]            # 不可行时为 ()
    weight: int                        # 不可行时为 -1
    vector: tuple[int, ...]            # 不可行时为 ()
    left_size: int                     # 折半位置（便于测试/复算展示）
    left_index_size: int               # 左半综合征索引条目数


def _validate(channels, checks):
    """校验原始输入，返回 (有序通道列表, [(通道集合元组, 奇偶值)])。"""
    errors: list[tuple[str, str]] = []

    # ---- 通道 ----
    if not isinstance(channels, list):
        raise ValidationError([("channels", "通道必须以数组形式提供")])

    norm_channels: list[str] = []
    seen: set[str] = set()
    for idx, ch in enumerate(channels):
        field = f"channels[{idx}]"
        if not isinstance(ch, str):
            errors.append((field, "通道标识必须是字符串"))
            continue
        name = ch.strip()
        if not name:
            errors.append((field, "通道标识不得为空"))
            continue
        if name in seen:
            errors.append((field, f"通道 {name!r} 重复"))
            continue
        seen.add(name)
        norm_channels.append(name)

    if not (MIN_CHANNELS <= len(norm_channels) <= MAX_CHANNELS):
        errors.append((
            "channels",
            f"唯一通道数量须在 {MIN_CHANNELS}–{MAX_CHANNELS} 之间，"
            f"当前 {len(norm_channels)}",
        ))

    # ---- 校验 ----
    if not isinstance(checks, list):
        raise ValidationError([("checks", "校验必须以数组形式提供")])
    if not checks:
        errors.append(("checks", "至少需要 1 条校验"))
    if len(checks) > MAX_CHECKS:
        errors.append(("checks", f"校验数量不得超过 {MAX_CHECKS}，当前 {len(checks)}"))

    norm_checks: list[tuple[tuple[str, ...], int]] = []
    seen_sets: set[frozenset[str]] = set()
    for idx, ck in enumerate(checks):
        cfield = f"checks[{idx}]"
        # 同时接受 API 的对象形式 {"channels": [...], "parity": 0/1}
        # 与内部/测试使用的 (通道列表, 奇偶值) 元组形式。
        if isinstance(ck, dict):
            raw_chs = ck.get("channels")
            parity = ck.get("parity")
        elif isinstance(ck, (list, tuple)) and len(ck) == 2:
            raw_chs, parity = ck
        else:
            errors.append((cfield, "校验必须是对象或 [通道集合, 奇偶值] 数组"))
            continue
        members: list[str] = []
        member_seen: set[str] = set()
        if not isinstance(raw_chs, list) or not raw_chs:
            errors.append((f"{cfield}.channels", "引用通道集合不得为空"))
        else:
            for j, ch in enumerate(raw_chs):
                mfield = f"{cfield}.channels[{j}]"
                if not isinstance(ch, str) or not ch.strip():
                    errors.append((mfield, "通道引用必须是非空字符串"))
                    continue
                name = ch.strip()
                if name not in seen:
                    errors.append((mfield, f"引用了未定义通道 {name!r}"))
                if name in member_seen:
                    errors.append((mfield, f"通道 {name!r} 在本条校验中重复引用"))
                    continue
                member_seen.add(name)
                members.append(name)
        if not isinstance(parity, int) or isinstance(parity, bool) or parity not in (0, 1):
            errors.append((f"{cfield}.parity", "观测奇偶值必须是 0 或 1"))
            parity_val = -1
        else:
            parity_val = parity

        if members:
            key = frozenset(members)
            if key in seen_sets:
                errors.append((f"{cfield}.channels", "与另一条校验引用的通道集合重复"))
            else:
                seen_sets.add(key)
        if members and parity_val in (0, 1):
            norm_checks.append((tuple(members), parity_val))

    if errors:
        raise ValidationError(errors)

    norm_channels.sort()
    return norm_channels, norm_checks


def _build_rows(ordered_channels, checks):
    """把每条校验压缩成位掩码行（位 j 对应 ordered_channels[j]）与目标位。"""
    pos = {name: j for j, name in enumerate(ordered_channels)}
    rows: list[tuple[int, int]] = []
    for members, parity in checks:
        mask = 0
        for name in members:
            mask |= 1 << pos[name]
        rows.append((mask, parity))
    return rows


def _reverse_bits(value: int, width: int) -> int:
    """把 width 位整数位序反转。

    通道升序选择向量 (x_0,...,x_{k-1}) 的字典序，恰好等于把
    x_0 放在最高位后的整数大小关系；故位反转整数可作为
    “同重量下字典序”的单调比较键。
    """
    out = 0
    for _ in range(width):
        out = (out << 1) | (value & 1)
        value >>= 1
    return out


def _combinations_by_weight(width: int, weight: int):
    """生成恰好含 weight 个置位位的 width 位整数。"""
    chosen: list[int] = []

    def gen(start: int):
        if len(chosen) == weight:
            v = 0
            for p in chosen:
                v |= 1 << p
            yield v
            return
        for k in range(start, width - (weight - len(chosen)) + 1):
            chosen.append(k)
            yield from gen(k + 1)
            chosen.pop()

    yield from gen(0)


def solve(channels, checks) -> SolveResult:
    """
    求最小汉明重量可行故障向量；同重量按选择向量（通道标识升序）
    字典序最小裁决。无可行解释时返回 feasible=False。
    """
    ordered_channels, norm_checks = _validate(channels, checks)
    rows = _build_rows(ordered_channels, norm_checks)
    n = len(ordered_channels)
    m = len(rows)

    # 目标综合征 b：第 i 位为第 i 条校验的观测奇偶值。
    target = 0
    for i, (_, parity) in enumerate(rows):
        target |= parity << i

    l = n // 2                      # 折半：左 l 位，右 n-l 位
    r = n - l

    # 列掩码：通道 j 的列 = 所有引用该通道的校验行编号集合。
    # 某半区部分向量的综合征，即其置位通道列掩码的 XOR。
    left_masks = [0] * l
    right_masks = [0] * r
    for i, (mask, _) in enumerate(rows):
        for j in range(l):
            if (mask >> j) & 1:
                left_masks[j] |= 1 << i
        for j in range(r):
            if (mask >> (l + j)) & 1:
                right_masks[j] |= 1 << i
    full_mask = (1 << m) - 1

    def syndrome(vec: int, masks: list[int]) -> int:
        s = 0
        while vec:
            low = vec & -vec
            s ^= masks[low.bit_length() - 1]
            vec ^= low
        return s

    # ------------------------------------------------------------------
    # 步骤 1：左半折半，枚举全部 2^l 个部分向量并建立综合征索引。
    # 每个综合征只保留：重量最小 -> 同重量选择向量字典序最小 的候选。
    # ------------------------------------------------------------------
    # index: syndrome -> (weight, vec, lex_key)
    index: dict[int, tuple[int, int, int]] = {}
    for vec in range(1 << l):
        s = syndrome(vec, left_masks)
        w = vec.bit_count()
        key = _reverse_bits(vec, l)
        kept = index.get(s)
        if kept is None or w < kept[0] or (w == kept[0] and key < kept[2]):
            index[s] = (w, vec, key)

    # ------------------------------------------------------------------
    # 步骤 2：右半按重量层枚举，精确查找 sL = target XOR sR，
    # 合并两侧候选并裁决全局 (总重量, 完整选择向量字典序) 最优。
    # ------------------------------------------------------------------
    best_weight = n + 1
    best_full = -1
    best_key = -1

    for wR in range(r + 1):
        # wL 最小为 0；只有 wR 严格大于已知最优总重量时该层才无机会。
        # wR == best_weight 的层仍可能由 wL=0 给出同重量、字典序更小的解。
        if wR > best_weight:
            break
        for vecR in _combinations_by_weight(r, wR):
            sR = syndrome(vecR, right_masks)
            need = (target ^ sR) & full_mask
            kept = index.get(need)
            if kept is None:
                continue
            wL, vecL, _ = kept
            total = wL + wR
            if total > best_weight:
                continue
            full = vecL | (vecR << l)
            if total < best_weight:
                best_weight = total
                best_full = full
                best_key = _reverse_bits(full, n)
            else:
                key = _reverse_bits(full, n)
                if key < best_key:
                    best_full = full
                    best_key = key

    if best_full < 0:
        return SolveResult(
            feasible=False,
            channels=tuple(ordered_channels),
            faulty=(),
            weight=-1,
            vector=(),
            left_size=l,
            left_index_size=len(index),
        )

    vector = tuple((best_full >> j) & 1 for j in range(n))
    faulty = tuple(
        ordered_channels[j] for j in range(n) if (best_full >> j) & 1
    )
    return SolveResult(
        feasible=True,
        channels=tuple(ordered_channels),
        faulty=faulty,
        weight=best_weight,
        vector=vector,
        left_size=l,
        left_index_size=len(index),
    )


def recompute(ordered_channels, checks, vector) -> list[dict]:
    """逐校验复算：用给定选择向量重算每条 XOR，并与观测值比对。"""
    values = {ch: vector[j] for j, ch in enumerate(ordered_channels)}
    results = []
    for members, parity in checks:
        got = 0
        for name in members:
            got ^= values[name]
        results.append({
            "members": list(members),
            "observed": parity,
            "recomputed": got,
            "pass": got == parity,
        })
    return results
