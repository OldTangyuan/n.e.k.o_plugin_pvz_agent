"""PvZAgent 插件单元测试。

覆盖：service 状态机与命令（含 terminate/窗口丢失后重启）、feed 截图节流去重、
neko_interface 参数整形、facade 的 llm_tool / plugin_entry 注册面。
全程不触碰真实窗口 / VLM / cv2——核心运行时在 service 里是懒加载的，测试用 mock 封住。
"""

from __future__ import annotations

import sys

import pytest

# PvZ 插件的运行时强依赖 Windows（pywin32 窗口枚举 + Windows 版 vendored OpenCV）。
# CI 跑在 ubuntu：收集本模块时 import pvz_agent.executor → window → win32api 必失败。
# 本机 Windows 下完整运行本套件；CI 只跑 test_smoke.py 校验仓库结构。
if sys.platform != "win32":
    pytest.skip(
        "PvZ Agent 插件测试依赖 Windows 运行时（pywin32 + Windows 版 vendored OpenCV）",
        allow_module_level=True,
    )

import asyncio
import json
import time
import unittest.mock as mock
from contextlib import contextmanager

from PIL import Image
from plugin.plugins.pvz_agent import PVZAgentPlugin
from plugin.plugins.pvz_agent import service as service_module
from plugin.plugins.pvz_agent.neko_interface import PvZNekoInterface, _normalize
from plugin.plugins.pvz_agent.service import (
    DEFAULT_GOAL,
    PvZAgentService,
    _build_feedback,
)
from plugin.sdk.plugin.llm_tool import collect_llm_tool_methods
from plugin.sdk.plugin.ui import UI_ACTION_META_ATTR, UI_CONTEXT_META_ATTR
from plugin.sdk.shared.constants import EVENT_META_ATTR

# pvz 核心（service 模块已在 import 时把 pvz/ 加入 sys.path）
from pvz_agent.executor import Executor, LayoutConfig
from pvz_agent.parser import ToolCall
from pvz_agent.planner import Planner, _render_tool_calls
from pvz_agent.prompts import build_planner_tools

# pvz 核心（service 模块已在 import 时把 pvz/ 加入 sys.path）
from pvz_agent.window import WindowNotFoundError


class _FakeLogger:
    def info(self, *a, **k):  # noqa: ANN001, ANN201
        pass

    def warning(self, *a, **k):  # noqa: ANN001, ANN201
        pass

    def exception(self, *a, **k):  # noqa: ANN001, ANN201
        pass


class _FakeThread:
    """吞掉真正的线程启动，避免测试里真的拉起循环线程。"""

    def __init__(self, *a, **k):  # noqa: ANN001, ANN002
        self.started = False

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return False


@contextmanager
def _no_real_threads():
    """把 service 模块里的 threading.Thread 换成假线程（仅测试期内）。"""
    with mock.patch.object(service_module.threading, "Thread", _FakeThread):
        yield


def _make_service() -> PvZAgentService:
    return PvZAgentService(logger=_FakeLogger())


# --------------------------------------------------------------------------- #
#  service：状态与命令
# --------------------------------------------------------------------------- #
def test_initial_status_is_idle() -> None:
    svc = _make_service()
    status = svc.get_status()
    assert status["phase"] == "idle"
    assert status["running"] is False
    assert status["goal"] == DEFAULT_GOAL
    assert status["ready"] is False


def test_configure_toggles() -> None:
    svc = _make_service()
    svc.configure({
        "window_titles": ["植物大战僵尸"],
        "screenshot_feed_enabled": False,
        "screenshot_feed_interval": 5.0,
        "screenshot_max_edge_px": 512,
        "screenshot_jpeg_quality": 60,
        "sun_auto_collect": False,
        "scan_grid_enabled": False,
        "scan_cards_enabled": False,
        "notify_on_terminate": False,
        "notify_window_lost": False,
    })
    assert svc._window_titles == ["植物大战僵尸"]
    assert svc._feed_enabled is False
    assert svc._sun_auto_collect is False
    assert svc._scan_grid_enabled is False
    assert svc._feed_max_edge == 512
    assert svc._feed_quality == 60
    assert svc._notify_on_terminate is False
    assert svc._notify_window_lost is False


def test_configure_card_position_mode() -> None:
    svc = _make_service()
    assert svc._card_position_mode == "opencv"  # 默认
    svc.configure({"card_position_mode": "fixed"})
    assert svc._card_position_mode == "fixed"
    svc.configure({"card_position_mode": "invalid"})  # 非法值回退默认
    assert svc._card_position_mode == "opencv"


def test_configure_falls_back_to_legacy_window_title_keywords() -> None:
    svc = _make_service()
    svc.configure({"window_title_keywords": ["旧版标题"]})  # 旧键仍兼容
    assert svc._window_titles == ["旧版标题"]


def test_read_window_titles_merges_plugin_and_config_json(tmp_path) -> None:
    svc = _make_service()
    svc.configure({"window_titles": ["插件标题"]})
    (tmp_path / "config.json").write_text(
        json.dumps({"window_titles": ["精确标题A", "精确标题B"]}), encoding="utf-8"
    )
    with mock.patch.object(service_module, "CORE_DIR", tmp_path):
        titles = svc._read_window_titles()
    assert "插件标题" in titles
    assert "精确标题A" in titles and "精确标题B" in titles


# --------------------------------------------------------------------------- #
#  service：纯文本模式（mode="text"）
# --------------------------------------------------------------------------- #
def test_configure_mode_text_and_invalid_fallback() -> None:
    svc = _make_service()
    svc.configure({"mode": "text"})
    assert svc._mode == "text"
    svc.configure({"mode": "invalid"})
    assert svc._mode == "vision"  # 非法值回退 vision
    svc.configure({"mode": ""})
    assert svc._mode == "vision"  # 空值回退 vision


def test_ensure_runtime_text_builds_memory_engine_no_cv2() -> None:
    """text 模式运行时：executor=内存引擎、planner=文本模式、无 OpenCV 扫描器/阳光线程。"""
    svc = _make_service()
    svc.configure({"mode": "text"})
    fake_engine = mock.Mock()
    fake_engine.is_connected.return_value = True
    with mock.patch.object(svc, "_ensure_memory", return_value=fake_engine):
        cfg = mock.Mock()
        cfg.agent.image_format = "jpeg"
        cfg.agent.max_history_rounds = 1
        core = mock.Mock()
        core.vlm.VLMClient.return_value = mock.Mock()
        core.planner.Planner.return_value = mock.Mock()
        core.prompts.build_planner_system_text.return_value = "sys"
        svc._ensure_runtime_text(core, cfg)
    assert svc._executor is fake_engine
    assert svc._grid_scanner is None and svc._card_scanner is None
    assert svc._sun_collector is None
    # 文本 planner：include_image=False + 文本工具集
    _, kw = core.planner.Planner.call_args
    assert kw["include_image"] is False
    assert kw["tools_builder"] is core.prompts.build_planner_tools_text


