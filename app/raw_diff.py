"""两条事件原始载荷（events.raw）的结构差异比较（只读）。

定位
----
审计员核对两条事件的原始 JSON 载荷时，输入左 / 右两个事件 ID，对库内
``events.raw`` 做**结构差异比较**，不改写任何记录。比较规则：

- 对象按键递归：键仅在一侧出现 → ``added`` / ``removed`` 一整棵子树各记
  一项，**不重复展开**内部差异；两侧都有的键再递归比较；
- 标量值不同、或 JSON 类型不同 → ``replaced`` 一项（同样不展开）。
  **布尔值不得与数值视为相等**（``true != 1``），bool 在 Python 里是
  int 的子类，因此类型判定必须先 bool 后 number；
- 数组按**元素深度相等**求最长公共子序列（LCS）：匹配上的位置按序配对，
  未匹配元素分别记 ``removed``（仅左）/ ``added``（仅右），子树不展开。
  LCS 长度相同的多个最优对齐中，① 元素深度相等时优先配对（沿对角线走）；
  ② 否则优先跳过左侧元素（左元素记删除）。
- 每项带 ``op``（``added`` / ``removed`` / ``replaced``）、存在侧的值
  （``left`` / ``right``，缺失侧**省略该字段，绝不用 null 代替缺失**）
  与原数组下标对应的 JSON Pointer 路径（``~0`` / ``~1`` 转义，根为 ``""``）。

规模保护
--------
事件 raw 是导入时的可信 JSON，但仍可能很大 / 很深。为避免一份病态载荷
耗尽资源：嵌套深度超过 ``MAX_DEPTH``、或两侧配对位置上数组 DP 单元数
超过 ``MAX_ARRAY_CELLS`` 时明确拒绝（调用方映射为 400），不做静默截断。
"""
from __future__ import annotations

import json
from typing import Any

from .db import db

# 单侧数组最长长度；两侧配对数组的 DP 单元上限（n*m，防病态大数组）
MAX_ARRAY_SIDE = 5000
MAX_ARRAY_CELLS = 2_000_000
# 允许的最大嵌套深度（对象 / 数组各算一层）
MAX_DEPTH = 200


class RawDiffError(ValueError):
    """输入可归因于请求侧的比较错误（载荷过大 / 过深）。"""


# --------------------------------------------------------------- 基础判定

def _type_name(v: Any) -> str:
    """JSON 类型名；bool 独立于 number（布尔不得与数值视为相等）。"""
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
    # raw 来自 json.loads，理论上不会出现非 JSON 类型
    return type(v).__name__


def deep_equal(a: Any, b: Any) -> bool:
    """按 JSON 语义的深度相等（显式栈实现，避免病态深嵌套触发递归崩溃）。

    - 类型不同即不等（bool 与 number 不同型，``true != 1``）；
    - 同为 number 时按数值比较（``1 == 1.0``，这是同一 JSON 数字）；
    - dict 键集合与逐键值相等、list 逐元素深度相等；
    - 不依赖 NaN：json.loads 得到的 NaN 属非标准扩展，NaN != NaN 自然不等。
    """
    stack: list[tuple[Any, Any]] = [(a, b)]
    while stack:
        x, y = stack.pop()
        if isinstance(x, bool) or isinstance(y, bool):
            if not (isinstance(x, bool) and isinstance(y, bool) and x == y):
                return False
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            if x != y:
                return False
            continue
        if _type_name(x) != _type_name(y):
            return False
        if isinstance(x, dict):
            if x.keys() != y.keys():
                return False
            for k in x:
                stack.append((x[k], y[k]))
        elif isinstance(x, list):
            if len(x) != len(y):
                return False
            for i in range(len(x)):
                stack.append((x[i], y[i]))
        elif x != y:
            return False
    return True


def _escape_token(token: str) -> str:
    """JSON Pointer 段转义：~ → ~0、/ → ~1（顺序不可颠倒）。"""
    return token.replace("~", "~0").replace("/", "~1")


def _ptr(base: str, segment: str) -> str:
    return f"{base}/{segment}"


# --------------------------------------------------------------- 主递归

