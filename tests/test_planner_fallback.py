"""fc→legacy 降级修复测试（0.4.17）.

0.4.17 之前的 bug：游玩中把模型配置改坏（密钥/地址错），chat_with_tools
的瞬时异常（连接失败/401/超时）被 plan() 的 `except Exception` 当成
"provider 不支持 tools"——**永久**降级 legacy 文本模式。配置改回后
planner 卡在 legacy：模型不再输出可解析的 <tool_call>，每轮被降级成
wait，表现为"恢复后一直 wait 不动"。

修复：只有报错文本明确指向 tools/tool_choice 被服务端拒绝（能力缺陷）
才永久降级；瞬时错误原样上抛交上层重试。reconfigure 热替换 VLM 客户端
时调用 restore_native_tools() 重置降级状态（双向自愈）。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip(
        "PvZ Agent 插件测试依赖 Windows 运行时（executor/窗口模块 import ctypes.wintypes）",
        allow_module_level=True,
    )

# pvz_agent.planner 依赖 `from openai import OpenAI`（经 vlm.py）：环境没有
# openai 时（Windows 直跑 pytest）挂载内置副本；绝不能无条件插——Linux CI
# 上会遮蔽环境正常的 openai（vendored pydantic_core 是 Windows 编译的 .pyd）。
if importlib.util.find_spec("openai") is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pvz" / "vendor" / "openai_stack"))

from pvz_agent.planner import Planner  # noqa: E402
from pvz_agent.vlm import VLMError  # noqa: E402

_FC_SYS = "FC_NATIVE_SYSTEM_PROMPT"
_XML_SYS = "LEGACY_XML_SYSTEM_PROMPT"
_WAIT_CALL = '<tool_call>{"name": "wait", "arguments": {"time": 1}}</tool_call>'


def _planner(vlm: mock.Mock) -> Planner:
    return Planner(
        vlm=vlm, system_prompt=_FC_SYS, max_rounds=3, mime="image/png",
        system_prompt_xml=_XML_SYS, include_image=False, tool_call_mode="fc",
    )


def test_transient_error_raises_and_stays_fc() -> None:
    """连接失败等瞬时错误：原样上抛，不降级、system 不被换。"""
    vlm = mock.Mock()
    vlm.chat_with_tools = mock.Mock(
        side_effect=VLMError("VLM 调用失败（重试 2 次）: Connection error."))
    p = _planner(vlm)
    with pytest.raises(VLMError):
        p.plan("", "状态")
    assert p._legacy_prompt_used is False
    assert p._history[0]["content"] == _FC_SYS


def test_auth_error_raises_and_stays_fc() -> None:
    """401/限流等也是瞬时（配置可修复）错误：不降级。"""
    vlm = mock.Mock()
    vlm.chat_with_tools = mock.Mock(
        side_effect=VLMError("VLM 调用失败（重试 2 次）: Error code: 401 - invalid api key"))
    p = _planner(vlm)
    with pytest.raises(VLMError):
        p.plan("", "状态")
    assert p._legacy_prompt_used is False


def test_tools_rejection_falls_back_permanently() -> None:
    """服务端明确拒绝 tools 参数：永久降级 legacy，之后走文本路径。"""
    vlm = mock.Mock()
    vlm.chat_with_tools = mock.Mock(
        side_effect=Exception("Error code: 400 - Unrecognized request argument supplied: tools"))
    vlm.chat_with_image = mock.Mock(return_value=(_WAIT_CALL, ""))
    p = _planner(vlm)
    calls, raw = p.plan("", "状态")   # 第 1 轮：降级 + 走 legacy 成功
    assert p._legacy_prompt_used is True
    assert p._history[0]["content"] == _XML_SYS
    assert calls and calls[0].name == "wait"
    assert raw == _WAIT_CALL
    p.plan("", "状态")                # 第 2 轮：直接 legacy，不再碰 fc
    assert vlm.chat_with_tools.call_count == 1
    assert vlm.chat_with_image.call_count == 2


def test_chinese_rejection_marker_falls_back() -> None:
    vlm = mock.Mock()
    vlm.chat_with_tools = mock.Mock(side_effect=Exception("该模型不支持工具调用"))
    vlm.chat_with_image = mock.Mock(return_value=(_WAIT_CALL, ""))
    p = _planner(vlm)
    p.plan("", "状态")
    assert p._legacy_prompt_used is True


def test_restore_native_tools_swaps_back_and_retries_fc() -> None:
    """restore_native_tools：flag 复位 + system 换回 fc 提示，重新走 fc。"""
    vlm = mock.Mock()
    vlm.chat_with_tools = mock.Mock(
        side_effect=Exception("tool_choice is not supported"))
    vlm.chat_with_image = mock.Mock(return_value=(_WAIT_CALL, ""))
    p = _planner(vlm)
    p.plan("", "状态")
    assert p._legacy_prompt_used is True

    vlm.chat_with_tools.side_effect = None   # 端点已恢复
    vlm.chat_with_tools.return_value = (
        [{"name": "wait", "arguments": {"time": 1}}], _WAIT_CALL)
    p.restore_native_tools()
    assert p._legacy_prompt_used is False
    assert p._history[0]["content"] == _FC_SYS
    calls, _ = p.plan("", "状态")            # 回到原生 fc
    assert calls[0].name == "wait"
    assert vlm.chat_with_tools.call_count == 2


def test_restore_native_tools_idempotent_when_native() -> None:
    p = _planner(mock.Mock())
    p.restore_native_tools()   # 本来就是 native：no-op，system 不动
    assert p._legacy_prompt_used is False
    assert p._history[0]["content"] == _FC_SYS


# --------------------------------------------------------------------------- #
#  service.reconfigure：热替换 VLM 时重置 legacy 降级
# --------------------------------------------------------------------------- #

def test_reconfigure_restores_native_tools() -> None:
    import unittest.mock as _m

    from service import PvZAgentService

    s = PvZAgentService(logger=_m.Mock())
    restore = _m.Mock()
    fake_planner = _m.Mock()
    fake_planner.vlm = "old-vlm"
    fake_planner.restore_native_tools = restore
    s._planner = fake_planner
    s._runtime_mode = "text"
    s._mode = "text"
    s._phase = PvZAgentService.PHASE_IDLE

    result = s.reconfigure({
        "mode": "text",
        "api_base_url": "http://localhost/v1", "api_key": "k", "api_model": "m",
        "text_api_base_url": "http://localhost/v1", "text_api_key": "k2", "text_api_model": "m",
    })
    assert result.get("hot_applied") is True
    assert fake_planner.vlm != "old-vlm"      # VLM 客户端已热替换
    restore.assert_called_once()               # legacy 降级已重置