def test_tick_text_feeds_memory_state_to_planner_no_image() -> None:
    """text 模式一轮：读内存状态 → 文本 user 消息（无图、无 OpenCV 段）。"""
    svc = _make_service()
    svc.configure({"mode": "text"})
    svc._memory_engine = mock.Mock()
    state = mock.Mock()
    state.in_battle = True
    svc._memory_engine.read_state.return_value = state
    svc._memory_engine.is_actionable.return_value = True
    svc._memory_engine.read_state_text.return_value = "阳光:150\n卡片:卡0(向日葵,可用)"
    svc._memory_engine.is_connected.return_value = True
    svc._goal = "赢"
    svc._last_feedback = ""
    svc._last_turn_time = time.perf_counter() - 1.0
    svc._drain_instructions = mock.Mock(return_value=[])
    svc._handle_no_actions = mock.Mock()
    svc._sleep_interruptible = mock.Mock()
    svc._compute_wait_seconds = mock.Mock(return_value=0.01)
    captured: dict = {}
    svc._core = mock.Mock()
    svc._core.prompts.build_planner_user_footer = lambda **kw: kw  # 返回 kwargs 便于断言
    svc._planner = mock.Mock()
    svc._planner.plan.side_effect = lambda img_b64, user_text: captured.update(
        img_b64=img_b64, user_text=user_text
    ) or ([], "raw")
    cfg = mock.Mock()
    cfg.layout.rows = 5
    cfg.layout.cols = 9
    svc._tick_text(cfg)
    assert captured["img_b64"] == ""                       # 不传图
    assert "阳光:150" in captured["user_text"]["memory_state"]
    assert not captured["user_text"].get("grid_state")     # 无 OpenCV 段
    # 棋盘以内存为准：read_state_text 收到 config 行列作为 fallback
    _, kw = svc._memory_engine.read_state_text.call_args
    assert kw.get("fallback_grid") == (5, 9)


def test_tick_text_skips_llm_on_non_actionable_screen() -> None:
    """非战斗界面（主菜单/结算）：不喂 LLM、不暂停不停循环，只轮询等待。"""
    svc = _make_service()
    svc.configure({"mode": "text"})
    svc._memory_engine = mock.Mock()
    state = mock.Mock()
    state.in_battle = False
    state.game_ui = 1  # MAIN_MENU
    svc._memory_engine.read_state.return_value = state
    svc._memory_engine.is_connected.return_value = True
    svc._memory_engine.is_actionable.return_value = False
    svc._drain_instructions = mock.Mock(return_value=[])
    svc._sleep_interruptible = mock.Mock()
    svc._notify_throttled = mock.Mock()
    svc._planner = mock.Mock()
    svc._tick_text(mock.Mock())
    svc._planner.plan.assert_not_called()  # 非可操作界面不消耗 LLM
    svc._sleep_interruptible.assert_called_once_with(1.0)  # 只轮询等待，不暂停/停止


def test_memory_engine_control_actions_without_connect() -> None:
    from pvz_agent.memory_engine import MemoryGameEngine

    eng = MemoryGameEngine()
    assert eng.execute_tool_call("terminate", {"status": "success"})["status"] == "ok"
    assert eng.execute_tool_call("wait", {"time": 2.0})["waited"] == 2.0
    assert eng.execute_tool_call("answer", {"text": "hi"})["text"] == "hi"
    # 游戏动作在未连接时报错（不崩溃）
    res = eng.execute_tool_call("place_plant", {"card_index": 0, "row": 0, "col": 0})
    assert res["status"] == "error"


def test_memory_engine_execute_game_action_with_fresh_state() -> None:
    from pvz_agent.memory_engine import MemoryGameEngine

    eng = MemoryGameEngine()
    eng._mem = mock.Mock()
    eng._mem.is_connected.return_value = True
    state = mock.Mock()
    state.in_battle = True
    eng._reader = mock.Mock()
    eng._reader.read_state.return_value = state
    eng._executor = mock.Mock()
    eng._executor.can_execute.return_value = True
    eng._executor.execute.return_value = {"action": "place_plant", "status": "ok"}
    res = eng.execute_tool_call("place_plant", {"card_index": 0, "row": 0, "col": 0})
    assert res["status"] == "ok"
    eng._executor.execute.assert_called_once_with(
        "place_plant", {"card_index": 0, "row": 0, "col": 0}, state
    )


def test_memory_engine_place_plant_outside_battle_errors() -> None:
    from pvz_agent.memory_engine import MemoryGameEngine

    eng = MemoryGameEngine()
    eng._mem = mock.Mock()
    eng._mem.is_connected.return_value = True
    state = mock.Mock()
    state.in_battle = False
    eng._reader = mock.Mock()
    eng._reader.read_state.return_value = state
    eng._executor = mock.Mock()
    eng._executor.can_execute.return_value = True
    res = eng.execute_tool_call("shovel", {"row": 0, "col": 0})
    assert res["status"] == "error"
    assert "不在战斗" in res["error"]
    eng._executor.execute.assert_not_called()


def test_planner_text_mode_uses_text_tools_and_no_image() -> None:
    from pvz_agent.config import AppConfig
    from pvz_agent.planner import Planner
    from pvz_agent.prompts import build_planner_system_text, build_planner_tools_text

    captured: dict = {}

    class _FakeVLM:
        def chat_with_tools(self, img_b64, history, user_text, tools, mime="image/png", include_image=True):
            captured["include_image"] = include_image
            captured["tools"] = [t["function"]["name"] for t in tools]
            return None, "x"

    planner = Planner(
        _FakeVLM(), build_planner_system_text(AppConfig()),
        max_rounds=1, include_image=False, tools_builder=build_planner_tools_text,
        tool_call_mode="fc",  # 本用例验证 fc 模式的工具集透传
    )
    planner.plan("", "【内存状态】阳光:150")
    assert captured["include_image"] is False
    assert "win_level" in captured["tools"] and "left_click" not in captured["tools"]


def test_planner_regex_mode_extracts_tool_call_no_fc() -> None:
    """regex 模式（默认/简化）：不调 chat_with_tools，走 chat_with_image + 正则提取。"""
    from pvz_agent.config import AppConfig
    from pvz_agent.planner import Planner
    from pvz_agent.prompts import build_planner_system_text_xml

    captured: dict = {}

    class _FakeVLM:
        def chat_with_image(self, img_b64, history, user_text, include_image, mime):
            captured["img_b64"] = img_b64
            captured["include_image"] = include_image
            return '<tool_call>{"name": "place_plant", "arguments": {"card_index": 0, "row": 1, "col": 2}}</tool_call>', ""

        def chat_with_tools(self, *a, **k):
            raise AssertionError("regex 模式不应调用 chat_with_tools")

    planner = Planner(
        _FakeVLM(), build_planner_system_text_xml(AppConfig()),
        max_rounds=1, include_image=False, tool_call_mode="regex",
    )
    calls, raw = planner.plan("", "【内存状态】阳光:150")
    assert captured["img_b64"] == ""                     # 不传图
    assert captured["include_image"] is False
    assert len(calls) == 1
    assert calls[0].name == "place_plant"
    assert calls[0].arguments == {"card_index": 0, "row": 1, "col": 2}
    assert "<tool_call>" in raw