def structural_diff(left: Any, right: Any) -> dict[str, Any]:
    """对两份已解析的 JSON 值做结构差异比较，返回差异明细与分类计数。

    显式栈遍历（嵌套容器才入栈），深度超过 ``MAX_DEPTH`` 抛 RawDiffError，
    不依赖 Python 调用栈；返回
    ``{"op_counts": {"added":x,"removed":y,"replaced":z},
    "differences": [...]}``；完全相同时空列表、计数全 0。输出为确定的文档
    前序：容器本层的单侧差异先于子层递归，对象键按字典序、数组按原下标。
    """
    out: list[dict[str, Any]] = []
    # 帧有两种：
    #   ("node", a, b, path, depth) —— 待比较的一对值
    #   ("emit", entry)             —— 已成型的差异项（保持入栈顺序输出）
    stack: list[tuple] = [("node", left, right, "", 0)]

    while stack:
        frame = stack.pop()
        if frame[0] == "emit":
            out.append(frame[1])
            continue
        _, a, b, path, depth = frame
        ta, tb = _type_name(a), _type_name(b)
        if ta != tb:
            # 类型不同（含 标量↔容器、object↔array、bool↔number）：替换一项，
            # 不展开任一子树。
            out.append(_entry("replaced", path, a, b))
            continue
        if ta in ("object", "array"):
            if depth >= MAX_DEPTH:
                raise RawDiffError("载荷嵌套过深，无法比较")
            # actions 按文档顺序（对象键字典序 / 数组下标）混合收集两类产物：
            #   ("emit", entry)       —— 已成型的单侧差异（子树不展开）
            #   ("node", a, b, path)  —— 两侧都有、需继续递归的配对
            actions: list[tuple] = []
            if ta == "object":
                _collect_object_children(a, b, path, actions)
            else:
                _collect_array_children(a, b, path, actions)
            # 后入先出：倒序压栈，弹出即为正序；emit 与 node 的交错顺序
            # 与 actions 一致，保证文档前序（如 /m/a 先于 /m/b/0 先于 /m/c）。
            for act in reversed(actions):
                if act[0] == "emit":
                    stack.append(("emit", act[1]))
                else:
                    stack.append(("node", act[1], act[2], act[3], depth + 1))
        elif a != b:
            # 同型标量（string / number / boolean / null）：值不等才替换
            out.append(_entry("replaced", path, a, b))

    counts = {"added": 0, "removed": 0, "replaced": 0}
    for d in out:
        counts[d["op"]] += 1
    return {"op_counts": counts, "differences": out}


def _collect_object_children(a: dict, b: dict, path: str,
                             actions: list[tuple]) -> None:
    """按字典序收集对象产物：单侧键记增删（子树不展开），共有键待递归。"""
    for key in sorted(set(a) | set(b), key=str):
        kp = _ptr(path, _escape_token(str(key)))
        if key not in b:
            actions.append(("emit", _entry("removed", kp, a[key], _MISSING)))
        elif key not in a:
            actions.append(("emit", _entry("added", kp, _MISSING, b[key])))
        else:
            actions.append(("node", a[key], b[key], kp))


def _collect_array_children(a: list, b: list, path: str,
                            actions: list[tuple]) -> None:
    """数组 LCS 对齐：相等元素配对（无差异），未匹配元素记增删（不展开）。

    LCS 只在深度相等的元素之间配对，不存在"配对但值不同"的元素，因此数组
    内部唯一可能的差异就是未匹配元素的整子树新增 / 删除；``actions`` 里
    不会出现 "node"。
    """
    n, m = len(a), len(b)
    if n > MAX_ARRAY_SIDE or m > MAX_ARRAY_SIDE or n * m > MAX_ARRAY_CELLS:
        raise RawDiffError(
            f"数组过大（{n}×{m}），超过 LCS 比较上限，"
            f"单侧至多 {MAX_ARRAY_SIDE} 个元素")

    # LCS 长度表，dp[i][j] = a[:i] 与 b[:j] 的最长公共子序列长度，
    # 相等判定为元素深度相等。
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        ai = a[i - 1]
        row = dp[i]
        prev = dp[i - 1]
        for j in range(1, m + 1):
            if deep_equal(ai, b[j - 1]):
                row[j] = prev[j - 1] + 1
            elif prev[j] >= row[j - 1]:
                # 平局（跳过左 / 跳过右 LCS 等长）时优先跳过左侧元素
                row[j] = prev[j]
            else:
                row[j] = row[j - 1]

    # 回溯产出（天然倒序，最后反转）。平局规则：
    # - 当前元素深度相等 → 一律配对（相等元素优先配对，即使跳过也等长）；
    # - 否则 dp[i-1][j] >= dp[i][j-1] → 跳过左（左元素删除）；否则跳过右。
    i, j = n, m
    reversed_items: list[tuple[str, int, int]] = []  # (kind, ai, bj)
    while i > 0 or j > 0:
        if i > 0 and j > 0 and deep_equal(a[i - 1], b[j - 1]):
            reversed_items.append(("match", i - 1, j - 1))
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or dp[i - 1][j] >= dp[i][j - 1]):
            reversed_items.append(("remove", i - 1, -1))
            i -= 1
        else:
            reversed_items.append(("add", -1, j - 1))
            j -= 1

    for kind, ai, bj in reversed(reversed_items):
        if kind == "match":
            # 深度相等的元素无差异；按规则 LCS 只在相等元素间配对，
            # 因此数组内部不会出现"配对但需替换"的元素。
            continue
        if kind == "remove":
            actions.append(("emit", _entry("removed", _ptr(path, str(ai)),
                                           a[ai], _MISSING)))
        else:
            actions.append(("emit", _entry("added", _ptr(path, str(bj)),
                                           _MISSING, b[bj])))


