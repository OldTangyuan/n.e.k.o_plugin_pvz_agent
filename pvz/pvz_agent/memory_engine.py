"""纯文本模式的内存运行时：读游戏进程内存获取状态 + 代码注入执行动作。

与 pvz_agent.executor.Executor（pyautogui 鼠标 + OpenCV）完全隔开：

- 状态：``PvZStateReader`` 从进程内存读取，格式化为权威结构化文本，喂给文本 LLM；
- 动作：``PvZExecutor`` 代码注入执行（place_plant / shovel / select_seeds / win_level /
  click_card），阳光用注入器 ``set_auto_collect`` 自动收集；
- 接口与 ``Executor`` 对齐（``execute_tool_call`` + ``ACTION_DELAYS``），
  供 service 主循环统一调度。

依赖 vendored ``pvz_memory``（pvz/vendor/pvz_memory），零第三方运行时依赖。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from typing import Any

from pvz_memory import PvZExecutor, PvZMemory, PvZStateReader
from pvz_memory.reader import GameState


class MemoryGameEngine:
    """内存驱动游戏运行时（纯文本模式）。"""

    # 各动作执行后的最低观察等待（秒），供主循环计算等待时间
    ACTION_DELAYS: dict[str, float] = {
        "place_plant": 2.0,
        "shovel": 2.0,
        "click_card": 1.5,
        "select_seeds": 2.5,
        "win_level": 3.0,
    }
    DEFAULT_DELAY: float = 1.5

    def __init__(self, logger: Any | None = None) -> None:
        self._logger = logger or logging.getLogger("pvz_agent.memory_engine")
        self._mem: PvZMemory | None = None
        self._reader: PvZStateReader | None = None
        self._executor: PvZExecutor | None = None
        self.error = ""                # connect 失败 / 注入器不可用时的原因
        self.injector_ok = False       # 代码注入器是否可用
        self.auto_collect_ok = False   # 阳光自动收集是否开启
        # 失焦不暂停看门狗（start_force_run / stop_force_run）
        self._force_run_stop: threading.Event | None = None
        # 选卡门控：同一选卡会话只允许选一次，防止模型反复 select_seeds 出问题。
        self._seeds_selected = False   # 本会话已选卡
        self._prev_in_select = False   # 上一轮是否处于选卡界面（用于检测新选卡会话）
        # 是否允许 AgentB 操控选卡界面（默认关：选卡场景不触发 LLM，由玩家手动选卡）。
        self._allow_seed_selection = False

    # ------------------------------------------------------------------ #
    #  连接 / 断开
    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        """连接 PvZ 进程并初始化读取器/执行器（幂等）。失败时 self.error 带原因。"""
        if self.is_connected():
            return True
        mem = PvZMemory()
        if not mem.connect():
            self.error = self._describe_connect_failure(mem)
            return False
        self._mem = mem
        self._reader = PvZStateReader(mem)
        try:
            self._executor = PvZExecutor(mem)
        except Exception as exc:
            self.error = f"PvZExecutor 初始化失败: {exc}"
            self._executor = None
            return False

        injector = self._executor.injector
        self.injector_ok = injector is not None
        if injector is None:
            self.error = self._executor.inject_error or "代码注入器未启用"
            self._logger.warning("[memory] 注入器未启用（%s）；动作将退回鼠标模式，阳光不会自动收集", self.error)
        else:
            try:
                injector.set_auto_collect(True)
                self.auto_collect_ok = True
            except Exception as exc:
                self._logger.warning("[memory] 开启自动收集阳光失败: %s", exc)
            self.error = ""
        return True

    def is_connected(self) -> bool:
        return self._mem is not None and self._mem.is_connected()

    def close(self) -> None:
        """恢复注入 hack + 断开内存连接（幂等）。"""
        try:
            if self._executor is not None:
                self._executor.close()
        except Exception:
            pass
        try:
            if self._mem is not None:
                self._mem.disconnect()
        except Exception:
            pass
        self.stop_force_run()
        self._executor = None
        self._reader = None
        self._mem = None
        self.injector_ok = False
        self.auto_collect_ok = False
        self.error = ""

    # ------------------------------------------------------------------ #
    #  状态读取
    # ------------------------------------------------------------------ #
    def read_state(self) -> GameState:
        if self._reader is None:
            raise RuntimeError("内存未连接")
        state = self._reader.read_state()
        self._update_selection_gate(state)
        return state

    def _update_selection_gate(self, state: GameState) -> None:
        """根据当前界面维护"选卡门控"：从非选卡 → 进入选卡界面 = 新选卡会话，允许再次选卡。

        同一选卡会话内选过一次后，``_seeds_selected`` 保持 True（阻止重复 select_seeds）；
        进入下一关/生存模式下一轮的选卡界面时才重置。
        """
        ui = getattr(state, "game_ui", 0)
        in_select = ui == self._GAME_UI_SELECT_CARD
        if in_select and not self._prev_in_select:
            # 新的选卡会话开始（从非选卡进入选卡界面）→ 允许再次选卡
            self._seeds_selected = False
        self._prev_in_select = in_select

    def read_state_text(
        self,
        state: GameState | None = None,
        *,
        fallback_grid: tuple[int, int] | None = None,
    ) -> str:
        """读最新游戏状态并格式化为权威文本（供文本 LLM 决策）。

        ``state`` 已由调用方读好时传入，避免二次读取。文本顶部会加一行
        【棋盘】行列数——**以读内存的实际数据为准**（行数=割草机数量，列数回退 fallback）。
        """
        if self._reader is None:
            raise RuntimeError("内存未连接")
        if state is None:
            state = self._reader.read_state()
        if state.last_error:
            raise RuntimeError(f"内存状态读取异常: {state.last_error}")
        text = self._reader.format_state(state)
        rows, cols = self.grid_dims(fallback=fallback_grid)
        if rows and cols:
            grid_line = f"【棋盘】{rows} 行 x {cols} 列（row 0~{rows-1} / col 0~{cols-1}，0-based）"
            return f"{grid_line}\n{text}"
        return text

    def format_state(self, state: GameState) -> str:
        """把已读状态格式化为文本（供已判定的可操作状态喂 LLM）。"""
        if self._reader is None:
            raise RuntimeError("内存未连接")
        return self._reader.format_state(state)

    def grid_dims(self, fallback: tuple[int, int] | None = None) -> tuple[int, int]:
        """从内存读实际棋盘行列数：行数 = 割草机数量（标准 5 / 泳池 6），列数回退。

        读失败 / 数值非法时回退 ``fallback``（调用方传 config 的 rows/cols）。
        """
        fb_rows, fb_cols = fallback or (0, 0)
        if self._mem is not None:
            try:
                mo = self._mem.main_object
                off = self._mem.offsets
                rows = self._mem.read_int(mo + off.lawn_mower_count_max)
                if 1 <= rows <= 20:
                    return rows, fb_cols or 9
            except Exception:
                pass
        return fb_rows, fb_cols

    # GameUI.SELECT_CARD = 2（与 pvz_memory.offsets.GameUI 一致，避免额外导入）
    _GAME_UI_SELECT_CARD = 2

    def set_seed_selection_enabled(self, enabled: bool) -> None:
        """设置是否允许 AgentB 操控选卡界面。

        默认关闭：选卡场景**不触发 LLM**（玩家手动选卡）；开启后选卡界面喂 LLM 决策。
        """
        self._allow_seed_selection = bool(enabled)

    def is_actionable(self, state: GameState) -> bool:
        """该状态是否值得喂给 LLM 决策。

        - 战斗界面（in_battle）→ 可操作；
        - 选卡界面（SELECT_CARD）→ 仅在 ``agent_controls_seed_selection=true`` 时可操作
          （默认关：选卡不触发 LLM，由玩家手动选卡）；
        - 主菜单 / 结算 / 未知界面 → 不可操作（不喂 LLM，只轮询等待，循环不停不暂停）。
        """
        if state.in_battle:
            return True
        if not self._allow_seed_selection:
            return False
        return getattr(state, "game_ui", 0) == self._GAME_UI_SELECT_CARD

    # ------------------------------------------------------------------ #
    #  失焦不暂停（强制运行看门狗）
    # ------------------------------------------------------------------ #
    def start_force_run(self, interval: float = 0.2) -> None:
        """启动"强制运行"看门狗：PvZ 窗口失焦导致游戏自动暂停时，持续清 game_paused=0。

        仅在**窗口失焦**时干预（前台时不动，尊重用户手动 Esc 暂停）。
        注入器不可用（非支持版本）时静默跳过。幂等。
        """
        if self._force_run_stop is not None:
            return
        if self._executor is None or self._executor.injector is None:
            return
        self._force_run_stop = threading.Event()
        threading.Thread(
            target=self._force_run_loop, args=(max(interval, 0.05),),
            name="pvz-force-run", daemon=True,
        ).start()

    def stop_force_run(self) -> None:
        """停止看门狗（幂等）。"""
        if self._force_run_stop is not None:
            self._force_run_stop.set()
            self._force_run_stop = None

    def _force_run_loop(self, interval: float) -> None:
        evt = self._force_run_stop
        previously_paused = False
        while evt is not None and not evt.is_set():
            try:
                if self._paused_by_focus_loss():
                    paused = self._is_game_paused()
                    if paused and not previously_paused:
                        # 失焦时游戏刚进入暂停 → 发 Esc 让游戏走自己的恢复逻辑
                        # （真正关闭暂停面板，直接写 game_paused 关不掉 UI 面板），
                        # 再清一次标志兜底。
                        self._send_esc()
                        self._clear_game_paused()
                    previously_paused = paused
                else:
                    previously_paused = False  # 回到前台，reset 暂停边沿
            except Exception:
                pass  # 看门狗尽力而为，读/写失败不影响主流程
            evt.wait(interval)

    def _paused_by_focus_loss(self) -> bool:
        """PvZ 窗口是否失焦（失焦 = 游戏自动暂停，需要被看门狗取消）。"""
        hwnd = getattr(self._mem, "_hwnd", 0) if self._mem else 0
        if not hwnd or not hasattr(ctypes, "windll"):
            return False
        try:
            return ctypes.windll.user32.GetForegroundWindow() != hwnd
        except Exception:
            return False

    def _is_game_paused(self) -> bool:
        """读 game_paused 标志（PvZ 的暂停状态）。"""
        if self._mem is None:
            return False
        try:
            addr = self._mem.main_object + self._mem.offsets.game_paused
            return bool(self._mem.read_int(addr))
        except Exception:
            return False

    def _clear_game_paused(self) -> bool:
        """写 game_paused=0（PvZ 失焦自动暂停标志），让游戏继续推进。"""
        if self._executor is None or self._executor.injector is None or self._mem is None:
            return False
        addr = self._mem.main_object + self._mem.offsets.game_paused
        self._executor.injector.write_int(addr, 0)
        return True

    def _send_esc(self) -> None:
        """向游戏窗口发一次 Esc（WM_KEYDOWN/UP），触发游戏暂停/恢复逻辑。

        直接写 game_paused=0 关不掉暂停面板（面板由游戏 SetPaused 管理）；
        Esc 让游戏走自己的恢复路径，真正关闭面板。
        """
        hwnd = getattr(self._mem, "_hwnd", 0) if self._mem else 0
        if not hwnd or not hasattr(ctypes, "windll"):
            return
        try:
            user32 = ctypes.windll.user32
            user32.PostMessageW(hwnd, 0x0100, 0x1B, 0)  # WM_KEYDOWN, VK_ESCAPE
            user32.PostMessageW(hwnd, 0x0101, 0x1B, 0)  # WM_KEYUP, VK_ESCAPE
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  动作执行（与 Executor 接口对齐）
    # ------------------------------------------------------------------ #
    def execute_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 tool_call 的 name 分派动作（控制类直接返回，游戏类注入执行）。"""
        action = arguments.get("action") or name

        # 控制类：终止/回答/等待（无注入操作）
        if action == "terminate":
            return {"action": "terminate", "status": "ok", "terminate_status": arguments.get("status", "success")}
        if action == "answer":
            return {"action": "answer", "status": "ok", "text": arguments.get("text", "")}
        if action == "wait":
            return {"action": "wait", "status": "ok", "waited": float(arguments.get("time", 1.0))}

        if self._executor is None:
            return {"action": action, "status": "error", "error": self.error or "内存执行器未就绪"}
        if not self.is_connected():
            return {"action": action, "status": "error", "error": "内存连接已断开"}
        if not self._executor.can_execute(action):
            return {"action": action, "status": "error", "error": f"纯文本模式不支持的动作: {action}"}

        # 读最新内存状态（种植/铲除/通关需要战斗内 + 权威校验由 PvZExecutor 内部完成）
        try:
            state = self.read_state()
        except Exception as exc:
            return {"action": action, "status": "error", "error": f"读取游戏状态失败: {exc}"}

        # 选卡：只在选卡界面调用 + 同一选卡会话只允许一次 + seeds 名字→类型id 解析
        if action == "select_seeds":
            return self._execute_select_seeds(arguments, state)

        if not state.in_battle and action in ("place_plant", "shovel", "win_level"):
            return {"action": action, "status": "error",
                    "error": f"当前不在战斗中（UI={state.game_ui}），无法执行 {action}；可选卡请用 select_seeds"}
        return self._executor.execute(action, arguments, state)

    def _execute_select_seeds(self, arguments: dict, state: GameState) -> dict:
        """选卡执行（带门控 + 名字解析）。

        同一选卡会话内选过一次后不再允许（防止模型反复 select_seeds 出问题），
        进入下一关/生存模式下一轮的选卡界面时才重置。
        """
        if not self._allow_seed_selection:
            return {"action": "select_seeds", "status": "error",
                    "error": "选卡由玩家手动控制（agent_controls_seed_selection=false），AgentB 不操控选卡"}
        if self._seeds_selected:
            return {"action": "select_seeds", "status": "error",
                    "error": "本关已完成选卡，不能重复选卡；等待下一关/下一轮选卡界面再选"}
        ui = getattr(state, "game_ui", 0)
        if ui != self._GAME_UI_SELECT_CARD:
            return {"action": "select_seeds", "status": "error",
                    "error": f"当前不在选卡界面（UI={ui}），无法选卡"}
        raw = arguments.get("seeds")
        if not isinstance(raw, list) or not raw:
            return {"action": "select_seeds", "status": "error",
                    "error": "select_seeds 需要 seeds 参数（植物名列表如 ['向日葵','豌豆射手'] 或类型id列表）"}
        resolved = self._resolve_seeds(raw)
        if isinstance(resolved, str):
            return {"action": "select_seeds", "status": "error", "error": resolved}
        args = dict(arguments)
        args["seeds"] = resolved
        result = self._executor.execute("select_seeds", args, state)
        if result.get("status") == "ok":
            self._seeds_selected = True  # 选卡成功 → 锁定，直到下一选卡会话
        return result

    def _resolve_seeds(self, raw: list) -> list[int] | str:
        """把 seeds 解析成植物类型 id 列表：支持名字（'向日葵'）或 id（0/1）。

        返回类型 id 列表；有无法识别的项时返回错误描述字符串。
        """
        from pvz_memory.offsets import PLANT_NAMES  # noqa: PLC0415

        name_to_id = {name: t for t, name in PLANT_NAMES.items() if name}
        resolved: list[int] = []
        for item in raw:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                resolved.append(item)
                continue
            if isinstance(item, str):
                key = item.strip()
                if key in name_to_id:
                    resolved.append(name_to_id[key])
                    continue
                if key.isdigit():
                    resolved.append(int(key))
                    continue
                return f"未知植物名: {item}（可从【可选植物库】按名字选，如 向日葵/豌豆射手）"
            return f"select_seeds 的 seeds 元素需是植物名或类型id: {item!r}"
        if not resolved:
            return "seeds 为空，请从【可选植物库】选择植物"
        return resolved

    # ------------------------------------------------------------------ #
    #  辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _describe_connect_failure(mem: PvZMemory) -> str:
        """把 connect 失败翻译成可读原因。"""
        from pvz_memory.offsets import PvZVersion  # noqa: PLC0415

        ver = getattr(mem, "_version", PvZVersion.NOT_FOUND)
        if ver == PvZVersion.OPEN_ERROR:
            return "无法打开 PvZ 进程（需要以管理员身份运行宿主）"
        if ver == PvZVersion.NOT_FOUND:
            return "未找到 PvZ 进程（请确认游戏已启动且为 pvz_memory 支持的版本）"
        if ver == PvZVersion.UNSUPPORTED:
            return f"不支持的游戏版本（version={ver}），请用受支持版本（见 pvz_memory 说明）"
        return f"连接 PvZ 内存失败（version={ver}）"