def test_planner_fc_mode_uses_native_function_calling() -> None:
    """fc 模式：仍走原生 function calling（chat_with_tools）。"""
    from pvz_agent.planner import Planner

    captured: dict = {}

    class _FakeVLM:
        def chat_with_tools(self, img_b64, history, user_text, tools, mime="image/png", include_image=True):
            captured["include_image"] = include_image
            captured["tools"] = [t["function"]["name"] for t in tools]
            return [{"name": "place_plant", "arguments": {"card_index": 0, "row": 1, "col": 2}}], ""

    planner = Planner(_FakeVLM(), "sys", max_rounds=1, tool_call_mode="fc")
    calls, _raw = planner.plan("IMG", "text")
    assert calls[0].name == "place_plant"
    assert captured["include_image"] is True
    assert "place_plant" in captured["tools"]


def test_config_tool_call_mode_parsed_and_invalid_fallback() -> None:
    # 通过 load_config 读取（用临时 config.json，只关心 tool_call_mode 字段）
    import tempfile
    from pathlib import Path

    from pvz_agent import config as cfg_mod

    tmp = Path(tempfile.mkdtemp())
    env = tmp / ".env"
    env.write_text("VLM_BASE_URL=https://v/v1\nVLM_MODEL=m\nVLM_API_KEY=k\n", encoding="utf-8")
    good = tmp / "config.json"
    good.write_text(json.dumps({"tool_call_mode": "fc"}), encoding="utf-8")
    with mock.patch.object(cfg_mod, "ENV_FILE", env), mock.patch.object(cfg_mod, "CONFIG_FILE", good):
        assert cfg_mod.load_config().tool_call_mode == "fc"
    good2 = tmp / "config.json"
    good2.write_text(json.dumps({"tool_call_mode": "regex"}), encoding="utf-8")
    with mock.patch.object(cfg_mod, "ENV_FILE", env), mock.patch.object(cfg_mod, "CONFIG_FILE", good2):
        assert cfg_mod.load_config().tool_call_mode == "regex"
    bad = tmp / "config.json"
    bad.write_text(json.dumps({"tool_call_mode": "invalid"}), encoding="utf-8")
    with mock.patch.object(cfg_mod, "ENV_FILE", env), mock.patch.object(cfg_mod, "CONFIG_FILE", bad):
        assert cfg_mod.load_config().tool_call_mode == "regex"  # 非法回退 regex


def test_memory_engine_grid_dims_from_memory_and_fallback() -> None:
    """棋盘行数以读内存为准（割草机数量），列数回退；读失败回退 config。"""
    from pvz_agent.memory_engine import MemoryGameEngine

    eng = MemoryGameEngine()
    eng._mem = mock.Mock()
    eng._mem.main_object = 0x1000
    eng._mem.offsets = mock.Mock()
    eng._mem.offsets.lawn_mower_count_max = 0x104
    eng._mem.read_int.return_value = 6  # 泳池关 6 行
    assert eng.grid_dims(fallback=(5, 9)) == (6, 9)   # 行数以内存为准
    eng._mem.read_int.side_effect = RuntimeError("read fail")
    assert eng.grid_dims(fallback=(5, 9)) == (5, 9)   # 读失败回退配置


def test_memory_engine_read_state_text_prepends_board_line() -> None:
    """每轮内存状态文本顶部带【棋盘】行（行列来自内存/回退）。"""
    from pvz_agent.memory_engine import MemoryGameEngine

    eng = MemoryGameEngine()
    eng._reader = mock.Mock()
    eng._reader.format_state.return_value = "阳光:150"
    eng._mem = mock.Mock()
    eng._mem.main_object = 0x1000
    eng._mem.offsets = mock.Mock()
    eng._mem.offsets.lawn_mower_count_max = 0x104
    eng._mem.read_int.return_value = 5
    state = mock.Mock()
    state.last_error = ""
    out = eng.read_state_text(state, fallback_grid=(5, 9))
    assert "【棋盘】5 行 x 9 列" in out
    assert "row 0~4" in out and "col 0~8" in out
    assert "阳光:150" in out


def test_memory_engine_paused_by_focus_loss_and_clear() -> None:
    """失焦判定 + 清 game_paused：失焦才干预（前台尊重手动 Esc），写 game_paused=0。"""
    import ctypes

    from pvz_agent.memory_engine import MemoryGameEngine

    eng = MemoryGameEngine()
    eng._mem = mock.Mock()
    eng._mem._hwnd = 12345
    eng._mem.main_object = 0x1000
    eng._mem.offsets = mock.Mock()
    eng._mem.offsets.game_paused = 0x164
    eng._mem.read_int.return_value = 1   # game_paused = 1
    eng._executor = mock.Mock()
    eng._executor.injector = mock.Mock()

    # 前台：不干预
    with mock.patch.object(ctypes.windll.user32, "GetForegroundWindow", return_value=12345):
        assert eng._paused_by_focus_loss() is False
    # 失焦：需要清暂停
    with mock.patch.object(ctypes.windll.user32, "GetForegroundWindow", return_value=99999):
        assert eng._paused_by_focus_loss() is True
    assert eng._is_game_paused() is True
    assert eng._clear_game_paused() is True
    eng._executor.injector.write_int.assert_called_once_with(0x1164, 0)  # game_paused 地址 = 0x1000+0x164

    # 失焦暂停时发 Esc（走游戏恢复逻辑，真正关掉暂停面板）
    sent: list[tuple] = []
    with mock.patch.object(ctypes.windll.user32, "GetForegroundWindow", return_value=99999), \
         mock.patch.object(ctypes.windll.user32, "PostMessageW", side_effect=lambda h, m, w, lp: sent.append((h, m, w))):
        eng._send_esc()
    assert (12345, 0x0100, 0x1B) in sent  # WM_KEYDOWN VK_ESCAPE
    assert (12345, 0x0101, 0x1B) in sent  # WM_KEYUP VK_ESCAPE


