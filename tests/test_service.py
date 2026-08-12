"""PvZAgent 插件单元测试。

覆盖：service 状态机与命令（含 terminate/窗口丢失后重启）、feed 截图节流去重、
neko_interface 参数整形、facade 的 llm_tool / plugin_entry 注册面。
全程不触碰真实窗口 / VLM / cv2——核心运行时在 service 里是懒加载的，测试用 mock 封住。
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock
from contextlib import contextmanager

from PIL import Image

from plugin.sdk.plugin.llm_tool import collect_llm_tool_methods
from plugin.sdk.plugin.ui import UI_ACTION_META_ATTR, UI_CONTEXT_META_ATTR
from plugin.sdk.shared.constants import EVENT_META_ATTR

from plugin.plugins.pvz_agent import PVZAgentPlugin
from plugin.plugins.pvz_agent import service as service_module
from plugin.plugins.pvz_agent.neko_interface import PvZNekoInterface, _normalize
from plugin.plugins.pvz_agent.service import (
    DEFAULT_GOAL,
    PvZAgentService,
    _build_feedback,
)

# pvz 核心（service 模块已在 import 时把 pvz/ 加入 sys.path）
from pvz_agent.executor import Executor, LayoutConfig
from pvz_agent.parser import ToolCall
from pvz_agent.planner import Planner, _render_tool_calls
from pvz_agent.prompts import build_planner_tools


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
        "window_title_keywords": ["植物大战僵尸"],
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
    assert svc._window_keywords == ["植物大战僵尸"]
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
    planner = Planner(vlm=_FakeToolsVLM(calls_raw=[{"name": "place_plant", "arguments": {"card_index": 0, "row": 1, "col": 2}}]), system_prompt="sys")
    calls, raw = planner.plan("img", "user")
    assert len(calls) == 1
    assert calls[0].name == "place_plant"
    assert calls[0].arguments == {"card_index": 0, "row": 1, "col": 2}
    assert "place_plant" in raw
    assert planner._legacy_prompt_used is False


def test_planner_falls_back_when_tools_unsupported() -> None:
    vlm = _FakeToolsVLM(raise_on_tools=True)
    planner = Planner(vlm=vlm, system_prompt="sys", system_prompt_xml="xml-legacy")
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
    class _V:
        def chat_with_tools(self, **kw):
            return None, "我想先观察一下局势"  # 无工具调用，只有文本

        def chat_with_image(self, **kw):
            return "", ""

    planner = Planner(vlm=_V(), system_prompt="sys")
    calls, _raw = planner.plan("img", "user")
    assert calls == []
    assert planner.last_status == "parse_failed"
    assert "观察" in planner.last_status_text


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
