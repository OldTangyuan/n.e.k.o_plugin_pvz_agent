"""Agent B：规划执行者（截图 + 文本历史 → 原生 function-call 动作，维护文本历史）。

优先用 OpenAI 原生 function calling（``tools`` → 结构化 ``tool_calls``），
provider 不支持时回退到文本 ``<tool_call>`` 解析（换 legacy system prompt）。
"""

from __future__ import annotations

from typing import Any

from .parser import ToolCall, parse_tool_calls
from .prompts import build_planner_tools
from .vlm import VLMClient


def _render_tool_calls(calls_raw: list[dict]) -> str:
    """把原生 tool_calls 渲染成文本，供历史反馈回填。"""
    parts = []
    for item in calls_raw:
        name = str(item.get("name", ""))
        args = item.get("arguments", {})
        if isinstance(args, dict):
            arg_str = ", ".join(f"{k}={v}" for k, v in args.items())
        else:
            arg_str = str(args)
        parts.append(f"调用 {name}({arg_str})")
    return "；".join(parts) or "[无动作]"


class Planner:
    """规划执行者。

    - 历史只存文本（system + assistant 输出 + user 反馈），不持久化截图；
      截图每轮重发，避免上下文被图片撑爆。
    - trim() 按轮数保留最近 max_rounds 轮，system 消息固定在第 0 条。
    - ``system_prompt_xml``：provider 不支持原生 tools 时回退用的 legacy 提示。
    """

    def __init__(
        self,
        vlm: VLMClient,
        system_prompt: str,
        max_rounds: int = 3,
        mime: str = "image/png",
        system_prompt_xml: str | None = None,
        include_image: bool = True,
        tools_builder: Any = None,
        tool_call_mode: str = "fc",
    ) -> None:
        self.vlm = vlm
        self.max_rounds = max(1, max_rounds)
        self._mime = mime
        # 纯文本模式（include_image=False）：决策只看文本（内存状态），不喂截图。
        self._include_image = include_image
        # 工具调用模式："regex"=简化正则（默认，模型输出 <tool_call>JSON，不依赖原生函数调用）；
        # "fc"=OpenAI 原生 function calling（tools 参数）。
        self._tool_call_mode = tool_call_mode if tool_call_mode in ("regex", "fc") else "regex"
        # 工具 schema 构造器：视觉模式用 build_planner_tools；文本模式用 build_planner_tools_text
        # （后者去掉鼠标动作、新增 win_level）。默认视觉工具集。仅 fc 模式使用。
        self._tools_builder = tools_builder or build_planner_tools
        self._system_prompt = system_prompt
        self._system_prompt_xml = system_prompt_xml or system_prompt
        self._history: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        self._legacy_prompt_used = False
        # 最近一次 plan() 的结果诊断，供上层（主循环）在"无动作"时通报主模型
        self.last_status = "ok"
        self.last_status_text = ""

    # ------------------------------------------------------------------ #
    #  主流程
    # ------------------------------------------------------------------ #
    def plan(self, img_b64: str, user_text: str) -> tuple[list[ToolCall], str]:
        """发一次 VLM 请求，返回 (动作列表, 原始文本)。

        工具调用模式（``tool_call_mode``）：
        - ``"regex"``（默认，简化）：直接走文本路径——模型输出
          ``<tool_call>{"name":..., "arguments":{...}}</tool_call>``，用正则提取
          （``parse_tool_calls``，与 LLM_PvZ_Player 的 parser 一致），不依赖原生函数调用。
        - ``"fc"``：优先原生 function calling，模型返回 tool_calls 转成 ToolCall；
          provider 不支持 tools 抛异常 → 换 legacy system 回退文本模式。

        图片仅放本轮请求，不写回历史（历史保持纯文本）。
        """
        self.last_status = "ok"
        self.last_status_text = ""
        if self._tool_call_mode != "fc":
            # 简化正则模式：直接走文本 <tool_call> 提取，不传 tools。
            content, _reasoning = self.vlm.chat_with_image(
                img_b64=img_b64,
                history=self._history,
                user_text=user_text,
                include_image=self._include_image,
                mime=self._mime,
            )
            return self._finish_text(content)

        tools = self._tools_builder()
        if not self._legacy_prompt_used:
            try:
                calls_raw, content = self.vlm.chat_with_tools(
                    img_b64=img_b64,
                    history=self._history,
                    user_text=user_text,
                    tools=tools,
                    mime=self._mime,
                    include_image=self._include_image,
                )
            except Exception:
                # 原生工具不可用（provider 不支持 tools 等）→ 永久回退文本模式
                self._use_legacy_prompt()
            else:
                if calls_raw:
                    calls = [ToolCall(name=item["name"], arguments=item["arguments"]) for item in calls_raw]
                    return calls, _render_tool_calls(calls_raw)
                # 模型没走工具（只回文本）→ 兜底解析文本动作
                return self._finish_text(content)

        # 文本 <tool_call> 回退模式（legacy system prompt）
        content, _reasoning = self.vlm.chat_with_image(
            img_b64=img_b64,
            history=self._history,
            user_text=user_text,
            include_image=self._include_image,
            mime=self._mime,
        )
        return self._finish_text(content)

    def _finish_text(self, content: str) -> tuple[list[ToolCall], str]:
        """文本兜底路径：解析 <tool_call>，解析失败时记录 last_status 供上层通报。

        模型只回了可读文本但没出工具调用（如"等阳光""先观察"）时，**降级为 wait**，
        避免"未解析到动作"死轮——wait 不操作游戏，只是让主循环等一下再继续。
        """
        parsed = parse_tool_calls(content)
        if parsed:
            self.last_status = "ok"
            return parsed, content
        text = (content or "").strip()
        if text:
            # 有文本无动作 → 降级 wait（保持循环前进；下轮模型可再决策）
            self.last_status = "ok"
            self.last_status_text = "模型只回了文本未出动作，已降级为 wait"
            return [ToolCall("wait", {"time": 1.5})], content
        self.last_status = "empty"
        self.last_status_text = "模型本轮没有任何输出（无工具调用也无文本）"
        return [], content

    # ------------------------------------------------------------------ #
    #  历史维护
    # ------------------------------------------------------------------ #
    def add_assistant(self, text: str) -> None:
        """把模型本轮原始输出加入历史。"""
        if text and text.strip():
            self._history.append({"role": "assistant", "content": text.strip()})
            self._trim()

    def add_feedback(self, text: str) -> None:
        """把执行反馈（文本化）加入历史。"""
        if text and text.strip():
            self._history.append({"role": "user", "content": text.strip()})
            self._trim()

    def add_user_note(self, text: str) -> None:
        """注入一条非反馈的用户提示（如目标变更、手动指令）。"""
        if text and text.strip():
            self._history.append({"role": "user", "content": f"[指令] {text.strip()}"})
            self._trim()

    def _use_legacy_prompt(self) -> None:
        """把 system 换成文本 <tool_call> 回退提示（幂等，只在原生失败时触发）。"""
        if self._legacy_prompt_used:
            return
        if self._history and self._system_prompt_xml:
            self._history[0]["content"] = self._system_prompt_xml
        self._legacy_prompt_used = True

    def _trim(self) -> None:
        """保留 system + 最近 max_rounds 轮（一轮 ≈ user+assistant+反馈 3 条消息）。"""
        system = self._history[0]
        rest = self._history[1:]
        max_msgs = self.max_rounds * 3
        if len(rest) > max_msgs:
            rest = rest[-max_msgs:]
        self._history = [system] + rest

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def reset(self) -> None:
        """清空历史，仅保留 system。"""
        system = self._history[0]
        self._history = [system]