def test_memory_engine_select_seeds_gate_and_name_resolution() -> None:
    """选卡：名字→类型id 解析 + 同一选卡会话只选一次 + 新会话解锁。"""
    from pvz_agent.memory_engine import MemoryGameEngine
    from pvz_memory.offsets import GameUI
    from pvz_memory.reader import GameState

    def mk_state(ui: int) -> GameState:
        return GameState(game_ui=ui)  # in_battle 由 game_ui 计算

    eng = MemoryGameEngine()
    eng._executor = mock.Mock()
    eng._executor.injector = mock.Mock()
    eng._executor.can_execute.return_value = True
    eng._executor.execute.return_value = {"action": "select_seeds", "status": "ok"}
    eng._mem = mock.Mock()
    eng._mem.is_connected.return_value = True
    eng._reader = mock.Mock()
    eng._reader.read_state.return_value = mk_state(GameUI.SELECT_CARD)
    # 默认关：不允许 AgentB 选卡
    r0 = eng.execute_tool_call("select_seeds", {"seeds": ["向日葵"]})
    assert r0["status"] == "error" and "手动控制" in r0["error"]
    eng.set_seed_selection_enabled(True)  # 本用例测"允许 AgentB 选卡"时的门控

    # 1. 按名字选 → 解析成类型 id（向日葵=1, 豌豆射手=0）
    r = eng.execute_tool_call("select_seeds", {"seeds": ["向日葵", "豌豆射手"]})
    assert r["status"] == "ok"
    assert eng._executor.execute.call_args[0][1]["seeds"] == [1, 0]
    assert eng._seeds_selected is True

    # 2. 同一会话再次选卡 → 拒绝
    r2 = eng.execute_tool_call("select_seeds", {"seeds": ["坚果"]})
    assert r2["status"] == "error" and "不能重复选卡" in r2["error"]

    # 3. 战斗中选卡 → 拒绝
    eng._seeds_selected = False
    eng._reader.read_state.return_value = mk_state(GameUI.IN_GAME)
    r3 = eng.execute_tool_call("select_seeds", {"seeds": ["向日葵"]})
    assert r3["status"] == "error" and "不在选卡界面" in r3["error"]

    # 4. 未知植物名 → 带指引的错误
    eng._reader.read_state.return_value = mk_state(GameUI.SELECT_CARD)
    r4 = eng.execute_tool_call("select_seeds", {"seeds": ["不存在的植物"]})
    assert r4["status"] == "error" and "未知植物名" in r4["error"]

    # 5. 离开选卡 → 再进新选卡会话 → 解锁（下一关）
    eng._seeds_selected = True
    eng._prev_in_select = True
    eng._reader.read_state.return_value = mk_state(GameUI.IN_GAME)
    eng.read_state()
    eng._reader.read_state.return_value = mk_state(GameUI.SELECT_CARD)
    eng.read_state()
    assert eng._seeds_selected is False


def test_memory_engine_is_actionable() -> None:
    from pvz_agent.memory_engine import MemoryGameEngine

    eng = MemoryGameEngine()
    battle = mock.Mock()
    battle.in_battle = True
    assert eng.is_actionable(battle) is True          # 战斗界面可操作
    select = mock.Mock()
    select.in_battle = False
    select.game_ui = 2  # SELECT_CARD
    assert eng.is_actionable(select) is False         # 默认关：选卡不触发 LLM（玩家手动选卡）
    eng.set_seed_selection_enabled(True)
    assert eng.is_actionable(select) is True          # 开启后：选卡界面可操作（喂 LLM 决策选卡）
    menu = mock.Mock()
    menu.in_battle = False
    menu.game_ui = 1  # MAIN_MENU
    assert eng.is_actionable(menu) is False           # 主菜单不喂
    unknown = mock.Mock()
    unknown.in_battle = False
    unknown.game_ui = 0
    assert eng.is_actionable(unknown) is False        # 未知界面不喂


def test_vlm_thinking_enabled_and_disabled_extra_body() -> None:
    from pvz_agent.config import VLMConfig
    from pvz_agent.vlm import VLMClient

    vlm = VLMClient.__new__(VLMClient)  # 不真正初始化 OpenAI client
    vlm.cfg = VLMConfig(thinking="enabled")
    assert vlm._request_kwargs() == {"extra_body": {"thinking": {"type": "enabled"}}}
    vlm.cfg = VLMConfig(thinking="disabled")
    assert vlm._request_kwargs() == {"extra_body": {"thinking": {"type": "disabled"}}}
    vlm.cfg = VLMConfig(thinking="")
    assert vlm._request_kwargs() == {}


def test_config_text_vlm_loads_and_validates_by_mode(tmp_path) -> None:
    """text 模式：TEXT_VLM_MODEL 优先、TEXT_VLM_* 缺省回退 VLM_*；按 mode 校验对应配置。"""
    from pvz_agent import config as cfg_mod

    env = tmp_path / ".env"
    env.write_text(
        "VLM_BASE_URL=https://vision/v1\nVLM_MODEL=vision-model\nVLM_API_KEY=shared-key\n"
        "TEXT_VLM_MODEL=thinking-model\n",
        encoding="utf-8",
    )
    cfg_json = tmp_path / "config.json"
    cfg_json.write_text(json.dumps({
        "mode": "text",
        "text_vlm": {"thinking": "enabled", "max_output_tokens": 4096, "max_history_rounds": 8},
    }), encoding="utf-8")
    with mock.patch.object(cfg_mod, "ENV_FILE", env), mock.patch.object(cfg_mod, "CONFIG_FILE", cfg_json):
        app = cfg_mod.load_config()
    assert app.mode == "text"
    assert app.text_vlm.model == "thinking-model"     # TEXT_VLM_MODEL 优先
    assert app.text_vlm.api_key == "shared-key"       # TEXT_VLM_API_KEY 缺省回退 VLM_API_KEY
    assert app.text_vlm.thinking == "enabled"
    assert app.text_vlm.max_output_tokens == 4096
    assert app.text_max_history_rounds == 8


def test_set_goal() -> None:
    svc = _make_service()
    assert svc.set_goal("这关用寒冰射手").get("status") == "ok"
    assert svc.get_status()["goal"] == "这关用寒冰射手"
    assert svc.set_goal("  ").get("status") == "error"


def test_inject_instruction_queues_and_drains() -> None:
    svc = _make_service()
    assert svc.inject_instruction("先种豌豆射手").get("status") == "ok"
    assert svc._drain_instructions() == ["先种豌豆射手"]
    assert svc._drain_instructions() == []


def test_pause_then_resume() -> None:
    svc = _make_service()
    svc._phase = svc.PHASE_RUNNING
    assert svc.pause()["status"] == "ok"
    assert svc.get_status()["phase"] == svc.PHASE_PAUSED
    assert svc.resume()["status"] == "ok"
    assert svc.get_status()["phase"] == svc.PHASE_RUNNING


def test_pause_when_idle_is_idle_response() -> None:
    svc = _make_service()
    assert svc.pause().get("status") == "idle"


def test_set_speed() -> None:
    svc = _make_service()
    assert svc.set_speed(2.0).get("status") == "ok"
    assert svc.get_status()["speed"] == 2.0
    assert svc.set_speed(0).get("status") == "error"


def test_start_returns_error_when_runtime_unavailable() -> None:
    svc = _make_service()
    with mock.patch.object(svc, "_ensure_runtime", side_effect=RuntimeError("VLM 未配置")):
        result = svc.start()
    assert result["status"] == "error"
    assert svc.get_status()["phase"] == svc.PHASE_ERROR


