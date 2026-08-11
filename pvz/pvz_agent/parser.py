"""VLM 输出解析：提取 <tool_call> 动作指令 与 <compact> 折叠标记。"""

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


def parse_tool_calls(text: str) -> list[ToolCall]:
    """从 VLM 输出文本中解析所有 <tool_call> 块（含 JSON 兜底）。

    Args:
        text: VLM 返回的原始文本。

    Returns:
        ToolCall 列表，无匹配则返回空列表。
    """
    calls: list[ToolCall] = []
    for match in _tool_call_re.finditer(text):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
            calls.append(ToolCall(
                name=str(data.get("name", "")),
                arguments=dict(data.get("arguments", {})),
            ))
        except json.JSONDecodeError:
            print(f"[解析] 跳过无法解析的 tool_call: {raw[:120]}...")
            continue

    # 兜底：有些模型不包 XML 标签，直接返回 JSON 对象或数组
    if not calls:
        stripped = text.strip()
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and "action" in data:
                calls.append(ToolCall("computer_use", data))
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "action" in item:
                        calls.append(ToolCall("computer_use", item))
        except json.JSONDecodeError:
            pass

    return calls


_compact_re = re.compile(r"<compact>\s*(.*?)\s*</compact>", re.DOTALL)


def parse_compact(text: str) -> str | None:
    """检测并提取 <compact> 摘要内容；无标记返回 None。"""
    m = _compact_re.search(text)
    if not m:
        return None
    summary = m.group(1).strip()
    return summary if summary else None
