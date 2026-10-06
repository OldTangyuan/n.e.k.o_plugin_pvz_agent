"""思考参数预设 + 参数被拒自动降级测试（0.4.13）.

背景：各端点思维链参数互不兼容（DS 的 thinking.type / qwen 的
enable_thinking / OpenAI 原生 reasoning_effort）。预设选错 provider 时
服务端 400 "Unrecognized request argument"——0.4.13 起按报错文本自动
降级请求参数（撤思考参数 → max_tokens 换 max_completion_tokens → 去掉
temperature）并立即重试，降级不消耗重试次数。

另修：Gemini 预设此前多包一层 extra_body，请求出现嵌套 extra_body
（Gemini 端点直接 400）。

只测纯逻辑，不依赖真实游戏进程；非 Windows 跳过。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

import pytest

if sys.platform != "win32":
    pytest.skip(
        "PvZ Agent 插件测试依赖 Windows 运行时（executor 模块 import ctypes.wintypes）",
        allow_module_level=True,
    )

if importlib.util.find_spec("openai") is None:
    # 运行时由 service.py 把内置副本 pvz/vendor/openai_stack 挂进 sys.path；
    # 仅在环境没有 openai 时（Windows 直跑 pytest）挂载 vendored 副本。
    # 绝不能无条件插——Linux CI 上会遮蔽环境正常的 openai（vendored
    # pydantic_core 是 Windows 编译的 .pyd，加载必炸）。
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pvz" / "vendor" / "openai_stack"))

from pvz_agent.config import VLMConfig  # noqa: E402
from pvz_agent.vlm import THINKING_EXTRA_BODY_PRESETS, VLMClient  # noqa: E402


def _vlm(thinking: str = "disabled", retries: int = 2) -> VLMClient:
    v = VLMClient.__new__(VLMClient)
    v.cfg = VLMConfig(
        base_url="http://localhost", api_key="k", model="m",
        max_output_tokens=512, temperature=0.3,
        retries=retries, retry_delay=0, timeout=5, thinking=thinking,
    )
    v._client = mock.Mock(name="OpenAI")
    v.last_prompt_tokens = 0
    v.last_tool_parse = {}
    return v


def _kwargs(**over):
    kw = {"extra_body": {"thinking": {"type": "disabled"}},
          "max_tokens": 512, "temperature": 0.3}
    kw.update(over)
    return kw


# --------------------------------------------------------------------------- #
#  预设表
# --------------------------------------------------------------------------- #

def test_core_presets() -> None:
    assert THINKING_EXTRA_BODY_PRESETS["disabled"] == {"thinking": {"type": "disabled"}}
    assert THINKING_EXTRA_BODY_PRESETS["openai"] == {"enable_thinking": False}
    assert THINKING_EXTRA_BODY_PRESETS["openai_native"] == {"reasoning_effort": "none"}


def test_gemini_preset_no_double_wrap() -> None:
    """Gemini 预设不得再包一层 extra_body（0.4.12 前的嵌套 bug → 端点 400）。"""
    kw = _vlm("gemini")._request_kwargs()
    assert "extra_body" in kw
    assert "extra_body" not in kw["extra_body"]
    assert kw["extra_body"]["google"]["thinking_config"]["thinking_budget"] == 0


def test_unknown_preset_sends_nothing() -> None:
    assert _vlm("彻底瞎填")._request_kwargs() == {}
    assert _vlm("")._request_kwargs() == {}


# --------------------------------------------------------------------------- #
#  _degrade_kwargs：按报错文本降级
# --------------------------------------------------------------------------- #

def test_degrade_strips_extra_body_on_unrecognized_thinking() -> None:
    kw = _kwargs()
    changed = VLMClient._degrade_kwargs(
        kw, Exception("Unrecognized request argument supplied: thinking"))
    assert changed
    assert "extra_body" not in kw
    assert kw["max_tokens"] == 512 and kw["temperature"] == 0.3


def test_degrade_strips_extra_body_on_enable_thinking() -> None:
    kw = _kwargs(extra_body={"enable_thinking": False})
    changed = VLMClient._degrade_kwargs(
        kw, Exception("400 Unrecognized request argument supplied: enable_thinking"))
    assert changed and "extra_body" not in kw


def test_degrade_swaps_max_completion_tokens() -> None:
    kw = _kwargs()
    changed = VLMClient._degrade_kwargs(
        kw, Exception("max_tokens is not supported with this model."
                      " Use max_completion_tokens instead."))
    assert changed
    assert "max_tokens" not in kw and kw["max_completion_tokens"] == 512


def test_degrade_strips_temperature() -> None:
    kw = _kwargs()
    changed = VLMClient._degrade_kwargs(
        kw, Exception("temperature does not support 0.3 with this model"))
    assert changed and "temperature" not in kw


def test_degrade_noop_on_unrelated_error() -> None:
    kw = _kwargs()
    snapshot = dict(kw)
    assert not VLMClient._degrade_kwargs(kw, Exception("Connection reset by peer"))
    assert kw == snapshot


# --------------------------------------------------------------------------- #
#  重试流：降级不消耗重试次数
# --------------------------------------------------------------------------- #

def _ok_resp() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="好的", reasoning_content=""))],
        usage=None,
    )


def test_retry_flow_degrades_then_succeeds() -> None:
    """第 1 次思考参数被拒 → 降级后第 2 次成功，返回内容正常。"""
    v = _vlm("disabled", retries=2)
    v._client.chat.completions.create = mock.Mock(side_effect=[
        Exception("Unrecognized request argument supplied: thinking"), _ok_resp(),
    ])
    content, reasoning = v.chat_with_image("", [], "打", include_image=False)
    assert (content, reasoning) == ("好的", "")
    calls = v._client.chat.completions.create.call_args_list
    assert len(calls) == 2
    assert "extra_body" in calls[0].kwargs          # 第 1 次按预设发送
    assert "extra_body" not in calls[1].kwargs      # 第 2 次已降级
    assert calls[1].kwargs["max_tokens"] == 512     # 其余参数保留


def test_retry_flow_three_degrades_do_not_consume_retries() -> None:
    """思考参数→max_tokens→temperature 三连降级都不计次数，retries=2 也够用。"""
    v = _vlm("disabled", retries=2)
    v._client.chat.completions.create = mock.Mock(side_effect=[
        Exception("Unrecognized request argument supplied: thinking"),
        Exception("max_tokens is not supported with this model. Use max_completion_tokens"),
        Exception("temperature does not support 0.3 with this model"),
        _ok_resp(),
    ])
    content, _ = v.chat_with_image("", [], "打", include_image=False)
    assert content == "好的"
    calls = v._client.chat.completions.create.call_args_list
    assert len(calls) == 4
    final = calls[3].kwargs
    assert "extra_body" not in final
    assert "max_tokens" not in final and final["max_completion_tokens"] == 512
    assert "temperature" not in final


def test_retry_flow_exhausts_and_raises() -> None:
    """非参数错误照常退避重试，耗尽后抛 VLMError（回归）。"""
    from pvz_agent.vlm import VLMError
    v = _vlm("disabled", retries=2)
    v._client.chat.completions.create = mock.Mock(
        side_effect=Exception("Connection reset by peer"))
    with pytest.raises(VLMError, match="重试 2 次"):
        v.chat_with_image("", [], "打", include_image=False)