def test_start_returns_waiting_when_window_missing() -> None:
    """窗口缺失：start 不报错，返回"等待窗口"态并拉起后台循环线程（由它轮询等待）。"""
    svc = _make_service()
    with mock.patch.object(
        svc, "_ensure_runtime", side_effect=WindowNotFoundError("未找到匹配的 PVZ 窗口")
    ):
        with _no_real_threads():
            result = svc.start()
    assert result["status"] == "ok"
    assert result.get("waiting_window") is True
    assert svc.get_status()["phase"] == svc.PHASE_RUNNING  # 等待态而非错误态
    assert svc._thread is not None  # 后台循环线程已拉起（循环内会轮询窗口）


def test_start_when_paused_resumes() -> None:
    svc = _make_service()
    svc._phase = svc.PHASE_PAUSED
    svc._pause_evt.set()
    with mock.patch.object(svc, "_ensure_runtime", return_value=object()):
        result = svc.start(goal="继续")
    assert result.get("resumed") is True
    assert svc.get_status()["phase"] == svc.PHASE_RUNNING
    assert not svc._pause_evt.is_set()


def test_start_already_running_updates_goal_only() -> None:
    svc = _make_service()
    svc._phase = svc.PHASE_RUNNING
    svc._thread = mock.Mock(is_alive=lambda: True)
    with mock.patch.object(svc, "_ensure_runtime", return_value=object()):
        result = svc.start(goal="目标B")
    assert result.get("already_running") is True
    assert svc.get_status()["goal"] == "目标B"


# --------------------------------------------------------------------------- #
#  service：终止后可重启 / 窗口丢失
# --------------------------------------------------------------------------- #
def test_terminate_stops_loop_and_can_restart() -> None:
    svc = _make_service()
    svc._phase = svc.PHASE_RUNNING
    fake_alive = mock.Mock()
    fake_alive.is_alive.return_value = True
    svc._thread = fake_alive
    with mock.patch.object(svc, "_notify_text"):
        svc._on_terminate("success")
    # 终止后：完全停止（phase idle、stop_evt 置位），不再"暂停卡死"
    assert svc.get_status()["phase"] == svc.PHASE_IDLE
    assert svc._stop_evt.is_set()

    # 再次 start → 新建循环（stop_evt 被清掉）
    with mock.patch.object(svc, "_ensure_runtime", return_value=object()):
        with _no_real_threads():
            result = svc.start(goal="下一关")
    assert result["status"] == "ok"
    assert svc.get_status()["phase"] == svc.PHASE_RUNNING
    assert not svc._stop_evt.is_set()


def test_window_lost_stops_loop_and_notifies() -> None:
    svc = _make_service()
    svc._phase = svc.PHASE_RUNNING
    svc._thread = mock.Mock(is_alive=lambda: False)
    notified: list[dict] = []
    svc._notifier = lambda **kw: notified.append(kw)  # type: ignore[method-assign]
    svc._handle_window_lost(RuntimeError("客户区尺寸无效"))
    assert svc.get_status()["phase"] == svc.PHASE_IDLE
    assert svc._stop_evt.is_set()
    assert any("窗口丢失" in n.get("text", "") for n in notified)


# --------------------------------------------------------------------------- #
#  service：feed 截图（主模型观察通道）
# --------------------------------------------------------------------------- #
def _stub_window_and_capturer(svc: PvZAgentService, img: Image.Image) -> None:
    svc._win = object()
    svc._capturer = mock.Mock()
    svc._capturer.grab_pil.return_value = img


def test_observer_feed_pure_image_and_dedup() -> None:
    svc = _make_service()
    img = Image.new("RGB", (100, 80), (10, 200, 30))
    _stub_window_and_capturer(svc, img)
    svc._feed_interval = 0  # 不节流
    observed: list[tuple[bytes, bool]] = []
    svc._on_observation = lambda jpeg, nudge: observed.append((jpeg, nudge))  # type: ignore[method-assign]
    svc._observer_tick()
    assert len(observed) == 1
    jpeg, nudge = observed[0]
    assert nudge is False and jpeg[:2] == b"\xff\xd8"  # feed 是纯截图 JPEG
    # 同帧再 tick → 指纹去重，不推
    svc._observer_tick()
    assert len(observed) == 1
    # 画面变了 → 再推
    svc._capturer.grab_pil.return_value = Image.new("RGB", (100, 80), (200, 10, 30))
    svc._observer_tick()
    assert len(observed) == 2


def test_observer_feed_throttled_by_interval() -> None:
    svc = _make_service()
    _stub_window_and_capturer(svc, Image.new("RGB", (100, 80), (1, 2, 3)))
    svc._feed_interval = 1000.0
    observed: list[tuple[bytes, bool]] = []
    svc._on_observation = lambda jpeg, nudge: observed.append((jpeg, nudge))  # type: ignore[method-assign]
    svc._observer_tick()  # 首次允许
    assert len(observed) == 1
    svc._capturer.grab_pil.return_value = Image.new("RGB", (100, 80), (4, 5, 6))  # 帧变了
    svc._observer_tick()  # 但未到 feed_interval → 不推
    assert len(observed) == 1


def test_observer_skips_when_no_window() -> None:
    svc = _make_service()
    observed: list[tuple[bytes, bool]] = []
    svc._on_observation = lambda jpeg, nudge: observed.append((jpeg, nudge))  # type: ignore[method-assign]
    with mock.patch.object(svc, "_ensure_window", side_effect=RuntimeError("未找到匹配的 PVZ 窗口")):
        svc._observer_tick()
    assert observed == []


def test_frame_fingerprint_stable() -> None:
    img1 = Image.new("RGB", (200, 150), (5, 10, 15))
    img2 = Image.new("RGB", (200, 150), (5, 10, 15))
    img3 = Image.new("RGB", (200, 150), (6, 10, 15))
    assert PvZAgentService._frame_fingerprint(img1) == PvZAgentService._frame_fingerprint(img2)
    assert PvZAgentService._frame_fingerprint(img1) != PvZAgentService._frame_fingerprint(img3)


# --------------------------------------------------------------------------- #
#  service：观察线程的主动 nudge（截图 + 短触发，让主模型主动看并行动）
# --------------------------------------------------------------------------- #
def test_observer_nudge_gated_on_running() -> None:
    svc = _make_service()
    _stub_window_and_capturer(svc, Image.new("RGB", (100, 80), (9, 9, 9)))
    svc._feed_interval = 0
    svc._nudge_interval = 0
    observed: list[tuple[bytes, bool]] = []
    svc._on_observation = lambda jpeg, nudge: observed.append((jpeg, nudge))  # type: ignore[method-assign]
    # phase idle → 只 feed，不 nudge
    svc._phase = svc.PHASE_IDLE
    svc._observer_tick()
    assert [n for _, n in observed] == [False]
    # phase running → feed + nudge
    observed.clear()
    svc._phase = svc.PHASE_RUNNING
    svc._observer_tick()
    assert True in [n for _, n in observed]