# 哨兵：表达"该侧缺失"。不能用 None —— 缺失与显式 JSON null 必须区分，
# 差异项里缺失侧直接省略字段，绝不用 null 代替缺失。
_MISSING = object()


def _entry(op: str, path: str, left: Any, right: Any) -> dict[str, Any]:
    """构造一条差异项；缺失侧省略对应字段，不以 null 表达缺失。

    ``added`` 项只有右侧值，``removed`` 项只有左侧值，``replaced`` 两侧
    都在（值本身可以是 JSON null，此时照常写 null）。
    """
    item: dict[str, Any] = {"op": op, "path": path}
    if left is not _MISSING:
        item["left"] = left
    if right is not _MISSING:
        item["right"] = right
    return item


# --------------------------------------------------------------- 取数入口

def load_event_raw(event_id: str) -> dict[str, Any]:
    """读取一条事件的库内 raw（已解析 JSON）。不存在抛 KeyError。"""
    with db() as conn:
        row = conn.execute(
            "SELECT id, raw FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise KeyError(event_id)
    try:
        return {"id": row["id"], "raw": json.loads(row["raw"])}
    except (ValueError, TypeError) as exc:
        raise RawDiffError(f"事件 {event_id} 的库内 raw 不是合法 JSON") from exc


def compare_event_raws(left_id: str, right_id: str) -> dict[str, Any]:
    """比较两条事件的原始载荷；只读，不修改记录。

    ID 为空 / 非字符串 → RawDiffError(400)；任一 ID 不存在 → KeyError，
    KeyError 的参数为 ``(event_id, side)``，由路由层拼成 404 说明。
    """
    if not isinstance(left_id, str) or not isinstance(right_id, str):
        raise RawDiffError("事件 ID 必须为字符串")
    left_id, right_id = left_id.strip(), right_id.strip()
    if not left_id or not right_id:
        raise RawDiffError("左 / 右事件 ID 均不能为空")

    # 一次连接把两侧 raw 都取出，任一缺失立即拒绝并说明侧与 ID
    with db() as conn:
        left_row = conn.execute(
            "SELECT id, raw FROM events WHERE id=?", (left_id,)).fetchone()
        right_row = conn.execute(
            "SELECT id, raw FROM events WHERE id=?", (right_id,)).fetchone()
    if left_row is None:
        raise KeyError(left_id, "left")
    if right_row is None:
        raise KeyError(right_id, "right")
    try:
        left_raw = json.loads(left_row["raw"])
        right_raw = json.loads(right_row["raw"])
    except (ValueError, TypeError) as exc:
        raise RawDiffError("库内 raw 不是合法 JSON，无法比较") from exc

    result = structural_diff(left_raw, right_raw)
    return {
        "comparison_type": "event-raw-diff",
        "left": {"event_id": left_row["id"]},
        "right": {"event_id": right_row["id"]},
        "identical": not result["differences"],
        "op_counts": result["op_counts"],
        "differences": result["differences"],
    }
