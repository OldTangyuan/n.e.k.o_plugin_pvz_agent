"""PvZ 双 Agent 主循环：截图 → 规划执行 → 反馈 → 描述现状 → 节拍等待。

Agent B (planner) 决策并执行；Agent A (narrator) 描述现状仅展示给用户。
用户调控（controller）在后台线程解析命令，主循环每轮读取其状态。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# 内置依赖：cv2 / pvz_memory 在 pvz/vendor/（不随 pip 安装），加入 sys.path。
# 支持直接从仓库根 `python -m pvz.pvz_agent.main` 运行，也兼容 pvz/ 下 `python -m pvz_agent.main`。
_PVZ_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if str(_PVZ_VENDOR) not in sys.path:
    sys.path.insert(0, str(_PVZ_VENDOR))

from .config import load_config
from .controller import Controller
from .memory_engine import MemoryGameEngine
from .narrator import Narrator
from .parser import ToolCall
from .planner import Planner
from .prompts import (
    build_planner_system,
    build_planner_system_text,
    build_planner_system_text_xml,
    build_planner_system_xml,
    build_planner_tools,
    build_planner_tools_text,
    build_planner_user_footer,
    translate_action,
)
from .vlm import VLMClient
from .window import Capturer, wait_for_window

DEFAULT_GOAL = "自动玩完当前这一关并尽可能取得胜利"

# 防循环：同一动作 + 同坐标连续失败达到该次数则强制停止该轮并提示换策略
MAX_CONSECUTIVE_FAIL = 2


def _build_feedback(results: list[dict]) -> str:
    """把执行结果文本化为反馈，回填给 Agent B。"""
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


def _compute_wait(results: list[dict], executor: Executor, base: float) -> float:
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


class GameAgentApp:
    """组装所有模块并跑主循环。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

        # 窗口：精确标题匹配 + 轮询等待（可编辑 pvz/config.json 的 window_titles）
        self.win = wait_for_window(
            titles_provider=lambda: list(cfg.window_titles),
            timeout=None,     # 等待直到出现（Ctrl+C 可中断）
            interval=1.0,
            on_try=lambda attempt, titles: print(
                f"[等待] 未找到匹配窗口（window_titles: {' / '.join(titles)}），"
                f"1 秒后重试...（按 Ctrl+C 退出）"
            ),
        )
        print(f"已选择窗口: '{self.win.title}' (句柄 {self.win.hwnd})")

        self.capturer = Capturer(self.win, cfg.agent.screenshot_dir)

        # 鼠标互斥锁：阳光收集线程 与 Agent B 执行器共享，同一时刻只一方点鼠标
        self.mouse_lock = threading.Lock()

        self.memory_engine: MemoryGameEngine | None = None
        if cfg.mode == "text":
            # 纯文本模式：内存引擎（读状态 + 注入执行），不用 OpenCV/鼠标
            self.executor = MemoryGameEngine()
            if not self.executor.connect():
                raise RuntimeError(f"[内存] {self.executor.error}")
            self.executor.start_force_run()  # 失焦不暂停：看门狗清 game_paused
            self.memory_engine = self.executor
            self.sun_collector = None
            self.grid_scanner = None
            self.card_scanner = None
            self.select_scanner = None
        else:
            # cv2/pyautogui 依赖的扫描/阳光/鼠标执行器只在视觉模式懒加载，
            # 避免 text 模式也被迫依赖 OpenCV。
            from .card_scan import CardScanner  # noqa: PLC0415
            from .executor import Executor  # noqa: PLC0415
            from .grid_scan import GridScanner  # noqa: PLC0415
            from .select_scan import SelectScanner  # noqa: PLC0415
            from .sun import SunCollector  # noqa: PLC0415

            self.executor = Executor(self.win, cfg.layout, mouse_lock=self.mouse_lock)
            self.sun_collector = SunCollector(self.win, cfg.layout, cfg.sun, mouse_lock=self.mouse_lock)
            self.grid_scanner = GridScanner(cfg.layout, cfg.grid_scan) if cfg.grid_scan.enabled else None
            self.card_scanner = CardScanner(cfg.layout, cfg.card_scan) if cfg.card_scan.enabled else None
            if self.card_scanner is not None:
                self.executor.attach_card_scanner(self.card_scanner)
            self.select_scanner = SelectScanner(cfg.layout, cfg.select_scan) if cfg.select_scan.enabled else None
            if self.select_scanner is not None:
                self.executor.attach_select_scanner(self.select_scanner)
        self.vlm = VLMClient(cfg.text_vlm if cfg.mode == "text" else cfg.vlm)  # text 用独立模型配置

        # 调控
        self.controller = Controller(
            goal=DEFAULT_GOAL,
            tick_interval=cfg.agent.tick_interval,
            stop_hotkey="f12",
        )

        # 截图发 VLM 的格式与 MIME（JPEG 压缩可显著减小上传体积/推理时间）
        self._img_fmt = cfg.agent.image_format
        self._img_mime = f"image/{'png' if cfg.agent.image_format == 'png' else 'jpeg'}"

        # Agent A（描述，可关；纯文本模式无需视觉描述）
        self.narrator = None if cfg.mode == "text" else (
            Narrator(self.vlm, mime=self._img_mime) if cfg.agent.narrator_on else None
        )

        # Agent B（规划执行）
        text_mode = cfg.mode == "text"
        xml_mode = cfg.tool_call_mode == "regex"  # 简化正则：系统提示用 <tool_call> 输出格式
        if text_mode:
            _sys = build_planner_system_text_xml(cfg) if xml_mode else build_planner_system_text(cfg)
            _rounds = cfg.text_max_history_rounds
        else:
            _sys = build_planner_system_xml(cfg) if xml_mode else build_planner_system(cfg)
            _rounds = cfg.agent.max_history_rounds
        self.planner = Planner(
            vlm=self.vlm,
            system_prompt=_sys,
            max_rounds=_rounds,  # text 模式更多历史上下文
            mime=self._img_mime,
            system_prompt_xml=_sys,
            include_image=not text_mode,  # 纯文本模式不喂截图
            tools_builder=build_planner_tools_text if text_mode else build_planner_tools,
            tool_call_mode=cfg.tool_call_mode,
        )

        # 防循环状态
        self._last_fail_key: tuple | None = None
        self._consecutive_fail = 0

    # ------------------------------------------------------------------ #
    #  主循环
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        """按模式跑主循环：vision=截图+OpenCV；text=读内存状态。"""
        if self.cfg.mode == "text":
            self._run_text()
        else:
            self._run_vision()

    def _run_vision(self) -> None:
        """视觉方案主循环：截图 → 规划 → 执行 → 反馈。"""
        cfg = self.cfg
        controller = self.controller
        controller.start()
        self.sun_collector.start()  # 独立线程高频收集阳光

        last_summary = ""
        last_turn_time = time.perf_counter()

        print(f"[启动] 目标: {controller.goal}")
        print(f"[启动] 每轮基础间隔 {cfg.agent.tick_interval} 秒（可 speed N 调速）。输入 h 看命令。")

        while not controller.exit_requested:
            # 1. 拍一帧（同一帧供 A/B 复用）
            try:
                img = self.capturer.grab_pil()
            except Exception as exc:
                print(f"[截图] 失败: {exc}")
                print(f"[截图] 请确认窗口 '{self.win.title}' 未被最小化/关闭，且游戏画面可见。1 秒后重试...")
                time.sleep(1)
                continue

            # 2. 用户即时请求（暂停/描述/截图优先处理）
            if controller.consume_screenshot_request():
                self.capturer.save(img, "manual")
            if controller.consume_describe_request():
                print("\n===== [人工请求现状描述] =====")
                try:
                    print(self.narrator.describe(self._img_b64(img)))
                except Exception as exc:
                    print(f"[描述] 失败: {exc}")
                print("===============================")

            # 3. 暂停中：只允许观察，不规划不执行（阳光线程仍在独立收）
            if controller.is_paused:
                time.sleep(cfg.agent.tick_interval * controller.speed)
                continue

            # 4. 并发调用 Agent B(规划) 与 Agent A(描述)——两者共用同一帧截图。
            #    同时启动两个线程，先 join B（行动者）执行动作，再 join A 打印描述，
            #    A 的 VLM 耗时被 B 的规划+执行时间掩盖，整体 = max(A, B) 而非 A+B。
            now = time.perf_counter()
            elapsed = now - last_turn_time
            last_turn_time = now

            # 4.1 OpenCV 网格扫描：检测植物/僵尸坐标作为 Agent B 辅助信息
            grid_text = ""
            if self.grid_scanner:
                try:
                    scan = self.grid_scanner.scan(img)
                    grid_text = scan.to_text()
                    if grid_text:
                        print(f"[网格] {grid_text}")
                except Exception as exc:
                    print(f"[网格] 扫描异常: {exc}")

            # 4.2 OpenCV 卡片扫描：检测可用/不可用卡片，作为 Agent B 辅助信息
            card_text = ""
            if self.card_scanner:
                try:
                    card_scan = self.card_scanner.scan(img)
                    card_text = card_scan.to_text()
                    if card_text:
                        print(f"[卡片] {card_text}")
                except Exception as exc:
                    print(f"[卡片] 扫描异常: {exc}")

            user_text = build_planner_user_footer(
                goal=controller.goal,
                elapsed=elapsed,
                last_summary=last_summary,
                note="",
                grid_state=grid_text,
                card_state=card_text,
            )
            img_b64 = self._img_b64(img)

            b_result: dict = {"calls": [], "raw": "", "error": ""}
            a_result: dict = {"text": "", "error": ""}

            def _run_planner():
                try:
                    calls, raw = self.planner.plan(img_b64, user_text)
                    b_result["calls"], b_result["raw"] = calls, raw
                except Exception as exc:
                    b_result["error"] = str(exc)

            def _run_narrator():
                try:
                    a_result["text"] = self.narrator.describe(img_b64)
                except Exception as exc:
                    a_result["error"] = str(exc)

            b_thread = threading.Thread(target=_run_planner, daemon=True)
            a_thread = threading.Thread(target=_run_narrator, daemon=True) if self.narrator else None
            b_thread.start()
            if a_thread:
                a_thread.start()

            # 5. 先等 Agent B 结果（行动者），执行动作
            b_thread.join(timeout=cfg.vlm.timeout + 5)
            if b_result["error"]:
                print(f"[Agent B] 规划失败: {b_result['error']}，2 秒后重试")
                time.sleep(2)
                continue

            calls, raw = b_result["calls"], b_result["raw"]

            # 6. 执行动作
            results = []
            if not calls:
                print("[Agent B] 未解析到动作，本轮仅观察。")
                last_summary = _build_feedback(results)
                if a_thread:
                    a_thread.join(timeout=cfg.vlm.timeout + 5)
                    self._show_narrator_result(a_result)
                time.sleep(self._compute_wait_seconds([], controller))
                continue

            stop_round = False
            terminate_status = None
            for tc in calls:
                action = tc.arguments.get("action") or tc.name
                translated = translate_action(tc.name, tc.arguments)

                # 控制类动作：终止/回答
                if action == "terminate":
                    terminate_status = tc.arguments.get("status", "success")
                    print(f"[Agent B] 任务终止: {terminate_status}")
                    results.append({"action": "terminate", "status": "ok", "terminate_status": terminate_status})
                    stop_round = True
                    break
                if action == "answer":
                    ans = tc.arguments.get("text", "")
                    print(f"[Agent B 回答] {ans}")
                    results.append({"action": "answer", "status": "ok", "text": ans})
                    continue

                print(f"[执行] {translated}")
                result = self.executor.execute_tool_call(tc.name, tc.arguments)
                results.append(result)
                if result.get("status") == "error":
                    print(f"[执行] {translated} → 失败: {result.get('error', '')}")
                else:
                    print(f"[执行] {translated} → 成功")

                # 防循环：同一动作+同坐标连续失败 N 次 → 中止本轮
                fail_key = self._fail_key(tc, result)
                if fail_key is not None:
                    if fail_key == self._last_fail_key:
                        self._consecutive_fail += 1
                    else:
                        self._consecutive_fail = 1
                        self._last_fail_key = fail_key
                    if self._consecutive_fail >= MAX_CONSECUTIVE_FAIL:
                        print(f"[防循环] 动作 {fail_key} 已连续失败 {MAX_CONSECUTIVE_FAIL} 次，中止本轮并提示换策略。")
                        results.append({
                            "action": "_loop_guard", "status": "error",
                            "error": "连续失败，请换目标或坐标，不要重复相同操作",
                        })
                        stop_round = True
                        break

            # 6. 反馈回填给 Agent B
            feedback = _build_feedback(results)
            self.planner.add_assistant(raw)
            self.planner.add_feedback(feedback)
            last_summary = feedback

            if terminate_status is not None:
                print(f"[完成] Agent 判定任务以 {terminate_status} 结束。输入 quit 退出，或输入 goal <文本> 设定新目标继续。")
                self.planner.reset()  # 清历史，准备新目标
                time.sleep(2)
                continue

            if stop_round:
                # 防循环触发：注入一条强制换策略的 user 提示
                self.planner.add_user_note(
                    "上轮同一动作连续失败多次。请停止重复，重新观察截图，换一个目标或调整坐标。"
                )

            # 9. Agent A 描述结果（已在后台并行跑，等它完成并打印）
            if a_thread:
                a_thread.join(timeout=cfg.vlm.timeout + 5)
                self._show_narrator_result(a_result)

            # 10. 节拍等待
            wait = self._compute_wait_seconds(results, controller)
            time.sleep(wait)

        print("[退出] 主循环结束。")
        self._cleanup()

    def _run_text(self) -> None:
        """纯文本模式主循环：读内存状态 → 文本 LLM 规划 → 注入执行。"""
        cfg = self.cfg
        controller = self.controller
        controller.start()
        assert self.memory_engine is not None

        last_summary = ""
        last_turn_time = time.perf_counter()

        print(f"[启动] 纯文本模式（读内存）目标: {controller.goal}")
        print(f"[启动] 每轮基础间隔 {cfg.agent.tick_interval} 秒（可 speed N 调速）。输入 h 看命令。")

        while not controller.exit_requested:
            # 暂停中：只观察不执行
            if controller.is_paused:
                time.sleep(cfg.agent.tick_interval * controller.speed)
                continue

            # 读内存状态（权威文本，一切决策依据）
            try:
                state = self.memory_engine.read_state()
            except Exception as exc:
                print(f"[内存] 读取失败: {exc}")
                if not self.memory_engine.is_connected():
                    print("[内存] 游戏进程/内存已断开，主循环退出。")
                    break
                time.sleep(1)
                continue

            # 非可操作界面（主菜单/结算/未知）：不喂 LLM，只轮询等待（不冻结、不停循环）
            if not self.memory_engine.is_actionable(state):
                print(f"[等待] 非战斗界面（UI={getattr(state, 'game_ui', '?')}），等待进入选卡/战斗...")
                time.sleep(1)
                continue

            memory_text = self.memory_engine.read_state_text(
                state, fallback_grid=(cfg.layout.rows, cfg.layout.cols)
            )

            now = time.perf_counter()
            elapsed = now - last_turn_time
            last_turn_time = now

            user_text = build_planner_user_footer(
                goal=controller.goal,
                elapsed=elapsed,
                last_summary=last_summary,
                note="",
                memory_state=memory_text,
            )

            # 文本 LLM 规划（include_image=False，不传图）
            try:
                calls, raw = self.planner.plan("", user_text)
            except Exception as exc:
                print(f"[Agent B] 规划失败: {exc}，2 秒后重试")
                time.sleep(2)
                continue

            # 执行动作
            results = []
            if not calls:
                print("[Agent B] 未解析到动作，本轮仅观察。")
                last_summary = _build_feedback(results)
                time.sleep(self._compute_wait_seconds([], controller))
                continue

            stop_round = False
            terminate_status = None
            for tc in calls:
                action = tc.arguments.get("action") or tc.name
                translated = translate_action(tc.name, tc.arguments)

                if action == "terminate":
                    terminate_status = tc.arguments.get("status", "success")
                    print(f"[Agent B] 任务终止: {terminate_status}")
                    results.append({"action": "terminate", "status": "ok", "terminate_status": terminate_status})
                    stop_round = True
                    break
                if action == "answer":
                    ans = tc.arguments.get("text", "")
                    print(f"[Agent B 回答] {ans}")
                    results.append({"action": "answer", "status": "ok", "text": ans})
                    continue

                print(f"[执行] {translated}")
                result = self.executor.execute_tool_call(tc.name, tc.arguments)
                results.append(result)
                if result.get("status") == "error":
                    print(f"[执行] {translated} → 失败: {result.get('error', '')}")
                else:
                    print(f"[执行] {translated} → 成功")

                fail_key = self._fail_key(tc, result)
                if fail_key is not None:
                    if fail_key == self._last_fail_key:
                        self._consecutive_fail += 1
                    else:
                        self._consecutive_fail = 1
                        self._last_fail_key = fail_key
                    if self._consecutive_fail >= MAX_CONSECUTIVE_FAIL:
                        print(f"[防循环] 动作 {fail_key} 已连续失败 {MAX_CONSECUTIVE_FAIL} 次，中止本轮并提示换策略。")
                        results.append({
                            "action": "_loop_guard", "status": "error",
                            "error": "连续失败，请换目标或坐标，不要重复相同操作",
                        })
                        stop_round = True
                        break

            # 反馈回填给 Agent B
            feedback = _build_feedback(results)
            self.planner.add_assistant(raw)
            self.planner.add_feedback(feedback)
            last_summary = feedback

            if terminate_status is not None:
                print(f"[完成] Agent 判定任务以 {terminate_status} 结束。输入 quit 退出，或输入 goal <文本> 设定新目标继续。")
                self.planner.reset()
                time.sleep(2)
                continue

            if stop_round:
                self.planner.add_user_note(
                    "上轮同一动作连续失败多次。请停止重复，重新观察战局状态，换一个目标或调整坐标。"
                )

            wait = self._compute_wait_seconds(results, controller)
            time.sleep(wait)

        print("[退出] 主循环结束。")
        self._cleanup()

    # ------------------------------------------------------------------ #
    #  辅助
    # ------------------------------------------------------------------ #
    def _img_b64(self, img) -> str:
        """按配置格式把 PIL 图转 base64（JPEG 压缩减小上传体积）。"""
        return self.capturer.to_base64(
            img,
            image_format=self._img_fmt,
            jpeg_quality=self.cfg.agent.jpeg_quality,
        )

    @staticmethod
    def _show_narrator_result(a_result: dict) -> None:
        """打印 Agent A 描述结果（线程结果）。"""
        if a_result.get("error"):
            print(f"[Agent A] 描述失败: {a_result['error']}")
        elif a_result.get("text"):
            print(f"\n[Agent A 现状描述]\n{a_result['text']}\n")

    def _compute_wait_seconds(self, results: list[dict], controller: Controller) -> float:
        base = self.cfg.agent.tick_interval * controller.speed
        return _compute_wait(results, self.executor, base)

    @staticmethod
    def _fail_key(tc: ToolCall, result: dict) -> tuple | None:
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

    def _cleanup(self) -> None:
        try:
            if getattr(self, "sun_collector", None) is not None:
                self.sun_collector.stop()
        except Exception:
            pass
        try:
            if getattr(self, "memory_engine", None) is not None:
                self.memory_engine.close()
        except Exception:
            pass
        try:
            if getattr(self, "win", None) is not None:
                self.win.ensure_foreground()
        except Exception:
            pass


def main() -> None:
    """入口。"""
    try:
        cfg = load_config()
    except SystemExit as exc:
        # load_config 在 api_key/model 缺失时打印指引并退出
        sys.exit(exc.code or 1)

    app = GameAgentApp(cfg)
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n[退出] 收到中断信号。")
    except Exception as exc:
        print(f"[错误] 主循环异常退出: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        if hasattr(app, "controller"):
            app.controller.request_exit()


if __name__ == "__main__":
    main()