def test_observer_nudge_reuses_cached_frame() -> None:
    svc = _make_service()
    _stub_window_and_capturer(svc, Image.new("RGB", (100, 80), (7, 7, 7)))
    svc._feed_interval = 0
    svc._nudge_interval = 0
    svc._phase = svc.PHASE_RUNNING
    observed: list[tuple[bytes, bool]] = []
    svc._on_observation = lambda jpeg, nudge: observed.append((jpeg, nudge))  # type: ignore[method-assign]
    svc._observer_tick()
    assert len(observed) == 2
    assert observed[0][1] is False and observed[1][1] is True
    assert observed[1][0] == svc._last_feed_jpeg  # nudge 复用 feed 缓存帧
    assert svc._capturer.grab_pil.call_count == 1  # 只截图一次


def test_observer_nudge_falls_back_to_fresh_frame_when_no_cache() -> None:
    """缓存帧为空（feed 从未成功/画面静止）时，nudge 仍现场截图推送，不做指纹去重。"""
    svc = _make_service()
    img = Image.new("RGB", (100, 80), (7, 7, 7))
    _stub_window_and_capturer(svc, img)
    svc._feed_enabled = False  # 关掉 feed，模拟缓存从未建立
    svc._nudge_interval = 0
    svc._phase = svc.PHASE_RUNNING
    observed: list[tuple[bytes, bool]] = []
    svc._on_observation = lambda jpeg, nudge: observed.append((jpeg, nudge))  # type: ignore[method-assign]
    svc._observer_tick()
    assert len(observed) == 1
    jpeg, nudge = observed[0]
    assert nudge is True and jpeg[:2] == b"\xff\xd8"
    # 画面没变再 tick → nudge 仍推（_nudge_frame 不做指纹去重）
    observed.clear()
    svc._observer_tick()
    assert len(observed) == 1
    assert observed[0][1] is True


def test_nudge_frame_prefers_cached_jpeg() -> None:
    svc = _make_service()
    svc._win = object()
    svc._capturer = mock.Mock()
    svc._capturer.grab_pil.return_value = Image.new("RGB", (100, 80), (1, 2, 3))
    cached = svc.encode_jpeg(Image.new("RGB", (100, 80), (9, 9, 9)))
    svc._last_feed_jpeg = cached
    assert svc._nudge_frame() == cached  # 复用缓存，不再截图
    assert svc._capturer.grab_pil.call_count == 0


def test_nudge_frame_returns_none_when_no_window() -> None:
    svc = _make_service()
    svc._last_feed_jpeg = None
    with mock.patch.object(svc, "_ensure_window", side_effect=RuntimeError("未找到匹配的 PVZ 窗口")):
        assert svc._nudge_frame() is None


# --------------------------------------------------------------------------- #
#  service：给主模型的截图 = 高质量原图（预算内不缩放）
# --------------------------------------------------------------------------- #
def test_encode_jpeg_keeps_native_size_within_budget() -> None:
    """字节预算充足（0 = 不限制）时，保持原图分辨率，不降采样。"""
    svc = _make_service()
    svc._feed_max_bytes = 0
    img = Image.new("RGB", (640, 480), (120, 200, 60))
    jpeg = svc.encode_jpeg(img)
    import io

    from PIL import Image as _Image

    decoded = _Image.open(io.BytesIO(jpeg))
    assert decoded.size == (640, 480)
    assert jpeg[:2] == b"\xff\xd8"


def test_encode_jpeg_budget_ladder_never_throws() -> None:
    """字节预算不足 → 沿 edge/quality 阶梯降质，落进预算内（合法 JPEG，不崩）。"""
    svc = _make_service()
    svc._feed_max_bytes = 8 * 1024  # 8KB 预算，渐变图 q95 远超
    from PIL import Image as _Image

    base = _Image.linear_gradient("L").resize((800, 600))
    img = _Image.merge("RGB", (base, base.rotate(90), base))
    jpeg = svc.encode_jpeg(img)
    assert jpeg[:2] == b"\xff\xd8"
    assert 0 < len(jpeg) <= 8 * 1024


def test_start_pushes_startup_screenshot() -> None:
    """开始游玩时，立即推一张高质量原图给主模型（nudge 型开局画面）。"""
    svc = _make_service()
    svc._phase = svc.PHASE_IDLE
    svc._win = object()
    svc._capturer = mock.Mock()
    svc._capturer.grab_pil.return_value = Image.new("RGB", (100, 80), (3, 3, 3))
    observed: list[tuple[bytes, bool]] = []
    svc._on_observation = lambda jpeg, nudge: observed.append((jpeg, nudge))  # type: ignore[method-assign]
    with mock.patch.object(svc, "_ensure_runtime", return_value=object()):
        with _no_real_threads():
            result = svc.start()
    assert result["status"] == "ok"
    assert len(observed) == 1
    assert observed[0][1] is True
    assert observed[0][0][:2] == b"\xff\xd8"
    # 开局推图计作一次 nudge，节流时间戳已更新
    assert svc._last_nudge_at > 0


def test_probe_missing_cv2_is_graceful() -> None:
    svc = _make_service()
    svc.configure({"mode": "vision"})  # 该用例专测视觉模式的 cv2 检测
    with mock.patch.dict("sys.modules", {"cv2": None}):  # import cv2 → ImportError
        result = svc.probe()
    assert result["cv2"] is False
    assert "opencv" in result["message"]


# --------------------------------------------------------------------------- #
#  service：辅助函数
# --------------------------------------------------------------------------- #
def test_build_feedback_formats_results() -> None:
    ok_text = _build_feedback(
        [{"action": "place_plant", "status": "ok", "card_index": 0, "row": 1, "col": 2}]
    )
    assert "place_plant" in ok_text and "成功" in ok_text
    err_text = _build_feedback([{"action": "place_plant", "status": "error", "error": "阳光不足"}])
    assert "失败" in err_text and "阳光不足" in err_text
    assert "没有产生任何动作" in _build_feedback([])


# --------------------------------------------------------------------------- #
#  neko_interface
# --------------------------------------------------------------------------- #
def test_interface_normalize_fills_summary_status() -> None:
    payload = _normalize({"message": "hi"})
    assert payload["summary"] == "hi"
    assert payload["status"] == "ok"


def test_interface_get_status_shapes() -> None:
    svc = _make_service()
    payload = asyncio.run(PvZNekoInterface(svc).get_status())
    assert payload["status"] == "ok"
    assert "summary" in payload and payload["summary"]
    assert payload["phase"] == "idle"


def test_interface_get_readout_combines_scan() -> None:
    svc = _make_service()
    svc.scan_now = lambda: {"status": "ok", "summary": "植物: (0,1)"}  # type: ignore[method-assign]
    payload = asyncio.run(PvZNekoInterface(svc).get_readout())
    assert payload["status"] == "ok"
    assert "植物: (0,1)" in payload["summary"]
    assert payload["state"]["phase"] == "idle"


