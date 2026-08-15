"""PvZ 双 Agent 主循环：截图 → 规划执行 → 反馈 → 描述现状 → 节拍等待。

Agent B (planner) 决策并执行；Agent A (narrator) 描述现状仅展示给用户。
用户调控（controller）在后台线程解析命令，主循环每轮读取其状态。
"""

from __future__ import annotations

import sys
import threading
import time

from .card_scan import CardScanner
from .config import load_config
from .controller import Controller
from .executor import Executor
from .grid_scan import GridScanner
from .narrator import Narrator
from .parser import ToolCall
from .planner import Planner
from .prompts import build_planner_system, build_planner_system_xml, build_planner_user_footer, translate_action
from .select_scan import SelectScanner
from .sun import SunCollector
from .vlm import VLMClient
from .window import Capturer, find_target_windows, pick_single

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

        # 窗口
        handles = find_target_windows(cfg.window_title_keywords)
        self.win = pick_single(handles)
        print(f"已选择窗口: '{self.win.title}' (句柄 {self.win.hwnd})")

        self.capturer = Capturer(self.win, cfg.agent.screenshot_dir)

        # 鼠标互斥锁：阳光收集线程 与 Agent B 执行器共享，同一时刻只一方点鼠标
        self.mouse_lock = threading.Lock()

        self.executor = Executor(self.win, cfg.layout, mouse_lock=self.mouse_lock)
        self.sun_collector = SunCollector(self.win, cfg.layout, cfg.sun, mouse_lock=self.mouse_lock)
        self.grid_scanner = GridScanner(cfg.layout, cfg.grid_scan) if cfg.grid_scan.enabled else None
        self.card_scanner = CardScanner(cfg.layout, cfg.card_scan) if cfg.card_scan.enabled else None
        if self.card_scanner is not None:
            self.executor.attach_card_scanner(self.card_scanner)
        self.select_scanner = SelectScanner(cfg.layout, cfg.select_scan) if cfg.select_scan.enabled else None
        if self.select_scanner is not None:
            self.executor.attach_select_scanner(self.select_scanner)
        self.vlm = VLMClient(cfg.vlm)

        # 调控
        self.controller = Controller(
            goal=DEFAULT_GOAL,
            tick_interval=cfg.agent.tick_interval,
            stop_hotkey="f12",
        )

        # 截图发 VLM 的格式与 MIME（JPEG 压缩可显著减小上传体积/推理时间）
        self._img_fmt = cfg.agent.image_format
        self._img_mime = f"image/{'png' if cfg.agent.image_format == 'png' else 'jpeg'}"

        # Agent A（描述，可关）
        self.narrator = Narrator(self.vlm, mime=self._img_mime) if cfg.agent.narrator_on else None

        # Agent B（规划执行）
        self.planner = Planner(
            vlm=self.vlm,
            system_prompt=build_planner_system(cfg),
            max_rounds=cfg.agent.max_history_rounds,
            mime=self._img_mime,
            system_prompt_xml=build_planner_system_xml(cfg),
        )

        # 防循环状态
        self._last_fail_key: tuple | None = None
        self._consecutive_fail = 0

    # ------------------------------------------------------------------ #
    #  主循环
    # ------------------------------------------------------------------ #
    def run(self) -> None:
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
