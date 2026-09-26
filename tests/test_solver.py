"""求解器单元测试：唯一故障、多解裁决、不可行、输入校验、规模边界。

运行：python -m pytest -q  （无 pytest 时：python tests/test_solver.py）
"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.solver import (
    MAX_CHECKS,
    MAX_CHANNELS,
    ValidationError,
    recompute,
    solve,
)


def brute_force(channels, checks):
    """枚举完整向量的参考实现（仅测试用，用于核对 MITM 最优性）。"""
    channels = sorted(set(channels))
    n = len(channels)
    pos = {c: i for i, c in enumerate(channels)}
    rows = []
    for members, parity in checks:
        mask = 0
        for m in members:
            mask |= 1 << pos[m]
        rows.append((mask, parity))

    best = None  # (weight, vector tuple)
    for bits in range(1 << n):
        ok = True
        for mask, parity in rows:
            if ((mask & bits).bit_count() & 1) != parity:
                ok = False
                break
        if ok:
            vec = tuple((bits >> j) & 1 for j in range(n))
            cand = (sum(vec), vec)
            if best is None or cand < best:
                best = cand
    return channels, best


class SolverTests(unittest.TestCase):
    def test_unique_single_fault(self):
        channels = ["CH0", "CH1", "CH2", "CH3", "CH4", "CH5"]
        checks = [
            (["CH0", "CH1", "CH3"], 1),
            (["CH2", "CH3", "CH4"], 1),
            (["CH3", "CH5"], 1),
            (["CH0", "CH2", "CH4"], 0),
        ]
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 1)
        self.assertEqual(r.faulty, ("CH3",))
        # 折半：6 通道 -> 左 3 右 3
        self.assertEqual(r.left_size, 3)
        # 逐校验复算必须全部一致
        rows = [(tuple(m), p) for m, p in checks]
        for row in recompute(list(r.channels), rows, list(r.vector)):
            self.assertTrue(row["pass"], row)

    def test_zero_weight_solution(self):
        # 所有观测奇偶均为 0：零向量可行，最优重量 0。
        channels = ["a", "b", "c", "d"]
        checks = [(["a", "b"], 0), (["b", "c", "d"], 0)]
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 0)
        self.assertEqual(r.faulty, ())
        self.assertEqual(r.vector, (0, 0, 0, 0))

    def test_tie_break_lexicographic(self):
        # 仅一条校验 {a,b,c} 奇偶 1：重量 1 的解有 a / b / c 三个，
        # 选择向量按通道升序为 (x_a,x_b,x_c)，标准字典序
        # (0,0,1) < (0,1,0) < (1,0,0)，故裁决给 c。
        channels = ["c", "a", "b"]  # 故意乱序录入
        checks = [(["a", "b", "c"], 1)]
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 1)
        self.assertEqual(r.faulty, ("c",))
        self.assertEqual(r.vector, (0, 0, 1))

    def test_tie_break_weight_two(self):
        # 无约束（空系统不允许），构造两个重量 2 解：
        # x1 xor x2 = 0 且 x3 xor x4 = 0，外加目标使 0000 不可行，
        # 用 x1 xor x3 = 1 ：可行重量 2 解为 (1,1,0,0) 与 (0,0,1,1)，
        # 字典序较小者 (1,1,0,0) -> 通道 a,b。
        channels = ["a", "b", "c", "d"]
        checks = [
            (["a", "b"], 1),
            (["c", "d"], 0),
            (["a", "c"], 1),
        ]
        # 校验：a⊕b=1, c⊕d=0, a⊕c=1
        # 重量1？a=1 -> b=0,c=0,d=0: a⊕c=1 OK, c⊕d=0 OK -> 唯一重量1 a
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 1)
        self.assertEqual(r.faulty, ("a",))

        # 改为迫使 a=0：a⊕b=0, c⊕d=0, a⊕c=1, b⊕d=1
        # 重量2 解：(a,b)=00 -> (c,d)=11 -> 向量 (0,0,1,1)
        #          (a,b)=11 -> (c,d)=00 -> 向量 (1,1,0,0)
        # 标准字典序 (0,0,1,1) 更小，裁决给 c,d。
        checks2 = [
            (["a", "b"], 0),
            (["c", "d"], 0),
            (["a", "c"], 1),
            (["b", "d"], 1),
        ]
        r2 = solve(channels, checks2)
        self.assertTrue(r2.feasible)
        self.assertEqual(r2.weight, 2)
        self.assertEqual(r2.faulty, ("c", "d"))

    def test_infeasible_simple(self):
        # 同一通道集合出现奇偶 0 与 1 直接矛盾。
        channels = ["a", "b", "c"]
        checks = [(["a", "b"], 0), (["a", "b"], 1)]
        # 注意：集合重复会被输入校验拒绝；这里用不同集合制造矛盾：
        checks = [
            (["a", "b"], 0),
            (["a", "b", "c"], 0),
            (["c"], 1),
        ]
        # a⊕b=0, a⊕b⊕c=0 => c=0，与 c=1 矛盾。
        r = solve(channels, checks)
        self.assertFalse(r.feasible)
        self.assertEqual(r.weight, -1)
        self.assertEqual(r.faulty, ())
        self.assertEqual(r.vector, ())

    def test_infeasible_xor_contradiction(self):
        # 行 3 = 行1 xor 行2 但目标位不满足对应关系。
        channels = ["a", "b", "c", "d"]
        checks = [
            (["a", "b"], 1),
            (["b", "c"], 1),
            (["a", "c"], 1),  # 应为 1⊕1=0，给 1 => 矛盾
            (["d"], 0),
        ]
        r = solve(channels, checks)
        self.assertFalse(r.feasible)

    # ---- 随机对照：MITM 结果必须与完整枚举逐例一致 ----
    def test_random_against_bruteforce(self):
        rng = random.Random(20260926)
        for trial in range(60):
            n = rng.randint(2, 12)
            channels = [f"c{j:02d}" for j in range(n)]
            m = rng.randint(1, min(2 * n, 28, 2 ** n - 1))
            seen = set()
            checks = []
            while len(checks) < m:
                k = rng.randint(1, n)
                members = tuple(sorted(rng.sample(channels, k)))
                if members in seen:
                    continue
                seen.add(members)
                checks.append((list(members), rng.randint(0, 1)))

            ordered, best = brute_force(channels, checks)
            r = solve(channels, checks)
            if best is None:
                self.assertFalse(r.feasible, f"trial {trial}: 应为不可行")
                continue
            bw, bvec = best
            self.assertTrue(r.feasible, f"trial {trial}: 应可行")
            self.assertEqual(r.weight, bw, f"trial {trial}: 重量不一致")
            self.assertEqual(r.vector, bvec, f"trial {trial}: 裁决向量不一致 {checks}")

    def test_max_size_performance(self):
        # 36 通道、28 校验必须可接受时间内完成（2^18 折半枚举）。
        rng = random.Random(7)
        channels = [f"PX{j:02d}" for j in range(MAX_CHANNELS)]
        checks = []
        seen = set()
        while len(checks) < MAX_CHECKS:
            k = rng.randint(2, 10)
            members = tuple(sorted(rng.sample(channels, k)))
            if members in seen:
                continue
            seen.add(members)
            checks.append((list(members), rng.randint(0, 1)))
        r = solve(channels, checks)
        # 只要求算法正常结束并自洽；满秩随机系统大概率有低重量解或可行。
        if r.feasible:
            rows = [(tuple(m), p) for m, p in checks]
            self.assertTrue(
                all(row["pass"] for row in recompute(
                    list(r.channels), rows, list(r.vector)))
            )

    # ---- 输入校验 ----
    def test_duplicate_channel_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            solve(["a", "b", "a"], [(["a", "b"], 0)])
        fields = {f for f, _ in ctx.exception.errors}
        self.assertTrue(any("channels[2]" in f for f in fields))

    def test_channel_count_bounds(self):
        with self.assertRaises(ValidationError):
            solve(["a"], [(["a"], 0)])
        with self.assertRaises(ValidationError):
            solve([f"c{j}" for j in range(37)],
                  [(["c0", "c1"], 0)])

    def test_empty_or_duplicate_check_set_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            solve(["a", "b", "c"], [([], 0)])
        self.assertTrue(any("channels" in f for f, _ in ctx.exception.errors))

        with self.assertRaises(ValidationError) as ctx:
            solve(["a", "b", "c"],
                  [(["a", "b"], 0), (["b", "a"], 1)])
        self.assertTrue(any("重复" in m for _, m in ctx.exception.errors))

    def test_unknown_channel_and_bad_parity_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            solve(["a", "b"], [(["a", "z"], 5)])
        msgs = " ".join(m for _, m in ctx.exception.errors)
        self.assertIn("未定义通道", msgs)
        self.assertIn("奇偶", msgs)

    def test_too_many_checks(self):
        with self.assertRaises(ValidationError):
            solve(["a", "b"], [(["a"], 0)] * (MAX_CHECKS + 1))

    def test_syndrome_index_built_per_spec(self):
        # 5 通道折半为 l=2 / r=3；左半至多 2^2=4 个综合征条目。
        r = solve(["a", "b", "c", "d", "e"],
                  [(["a", "c"], 1), (["b", "d", "e"], 0)])
        self.assertTrue(r.feasible)
        self.assertEqual(r.left_size, 2)
        self.assertLessEqual(r.left_index_size, 4)

    def test_all_single_bit_syndromes(self):
        # 每条校验单独覆盖一个通道，直接定位所有观测为 1 的通道，
        # 且通道按标识排序输出。
        channels = [f"x{j}" for j in range(8)]
        checks = [
            (["x7"], 1), (["x1"], 1), (["x3"], 1),
        ] + [([f"x{j}"], 0) for j in (0, 2, 4, 5, 6)]
        r = solve(channels, checks)
        self.assertTrue(r.feasible)
        self.assertEqual(r.weight, 3)
        self.assertEqual(r.faulty, ("x1", "x3", "x7"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
