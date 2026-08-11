"""用户调控：后台 stdin 线程解析命令 → 线程安全状态；主循环每轮查询。

paused 只挡"规划+执行"，不挡截图/描述/存盘——暂停期间用户仍可观察。
命令在后台线程直接作用于线程安全状态（Event / lock 保护的字段），
主循环每轮开头读取这些状态即可，无需再经队列中转。
"""

from __future__ import annotations

import threading

try:
    from pynput import keyboard as _pynput_keyboard
except Exception:  # pragma: no cover
    _pynput_keyboard = None

HELP_TEXT = """可用命令:
  pause        暂停自动执行（仍可截图/描述观察）
  resume       恢复自动执行
  describe     立即要一份现状描述
  screenshot   截图并保存
  speed <N>    设置每轮间隔倍率（如 speed 2 = 每轮间隔翻倍，N>=0.1）
  goal <文本>  设定/修改当前目标
  h            显示本帮助
  quit         退出程序
"""


class Controller:
    """用户调控控制器。"""

    def __init__(self, goal: str = "", tick_interval: float = 4.0, stop_hotkey: str = "f12") -> None:
        self.tick_interval = tick_interval

        # 线程安全状态
        self.paused = threading.Event()            # set = 暂停自动执行
        self._describe_requested = threading.Event()
        self._screenshot_requested = threading.Event()
        self._exit_requested = threading.Event()

        self._speed: float = 1.0
        self._speed_lock = threading.Lock()
        self._goal_lock = threading.Lock()
        self._goal: str = goal

        self.stop_hotkey = stop_hotkey
        self._hotkey_listener = None

    # ------------------------------------------------------------------ #
    #  启动
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动 stdin 读取线程（daemon）与全局急停热键监听。"""
        t = threading.Thread(target=self._read_stdin, daemon=True, name="controller-stdin")
        t.start()

        if _pynput_keyboard is not None:
            try:
                self._hotkey_listener = _pynput_keyboard.Listener(on_press=self._on_hotkey)
                self._hotkey_listener.daemon = True
                self._hotkey_listener.start()
                print(f"[控制] 后台命令线程已启动。随时输入命令（输入 h 看帮助）；按 {self.stop_hotkey} 急停。")
            except Exception:
                print("[控制] 全局热键监听启动失败，仅支持命令行调控。")
        else:
            print("[控制] pynput 未安装，仅支持命令行调控（无全局热键）。")

    # ------------------------------------------------------------------ #
    #  后台线程
    # ------------------------------------------------------------------ #
    def _read_stdin(self) -> None:
        while not self._exit_requested.is_set():
            try:
                line = input("cmd> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.request_exit()
                break
            if line:
                self._parse_command(line)

    def _on_hotkey(self, key) -> bool | None:  # pragma: no cover
        try:
            if self.stop_hotkey and self.stop_hotkey.lower() in str(key).lower():
                print("\n[急停] 检测到急停热键，正在退出...")
                self.request_exit()
                return False
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ #
    #  命令解析（后台线程）
    # ------------------------------------------------------------------ #
    def _parse_command(self, line: str) -> None:
        parts = line.split()
        cmd = parts[0].lower()
        arg = " ".join(parts[1:]).strip() if len(parts) > 1 else ""

        if cmd in ("pause", "p"):
            self.paused.set()
            print("[控制] 已暂停自动执行。输入 resume 恢复；暂停期间仍可 describe / screenshot 观察。")
        elif cmd in ("resume", "r"):
            self.paused.clear()
            print("[控制] 已恢复自动执行。")
        elif cmd in ("describe", "d"):
            self._describe_requested.set()
            print("[控制] 已请求现状描述。")
        elif cmd in ("screenshot", "s"):
            self._screenshot_requested.set()
            print("[控制] 已请求截图保存。")
        elif cmd in ("speed", "sp"):
            try:
                val = float(arg)
                if val < 0.1:
                    raise ValueError
                with self._speed_lock:
                    self._speed = val
                print(f"[控制] 每轮间隔倍率已设为 {val:.1f}（实际间隔 ≈ {self.tick_interval * val:.1f} 秒）")
            except ValueError:
                print("[控制] speed 需要 >= 0.1 的数字，如: speed 2")
        elif cmd in ("goal", "g"):
            if arg:
                with self._goal_lock:
                    self._goal = arg
                print(f"[控制] 目标已更新: {arg}")
            else:
                print(f"[控制] 当前目标: {self.goal}")
        elif cmd in ("h", "help", "?"):
            print(HELP_TEXT)
        elif cmd in ("quit", "q", "exit"):
            self.request_exit()
        else:
            print(f"[控制] 未知命令: {line}（输入 h 看帮助）")

    def request_exit(self) -> None:
        self._exit_requested.set()

    # ------------------------------------------------------------------ #
    #  主循环读取（线程安全）
    # ------------------------------------------------------------------ #
    @property
    def is_paused(self) -> bool:
        return self.paused.is_set()

    @property
    def exit_requested(self) -> bool:
        return self._exit_requested.is_set()

    @property
    def speed(self) -> float:
        with self._speed_lock:
            return self._speed

    @property
    def goal(self) -> str:
        with self._goal_lock:
            return self._goal

    @goal.setter
    def goal(self, value: str) -> None:
        with self._goal_lock:
            self._goal = value

    def consume_describe_request(self) -> bool:
        """消费一次"要描述"请求（线程安全）。"""
        if self._describe_requested.is_set():
            self._describe_requested.clear()
            return True
        return False

    def consume_screenshot_request(self) -> bool:
        """消费一次"要截图"请求（线程安全）。"""
        if self._screenshot_requested.is_set():
            self._screenshot_requested.clear()
            return True
        return False