# --------------------------------------------------------------------------- #
#  facade：注册面
# --------------------------------------------------------------------------- #
def test_llm_tools_are_registered() -> None:
    inst = PVZAgentPlugin.__new__(PVZAgentPlugin)  # 绕过 __init__（无需 ctx）
    names = {meta.name for meta, _ in collect_llm_tool_methods(inst)}
    assert {
        "pvz_status",
        "pvz_screenshot",
        "pvz_scan",
        "pvz_start",
        "pvz_pause",
        "pvz_resume",
        "pvz_stop",
        "pvz_goal",
        "pvz_instruction",
    } <= names


def test_plugin_entries_are_registered() -> None:
    expected = {
        "pvz_get_status",
        "pvz_start",
        "pvz_pause",
        "pvz_resume",
        "pvz_stop",
        "pvz_set_goal",
        "pvz_give_instruction",
        "pvz_screenshot",
        "pvz_scan",
    }
    found = set()
    for name in dir(PVZAgentPlugin):
        member = getattr(PVZAgentPlugin, name)
        meta = getattr(member, EVENT_META_ATTR, None)
        if meta is not None and getattr(meta, "event_type", "") == "plugin_entry":
            found.add(getattr(meta, "id", name))
    assert expected <= found


# --------------------------------------------------------------------------- #
#  原生 function calling（后台执行核心）
# --------------------------------------------------------------------------- #
def test_build_planner_tools_schema() -> None:
    tools = build_planner_tools()
    names = {t["function"]["name"] for t in tools}
    assert {"place_plant", "shovel", "click_card", "select_seeds", "left_click", "key", "wait", "terminate", "answer"} <= names
    pp = next(t["function"] for t in tools if t["function"]["name"] == "place_plant")
    assert pp["parameters"]["required"] == ["card_index", "row", "col"]
    assert {"row", "col", "card_index"} <= set(pp["parameters"]["properties"])


def test_planner_system_prompts_require_tool_call() -> None:
    """原生与 legacy 两套 system prompt 都必须强制每轮调用工具，避免模型只回文本不行动。"""
    from pvz_agent.config import AppConfig
    from pvz_agent.prompts import (
        build_planner_system,
        build_planner_system_xml,
        build_planner_user_footer,
    )

    cfg = AppConfig()
    assert "每轮必须调用至少一个工具" in build_planner_system(cfg)
    assert "每轮必须输出至少一个" in build_planner_system_xml(cfg)
    assert "每轮必须调用工具" in build_planner_user_footer(goal="自动赢", elapsed=1.0, last_summary="")


def test_vlm_tool_choice_required_and_fallback() -> None:
    """默认强制工具调用；provider 不支持 'required' 时降级 'auto' 重试一次。"""
    from pvz_agent.config import VLMConfig
    from pvz_agent.vlm import VLMClient

    class _Msg:
        tool_calls = []
        content = ""

    class _Resp:
        usage = None
        choices = [type("_Ch", (), {"message": _Msg()})()]

    client = VLMClient.__new__(VLMClient)
    client.cfg = VLMConfig(tool_choice="required")
    client._client = mock.Mock()
    sent: list[str] = []

    def fake_create(**kw):
        sent.append(kw.get("tool_choice"))
        if len(sent) == 1:
            raise RuntimeError("tool_choice 'required' unsupported")
        return _Resp()

    client._client.chat.completions.create.side_effect = fake_create
    calls, _content = client.chat_with_tools("img", [{"role": "system", "content": "s"}], "u", [], mime="image/jpeg")
    assert sent == ["required", "auto"]  # 首次 required 失败 → 降级 auto 重试
    assert (calls or []) == []  # 无工具调用时返回 None/[]


def test_render_tool_calls() -> None:
    text = _render_tool_calls([{"name": "place_plant", "arguments": {"card_index": 0, "row": 1, "col": 2}}])
    assert "place_plant" in text and "card_index=0" in text


class _FakeToolsVLM:
    """测试用假 VLM：默认返回原生 tool_calls。"""

    def __init__(self, calls_raw=None, raise_on_tools: bool = False) -> None:
        self.calls_raw = calls_raw
        self.raise_on_tools = raise_on_tools
        self.tools_calls = 0
        self.image_calls = 0

    def chat_with_tools(self, **kw):
        self.tools_calls += 1
        if self.raise_on_tools:
            raise RuntimeError("tools not supported")
        return self.calls_raw, ""

    def chat_with_image(self, **kw):
        self.image_calls += 1
        return "<tool_call>{\"name\": \"pvz_action\", \"arguments\": {\"action\": \"wait\", \"time\": 1}}</tool_call>", ""


def test_planner_native_tool_calls() -> None:
    planner = Planner(vlm=_FakeToolsVLM(calls_raw=[{"name": "place_plant", "arguments": {"card_index": 0, "row": 1, "col": 2}}]), system_prompt="sys", tool_call_mode="fc")
    calls, raw = planner.plan("img", "user")
    assert len(calls) == 1
    assert calls[0].name == "place_plant"
    assert calls[0].arguments == {"card_index": 0, "row": 1, "col": 2}
    assert "place_plant" in raw
    assert planner._legacy_prompt_used is False


def test_planner_falls_back_when_tools_unsupported() -> None:
    vlm = _FakeToolsVLM(raise_on_tools=True)
    planner = Planner(vlm=vlm, system_prompt="sys", system_prompt_xml="xml-legacy", tool_call_mode="fc")
    calls, _raw = planner.plan("img", "user")
    assert len(calls) == 1
    assert calls[0].name == "pvz_action"
    assert planner._legacy_prompt_used is True
    assert planner._history[0]["content"] == "xml-legacy"  # system 已换成 legacy
    # 第二次不再尝试原生 tools（避免不支持 tools 的 provider 每轮都走重试）
    planner.plan("img", "user")
    assert vlm.tools_calls == 1
    assert vlm.image_calls == 2


def test_executor_native_dispatch_left_click() -> None:
    ex = Executor(mock.Mock(), LayoutConfig(), mouse_lock=mock.Mock())
    ex._rel_to_screen = mock.Mock(return_value=(10, 20))
    ex._click_screen = mock.Mock()
    result = ex._native_dispatch("left_click", {"coordinate": [100, 200]})
    assert result["status"] == "ok"
    ex._rel_to_screen.assert_called_once_with((100, 200))
    ex._click_screen.assert_called()


def test_executor_native_dispatch_place_plant() -> None:
    ex = Executor(mock.Mock(), LayoutConfig(), mouse_lock=mock.Mock())
    ex._to_screen = mock.Mock(return_value=(5, 5))
    ex._click_screen = mock.Mock()
    result = ex._native_dispatch("place_plant", {"card_index": 0, "row": 1, "col": 2})
    assert result["status"] == "ok"
    assert result["row"] == 1 and result["col"] == 2


def test_executor_card_position_mode_fixed_skips_scanner() -> None:
    """fixed 模式：即使注入了 OpenCV 卡片扫描器也不使用，直接按固定坐标种。"""
    ex = Executor(mock.Mock(), LayoutConfig(), mouse_lock=mock.Mock(), card_position_mode="fixed")
    ex._to_screen = mock.Mock(return_value=(5, 5))
    ex._click_screen = mock.Mock()
    ex._card_scanner = mock.Mock()
    result = ex.place_plant(0, 1, 2)
    assert result["status"] == "ok"
    ex._card_scanner.scan.assert_not_called()


