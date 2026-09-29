"""两事件原始载荷结构差异比较的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_raw_diff.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-rawdiff-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import db, init_db  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402
from app.raw_diff import (  # noqa: E402
    RawDiffError,
    compare_event_raws,
    structural_diff,
)

init_db(load_sample=False)


def ops(diff):
    """{op: [paths]} 便于断言。"""
    out = {"added": [], "removed": [], "replaced": []}
    for d in diff["differences"]:
        out[d["op"]].append(d["path"])
    return out


def item(diff, path, op=None):
    for d in diff["differences"]:
        if d["path"] == path and (op is None or d["op"] == op):
            return d
    raise KeyError((path, op))


class ScalarAndTypeRules(unittest.TestCase):
    def test_identical_is_empty(self):
        for v in [0, 1, 1.0, True, False, None, "x", "",
                  {"a": [1, {"b": True}]}, [None, False, "s"]]:
            d = structural_diff(v, v)
            self.assertEqual(d["differences"], [])
            self.assertEqual(d["op_counts"], {"added": 0, "removed": 0,
                                              "replaced": 0})

    def test_scalar_value_replacement(self):
        d = structural_diff(1, 2)
        self.assertEqual(ops(d), {"added": [], "removed": [], "replaced": [""]})
        self.assertEqual(item(d, ""), {"op": "replaced", "path": "",
                                       "left": 1, "right": 2})
        self.assertEqual(item(structural_diff("a", "b"), "")["path"], "")

    def test_bool_not_equal_to_number(self):
        # 关键规则：布尔不得与数值视为相等
        d = structural_diff(True, 1)
        self.assertEqual(ops(d)["replaced"], [""])
        self.assertIs(item(d, "")["left"], True)
        self.assertEqual(item(d, "")["right"], 1)
        d2 = structural_diff(False, 0)
        self.assertEqual(ops(d2)["replaced"], [""])
        # 但同是布尔按布尔比
        self.assertEqual(structural_diff(True, True)["differences"], [])
        # 数值 1 与 1.0 是同一个 JSON 数字
        self.assertEqual(structural_diff(1, 1.0)["differences"], [])

    def test_type_differences_are_replacement(self):
        d = structural_diff({"a": 1}, [1])
        self.assertEqual(ops(d)["replaced"], [""])
        d = structural_diff([1], {"0": 1})
        self.assertEqual(ops(d)["replaced"], [""])
        d = structural_diff("1", 1)
        self.assertEqual(ops(d)["replaced"], [""])
        d = structural_diff(None, 0)
        self.assertEqual(ops(d)["replaced"], [""])
        # 显式 null 仍然写出 null，而不是省略
        self.assertIsNone(item(d, "")["left"])

    def test_present_null_vs_missing_side_distinguished(self):
        # 键在右侧存在但值为 null：replaced，left 也写出 null
        d = structural_diff({"k": None}, {"k": 1})
        it = item(d, "/k")
        self.assertEqual(it["op"], "replaced")
        self.assertIn("left", it)
        self.assertIsNone(it["left"])
        self.assertEqual(it["right"], 1)


class ObjectRecursion(unittest.TestCase):
    def test_added_removed_keys_not_expanded(self):
        left = {"keep": 1, "gone": {"x": [1, 2, {"y": 2}]}, "rm": 9}
        right = {"keep": 1, "new": {"deep": {"z": 3}}}
        d = structural_diff(left, right)
        o = ops(d)
        self.assertEqual(o["added"], ["/new"])
        self.assertEqual(sorted(o["removed"]), ["/gone", "/rm"])
        # 子树不重复展开：/gone/x 等不应出现
        all_paths = [x["path"] for x in d["differences"]]
        self.assertNotIn("/gone/x", all_paths)
        self.assertNotIn("/new/deep", all_paths)
        # 整棵子树作为值带出
        self.assertEqual(item(d, "/gone")["left"], {"x": [1, 2, {"y": 2}]})
        self.assertEqual(item(d, "/new")["right"], {"deep": {"z": 3}})
        self.assertNotIn("right", item(d, "/gone"))
        self.assertNotIn("left", item(d, "/new"))

    def test_recurse_into_common_keys(self):
        d = structural_diff({"a": {"b": {"c": 1, "d": 2}}},
                            {"a": {"b": {"c": 1, "d": 3}}})
        self.assertEqual(ops(d)["replaced"], ["/a/b/d"])

    def test_keys_sorted_for_deterministic_output(self):
        left = {chr(c): 1 for c in range(ord("z"), ord("a") - 1, -1)}
        right = {}
        d = structural_diff(left, right)
        paths = [x["path"] for x in d["differences"]]
        self.assertEqual(paths, [f"/{chr(c)}" for c in
                                 range(ord("a"), ord("z") + 1)])

    def test_json_pointer_escaping(self):
        d = structural_diff({"a/b": 1, "c~d": 2, "~1": 3}, {})
        # 对象键按字典序处理：~ (0x7E) > c，故 ~1 最后
        self.assertEqual(ops(d)["removed"],
                         ["/a~1b", "/c~0d", "/~01"])

    def test_numeric_object_keys_are_strings(self):
        # JSON 对象键永远是字符串，不能被当成数组下标
        d = structural_diff({"0": "x"}, {"0": "y"})
        self.assertEqual(ops(d)["replaced"], ["/0"])


class ArrayLCS(unittest.TestCase):
    def test_unmatched_elements_listed_by_original_index(self):
        d = structural_diff([1, 2, 3], [2, 3, 4])
        o = ops(d)
        self.assertEqual(o["removed"], ["/0"])       # 左 1 被删（左下标 0）
        self.assertEqual(o["added"], ["/2"])         # 右 4 新增（右下标 2）
        self.assertEqual(item(d, "/0", "removed")["left"], 1)
        self.assertNotIn("right", item(d, "/0", "removed"))
        self.assertEqual(item(d, "/2", "added")["right"], 4)
        self.assertNotIn("left", item(d, "/2", "added"))

    def test_deep_equal_matching(self):
        d = structural_diff([{"a": 1}, {"b": 2}], [{"a": 1}, {"b": 3}])
        # {b:2} 与 {b:3} 不深度相等 → 不能配对，按 LCS 各自删除/新增
        o = ops(d)
        self.assertEqual(o["removed"], ["/1"])
        self.assertEqual(o["added"], ["/1"])
        # 子树整体记一项，不展开成 /1/b 的 replaced
        self.assertEqual(item(d, "/1", "removed")["op"], "removed")
        self.assertEqual(item(d, "/1", "removed")["left"], {"b": 2})
        self.assertEqual(item(d, "/1", "added")["right"], {"b": 3})

    def test_shifted_common_element_no_diff(self):
        # 公共元素位置移动但深度相等：只报两侧未匹配元素
        d = structural_diff([9, 1, 2], [1, 2, 9])
        o = ops(d)
        self.assertEqual(o["removed"], ["/0"])
        self.assertEqual(o["added"], ["/2"])

    def test_tie_prefer_skip_left(self):
        # 无任何相等元素：LCS=0，回溯每个位置 dp 均为 0，平局 → 先跳过左侧，
        # 全部左元素记删除，再全部右元素记新增。
        d = structural_diff([1, 2], [3, 4])
        o = ops(d)
        self.assertEqual(o["removed"], ["/0", "/1"])
        self.assertEqual(o["added"], ["/0", "/1"])

    def test_tie_equal_pair_preferred(self):
        # 多个最优对齐等长时，相等元素优先配对（而非为了对齐而拆对）。
        # a=[x,1], b=[1,y]：LCS 长度 1，唯一相等是 1；必须配对 1，
        # x 删除（/0），y 新增（/1）。
        d = structural_diff([7, 1], [1, 8])
        o = ops(d)
        self.assertEqual(o["removed"], ["/0"])
        self.assertEqual(o["added"], ["/1"])

    def test_bool_inside_arrays_not_equal_number(self):
        d = structural_diff([True], [1])
        self.assertEqual(ops(d)["removed"], ["/0"])
        self.assertEqual(ops(d)["added"], ["/0"])

    def test_nested_array_path_uses_original_indices(self):
        d = structural_diff({"k": [1, 2, 3]}, {"k": [2, 3]})
        self.assertEqual(ops(d)["removed"], ["/k/0"])


class CountsAndDB(unittest.TestCase):
    def setUp(self):
        reset_all()

    def _import(self, events):
        import_payload({
            "sources": [{"id": "s1"}],
            "events": [{"id": e["id"], "source": "s1", "time": 1, **e}
                       for e in events],
        })

    def test_counts(self):
        d = structural_diff(
            {"a": 1, "b": 2, "c": 3},
            {"a": 1, "b": 20, "d": 4})
        self.assertEqual(d["op_counts"], {"added": 1, "removed": 1,
                                          "replaced": 1})

    def test_compare_end_to_end_identical(self):
        # 导入时 raw 原样保存（含事件自身 id）；两份记录除 id 外完全相同，
        # 故差异只有 /id 一处替换。真正“完全相同”用同一条事件自比覆盖。
        self._import([
            {"id": "e1", "raw_marker": {"x": [1, True, None]}},
            {"id": "e2", "raw_marker": {"x": [1, True, None]}},
        ])
        r = compare_event_raws("e1", "e2")
        self.assertFalse(r["identical"])
        self.assertEqual(ops(r)["replaced"], ["/id"])
        r2 = compare_event_raws("e1", "e1")
        self.assertTrue(r2["identical"])
        self.assertEqual(r2["differences"], [])

    def test_compare_end_to_end_diffs(self):
        self._import([
            {"id": "e1", "m": {"a": 1, "b": [True]}},
            {"id": "e2", "m": {"a": 2, "b": [1], "c": "new"}},
        ])
        r = compare_event_raws("e1", "e2")
        self.assertFalse(r["identical"])
        o = ops(r)
        self.assertIn("/m/a", o["replaced"])
        self.assertIn("/id", o["replaced"])
        self.assertIn("/m/c", o["added"])
        # 数组 [True] vs [1]：布尔≠数值 → 两侧各一项（路径相同，按 op 区分）
        self.assertEqual(o["removed"], ["/m/b/0"])
        self.assertIn("/m/b/0", o["added"])
        # 计数：新增 2（/m/c、右数组元素），删除 1（左数组元素），替换 2（/id、/m/a）
        self.assertEqual(r["op_counts"], {"added": 2, "removed": 1,
                                          "replaced": 2})

    def test_same_id_compared_with_itself(self):
        self._import([{"id": "e1", "v": 1}])
        r = compare_event_raws("e1", "e1")
        self.assertTrue(r["identical"])
        self.assertEqual(r["left"]["event_id"], "e1")
        self.assertEqual(r["right"]["event_id"], "e1")

    def test_missing_id_rejected(self):
        self._import([{"id": "e1", "v": 1}])
        with self.assertRaises(KeyError) as cm:
            compare_event_raws("e1", "nope")
        self.assertEqual(cm.exception.args, ("nope", "right"))
        with self.assertRaises(KeyError) as cm:
            compare_event_raws("ghost-a", "ghost-b")
        self.assertEqual(cm.exception.args[0], "ghost-a")
        self.assertEqual(cm.exception.args[1], "left")

    def test_blank_id_rejected(self):
        with self.assertRaises(RawDiffError):
            compare_event_raws("  ", "e1")
        with self.assertRaises(RawDiffError):
            compare_event_raws("e1", "")

    def test_comparison_is_readonly(self):
        self._import([{"id": "e1", "v": 1}, {"id": "e2", "v": 2}])
        with db() as conn:
            before = conn.execute(
                "SELECT raw FROM events WHERE id='e1'").fetchone()["raw"]
        compare_event_raws("e1", "e2")
        with db() as conn:
            after = conn.execute(
                "SELECT raw FROM events WHERE id='e1'").fetchone()["raw"]
        self.assertEqual(before, after)


class Limits(unittest.TestCase):
    def test_too_deep_rejected(self):
        from app import raw_diff as mod
        # 对象嵌套深度超限（差异遍历走显式栈，抛业务错误而非递归崩溃）
        deep_a: object = 1
        deep_b: object = 2
        for _ in range(mod.MAX_DEPTH + 5):
            deep_a = {"k": deep_a}
            deep_b = {"k": deep_b}
        with self.assertRaises(RawDiffError):
            structural_diff(deep_a, deep_b)

    def test_deep_equal_does_not_crash_on_deep_equal_arrays(self):
        # 数组元素只做深度相等（显式栈）：深嵌套的全等数组不抛错
        from app import raw_diff as mod
        deep = [1]
        for _ in range(mod.MAX_DEPTH + 50):
            deep = [deep]
        d = structural_diff(deep, deep)
        self.assertEqual(d["differences"], [])

    def test_oversized_array_rejected_on_type_mismatch_path(self):
        from app import raw_diff as mod
        big = [0] * (mod.MAX_ARRAY_SIDE + 1)
        with self.assertRaises(RawDiffError):
            structural_diff(big, big)


if __name__ == "__main__":
    # 纯标准库断言风格，逐文件可直接运行
    unittest.main(verbosity=2)
