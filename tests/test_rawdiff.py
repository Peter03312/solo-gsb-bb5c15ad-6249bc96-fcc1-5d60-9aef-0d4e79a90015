"""两事件 raw 结构差异比较的纯标准库测试（无需 FastAPI / 网络）。

用法: python tests/test_rawdiff.py

覆盖：
- 纯函数 diff_values：对象新增/删除/递归替换、标量替换、类型替换不展开、
  布尔≠数值、null 语义、数字 int/float 相等、NaN、~0/~1 转义；
- 数组 LCS：删除/新增/匹配内部差异、相等优先配对、同优优先跳过左侧、
  下标按存在侧、整子树替换不重复展开；
- 入口 compare_raw：相同记录差异为空、不存在 ID 拒绝（单/双侧）、空 ID 拒绝、
  只读（比较前后 raw 不变）、计数正确、JSON Pointer 根为空串。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-rawdiff-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app import rawdiff  # noqa: E402
from app.db import db, init_db  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)

D = rawdiff.diff_values
ops = lambda items: [(it["op"], it["path"]) for it in items]


class ObjectDiff(unittest.TestCase):
    def test_key_only_one_side(self):
        items = D({"a": 1, "b": 2}, {"b": 2})
        self.assertEqual(ops(items), [("removed", "/a")])
        self.assertEqual(items[0]["left"], 1)
        self.assertNotIn("right", items[0])  # 缺失侧省略，不能用 null

        items = D({"b": 2}, {"a": 1, "b": 2})
        self.assertEqual(ops(items), [("added", "/a")])
        self.assertEqual(items[0]["right"], 1)
        self.assertNotIn("left", items[0])

    def test_key_order_independent_and_sorted(self):
        items = D({"z": 1, "a": 2}, {"z": 2, "a": 1})
        self.assertEqual([it["path"] for it in items], ["/a", "/z"])

    def test_nested_recursion(self):
        a = {"o": {"x": 1, "y": 2}}
        b = {"o": {"x": 9, "y": 2}}
        items = D(a, b)
        self.assertEqual(ops(items), [("replaced", "/o/x")])
        self.assertEqual((items[0]["left"], items[0]["right"]), (1, 9))
        self.assertFalse(items[0]["type_changed"])

    def test_type_replacement_subtree_not_expanded(self):
        a = {"o": {"x": 1, "y": 2}}
        b = {"o": [1, 2]}
        items = D(a, b)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["op"], "replaced")
        self.assertEqual(items[0]["path"], "/o")
        self.assertTrue(items[0]["type_changed"])
        self.assertEqual(items[0]["left"], {"x": 1, "y": 2})
        self.assertEqual(items[0]["right"], [1, 2])

    def test_explicit_null_is_present_not_missing(self):
        items = D({"a": None}, {"a": 1})
        self.assertEqual(ops(items), [("replaced", "/a")])
        self.assertTrue(items[0]["type_changed"])
        self.assertIsNone(items[0]["left"])

        items = D({"a": None}, {})
        # 一侧显式 null、一侧无键：删除，值为 null，区别于缺失省略
        self.assertEqual(ops(items), [("removed", "/a")])
        self.assertIsNone(items[0]["left"])

        self.assertEqual(D({"a": None}, {"a": None}), [])

    def test_pointer_escape(self):
        items = D({"a/b": 1, "m~n": 2, "x": {"p/q": 3}},
                  {"a/b": 1, "m~n": 9, "x": {"p/q": 8}})
        self.assertEqual([it["path"] for it in items],
                         ["/m~0n", "/x/p~1q"])

    def test_keys_sorting_is_by_name_not_type(self):
        items = D({"a": {"x": 1}, "b": {"x": 1}}, {"a": {"x": 2}})
        # /a 先（标量替换），/b 后（整子树删除），按键名排序
        self.assertEqual([it["path"] for it in items], ["/a/x", "/b"])


class ScalarDiff(unittest.TestCase):
    def test_scalar_value_replace(self):
        items = D({"v": "a"}, {"v": "b"})
        self.assertEqual(items[0]["op"], "replaced")
        self.assertFalse(items[0]["type_changed"])

    def test_boolean_not_equal_to_number(self):
        self.assertEqual(D({"v": True}, {"v": 1})[0]["type_changed"], True)
        self.assertEqual(D({"v": False}, {"v": 0})[0]["type_changed"], True)
        self.assertEqual(D(True, 1)[0]["op"], "replaced")
        self.assertTrue(D(True, 1)[0]["type_changed"])
        # 布尔彼此按值比较
        self.assertEqual(D(True, False)[0]["type_changed"], False)

    def test_number_int_float_equal(self):
        self.assertEqual(D({"v": 1}, {"v": 1.0}), [])
        self.assertEqual(D({"v": 1.5}, {"v": 1.5}), [])
        self.assertEqual(D({"v": 1}, {"v": 2})[0]["type_changed"], False)

    def test_type_pairs(self):
        for a, b in [("1", 1), (1, [1]), ([1], {"0": 1}), ({"x": 1}, "x")]:
            with self.subTest(a=a, b=b):
                items = D(a, b)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["path"], "")  # 根路径为空串
                self.assertTrue(items[0]["type_changed"])

    def test_nan_equals_nan(self):
        self.assertEqual(D(float("nan"), float("nan")), [])


class ArrayDiff(unittest.TestCase):
    def test_pure_add_remove(self):
        items = D([1, 2, 3], [1, 3])
        self.assertEqual(ops(items), [("removed", "/1")])
        self.assertEqual(items[0]["left"], 2)
        self.assertNotIn("right", items[0])

        items = D([1, 3], [1, 2, 3])
        self.assertEqual(ops(items), [("added", "/1")])
        self.assertEqual(items[0]["right"], 2)

    def test_matched_elements_recurse_with_left_index(self):
        # 数组按“深度相等”配对：完全相等的元素对才递归（无差异则不出项）
        a = [{"x": 1, "y": 0}, {"y": 2}]
        b = [{"x": 1, "y": 0}, {"y": 2}]
        self.assertEqual(D(a, b), [])

        # 元素不深度相等时不配对、不展开内部：整项删除/新增
        a = [{"x": 1}, {"y": 2}]
        b = [{"x": 9}, {"y": 2}]
        self.assertEqual(ops(D(a, b)),
                         [("removed", "/0"), ("added", "/0")])

    def test_matched_equal_pair_with_inner_equality_recurses_silently(self):
        # 三元素，中间的完全相等元素对在 LCS 中必须被选中（路径锚定左侧下标）
        items = D([{"x": 1}, {"k": 0}, {"y": 2}],
                  [{"x": 9}, {"k": 0}, {"y": 2}])
        self.assertEqual(ops(items), [("removed", "/0"), ("added", "/0")])

    def test_all_unmatched_uses_original_indices(self):
        # 无深度相等元素：删除锚定左侧下标，新增锚定右侧下标
        items = D([1, 2], [3, 4])
        self.assertEqual(ops(items),
                         [("removed", "/0"), ("removed", "/1"),
                          ("added", "/0"), ("added", "/1")])

    def test_equal_pairs_preferred(self):
        # 若不优先配对相等元素，可能把 2 与 2 错配；相等必须配对
        items = D([1, 2, 3], [2, 3, 4])
        # LCS = [2,3]；1 删除（/0），4 新增（/2）
        self.assertEqual(ops(items), [("removed", "/0"), ("added", "/2")])

    def test_tie_skip_left_first(self):
        # a=[1], b=[2] 且深度不等：删/增同优，删除（跳过左侧）在前
        items = D([1], [2])
        self.assertEqual(ops(items), [("removed", "/0"), ("added", "/0")])

        # 内部对象深度不等时同样整项删除/新增，不配对展开
        items = D([{"x": 1}, 2], [{"x": 9}, 2])
        self.assertEqual(ops(items), [("removed", "/0"), ("added", "/0")])

    def test_tie_between_unmatched_objects_skip_left(self):
        # 两个对象深度不等（无 LCS 匹配）：左侧对象整体删除后右侧整体新增，
        # 不应被错误配成一对后展开内部差异
        a = {"arr": [{"k": 1}]}
        b = {"arr": [{"k": 2}]}
        items = D(a, b)
        # {"k":1} 与 {"k":2} 深度相等性为假（值不同），故不配对
        self.assertEqual(ops(items),
                         [("removed", "/arr/0"), ("added", "/arr/0")])

    def test_lcs_deep_structures(self):
        a = [{"x": 1}, {"y": 2}, {"z": 3}]
        b = [{"y": 2}, {"z": 30}, {"w": 4}]
        # LCS 唯一匹配：a[1]==b[0] {"y":2}；相等元素必须优先配对，
        # 故 /1（左）不出现删除；b[0] 不出现新增。
        items = D(a, b)
        self.assertEqual(ops(items),
                         [("removed", "/0"), ("removed", "/2"),
                          ("added", "/1"), ("added", "/2")])

    def test_nested_arrays_paths(self):
        # 子数组内容不同即不深度相等 → 整棵子数组删除/新增，下标为外层原始
        # 下标（数组只按深度相等 LCS，不做“同位置”内部展开）
        items = D([[1, 2], [3]], [[1], [3, 4]])
        self.assertEqual(ops(items),
                         [("removed", "/0"), ("removed", "/1"),
                          ("added", "/0"), ("added", "/1")])

    def test_nested_arrays_deep_equal_match_silently(self):
        # 完全相同的子数组被 LCS 配对，无差异项
        self.assertEqual(D([[1, 2], [3]], [[1, 2], [3]]), [])

    def test_array_nested_inside_object_recurses_via_object(self):
        # 数组嵌在对象里：对象按键对齐后，数组走 LCS，路径挂在对象键下
        items = D({"o": [1, 2, 3]}, {"o": [1, 3]})
        self.assertEqual(ops(items), [("removed", "/o/1")])

    def test_type_change_in_array_yields_remove_add(self):
        # 数组层面只有“深度相等”才配对；同位置但类型不同的元素不配对，
        # 类型差异体现在左删除项 / 右新增项各自携带的值上（整子树一项）
        items = D([1, 2], [1, "2"])
        self.assertEqual(len(items), 2)
        self.assertEqual([it["op"] for it in items], ["removed", "added"])
        self.assertEqual([it["path"] for it in items], ["/1", "/1"])

    def test_empty_arrays(self):
        self.assertEqual(D([], []), [])
        self.assertEqual(ops(D([], [1])), [("added", "/0")])
        self.assertEqual(ops(D([1], [])), [("removed", "/0")])

    def test_root_scalar_arrays(self):
        self.assertEqual(D([1, 2, 3], [1, 2, 3]), [])
        items = D("left", "right")
        self.assertEqual(items[0]["path"], "")
        self.assertFalse(items[0]["type_changed"])


class CompareEntry(unittest.TestCase):
    def setUp(self):
        reset_all()
        import_payload({"sources": [{"id": "s1", "name": "S1"}], "events": [
            {"id": "e1", "source": "s1", "time": "2026-01-01T00:00:00Z",
             "type": "login", "actor": "alice", "detail": {"ip": "10.0.0.1",
                                                           "mfa": True,
                                                           "tags": ["a", "b"]}},
            {"id": "e2", "source": "s1", "time": "2026-01-01T00:01:00Z",
             "type": "login", "actor": "alice", "detail": {"ip": "10.0.0.2",
                                                           "mfa": 1,
                                                           "tags": ["a", "c"]}},
            {"id": "e3", "source": "s1", "time": "2026-01-01T00:02:00Z",
             "type": "login", "actor": "alice", "detail": {"ip": "10.0.0.1",
                                                           "mfa": True,
                                                           "tags": ["a", "b"]}},
        ]})

    def test_identical_records_have_empty_diff(self):
        # 库内业务 ID 唯一：直接插一条 raw 与 e1 完全相同的另一事件
        with db() as conn:
            row = conn.execute(
                "SELECT source_id,seq,local_ts,ts_text,event_type,actor,summary,raw "
                "FROM events WHERE id='e1'").fetchone()
            conn.execute(
                "INSERT INTO events(id,source_id,seq,local_ts,ts_text,event_type,"
                "actor,summary,raw,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("e1dup", row["source_id"], row["seq"], row["local_ts"],
                 row["ts_text"], row["event_type"], row["actor"], row["summary"],
                 row["raw"], "2026-01-01T00:00:00+00:00"))
        r = rawdiff.compare_raw("e1", "e1dup")
        self.assertTrue(r["identical"])
        self.assertEqual(r["counts"],
                         {"added": 0, "removed": 0, "replaced": 0, "total": 0})
        self.assertEqual(r["items"], [])

    def test_counts_and_items(self):
        r = rawdiff.compare_raw("e1", "e2")
        self.assertFalse(r["identical"])
        paths = {it["path"]: it for it in r["items"]}
        # 顶层差异：id、time；detail 内：ip 替换、mfa 类型替换(true≠1)；
        # tags 数组：a 匹配、b 删除、c 新增（后两者同路径 /detail/tags/1，
        # 是删除项与新增项各一条）
        self.assertFalse(paths["/id"]["type_changed"])
        self.assertEqual(paths["/time"]["op"], "replaced")
        self.assertEqual(paths["/detail/ip"]["op"], "replaced")
        self.assertTrue(paths["/detail/mfa"]["type_changed"])
        at_tag = [it for it in r["items"] if it["path"] == "/detail/tags/1"]
        self.assertEqual(sorted(it["op"] for it in at_tag), ["added", "removed"])
        self.assertEqual(next(it for it in at_tag if it["op"] == "added")["right"], "c")
        self.assertEqual(next(it for it in at_tag if it["op"] == "removed")["left"], "b")
        reps = {it["path"] for it in r["items"] if it["op"] == "replaced"}
        self.assertEqual(reps, {"/id", "/time", "/detail/ip", "/detail/mfa"})
        self.assertEqual(r["counts"],
                         {"added": 1, "removed": 1, "replaced": 4, "total": 6})

    def test_same_id_compared_to_itself(self):
        r = rawdiff.compare_raw("e1", "e1")
        self.assertTrue(r["identical"])
        self.assertEqual(r["items"], [])

    def test_missing_ids_rejected_with_reason(self):
        with self.assertRaises(rawdiff.RawDiffError) as cm:
            rawdiff.compare_raw("nope", "e1")
        self.assertIn("不存在", str(cm.exception))
        self.assertIn("nope", str(cm.exception))
        with self.assertRaises(rawdiff.RawDiffError) as cm:
            rawdiff.compare_raw("e1", "nope2")
        self.assertIn("nope2", str(cm.exception))
        with self.assertRaises(rawdiff.RawDiffError) as cm:
            rawdiff.compare_raw("x", "y")
        self.assertIn("x", str(cm.exception))
        self.assertIn("y", str(cm.exception))

    def test_blank_ids_rejected(self):
        with self.assertRaises(rawdiff.RawDiffError):
            rawdiff.compare_raw("  ", "e1")
        with self.assertRaises(rawdiff.RawDiffError):
            rawdiff.compare_raw("e1", "")

    def test_comparison_is_readonly(self):
        with db() as conn:
            before = conn.execute("SELECT raw FROM events WHERE id='e1'").fetchone()[0]
        rawdiff.compare_raw("e1", "e2")
        with self.assertRaises(rawdiff.RawDiffError):  # 拒绝路径也不改写
            rawdiff.compare_raw("e1", "missing-x")
        with db() as conn:
            after = conn.execute("SELECT raw FROM events WHERE id='e1'").fetchone()[0]
        self.assertEqual(before, after)

    def test_payload_roundtrips_json(self):
        r = rawdiff.compare_raw("e1", "e2")
        # 结果可被 json 序列化（证据包/下载需要）
        s = json.dumps(r, ensure_ascii=False)
        again = json.loads(s)
        self.assertEqual(again["counts"], r["counts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