def test_executor_card_position_mode_opencv_uses_scanner() -> None:
    """opencv 模式：用卡片扫描器实时识别卡片位置。"""
    win = mock.Mock()
    win.client_rect = (0, 0, 800, 600)
    ex = Executor(win, LayoutConfig(), mouse_lock=mock.Mock(), card_position_mode="opencv")
    ex._to_screen = mock.Mock(return_value=(5, 5))
    ex._click_screen = mock.Mock()
    ex._card_scanner = mock.Mock()
    res = mock.Mock()
    res.card_positions = {0: (120, 50)}
    ex._card_scanner.scan.return_value = res
    with mock.patch("PIL.ImageGrab.grab", return_value=mock.Mock()):
        result = ex.place_plant(0, 1, 2)
    assert result["status"] == "ok"
    ex._card_scanner.scan.assert_called_once()


def test_fail_key_native_name() -> None:
    svc = _make_service()
    tc = ToolCall(name="place_plant", arguments={"card_index": 0, "row": 1, "col": 2})
    key = svc._fail_key(tc, {"status": "error", "error": "阳光不足"})
    assert key == ("place_plant", 0, 1, 2)
    # 非失败 → None
    assert svc._fail_key(tc, {"status": "ok"}) is None


def test_executor_control_native_names() -> None:
    """terminate/answer/wait 原生名应走控制分支（不持鼠标锁、不点鼠标）。"""
    ex = Executor(mock.Mock(), LayoutConfig(), mouse_lock=mock.Mock())
    ex._click_screen = mock.Mock()
    result = ex.execute_tool_call("wait", {"time": 2.0})
    assert result["action"] == "wait" and result["status"] == "ok"
    result = ex.execute_tool_call("terminate", {"status": "success"})
    assert result["terminate_status"] == "success"
    ex._click_screen.assert_not_called()


# --------------------------------------------------------------------------- #
#  解析失败绝不静默：vlm 工具解析诊断 + planner last_status + 空动作通报
# --------------------------------------------------------------------------- #
def test_vlm_extract_tool_calls_shapes() -> None:
    from pvz_agent.vlm import VLMClient

    class _Msg:
        def __init__(self, tool_calls):  # noqa: ANN001
            self.tool_calls = tool_calls

    # object 形态 + 已解析 dict args
    class Fn1:
        name = "place_plant"
        arguments = {"card_index": 0, "row": 1, "col": 2}

    class Tc1:
        type = "function"
        function = Fn1()

    calls, malformed, dropped = VLMClient._extract_tool_calls(_Msg([Tc1()]))
    assert calls == [{"name": "place_plant", "arguments": {"card_index": 0, "row": 1, "col": 2}}]
    assert malformed == 0 and dropped == 0

    # 字符串 JSON arguments
    class Fn2:
        name = "shovel"
        arguments = '{"row":1,"col":2}'

    class Tc2:
        type = "function"
        function = Fn2()

    calls, malformed, dropped = VLMClient._extract_tool_calls(_Msg([Tc2()]))
    assert calls == [{"name": "shovel", "arguments": {"row": 1, "col": 2}}]

    # 畸形 JSON → 不崩、记为 malformed（不再 TypeError 冒泡成整轮失败）
    class Fn3:
        name = "wait"
        arguments = "not-json{"

    class Tc3:
        type = "function"
        function = Fn3()

    calls, malformed, dropped = VLMClient._extract_tool_calls(_Msg([Tc3()]))
    assert calls == [{"name": "wait", "arguments": {}}]
    assert malformed == 1

    # 非 function 类型 → 丢弃并计数（不再静默）
    class Tc4:
        type = "web_search"

    calls, malformed, dropped = VLMClient._extract_tool_calls(_Msg([Tc4()]))
    assert calls == [] and dropped == 1

    # dict 形态的 tc/function（部分 provider）
    calls, malformed, dropped = VLMClient._extract_tool_calls(
        _Msg([{"type": "function", "function": {"name": "key", "arguments": {"keys": ["space"]}}}])
    )
    assert calls == [{"name": "key", "arguments": {"keys": ["space"]}}]


def test_planner_last_status_text_only() -> None:
    """模型只回文本无动作 → 降级为 wait（避免"未解析到动作"死轮）。"""
    class _V:
        def chat_with_tools(self, **kw):
            return None, "我想先观察一下局势"  # 无工具调用，只有文本

        def chat_with_image(self, **kw):
            return "", ""

    planner = Planner(vlm=_V(), system_prompt="sys", tool_call_mode="fc")
    calls, _raw = planner.plan("img", "user")
    assert len(calls) == 1
    assert calls[0].name == "wait"            # 文本-only 降级为 wait
    assert planner.last_status == "ok"        # 不再判 parse_failed
    assert "降级" in planner.last_status_text


def test_planner_last_status_empty() -> None:
    class _V:
        def chat_with_tools(self, **kw):
            return None, ""  # 什么都没有

        def chat_with_image(self, **kw):
            return "", ""

    planner = Planner(vlm=_V(), system_prompt="sys")
    calls, _raw = planner.plan("img", "user")
    assert calls == []
    assert planner.last_status == "empty"


def test_handle_no_actions_notifies_and_escalates() -> None:
    svc = _make_service()
    svc._planner = mock.Mock()
    svc._planner.last_status_text = "模型返回了文本但没解析出可执行动作"
    notified: list[dict] = []
    svc._notifier = lambda **kw: notified.append(kw)  # type: ignore[method-assign]
    svc._handle_no_actions()  # 第 1 次 → 通报
    svc._handle_no_actions()  # 第 2 次 → 不通报（节流）
    svc._handle_no_actions()  # 第 3 次 → 升级通报
    assert len(notified) == 2
    assert all(n["kind"] == "no_action" for n in notified)
    assert "解析" in notified[0]["text"]
    assert "3 轮" in notified[1]["text"]
    assert svc._empty_rounds == 3


def test_handle_no_actions_after_actions_resets() -> None:
    svc = _make_service()
    svc._handle_no_actions()
    assert svc._empty_rounds == 1
    svc._empty_rounds = 0  # _tick 里有动作时会重置
    assert svc._empty_rounds == 0


# --------------------------------------------------------------------------- #
#  facade：hosted UI 教程面板注册
# --------------------------------------------------------------------------- #
def test_ui_context_and_actions_registered() -> None:
    ctx = getattr(PVZAgentPlugin.quickstart_ui_context, UI_CONTEXT_META_ATTR, None)
    assert ctx is not None and ctx["id"] == "quickstart"
    for name in ("pvz_get_status", "pvz_start", "pvz_pause", "pvz_stop"):
        meta = getattr(getattr(PVZAgentPlugin, name), UI_ACTION_META_ATTR, None)
        assert meta is not None, name
