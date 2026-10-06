"""配置热应用（0.4.16）单元测试.

覆盖：configure 捕获思考预设、_apply_plugin_overrides 覆盖语义（含
thinking/text_thinking 首次接入插件配置通道——此前面板设置从未生效的
修复）、reconfigure 的 pending / torn_down / deferred 三态判定、
配置文件监视回调。不依赖真实游戏进程；非 Windows 跳过。
"""

from __future__ import annotations

import os
import sys
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

import pytest

# 非 Windows 跳过（service 依赖的窗口模块 import ctypes.wintypes）。
# guard 必须在 service import 之前；sys.platform 条件是 Ruff E402 允许的
# 前置语句（与 test_conveyor_bowling.py 同模式）。pvz 导入路径由 conftest 设置。
if sys.platform != "win32":
    pytest.skip(
        "PvZ Agent 插件测试依赖 Windows 运行时（窗口模块 import ctypes.wintypes）",
        allow_module_level=True,
    )

from service import PvZAgentService  # noqa: E402


def _svc() -> PvZAgentService:
    return PvZAgentService(logger=mock.Mock())


def _fake_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        sun=SimpleNamespace(enabled=True),
        grid_scan=SimpleNamespace(enabled=True),
        card_scan=SimpleNamespace(enabled=True),
        tool_call_mode="fc",
        mode="vision",
        vlm=SimpleNamespace(thinking="from-config-json"),
        text_vlm=SimpleNamespace(thinking="enabled"),
    )


# --------------------------------------------------------------------------- #
#  configure：思考预设进插件配置通道
# --------------------------------------------------------------------------- #

def test_configure_captures_thinking() -> None:
    s = _svc()
    s.configure({"thinking": "OpenAI_NATIVE", "text_thinking": " disabled "})
    assert s._thinking == "openai_native"
    assert s._text_thinking == "disabled"


def test_configure_thinking_defaults_empty() -> None:
    s = _svc()
    s.configure({})
    assert s._thinking == "" and s._text_thinking == ""


# --------------------------------------------------------------------------- #
#  _apply_plugin_overrides：覆盖语义
# --------------------------------------------------------------------------- #

def test_overrides_apply_thinking_and_mode() -> None:
    s = _svc()
    s.configure({"mode": "text", "tool_call_mode": "regex",
                 "thinking": "openai_native", "text_thinking": "gemini"})
    cfg = _fake_cfg()
    s._apply_plugin_overrides(cfg)
    assert cfg.vlm.thinking == "openai_native"        # 面板设置首次真正生效
    assert cfg.text_vlm.thinking == "gemini"
    assert cfg.mode == "text"                          # 插件模式覆盖 config.json
    assert cfg.tool_call_mode == "regex"
    assert cfg.sun.enabled is True                     # 开关默认开 → 保留


def test_overrides_empty_thinking_keeps_config_json() -> None:
    s = _svc()
    s.configure({})  # thinking 未设置 → 维持 config.json 值
    cfg = _fake_cfg()
    s._apply_plugin_overrides(cfg)
    assert cfg.vlm.thinking == "from-config-json"
    assert cfg.text_vlm.thinking == "enabled"


def test_overrides_respect_disabled_toggles() -> None:
    s = _svc()
    s.configure({"sun_auto_collect": False, "scan_grid_enabled": False})
    cfg = _fake_cfg()
    s._apply_plugin_overrides(cfg)
    assert cfg.sun.enabled is False
    assert cfg.grid_scan.enabled is False
    assert cfg.card_scan.enabled is True


# --------------------------------------------------------------------------- #
#  reconfigure：三态判定（不 import core 的路径）
# --------------------------------------------------------------------------- #

def test_reconfigure_pending_when_runtime_absent() -> None:
    """运行时未构建：热应用只刷轻量项，报告 pending（无 core 依赖）。"""
    s = _svc()
    s.reconfigure({"thinking": "disabled"})
    assert s._thinking == "disabled"
    assert s._planner is None


def test_reconfigure_mode_change_idle_tears_down() -> None:
    """未游玩时切模式：丢弃旧模式运行时，下次开始按新模式重建。"""
    s = _svc()
    s._mode = "text"
    s._runtime_mode = "text"
    s._planner = object()      # 模拟已构建
    s._vlm = object()
    result = s.reconfigure({"mode": "vision"})
    assert result["runtime"] == "torn_down"
    assert s._planner is None and s._vlm is None and s._cfg is None
    assert s._runtime_mode is None
    assert s._mode == "vision"  # 新模式已记录，下次构建生效


def test_reconfigure_mode_change_running_defers() -> None:
    """游玩中切模式：执行器结构不同，deferred 到下次开始游玩。"""
    s = _svc()
    s._mode = "text"
    s._runtime_mode = "text"
    planner = object()
    s._planner = planner
    s._phase = PvZAgentService.PHASE_RUNNING
    result = s.reconfigure({"mode": "vision"})
    assert result["runtime"] == "deferred"
    assert result["deferred"] == ["mode"]
    assert s._planner is planner  # 旧运行时保持游玩
    assert s._mode == "vision"


def test_reconfigure_lightweight_keys_hot_applied() -> None:
    """非模式变更 + 无运行时：轻量项直接生效且不触碰 core。"""
    s = _svc()
    s._import_core = mock.Mock(side_effect=AssertionError("不应触碰 core"))
    s.reconfigure({"screenshot_feed_interval": 3.0, "window_titles": ["测试窗口"]})
    assert s._feed_interval == 3.0
    assert s._window_titles == ["测试窗口"]


# --------------------------------------------------------------------------- #
#  配置文件监视（探针文件放 tests 目录：tempfile 在部分沙箱环境拒绝建目录）
# --------------------------------------------------------------------------- #

def _probe_path() -> Path:
    return Path(__file__).resolve().parent / "_watch_probe.toml"


def test_config_watch_fires_on_mtime_change() -> None:
    s = _svc()
    calls: list[int] = []
    path = _probe_path()
    try:
        path.write_text('[pvz_agent]\nmode = "text"\n', encoding="utf-8")
        s.set_config_watch(path, lambda: calls.append(1))
        s._check_config_watch()               # 首个 tick：只建立基线
        assert calls == []
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        s._check_config_watch()               # mtime 变化 → 回调
        assert calls == [1]
        s._check_config_watch()               # 基线已刷新 → 不再触发
        assert calls == [1]
    finally:
        path.unlink(missing_ok=True)


def test_config_watch_baseline_skips_first_tick() -> None:
    """注册监视时文件已是"旧"的：首个 tick 建立基线不触发（防启动即误报）。"""
    s = _svc()
    calls: list[int] = []
    path = _probe_path()
    try:
        path.write_text("[pvz_agent]\n", encoding="utf-8")
        s.set_config_watch(path, lambda: calls.append(1))
        s._check_config_watch()
        assert calls == []
    finally:
        path.unlink(missing_ok=True)
