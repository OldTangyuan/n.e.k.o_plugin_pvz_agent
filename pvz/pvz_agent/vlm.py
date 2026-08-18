"""VLM 客户端（openai SDK）：支持图片消息，内置重试。"""

from __future__ import annotations

import json
import time
from typing import Any

from openai import OpenAI

from .config import VLMConfig


class VLMError(RuntimeError):
    """VLM 调用失败。"""


class VLMClient:
    """OpenAI 兼容接口的 VLM 客户端。

    - 历史消息 content 为纯文本字符串。
    - 图片只放每轮最新的 user 消息（content 数组 [image_url, text]），控制 token。
    - 内置重试（指数退避）。
    """

    def __init__(self, cfg: VLMConfig) -> None:
        self.cfg = cfg
        self._client = OpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=cfg.timeout,
        )
        self.last_prompt_tokens: int = 0
        # 最近一次工具调用的解析诊断（calls/malformed_args/dropped），供排障
        self.last_tool_parse: dict[str, Any] = {}

    def _request_kwargs(self) -> dict:
        """构造请求附加参数（含可选的思维链控制）。

        - ``thinking="disabled"``：关闭推理（如 kimi-k2.5 默认开长思维链会拖慢，视觉模式常关）；
        - ``thinking="enabled"``：强制开启推理（纯文本模式默认开，让模型多思考再决策）；
        - 其它值：不传 extra_body，交给模型/服务端默认。
        """
        kw: dict = {}
        thinking = (self.cfg.thinking or "").strip().lower()
        if thinking == "disabled":
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
        elif thinking == "enabled":
            kw["extra_body"] = {"thinking": {"type": "enabled"}}
        return kw

    # ------------------------------------------------------------------ #
    #  主入口
    # ------------------------------------------------------------------ #
    def chat_with_image(
        self,
        img_b64: str,
        history: list[dict[str, Any]],
        user_text: str,
        include_image: bool = True,
        mime: str = "image/png",
    ) -> tuple[str, str]:
        """发送聊天请求，返回 (content, reasoning_content)。

        Args:
            img_b64: 最新截图的 base64 字符串。
            history: 纯文本历史消息（system / assistant / user）。
            user_text: 本轮附加的文本提示（追加在图片之后）。
            include_image: 是否携带图片（False 则纯文本模式）。
            mime: 图片 MIME 类型。

        Returns:
            (content, reasoning) 元组；content 可能为空字符串。

        Raises:
            VLMError: 重试耗尽后抛出。
        """
        messages = self._build_messages(img_b64, history, user_text, include_image, mime)

        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.cfg.model,
                    messages=messages,
                    max_tokens=self.cfg.max_output_tokens,
                    temperature=self.cfg.temperature,
                    **self._request_kwargs(),
                )
                usage = getattr(resp, "usage", None)
                if usage is not None and usage.prompt_tokens:
                    self.last_prompt_tokens = usage.prompt_tokens

                msg = resp.choices[0].message
                content = (msg.content or "") if hasattr(msg, "content") else ""
                reasoning = ""
                if hasattr(msg, "reasoning_content") and msg.reasoning_content:
                    reasoning = msg.reasoning_content or ""
                return content, reasoning

            except Exception as exc:
                last_exc = exc
                if attempt < self.cfg.retries:
                    delay = self.cfg.retry_delay * (2 ** (attempt - 1))
                    print(f"[VLM] 第 {attempt} 次请求失败: {exc}，{delay:.0f} 秒后重试...")
                    time.sleep(delay)

        raise VLMError(f"VLM 调用失败（重试 {self.cfg.retries} 次）: {last_exc}") from last_exc

    # ------------------------------------------------------------------ #
    #  原生 function calling 模式
    # ------------------------------------------------------------------ #
    def chat_with_tools(
        self,
        img_b64: str,
        history: list[dict[str, Any]],
        user_text: str,
        tools: list[dict],
        mime: str = "image/png",
        include_image: bool = True,
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """发送带原生 function calling 的请求，返回 (tool_calls, content)。

        Args:
            img_b64: 最新截图的 base64 字符串（``include_image=False`` 时忽略）。
            history: 纯文本历史消息。
            user_text: 本轮附加的文本提示。
            tools: OpenAI 风格 tools 列表（``build_planner_tools`` 产物）。
            mime: 图片 MIME 类型。
            include_image: 是否携带图片。False 用于纯文本模式（决策只依赖内存状态文本）。

        Returns:
            (tool_calls, content)：
            - tool_calls: ``[{"name": str, "arguments": dict}, ...]``；
              模型未产生工具调用时为 ``None``。
            - content: 模型文本内容（原生调用模式下通常为空或简短说明）。

        Raises:
            VLMError: 重试耗尽后抛出（provider 不支持 ``tools`` 参数时由
            Planner 捕获并回退到文本模式）。
        """
        messages = self._build_messages(img_b64, history, user_text, include_image, mime)

        # 工具选择策略：默认强制每轮调用工具（required），避免模型只回文本不行动。
        # 个别 provider 不支持 "required"，调用失败时降级 "auto" 重试。
        tool_choice = (self.cfg.tool_choice or "").strip()
        if tool_choice not in ("auto", "required", "none"):
            tool_choice = "required"
        used_choice = tool_choice

        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.cfg.model,
                    messages=messages,
                    max_tokens=self.cfg.max_output_tokens,
                    temperature=self.cfg.temperature,
                    tools=tools,
                    tool_choice=used_choice,
                    **self._request_kwargs(),
                )
                usage = getattr(resp, "usage", None)
                if usage is not None and usage.prompt_tokens:
                    self.last_prompt_tokens = usage.prompt_tokens

                msg = resp.choices[0].message
                content = (msg.content or "") if hasattr(msg, "content") else ""
                calls, malformed, dropped = self._extract_tool_calls(msg)
                self.last_tool_parse = {
                    "calls": len(calls),
                    "malformed_args": malformed,
                    "dropped": dropped,
                }
                return (calls or None), content

            except Exception as exc:
                last_exc = exc
                if used_choice == "required":
                    # provider 不支持 "required" → 降级 "auto" 立即重试（不等待退避）
                    used_choice = "auto"
                    print("[VLM] tool_choice='required' 不被当前 provider 支持，已降级 'auto' 重试")
                    continue
                if attempt < self.cfg.retries:
                    delay = self.cfg.retry_delay * (2 ** (attempt - 1))
                    print(f"[VLM] 第 {attempt} 次请求失败: {exc}，{delay:.0f} 秒后重试...")
                    time.sleep(delay)

        raise VLMError(f"VLM 调用失败（重试 {self.cfg.retries} 次）: {last_exc}") from last_exc

    @staticmethod
    def _extract_tool_calls(msg: Any) -> tuple[list[dict[str, Any]], int, int]:
        """从响应 message 提取原生 tool_calls，返回 (calls, malformed_args, dropped)。

        - ``calls``: ``[{"name": str, "arguments": dict}]``；
        - ``malformed_args``: arguments 既不是 dict 也解析不了 JSON 的条数；
        - ``dropped``: 因缺 function / 非 function 类型被丢弃的条数。

        兼容 object 与 dict 两种形态的 tool_calls / function（部分 provider
        不按 openai SDK 的 pydantic 模型返回），并兜底 args 为 dict。
        """
        calls: list[dict[str, Any]] = []
        malformed = 0
        dropped = 0
        raw_calls = getattr(msg, "tool_calls", None)
        if raw_calls is None and isinstance(msg, dict):
            raw_calls = msg.get("tool_calls")
        if not raw_calls:
            return calls, malformed, dropped

        for tc in raw_calls:
            if isinstance(tc, dict):
                tc_type = tc.get("type", "function")
                fn = tc.get("function")
            else:
                tc_type = getattr(tc, "type", "function")
                fn = getattr(tc, "function", None)
            if tc_type not in (None, "function"):
                dropped += 1
                continue
            if fn is None:
                dropped += 1
                continue
            if isinstance(fn, dict):
                name = str(fn.get("name") or "")
                raw_args = fn.get("arguments")
            else:
                name = str(getattr(fn, "name", None) or "")
                raw_args = getattr(fn, "arguments", None)

            if isinstance(raw_args, dict):
                args = dict(raw_args)
            else:
                try:
                    args = json.loads(raw_args or "{}")
                    if not isinstance(args, dict):
                        args = {"_value": args} if args is not None else {}
                except (json.JSONDecodeError, TypeError):
                    malformed += 1
                    args = {}
            calls.append({"name": name, "arguments": args})
        return calls, malformed, dropped

    # ------------------------------------------------------------------ #
    #  纯文本（无图）模式，供其他调用
    # ------------------------------------------------------------------ #
    def chat_text(
        self,
        history: list[dict[str, Any]],
    ) -> tuple[str, str]:
        """纯文本请求（如上下文整理、追加推理）。"""
        messages: list[dict[str, Any]] = []
        for m in history:
            content = m.get("content", "")
            if isinstance(content, list):
                texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                content = "\n".join(texts)
            messages.append({"role": m.get("role", "user"), "content": content})

        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.cfg.model,
                    messages=messages,
                    max_tokens=self.cfg.max_output_tokens,
                    temperature=self.cfg.temperature,
                )
                msg = resp.choices[0].message
                content = (msg.content or "") if hasattr(msg, "content") else ""
                reasoning = ""
                if hasattr(msg, "reasoning_content") and msg.reasoning_content:
                    reasoning = msg.reasoning_content or ""
                return content, reasoning
            except Exception as exc:
                last_exc = exc
                if attempt < self.cfg.retries:
                    delay = self.cfg.retry_delay * (2 ** (attempt - 1))
                    print(f"[VLM] 第 {attempt} 次请求失败: {exc}，{delay:.0f} 秒后重试...")
                    time.sleep(delay)
        raise VLMError(f"VLM 调用失败（重试 {self.cfg.retries} 次）: {last_exc}") from last_exc

    # ------------------------------------------------------------------ #
    #  消息构建
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_messages(
        img_b64: str,
        history: list[dict[str, Any]],
        user_text: str,
        include_image: bool,
        mime: str,
    ) -> list[dict[str, Any]]:
        """组装请求消息：system/历史（纯文本字符串） + 最新 user（图片+文本）。

        加强的图片清理保证：
        - 历史消息无论 content 是字符串还是数组，一律只保留文本部分；
          任何历史里的 image_url 都会被剥离（即使调用方误传了图片数组）。
        - 只有最新一条 user 消息携带本轮截图（img_b64）。
        这确保 VLM 上下文里同一时刻至多一张图，避免图片 token 累积拖慢响应。
        """
        messages: list[dict[str, Any]] = []
        for m in history:
            content = m.get("content", "")
            if isinstance(content, list):
                # 剥离历史里的所有图片，只留文本
                texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                content = "\n".join(texts)
            if content and str(content).strip():
                messages.append({"role": m.get("role", "user"), "content": str(content)})

        if include_image and img_b64:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    {"type": "text", "text": user_text},
                ],
            })
        else:
            messages.append({"role": "user", "content": user_text})

        return messages
