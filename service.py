"""PvZAgentService：让猫娘玩《植物大战僵尸》的后台运行服务（线程安全）。

协作模型（观感 = 猫娘自己在玩；实现在她背后）：

- **猫娘（主模型）**：周期性收到最新游戏**截图**，看画面后自己给出策略
  （说给用户听），需要调整打法时用自然语言下发目标与引导
  （``set_goal`` / ``inject_instruction`` → ``planner.add_user_note``）；
- **执行核心**（``Planner``，VLM）：在后台循环线程里实时看截图、把猫娘的目标与
  引导翻译成具体动作实时执行（种到哪格、何时铲等），保证游玩实时性。
  它是猫娘的"双手"，对外统一表现为"猫娘自己在操作"，不暴露内部实现；
- **观察通道**：主模型侧周期截图推送（feed 纯截图 read + nudge 截图+短触发
  respond），本服务负责产出最新帧与执行循环。

关键行为修正：
- 窗口查找排除 cmd/资源管理器/终端（核心 ``window.py``）；
- 窗口丢失 → 通知主模型并**彻底停止循环**（不再无限重试）；
- 游戏内 terminate → 通知主模型并**彻底停止循环**，保证可再次 ``start``；
- ``push_message`` 走宿主 ZMQ outbox，后台线程可安全调用（与 sts2/minecraft 同模式）。

核心仅改动 ``window.py`` 排除规则；本模块把 ``pvz/`` 加入 sys.path 后 ``import pvz_agent``。
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

# 注入 pvz 核心所在目录（去重，幂等）。真正的 `import pvz_agent` 在
# ``_import_core`` 里懒加载，避免本模块在 cv2 缺失时 import 即失败。
CORE_DIR = Path(__file__).resolve().parent / "pvz"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

# 内置 OpenCV：cv2 已随插件装进 pvz/vendor/（无需用户显式安装），加入 sys.path。
VENDOR_DIR = CORE_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

DEFAULT_GOAL = "自动玩完当前这一关并尽可能取得胜利"

# 防循环：同一动作 + 同坐标连续失败达到该次数则强制停止该轮并提示换策略。
# 与 pvz/pvz_agent/main.py 保持一致。
MAX_CONSECUTIVE_FAIL = 2

# 窗口标题关键词默认值（与 pvz/config.json 一致；插件未配置时兜底）。
DEFAULT_WINDOW_KEYWORDS = [
    "plant vs zombie", "植物大战僵尸", "pvz",
    "杂交版", "plants vs. zombies", "plants vs zombies",
]


def _build_feedback(results: list[dict]) -> str:
    """把执行结果文本化为反馈，回填给后台执行核心。"""
    if not results:
        return "[执行结果] 本轮没有产生任何动作。"
    lines = ["[上轮动作与反馈]"]
    for r in results:
        action = r.get("action", "?")
        status = r.get("status", "ok")
        if action == "terminate":
            lines.append(f"  terminate: 已报告 {r.get('terminate_status', 'success')}")
            continue
        if action == "answer":
            lines.append(f"  answer: {r.get('text', '')}")
            continue
        if status == "error":
            lines.append(f"  {action}: 失败 - {r.get('error', '未知错误')}")
        else:
            detail = ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("action", "status"))
            lines.append(f"  {action}: 成功 ({detail})")
    return "\n".join(lines)


def _compute_wait(results: list[dict], executor, base: float) -> float:
    """本轮等待时间 = max(基础节拍, 动作感知延迟, 模型主动 wait)。"""
    max_delay = 0.0
    has_game_action = False
    for r in results:
        action = r.get("action", "")
        if action == "wait":
            max_delay = max(max_delay, float(r.get("waited", 0.0)))
            continue
        has_game_action = True
        max_delay = max(max_delay, executor.ACTION_DELAYS.get(action, executor.DEFAULT_DELAY))
    if not has_game_action:
        max_delay = max(max_delay, executor.DEFAULT_DELAY)
    return max(base, max_delay)


class PvZAgentService:
    """让猫娘玩 PVZ 的后台运行服务（线程安全）。"""

    PHASE_IDLE = "idle"
    PHASE_RUNNING = "running"
    PHASE_PAUSED = "paused"
    PHASE_STOPPING = "stopping"
    PHASE_ERROR = "error"

    def __init__(
        self,
        logger: Any,
        notifier: Callable[..., None] | None = None,
    ) -> None:
        """``notifier``：可选推送回调，收到 ``text``（与可选 ``kind`` 等 kwargs）。
        后台线程用它把游戏内 Agent 的 answer/terminate/窗口丢失等转达给主模型。"""
        self._logger = logger
        self._notifier = notifier

        # 锁与事件（线程同步）
        self._lock = threading.Lock()          # 保护共享状态字段
        self._stop_evt = threading.Event()     # set → 循环退出
        self._pause_evt = threading.Event()    # set → 暂停（只观察不执行）
        self._wake_evt = threading.Event()     # set → 提前唤醒 sleep（命令即时生效）
        self._thread: threading.Thread | None = None
        self._mouse_lock = threading.Lock()    # 阳光线程 与 执行器 共享

        # 懒加载的运行时组件（_ensure_window / _ensure_runtime 填充）
        self._core: Any = None                 # pvz_agent 核心模块
        self._cfg: Any = None                  # AppConfig
        self._win: Any = None                  # WindowHandle
        self._capturer: Any = None
        self._executor: Any = None
        self._sun_collector: Any = None
        self._grid_scanner: Any = None
        self._card_scanner: Any = None
        self._select_scanner: Any = None
        self._vlm: Any = None
        self._planner: Any = None
        self._img_fmt = "jpeg"
        self._img_mime = "image/jpeg"

        # 插件级配置（configure 填充）
        self._window_keywords: list[str] = list(DEFAULT_WINDOW_KEYWORDS)
        self._notify_on_terminate = True
        self._notify_window_lost = True
        self._sun_auto_collect = True
        self._scan_grid_enabled = True
        self._scan_cards_enabled = True

        # 观察通道（主模型观察：周期截图，原图高质量优先）
        self._feed_enabled = True
        self._feed_interval = 8.0
        self._nudge_enabled = True
        self._nudge_interval = 5.0
        # 给主模型的截图：原图优先（0 = 不缩放）+ 高质量 JPEG；超字节预算才降质。
        self._feed_max_edge = 0
        self._feed_quality = 95
        self._feed_max_bytes = 160 * 1024
        self._last_feed_at = 0.0
        self._last_feed_fingerprint = ""
        self._last_feed_jpeg: bytes | None = None   # 最近一帧 JPEG，nudge 复用
        self._last_nudge_at = 0.0
        self._observer_thread: threading.Thread | None = None
        self._observer_stop = threading.Event()
        self._on_observation: Callable[[bytes, bool], None] | None = None

        # 命令状态
        self._goal = DEFAULT_GOAL
        self._speed = 1.0
        self._pending_instructions: list[str] = []

        # 可观测状态（供 get_status）
        self._phase = self.PHASE_IDLE
        self._last_error = ""
        self._last_grid_text = ""
        self._last_card_text = ""
        self._last_feedback = ""
        self._last_action_at = 0.0
        self._step_count = 0
        self._last_turn_time = 0.0

        # 故障上报节流
        self._empty_rounds = 0                       # 连续无动作轮数
        self._last_notify_at: dict[str, float] = {}  # kind → 最近通报时间

        # 防循环状态
        self._last_fail_key: tuple | None = None
        self._consecutive_fail = 0

    # ------------------------------------------------------------------ #
    #  配置
    # ------------------------------------------------------------------ #
    def configure(self, plugin_cfg: dict) -> None:
        """注入插件级配置（``[pvz_agent]``），含截图推送 / 开关 / 通知。"""
        with self._lock:
            self._window_keywords = [
                str(k) for k in plugin_cfg.get("window_title_keywords", []) if isinstance(k, str)
            ] or list(DEFAULT_WINDOW_KEYWORDS)
            self._notify_on_terminate = bool(plugin_cfg.get("notify_on_terminate", True))
            self._notify_window_lost = bool(plugin_cfg.get("notify_window_lost", True))
            self._sun_auto_collect = bool(plugin_cfg.get("sun_auto_collect", True))
            self._scan_grid_enabled = bool(plugin_cfg.get("scan_grid_enabled", True))
            self._scan_cards_enabled = bool(plugin_cfg.get("scan_cards_enabled", True))
            self._feed_enabled = bool(plugin_cfg.get("screenshot_feed_enabled", True))
            self._feed_interval = float(plugin_cfg.get("screenshot_feed_interval", 8.0) or 8.0)
            self._nudge_enabled = bool(plugin_cfg.get("screenshot_nudge_enabled", True))
            self._nudge_interval = float(plugin_cfg.get("screenshot_nudge_interval", 5.0) or 5.0)
            self._feed_max_edge = int(plugin_cfg.get("screenshot_max_edge_px", 0) or 0)
            self._feed_quality = int(plugin_cfg.get("screenshot_jpeg_quality", 95) or 95)
            self._feed_max_bytes = int(plugin_cfg.get("screenshot_max_bytes", 160 * 1024) or (160 * 1024))

    # ------------------------------------------------------------------ #
    #  懒加载：窗口（无需 VLM）与完整运行时（需要 VLM 密钥）
    # ------------------------------------------------------------------ #
    def _import_core(self) -> Any:
        if self._core is None:
            import pvz_agent  # noqa: PLC0415  # pvz/ 已在 sys.path

            # 核心包的 __init__ 只 re-export config，不会自动导入其它子模块。
            # 这里显式导入无 cv2 依赖的子模块，使其成为包属性（core.window 等）。
            from pvz_agent import (  # noqa: PLC0415, F401
                config,
                executor,
                parser,
                planner,
                prompts,
                vlm,
                window,
            )
            self._core = pvz_agent
        return self._core

    def _ensure_window(self) -> Any:
        """找到目标窗口并创建截图器。只需窗口关键词，无需 VLM 密钥。

        多个匹配窗口时自动取第一个（核心的 ``pick_single`` 是交互式 input()，
        在插件进程里会挂起，不能复用）。
        """
        if self._win is not None:
            return self._win
        keywords = list(self._window_keywords)
        screenshot_dir = "screenshots"
        try:
            cfg_path = CORE_DIR / "config.json"
            if cfg_path.is_file():
                j = json.loads(cfg_path.read_text(encoding="utf-8"))
                sd = j.get("agent", {}).get("screenshot_dir")
                if isinstance(sd, str) and sd:
                    screenshot_dir = sd
        except Exception:
            pass

        core = self._import_core()
        handles = core.window.find_target_windows(keywords)
        if not handles:
            raise RuntimeError(
                "未找到匹配的 PVZ 窗口（标题含 "
                + "/".join(keywords[:3])
                + " 等；cmd/资源管理器/终端已被自动排除）。"
                "请确认游戏已启动。"
            )
        win = handles[0]
        if len(handles) > 1:
            self._logger.info("找到 %d 个匹配窗口，自动使用第一个: %s", len(handles), win.title)
        self._win = win
        self._capturer = core.window.Capturer(win, screenshot_dir)
        return win

    def _ensure_runtime(self) -> Any:
        """构建完整运行时（执行器/扫描器/VLM/planner）。

        需要 pvz/.env 的 AI 决策密钥（执行核心决策用）；缺失时抛 RuntimeError
        （携带指引），由调用方转成可读错误返回。反复调用幂等。
        """
        if self._planner is not None:
            return self._cfg
        win = self._ensure_window()
        core = self._import_core()
        try:
            cfg = core.config.load_config()
        except SystemExit as exc:
            raise RuntimeError(
                f"PVZ 配置不完整（{exc.code}）。请检查 pvz/.env："
                "AI 服务地址 / 模型 / 密钥（参照 pvz/.env.example）。"
            )
        # 应用插件级开关（覆盖 pvz/config.json 的对应项）
        cfg.sun.enabled = bool(cfg.sun.enabled) and self._sun_auto_collect
        cfg.grid_scan.enabled = bool(cfg.grid_scan.enabled) and self._scan_grid_enabled
        cfg.card_scan.enabled = bool(cfg.card_scan.enabled) and self._scan_cards_enabled
        self._cfg = cfg

        # cv2 相关子模块（sun/grid_scan/card_scan/select_scan）模块级 import cv2。
        # opencv 在仓库的 galgame 依赖组；缺失时降级为纯 VLM 游玩（无 OpenCV 扫描）。
        try:
            from pvz_agent import card_scan, grid_scan, select_scan, sun  # noqa: PLC0415, F401
            has_cv2 = True
        except ImportError as exc:
            card_scan = grid_scan = select_scan = sun = None  # type: ignore[assignment]
            has_cv2 = False
            self._logger.warning(
                "[pvz_agent] opencv(cv2) 未安装（%s），降级为无 OpenCV 辅助扫描的纯 VLM 模式；"
                "如需植物/僵尸/卡片扫描，请 `uv sync --group galgame` 后重启插件。", exc,
            )

        self._executor = core.executor.Executor(win, cfg.layout, mouse_lock=self._mouse_lock)
        if has_cv2:
            self._sun_collector = sun.SunCollector(win, cfg.layout, cfg.sun, mouse_lock=self._mouse_lock)
            if cfg.grid_scan.enabled:
                self._grid_scanner = grid_scan.GridScanner(cfg.layout, cfg.grid_scan)
            if cfg.card_scan.enabled:
                self._card_scanner = card_scan.CardScanner(cfg.layout, cfg.card_scan)
                self._executor.attach_card_scanner(self._card_scanner)
            if cfg.select_scan.enabled:
                self._select_scanner = select_scan.SelectScanner(cfg.layout, cfg.select_scan)
                self._executor.attach_select_scanner(self._select_scanner)

        self._vlm = core.vlm.VLMClient(cfg.vlm)
        self._img_fmt = cfg.agent.image_format
        self._img_mime = "image/png" if cfg.agent.image_format == "png" else "image/jpeg"
        self._planner = core.planner.Planner(
            vlm=self._vlm,
            system_prompt=core.prompts.build_planner_system(cfg),
            max_rounds=cfg.agent.max_history_rounds,
            mime=self._img_mime,
            system_prompt_xml=core.prompts.build_planner_system_xml(cfg),
        )
        self._last_turn_time = time.perf_counter()
        return cfg

    def _runtime_ready(self) -> bool:
        return self._planner is not None

    # ------------------------------------------------------------------ #
    #  只读操作（任何 phase 下都可用）
    # ------------------------------------------------------------------ #
    def probe(self) -> dict[str, Any]:
        """启动自检：cv2 依赖 → 找窗口 → 截图。不抛异常，返回可读结果。"""
        result: dict[str, Any] = {"cv2": False, "window": False, "screenshot": False}
        try:
            import cv2  # noqa: PLC0415, F401  # 核心的 sun/grid_scan 模块级依赖
            result["cv2"] = True
        except Exception:
            result["message"] = (
                "缺少 opencv(cv2)。请执行 `uv sync --group galgame` 或 "
                "`uv pip install opencv-python-headless` 后重启插件。"
            )
            return result
        try:
            self._ensure_window()
            result["window"] = True
        except Exception as exc:
            result["message"] = f"窗口查找失败：{exc}"
            return result
        try:
            img = self._capturer.grab_pil()
            result["screenshot"] = True
            result["width"], result["height"] = int(img.size[0]), int(img.size[1])
        except Exception as exc:
            result["message"] = f"截图失败：{exc}"
        return result

    def get_status(self) -> dict[str, Any]:
        """当前可观测状态快照（线程安全）。"""
        with self._lock:
            phase = self._phase
            goal = self._goal
            speed = self._speed
            last_error = self._last_error
            last_grid = self._last_grid_text
            last_card = self._last_card_text
            last_feedback = self._last_feedback
            last_action_at = self._last_action_at
            step_count = self._step_count
            feed_enabled = self._feed_enabled
            last_feed_at = self._last_feed_at
            window_found = self._win is not None
            window_title = self._win.title if self._win is not None else ""
        return {
            "phase": phase,
            "goal": goal,
            "speed": speed,
            "running": phase == self.PHASE_RUNNING,
            "paused": phase == self.PHASE_PAUSED,
            "ready": self._runtime_ready(),
            "window": {"found": window_found, "title": window_title},
            "feed": {"enabled": feed_enabled, "last_push_at": last_feed_at},
            "steps": step_count,
            "last_action_at": last_action_at,
            "last_error": last_error,
            "last_grid": last_grid,
            "last_card": last_card,
            "last_feedback": last_feedback,
        }

    def grab_screenshot(self):
        """截取最新一帧并返回 PIL 图片（失败抛异常，由调用方处理）。"""
        self._ensure_window()
        assert self._capturer is not None
        return self._capturer.grab_pil()

    def scan_now(self) -> dict[str, Any]:
        """对当前画面做一次 OpenCV 网格 + 卡片扫描（无需 VLM）。"""
        try:
            self._ensure_window()
            img = self.grab_screenshot()
        except Exception as exc:
            return {"status": "error", "message": str(exc), "summary": str(exc)}
        self._ensure_scan_only_runtime()
        result: dict[str, Any] = {"status": "ok", "grid_text": "", "card_text": ""}
        if self._grid_scanner is not None:
            try:
                res = self._grid_scanner.scan(img)
                result["grid_text"] = res.to_text()
                result["plants"] = [list(p) for p in res.plants]
                result["zombie_rows"] = list(res.zombie_rows)
                result["zombie_cells"] = [list(c) for c in res.zombie_cells]
                result["empty"] = [list(e) for e in res.empty]
                result["occluded"] = res.occluded
                with self._lock:
                    self._last_grid_text = result["grid_text"]
            except Exception as exc:
                result["grid_error"] = str(exc)
        if self._card_scanner is not None:
            try:
                res = self._card_scanner.scan(img)
                result["card_text"] = res.to_text()
                result["available_cards"] = list(res.available)
                result["unavailable_cards"] = list(res.unavailable)
                with self._lock:
                    self._last_card_text = result["card_text"]
            except Exception as exc:
                result["card_error"] = str(exc)
        result["summary"] = (
            (result.get("grid_text") or "") + ("；" + result["card_text"] if result.get("card_text") else "")
        ) or "画面扫描无异常（当前可能不在战斗/选卡界面）。"
        return result

    def _ensure_scan_only_runtime(self) -> None:
        """在没有 VLM 配置时，仍能按 pvz/config.json 构建 OpenCV 扫描器（幂等）。"""
        if self._grid_scanner is not None or self._card_scanner is not None:
            return
        core = self._import_core()
        j: dict = {}
        cfg_path = CORE_DIR / "config.json"
        try:
            if cfg_path.is_file():
                j = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            j = {}

        def _apply(target, section: str) -> None:
            data = j.get(section, {})
            if not isinstance(data, dict):
                return
            for k, v in data.items():
                if hasattr(target, k):
                    setattr(target, k, v)

        layout = core.config.LayoutConfig()
        _apply(layout, "layout")
        if self._scan_grid_enabled:
            try:
                from pvz_agent import grid_scan  # noqa: PLC0415, F401
                gs_cfg = core.config.GridScanConfig()
                _apply(gs_cfg, "grid_scan")
                self._grid_scanner = grid_scan.GridScanner(layout, gs_cfg)
            except Exception as exc:
                self._logger.warning("[pvz-agent] 网格扫描器构建失败: %s", exc)
        if self._scan_cards_enabled:
            try:
                from pvz_agent import card_scan  # noqa: PLC0415, F401
                cs_cfg = core.config.CardScanConfig()
                _apply(cs_cfg, "card_scan")
                self._card_scanner = card_scan.CardScanner(layout, cs_cfg)
            except Exception as exc:
                self._logger.warning("[pvz-agent] 卡片扫描器构建失败: %s", exc)

    # ------------------------------------------------------------------ #
    #  命令（线程安全）
    # ------------------------------------------------------------------ #
    def start(self, goal: str | None = None, *, restart: bool = False) -> dict[str, Any]:
        """开始游玩（猫娘在玩，后台实时执行）。

        - 已在运行：仅更新目标；
        - 已暂停：恢复并（可选）更新目标；
        - 空闲/已停止/出错：新建循环线程（可干净重启——terminate/窗口丢失后都能再 start）。
        """
        with self._lock:
            phase = self._phase
        if restart and phase in (self.PHASE_RUNNING, self.PHASE_PAUSED):
            self.stop(reason="restart")
            phase = self.PHASE_IDLE
        try:
            self._ensure_runtime()
        except Exception as exc:
            with self._lock:
                self._phase = self.PHASE_ERROR
                self._last_error = str(exc)
            return {"status": "error", "message": str(exc), "summary": str(exc)}
        with self._lock:
            if goal:
                self._goal = goal.strip()
            if self._phase == self.PHASE_PAUSED:
                self._pause_evt.clear()
                self._phase = self.PHASE_RUNNING
                self._last_error = ""
                self._wake_evt.set()
                msg = "已恢复游玩。" + (" 目标已更新。" if goal else "")
                return {"status": "ok", "message": msg, "summary": msg, "resumed": True, "goal": self._goal}
            if self._phase == self.PHASE_RUNNING:
                self._last_error = ""
                self._wake_evt.set()
                msg = "猫娘已在游玩。" + (" 目标已更新。" if goal else "")
                return {"status": "ok", "message": msg, "summary": msg, "already_running": True, "goal": self._goal}
            # 空闲/停止/出错 → 新建循环（清干净所有停止/暂停标记）
            self._last_error = ""
            self._pause_evt.clear()
            self._stop_evt.clear()
            self._last_fail_key = None
            self._consecutive_fail = 0
            self._phase = self.PHASE_RUNNING
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="pvz-agent-loop", daemon=True)
            self._thread.start()
        if self._sun_collector is not None and not self._sun_collector.alive:
            self._sun_collector.start()
        # 开局：立即把一张高质量原图推给主模型，让她一开打就能看到当前战局。
        self._push_startup_screenshot()
        msg = f"已开始游玩（猫娘自己看画面操作）。目标：{self._goal}"
        return {"status": "ok", "message": msg, "summary": msg, "goal": self._goal}

    def pause(self, reason: str = "user") -> dict[str, Any]:
        with self._lock:
            if self._phase == self.PHASE_RUNNING:
                self._pause_evt.set()
                self._phase = self.PHASE_PAUSED
            phase = self._phase
        self._wake_evt.set()
        msg = "已暂停游玩（截图/扫描仍可用）。" if phase == self.PHASE_PAUSED else f"当前状态 {phase}，无需暂停。"
        return {"status": "ok" if phase == self.PHASE_PAUSED else "idle", "message": msg, "summary": msg}

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if self._phase == self.PHASE_PAUSED:
                self._pause_evt.clear()
                self._phase = self.PHASE_RUNNING
            phase = self._phase
        self._wake_evt.set()
        msg = "已恢复游玩。" if phase == self.PHASE_RUNNING else f"当前状态 {phase}，无需恢复。"
        return {"status": "ok" if phase == self.PHASE_RUNNING else "idle", "message": msg, "summary": msg}

    def stop(self, reason: str = "manual") -> dict[str, Any]:
        with self._lock:
            was_running = self._phase in (self.PHASE_RUNNING, self.PHASE_PAUSED)
        self._stop_loop(self.PHASE_IDLE)
        if was_running:
            msg = f"已停止游玩（{reason}）。"
        else:
            msg = "游玩当前未在运行。"
        return {"status": "ok", "message": msg, "summary": msg}

    def set_goal(self, goal: str) -> dict[str, Any]:
        if not goal or not goal.strip():
            return {"status": "error", "message": "goal 不能为空", "summary": "goal 不能为空"}
        with self._lock:
            self._goal = goal.strip()
        self._wake_evt.set()
        msg = f"目标已更新：{goal.strip()}"
        return {"status": "ok", "message": msg, "summary": msg, "goal": goal.strip()}

    def inject_instruction(self, instruction: str) -> dict[str, Any]:
        """注入一条自然语言打法引导，下轮 tick 时交给后台执行核心（持久进历史）。

        例如"先种豌豆射手"、"这波优先攒阳光"——猫娘用自然语言调整打法，
        不提供精确坐标/步骤，具体执行由执行核心完成。
        """
        if not instruction or not instruction.strip():
            return {"status": "error", "message": "instruction 不能为空", "summary": "instruction 不能为空"}
        with self._lock:
            self._pending_instructions.append(instruction.strip())
        self._wake_evt.set()
        msg = f"已记录打法引导：{instruction.strip()}"
        return {"status": "ok", "message": msg, "summary": msg}

    def set_speed(self, speed: float) -> dict[str, Any]:
        try:
            val = float(speed)
            if val < 0.1:
                raise ValueError
        except (TypeError, ValueError):
            return {"status": "error", "message": "speed 需要 >= 0.1 的数字", "summary": "speed 需要 >= 0.1 的数字"}
        with self._lock:
            self._speed = val
        self._wake_evt.set()
        msg = f"每轮间隔倍率已设为 {val:.1f}"
        return {"status": "ok", "message": msg, "summary": msg}

    def shutdown(self) -> None:
        """停止并清理（插件 shutdown 时调用）。"""
        self.stop_observer()
        self.stop(reason="plugin_shutdown")

    # ------------------------------------------------------------------ #
    #  主模型观察通道（周期截图）：观察线程
    # ------------------------------------------------------------------ #
    def start_observer(self, on_observation: Callable[[bytes, bool], None]) -> None:
        """启动观察线程（独立于执行循环，持续给主模型推截图）。

        ``on_observation(jpeg, nudge)`` 由 facade 注册：nudge=False → 纯截图(read)；
        nudge=True → 截图 + 短触发文本(respond)。插件重载时可干净重启。
        """
        self._on_observation = on_observation
        if self._observer_thread is not None and self._observer_thread.is_alive() and not self._observer_stop.is_set():
            return  # 已在运行
        # 旧线程正在退出（stop 事件已置位）→ 等它结束再起新的，避免重载后线程丢失
        if self._observer_thread is not None and self._observer_thread.is_alive():
            self._observer_thread.join(timeout=2.0)
        self._observer_stop.clear()
        self._observer_thread = threading.Thread(
            target=self._observer_loop, name="pvz-observer", daemon=True
        )
        self._observer_thread.start()

    def stop_observer(self) -> None:
        self._observer_stop.set()

    def _observer_loop(self) -> None:
        while not self._observer_stop.is_set():
            self._observer_stop.wait(1.0)
            if self._observer_stop.is_set():
                break
            try:
                self._observer_tick()
            except Exception as exc:
                self._logger.warning("[pvz-agent] 观察线程异常: %s", exc)
        self._logger.info("[pvz-agent] 观察线程已退出。")

    def _observer_tick(self) -> None:
        """一次观察决策：feed（纯截图 read）+ nudge（截图+触发 respond）。可单测直调。"""
        now = time.time()
        # 被动 feed：每 feed_interval 推一帧纯截图（画面没变不推）
        if self._feed_enabled:
            with self._lock:
                feed_due = not (self._last_feed_at and now - self._last_feed_at < self._feed_interval)
            if feed_due:
                data = self._grab_feed_frame()
                if data:
                    self._push_observation(data, nudge=False)
        # 主动 nudge：游玩中时，每 nudge_interval 推截图+短触发，催猫娘看并行动
        if self._nudge_enabled:
            with self._lock:
                running = self._phase == self.PHASE_RUNNING
                nudge_due = not (self._last_nudge_at and now - self._last_nudge_at < self._nudge_interval)
            if running and nudge_due:
                data = self._nudge_frame()
                if data is not None:
                    with self._lock:
                        self._last_nudge_at = time.time()
                    self._push_observation(data, nudge=True)

    def _grab_feed_frame(self) -> bytes | None:
        """截最新一帧 → 指纹去重 → JPEG 压缩 → 缓存。窗口不可用/画面没变返回 None。"""
        try:
            img = self.grab_screenshot()
        except Exception:
            return None
        fp = self._frame_fingerprint(img)
        if fp == self._last_feed_fingerprint:
            return None
        self._last_feed_fingerprint = fp
        with self._lock:
            self._last_feed_at = time.time()
        jpeg = self.encode_jpeg(img)
        self._last_feed_jpeg = jpeg
        return jpeg

    def _nudge_frame(self) -> bytes | None:
        """供 nudge 用的截图帧：优先复用最近一次 feed 缓存帧，否则现场截一帧。

        与 ``_grab_feed_frame`` 不同，**不做画面指纹去重**——nudge 的目的是周期性
        唤起猫娘行动，即使画面几乎没变也要推（画面静止时 feed 不会更新缓存帧，
        若只依赖 feed 缓存，nudge 会一直拿不到帧而静默）。
        """
        if self._last_feed_jpeg is not None:
            return self._last_feed_jpeg
        try:
            img = self.grab_screenshot()
        except Exception:
            return None
        return self.encode_jpeg(img)

    def _push_startup_screenshot(self) -> None:
        """开始游玩时，立即把一张高质量原图推给主模型（开局画面），
        让她一开打就能看到当前战局。算作一次 nudge（更新节流时间戳）。"""
        if self._on_observation is None:
            return
        jpeg = self._nudge_frame()
        if jpeg is None:
            return
        with self._lock:
            self._last_nudge_at = time.time()
        self._push_observation(jpeg, nudge=True)

    def _push_observation(self, jpeg: bytes, nudge: bool) -> None:
        if self._on_observation is None:
            return
        try:
            self._on_observation(jpeg, nudge)
        except Exception as exc:
            self._logger.warning("[pvz-agent] 观察推送回调失败: %s", exc)

    @staticmethod
    def _frame_fingerprint(img) -> str:
        """96px 缩略图的 md5，用于判定画面是否变化（稳定、廉价）。"""
        thumb = img.copy()
        thumb.thumbnail((96, 96))
        if thumb.mode != "RGB":
            thumb = thumb.convert("RGB")
        return hashlib.md5(thumb.tobytes()).hexdigest()

    def encode_jpeg(self, img) -> bytes:
        """把画面编码为主模型视野用的 JPEG：**优先原图 + 高质量**。

        先按原分辨率、高 JPEG 质量编码；若超过 ``_feed_max_bytes`` 字节预算
        （message_plane 单条 payload 上限默认 256KB，图片走 base64 有 ~33% 膨胀），
        再按 edge→quality 阶梯逐步降质，直到落入预算——主模型看到尽可能清晰
        的原图，同时不会撑爆消息通道。``_feed_max_edge = 0`` 表示不缩放。
        """
        from PIL import Image

        if img.mode != "RGB":
            img = img.convert("RGB")
        budget = self._feed_max_bytes
        base_edge = self._feed_max_edge
        base_quality = self._feed_quality

        edges: list[int] = []
        for e in (base_edge, base_edge // 2, base_edge // 4):
            if e and e > 0 and e not in edges:
                edges.append(e)
        if not edges:  # max_edge=0 → 原图优先
            edges = [max(img.size) or 1]
        # 质量阶梯：绝不高于配置质量；超预算时逐步降。
        qualities = [q for q in (base_quality, 80, 65, 50, 40, 30) if q <= base_quality]
        if not qualities:
            qualities = [base_quality]

        smallest: bytes | None = None
        last: bytes | None = None
        for edge in edges:
            frame = img if max(img.size) <= edge else img.copy()
            if max(frame.size) > edge:
                frame.thumbnail((edge, edge), Image.LANCZOS)
            for ql in qualities:
                buf = io.BytesIO()
                frame.save(buf, format="JPEG", quality=ql, optimize=True)
                data = buf.getvalue()
                last = data
                if budget <= 0 or len(data) <= budget:
                    return data
                if smallest is None or len(data) < len(smallest):
                    smallest = data
        return smallest if smallest is not None else (last if last is not None else b"")

    # ------------------------------------------------------------------ #
    #  后台主循环（实时游玩执行）
    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            if self._pause_evt.is_set():
                # 暂停中：只观察不执行，短睡以响应 resume/stop
                self._sleep_interruptible(0.5)
                continue
            try:
                self._tick()
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                    self._phase = self.PHASE_ERROR
                self._logger.warning("[pvz-agent] 本轮异常: %s", exc)
                self._sleep_interruptible(1.0)
        self._stop_sun()
        self._logger.info("[pvz-agent] 主循环已退出。")

    def _sleep_interruptible(self, seconds: float) -> None:
        """节拍等待，可被任何命令（pause/resume/stop/instruction）提前唤醒。"""
        if seconds <= 0:
            return
        self._wake_evt.clear()
        if self._wake_evt.wait(seconds):
            self._wake_evt.clear()

    def _tick(self) -> None:
        """一轮迭代：拍帧 → 消费指令 → 扫描 → 执行核心规划 → 执行 → 反馈。"""
        cfg = self._cfg
        capturer = self._capturer
        assert cfg is not None and capturer is not None

        # 1. 拍一帧（供执行核心复用）
        try:
            img = capturer.grab_pil()
        except Exception as exc:
            # 窗口丢失/最小化到抓不到 → 通知主模型并彻底停止，不再无限重试
            self._handle_window_lost(exc)
            return

        # 2. 消费主模型注入的自然语言引导
        notes = self._drain_instructions()
        for note in notes:
            self._planner.add_user_note(note)

        # 3. OpenCV 网格/卡片扫描（执行核心辅助信息）
        grid_text = self._scan_grid(img)
        card_text = self._scan_cards(img)

        now = time.perf_counter()
        elapsed = now - self._last_turn_time
        self._last_turn_time = now

        user_text = self._core.prompts.build_planner_user_footer(
            goal=self._goal,
            elapsed=elapsed,
            last_summary=self._last_feedback,
            note="",
            grid_state=grid_text,
            card_state=card_text,
        )
        img_b64 = capturer.to_base64(
            img, image_format=self._img_fmt, jpeg_quality=cfg.agent.jpeg_quality
        )

        # 4. 执行核心（VLM）规划：看截图 + 目标/引导 → 具体动作
        try:
            calls, raw = self._planner.plan(img_b64, user_text)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            self._logger.warning("[pvz-agent] 执行核心规划失败: %s", exc)
            self._notify_throttled(
                f"[PVZ] 决策引擎调用失败（{type(exc).__name__}: {exc}）。已在重试。",
                kind="planner_error",
                cooldown=30.0,
            )
            self._sleep_interruptible(2)
            return
        if self._stop_evt.is_set():
            return  # 规划期间被 stop，不执行动作

        # 5. 执行动作
        results: list[dict] = []
        if not calls:
            # 空动作：绝不静默——通报主模型（按连续轮数节流/升级）
            self._handle_no_actions()
            self._last_feedback = _build_feedback(results)
            self._sleep_interruptible(self._compute_wait_seconds([], cfg))
            return
        with self._lock:
            self._empty_rounds = 0  # 有动作了，重置空动作计数

        stop_round = False
        terminate_status: str | None = None
        for tc in calls:
            action = tc.arguments.get("action") or tc.name
            if action == "terminate":
                terminate_status = tc.arguments.get("status", "success")
                results.append({"action": "terminate", "status": "ok", "terminate_status": terminate_status})
                stop_round = True
                break
            if action == "answer":
                ans = tc.arguments.get("text", "")
                self._notify_text(f"[PVZ] {ans}", kind="answer")
                results.append({"action": "answer", "status": "ok", "text": ans})
                continue

            result = self._executor.execute_tool_call(tc.name, tc.arguments)
            results.append(result)
            if result.get("status") == "error":
                self._logger.warning("[pvz-agent] %s 失败: %s", tc.name, result.get("error", ""))
            fail_key = self._fail_key(tc, result)
            if fail_key is not None:
                if fail_key == self._last_fail_key:
                    self._consecutive_fail += 1
                else:
                    self._consecutive_fail = 1
                    self._last_fail_key = fail_key
                if self._consecutive_fail >= MAX_CONSECUTIVE_FAIL:
                    results.append({
                        "action": "_loop_guard", "status": "error",
                        "error": "连续失败，请换目标或坐标，不要重复相同操作",
                    })
                    stop_round = True
                    break

        # 6. 反馈回填给执行核心
        self._planner.add_assistant(raw)
        feedback = _build_feedback(results)
        self._planner.add_feedback(feedback)
        with self._lock:
            self._last_feedback = feedback
            self._step_count += 1
            self._last_action_at = time.time()

        # 本轮有失败动作（未触发循环守卫时也要通报主模型，不静默）
        failed = [r for r in results if r.get("status") == "error"]
        if failed:
            first = failed[0]
            self._notify_throttled(
                f"[PVZ] 本轮有 {len(failed)} 个操作失败"
                f"（如 {first.get('action', '?')}: {first.get('error', '')}）。"
                "已反馈给执行核心；必要时可暂停或调整打法。",
                kind="action_error",
                cooldown=30.0,
            )

        if terminate_status is not None:
            self._planner.reset()  # 清历史，准备新目标
            self._on_terminate(terminate_status)
            return

        if stop_round:
            self._planner.add_user_note(
                "上轮同一动作连续失败多次。请停止重复，重新观察截图，换一个目标或调整坐标。"
            )
            self._notify_throttled(
                "[PVZ] 连续重复操作失败，已中止本轮并自动换打法；"
                "你也可以用 pvz_instruction 明确引导换个打法。",
                kind="action_error",
                cooldown=30.0,
            )

        # 7. 节拍等待（可被命令唤醒）
        self._sleep_interruptible(self._compute_wait_seconds(results, cfg))

    # ------------------------------------------------------------------ #
    #  辅助
    # ------------------------------------------------------------------ #
    def _drain_instructions(self) -> list[str]:
        with self._lock:
            notes = list(self._pending_instructions)
            self._pending_instructions.clear()
        return notes

    def _scan_grid(self, img) -> str:
        if self._grid_scanner is None:
            return ""
        try:
            res = self._grid_scanner.scan(img)
            text = res.to_text()
            with self._lock:
                self._last_grid_text = text
            return text
        except Exception as exc:
            self._logger.warning("[pvz-agent] 网格扫描异常: %s", exc)
            return ""

    def _scan_cards(self, img) -> str:
        if self._card_scanner is None:
            return ""
        try:
            res = self._card_scanner.scan(img)
            text = res.to_text()
            with self._lock:
                self._last_card_text = text
            return text
        except Exception as exc:
            self._logger.warning("[pvz-agent] 卡片扫描异常: %s", exc)
            return ""

    def _handle_window_lost(self, exc: Exception) -> None:
        """窗口抓不到（游戏被关/最小化/画面不可见）→ 通报主模型并彻底停止循环。"""
        self._logger.warning("[pvz-agent] 窗口丢失: %s", exc)
        with self._lock:
            self._last_error = str(exc)
        if self._notify_window_lost:
            self._notify_text(
                f"[PVZ] 游戏窗口丢失/不可用（{exc}）。已停止游玩；"
                "确认游戏仍在运行后可让主模型重新开始。",
                kind="window_lost",
            )
        self._stop_loop(self.PHASE_IDLE)

    def _on_terminate(self, status: str) -> None:
        """游戏内判定本关结束：通报主模型并**彻底停止**，保证可再次 start。

        步数已在 _tick 的动作执行段累计，这里不再重复计数。
        """
        if self._notify_on_terminate:
            self._notify_text(
                f"PVZ 本关已判定结束（{status}）。已停止游玩，可发新指令重新开始。",
                kind="terminate",
            )
        self._logger.info("[pvz-agent] 本关结束: %s，已停止（可重启）。", status)
        self._stop_loop(self.PHASE_IDLE)

    def _stop_loop(self, phase_after: str) -> None:
        """彻底停掉循环线程与阳光收集，phase 置为 ``phase_after``。幂等。"""
        self._stop_evt.set()
        self._wake_evt.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
            if thread.is_alive():
                self._logger.warning("[pvz-agent] 循环线程在超时内未退出，转为后台继续结束（daemon）。")
        self._stop_sun()
        with self._lock:
            self._phase = phase_after

    def _notify_text(self, text: str, *, kind: str = "") -> None:
        """后台线程安全地转达文本给宿主（无 notifier 时仅记日志）。"""
        try:
            if self._notifier is not None:
                self._notifier(text=text, kind=kind)
        except Exception as exc:
            self._logger.warning("[pvz-agent] 推送失败(%s): %s", kind, exc)

    def _notify_throttled(self, text: str, *, kind: str, cooldown: float = 30.0) -> None:
        """按 kind 节流地通报（避免同一种故障每轮刷屏）。"""
        now = time.time()
        with self._lock:
            last = self._last_notify_at.get(kind, 0.0)
            if now - last < cooldown:
                return
            self._last_notify_at[kind] = now
        self._notify_text(text, kind=kind)

    def _handle_no_actions(self) -> None:
        """本轮没做出有效操作——绝不静默。

        首次出现即通报主模型，连续出现按 1/3/6/9... 轮升级（消息带轮数与原因）。
        """
        with self._lock:
            self._empty_rounds += 1
            empty_rounds = self._empty_rounds
        reason = ""
        try:
            if self._planner is not None:
                reason = str(getattr(self._planner, "last_status_text", "") or "")
        except Exception:
            pass
        self._logger.warning(
            "[pvz-agent] 本轮无动作（连续第 %d 轮）: %s",
            empty_rounds, reason or "原因未知",
        )
        if empty_rounds == 1 or empty_rounds % 3 == 0:
            reason_part = reason or "执行核心未产出可执行动作"
            self._notify_text(
                f"[PVZ] 已连续 {empty_rounds} 轮没有做出有效操作"
                f"（{reason_part}）。已继续观察；若持续可暂停或调整打法。",
                kind="no_action",
            )

    def _compute_wait_seconds(self, results: list[dict], cfg) -> float:
        base = cfg.agent.tick_interval * self._speed
        return _compute_wait(results, self._executor, base)

    def _stop_sun(self) -> None:
        try:
            if self._sun_collector is not None:
                self._sun_collector.stop()
        except Exception:
            pass

    @staticmethod
    def _fail_key(tc, result: dict) -> tuple | None:
        """提取"动作+坐标"作为防循环标识；仅对失败的带坐标动作有意义。"""
        if result.get("status") != "error":
            return None
        action = tc.arguments.get("action") or tc.name
        if action in ("place_plant",):
            return ("place_plant", tc.arguments.get("card_index"), tc.arguments.get("row"), tc.arguments.get("col"))
        if action in ("shovel",):
            return ("shovel", tc.arguments.get("row"), tc.arguments.get("col"))
        if action == "left_click":
            coord = tuple(tc.arguments.get("coordinate", []))
            return ("left_click", coord)
        return (action,)


__all__ = ["PvZAgentService", "DEFAULT_GOAL", "CORE_DIR"]
