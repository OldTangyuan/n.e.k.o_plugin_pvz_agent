"""VLM 输出解析：提取工具调用动作指令 与 <compact> 折叠标记。

解析多格式容错（参考 LLM_PvZ_Player parser，并扩展健壮性）：
1. ``<tool_call>{"name":..,"arguments":{..}}</tool_call>``（原生工具名）或旧式
   ``{"action":..}``；
2. 整段直接是 JSON 对象/数组（原生 name 或旧 action）；
3. 文本中内嵌的 JSON 对象（模型把 tool_call 写进代码块 / 漏掉标签 / 夹杂在思考里）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """解析后的工具调用。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"ToolCall(name={self.name}, args={self.arguments})"


_tool_call_re = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def _try_loads(s: str) -> Any:
    """宽容 JSON 解析；失败返回 None。"""
    try:
        return json.loads(s.strip())
    except (json.JSONDecodeError, TypeError):
        return None


def _toolcall_from_dict(data: dict) -> ToolCall | None:
    """dict → ToolCall：原生 {name, arguments} 或旧式 {action}；都不是返回 None。"""
    if "name" in data:
        args = data.get("arguments", {})
        if not isinstance(args, dict):
            args = {"_value": args} if args is not None else {}
        return ToolCall(str(data.get("name", "")), dict(args))
    if "action" in data:
        return ToolCall("computer_use", dict(data))
    return None


def _toolcall_from_json(raw: str) -> ToolCall | None:
    """JSON 文本 → 第一个可用的 ToolCall（对象或数组中含 name/action 的元素）。"""
    data = _try_loads(raw)
    if isinstance(data, dict):
        return _toolcall_from_dict(data)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                tc = _toolcall_from_dict(item)
                if tc is not None:
                    return tc
    return None


def _iter_json_objects(text: str):
    """迭代文本中所有**顶层** {...} JSON 对象（括号配对 + 字符串感知）。

    用于从"思考 + 代码块 + 夹杂说明"的输出里捞出工具调用 JSON。
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start:i + 1]
                start = -1
    if depth > 0 and start >= 0:  # 括号未闭合，退化截取到结尾
        yield text[start:]


def parse_tool_calls(text: str) -> list[ToolCall]:
    """从 VLM 输出文本中解析工具调用（多格式容错）。

    Args:
        text: VLM 返回的原始文本。

    Returns:
        ToolCall 列表，无匹配则返回空列表。
    """
    calls: list[ToolCall] = []
    if not text or not text.strip():
        return calls

    # 1. <tool_call>...</tool_call> 块（参考项目逻辑）
    for match in _tool_call_re.finditer(text):
        tc = _toolcall_from_json(match.group(1))
        if tc is not None:
            calls.append(tc)
    if calls:
        return calls

    # 2. 整段就是 JSON：原生 {"name":..,"arguments":..} / 旧式 {"action":..} / 数组
    data = _try_loads(text.strip())
    if isinstance(data, dict):
        tc = _toolcall_from_dict(data)
        if tc is not None:
            return [tc]
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                tc = _toolcall_from_dict(item)
                if tc is not None:
                    calls.append(tc)
        if calls:
            return calls

    # 3. 文本中内嵌的 JSON 对象（漏标签 / 代码块 / 夹杂思考）
    seen: set[tuple[str, str]] = set()
    for span in _iter_json_objects(text):
        tc = _toolcall_from_json(span)
        if tc is not None:
            key = (tc.name, json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False))
            if key not in seen:
                seen.add(key)
                calls.append(tc)

    return calls


_compact_re = re.compile(r"<compact>\s*(.*?)\s*</compact>", re.DOTALL)


def parse_compact(text: str) -> str | None:
    """检测并提取 <compact> 摘要内容；无标记返回 None。"""
    m = _compact_re.search(text)
    if not m:
        return None
    summary = m.group(1).strip()
    return summary if summary else None
