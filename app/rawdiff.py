"""两条事件原始载荷（events.raw）的结构差异比较（只读）。

规则
----
- 对象按键递归：键仅在一侧出现 → ``removed`` / ``added``（整棵子树记一项，
  不再展开）；两侧都有则递归。键顺序无关，输出按联合键名稳定排序。
- 标量值不同、或两侧 JSON 类型不同 → ``replaced``；布尔与数值是不同 JSON
  类型（``true`` 不等于 ``1``，``false`` 不等于 ``0``）。
- 数组按元素“深度相等”求最长公共子序列（LCS）：
  匹配的元素对递归比较；未匹配的左 / 右元素分别记 ``removed`` / ``added``。
  回溯时相等元素优先配对；同为最优且当前不相等时优先跳过左侧元素
  （左删先于右增），保证多种最优对齐下结果唯一。
- 路径为 JSON Pointer（RFC 6901）：``~`` 转义为 ``~0``、``/`` 转义为 ``~1``，
  根路径为空串 ``""``；数组下标用该元素在**存在侧**的原始下标
  （删除用左侧、新增用右侧；匹配元素对的内部路径锚定左侧下标）。
- 缺失侧不带 ``left`` / ``right`` 键（省略而非给 null）。
"""
from __future__ import annotations

import json
import math
from typing import Any

from .db import db

_MISSING = object()  # 哨兵：该侧路径不存在（区别于显式 null）


class RawDiffError(Exception):
    """比较输入错误（空 ID 为 400，未知 ID 为 404）。"""


# --------------------------------------------------------------- 类型与相等

def type_name(v: Any) -> str:
    """JSON 类型名（bool 必须先于 int 判断）。"""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "unknown"


def deep_equal(a: Any, b: Any) -> bool:
    """JSON 语义的深度相等：类型不同即不等；布尔不与数值相等。"""
    if type_name(a) != type_name(b):
        return False
    if isinstance(a, dict):
        if a.keys() != b.keys():
            return False
        return all(deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(
            deep_equal(x, y) for x, y in zip(a, b))
    # 标准 json 可解析出 NaN；NaN 彼此视为相等，避免纯浮点噪声。
    if isinstance(a, float) and math.isnan(a):
        return isinstance(b, float) and math.isnan(b)
    return a == b


# -------------------------------------------------------------- JSON Pointer

def escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


# ----------------------------------------------------------------- 差异核心

def diff_values(left: Any, right: Any) -> list[dict[str, Any]]:
    """比较两份已解析的 JSON 值，返回差异明细（顺序稳定）。"""
    items: list[dict[str, Any]] = []
    _diff(left, right, "", items)
    return items


def _diff(a: Any, b: Any, path: str, out: list[dict[str, Any]]) -> None:
    if a is _MISSING:
        out.append({"op": "added", "path": path, "right": b})
        return
    if b is _MISSING:
        out.append({"op": "removed", "path": path, "left": a})
        return

    ta, tb = type_name(a), type_name(b)
    if ta != tb:
        # 类型替换：整棵子树记一项，不递归展开。
        out.append({"op": "replaced", "path": path, "left": a, "right": b,
                    "type_changed": True})
        return

    if ta == "object":
        for key in sorted(set(a) | set(b)):
            child = f"{path}/{escape_token(str(key))}"
            if key not in b:
                out.append({"op": "removed", "path": child, "left": a[key]})
            elif key not in a:
                out.append({"op": "added", "path": child, "right": b[key]})
            else:
                _diff(a[key], b[key], child, out)
        return

    if ta == "array":
        _diff_arrays(a, b, path, out)
        return

    # 同类型标量：值不同记替换（布尔 / 数值已由类型检查区分）。
    if not deep_equal(a, b):
        out.append({"op": "replaced", "path": path, "left": a, "right": b,
                    "type_changed": False})


def _diff_arrays(a: list, b: list, path: str, out: list[dict[str, Any]]) -> None:
    n, m = len(a), len(b)
    # dp[i][j]：a[i:] 与 b[j:] 基于深度相等的 LCS 长度。
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if deep_equal(a[i], b[j]):
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])

    i = j = 0
    while i < n or j < m:
        if i < n and j < m and deep_equal(a[i], b[j]):
            # 相等元素优先配对；深度相等的元素对无内部差异（LCS 判据即深度
            # 相等），直接消费，数组层面不产生类型替换项。
            i += 1
            j += 1
        elif i == n:
            out.append({"op": "added", "path": f"{path}/{j}", "right": b[j]})
            j += 1
        elif j == m:
            out.append({"op": "removed", "path": f"{path}/{i}", "left": a[i]})
            i += 1
        elif dp[i + 1][j] < dp[i][j + 1]:
            # 跳过右侧（新增 b[j]）能保留更多匹配。
            out.append({"op": "added", "path": f"{path}/{j}", "right": b[j]})
            j += 1
        else:
            # dp[i+1][j] >= dp[i][j+1]：同优时优先跳过左侧（删除 a[i]）。
            out.append({"op": "removed", "path": f"{path}/{i}", "left": a[i]})
            i += 1


# ------------------------------------------------------------------ 入口

def compare_raw(left_id: str, right_id: str) -> dict[str, Any]:
    """取库内两条事件的 raw 做结构比较。只读，不修改任何记录。"""
    left_id = (left_id or "").strip()
    right_id = (right_id or "").strip()
    if not left_id:
        raise RawDiffError("左侧事件 ID 不能为空")
    if not right_id:
        raise RawDiffError("右侧事件 ID 不能为空")

    with db() as conn:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, source_id, event_type, raw FROM events "
            "WHERE id IN (?, ?)", (left_id, right_id)).fetchall()}

    missing: list[str] = []
    if left_id not in rows:
        missing.append(f"左侧 ID {left_id!r}")
    if right_id not in rows:
        missing.append(f"右侧 ID {right_id!r}")
    if missing:
        if len(missing) == 2:
            raise RawDiffError(
                f"事件不存在：左侧 ID {left_id!r} 与右侧 ID {right_id!r} 均未在库中找到")
        raise RawDiffError(f"事件不存在：{missing[0]} 未在库中找到")

    def _load(row) -> Any:
        try:
            return json.loads(row["raw"])
        except (ValueError, TypeError):
            return {}  # 正常入库路径写出的恒为合法 JSON；防御性兜底

    left_raw = _load(rows[left_id])
    right_raw = _load(rows[right_id])

    items = diff_values(left_raw, right_raw)
    counts = {"added": 0, "removed": 0, "replaced": 0}
    for it in items:
        counts[it["op"]] += 1
    counts["total"] = len(items)

    return {
        "diff_type": "event-raw-structural-diff",
        "left": {"id": left_id, "source_id": rows[left_id]["source_id"],
                 "event_type": rows[left_id]["event_type"]},
        "right": {"id": right_id, "source_id": rows[right_id]["source_id"],
                  "event_type": rows[right_id]["event_type"]},
        "identical": not items,
        "counts": counts,
        "items": items,
    }
