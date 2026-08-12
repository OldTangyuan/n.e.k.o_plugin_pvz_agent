"""动作执行器：PvZ 语义动作 + 通用 GUI 动作 → pyautogui。

坐标映射：所有 PvZ 语义坐标基于 800x600 虚拟画布（LayoutConfig），
executor 用 scale_x = 实际宽/800、scale_y = 实际高/600 分轴缩放到客户区，
再偏移到屏幕绝对坐标——天然自适应不同窗口尺寸（原版/杂交版）。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pyautogui

from .config import LayoutConfig
from .window import WindowHandle

pyautogui.FAILSAFE = True  # 鼠标甩到左上角紧急停止


class Executor:
    """动作执行器。

    mouse_lock: 与阳光收集线程共享的鼠标互斥锁。所有动作在执行前先取锁，
    保证同一时刻只有一方在操作鼠标（Agent B 或阳光线程），避免点击冲突。
    """

    # 各动作执行后的最低观察等待（秒），供主循环计算等待时间
    ACTION_DELAYS: dict[str, float] = {
        "place_plant": 2.0,
        "shovel": 2.0,
        "click_card": 1.5,
        "select_seeds": 2.5,
        "left_click": 1.5,
        "drag": 2.0,
        "key": 1.0,
    }
    DEFAULT_DELAY: float = 1.5

    def __init__(
        self,
        win: WindowHandle,
        layout: LayoutConfig,
        mouse_lock: threading.Lock | None = None,
        card_position_mode: str = "opencv",
    ) -> None:
        self.win = win
        self.layout = layout
        self.mouse_lock = mouse_lock or threading.Lock()
        # 种植植物时定位卡片的策略："opencv"=OpenCV 实时识别（失败回退固定坐标）；
        # "fixed"=直接用 config.json 预定的固定坐标（card_left/card_step/card_top）。
        self.card_position_mode = card_position_mode
        self._card_scanner = None  # 由 main 注入 CardScanner，place_plant 前扫描卡片位置
        self._select_scanner = None  # 由 main 注入 SelectScanner，select_seeds 扫描选卡界面

    def attach_card_scanner(self, scanner) -> None:
        """注入卡片扫描器（用于种植前实时定位卡片，适配传送带关卡）。"""
        self._card_scanner = scanner

    def attach_select_scanner(self, scanner) -> None:
        """注入选卡界面扫描器（select_seeds 用 OpenCV 定位植物库卡片）。"""
        self._select_scanner = scanner

    # ------------------------------------------------------------------ #
    #  坐标换算
    # ------------------------------------------------------------------ #
    def _to_screen(self, gx: float, gy: float) -> tuple[int, int]:
        """虚拟画布 (gx, gy) → 屏幕绝对像素（按客户区实时尺寸分轴缩放）。"""
        left, top, right, bottom = self.win.client_rect
        cw = max(right - left, 1)
        ch = max(bottom - top, 1)
        scale_x = cw / self.layout.canvas_w
        scale_y = ch / self.layout.canvas_h
        return int(left + gx * scale_x), int(top + gy * scale_y)

    def _rel_to_screen(self, rel: tuple[float, float]) -> tuple[int, int]:
        """相对坐标 [0,1000]² → 屏幕绝对像素（通用 GUI 动作用）。"""
        left, top, right, bottom = self.win.client_rect
        cw = max(right - left, 1)
        ch = max(bottom - top, 1)
        x = rel[0] / 1000.0 * cw
        y = rel[1] / 1000.0 * ch
        return int(left + x), int(top + y)

    def grid_center(self, row: int, col: int) -> tuple[int, int]:
        """战斗格子 (row, col) 0-based → 虚拟画布中心 (gx, gy)。

        公式来自参考项目：x = (col+1)*80，y = 50 + row*100 + 40（即 row0=90）。
        用 config 的 grid_top/grid_row_h 表示基准，保持可调。
        """
        x = (col + 1) * self.layout.col_width
        y = self.layout.grid_top + row * self.layout.grid_row_h
        return x, y

    def card_center(self, index: int) -> tuple[int, int]:
        """战斗卡槽卡片 index (0-based) → 虚拟画布中心。

        卡片 x = card_left + idx*card_step，y = card_top。
        card_left/card_step 可配置：点击偏左/偏右时微调 config.json。
        """
        x = self.layout.card_left + index * self.layout.card_step
        y = self.layout.card_top
        return x, y

    def _click_screen(self, abs_x: int, abs_y: int) -> None:
        self.win.ensure_foreground()
        pyautogui.click(abs_x, abs_y)

    # ------------------------------------------------------------------ #
    #  通用 GUI 动作（name=computer_use）
    # ------------------------------------------------------------------ #
    def left_click(self, rel: list[float]) -> dict[str, Any]:
        if len(rel) < 2:
            return {"action": "left_click", "status": "error", "error": "coordinate 需要两个元素"}
        ax, ay = self._rel_to_screen((rel[0], rel[1]))
        self._click_screen(ax, ay)
        time.sleep(0.01)
        self._click_screen(ax, ay)
        return {"action": "left_click", "status": "ok", "abs": (ax, ay), "rel": (rel[0], rel[1])}

    def right_click(self, rel: list[float]) -> dict[str, Any]:
        if len(rel) < 2:
            return {"action": "right_click", "status": "error", "error": "coordinate 需要两个元素"}
        ax, ay = self._rel_to_screen((rel[0], rel[1]))
        self.win.ensure_foreground()
        pyautogui.rightClick(ax, ay)
        return {"action": "right_click", "status": "ok", "abs": (ax, ay)}

    def mouse_move(self, rel: list[float]) -> dict[str, Any]:
        if len(rel) < 2:
            return {"action": "mouse_move", "status": "error", "error": "coordinate 需要两个元素"}
        ax, ay = self._rel_to_screen((rel[0], rel[1]))
        self.win.ensure_foreground()
        pyautogui.moveTo(ax, ay, duration=0.15)
        return {"action": "mouse_move", "status": "ok", "abs": (ax, ay)}

    def double_click(self, rel: list[float]) -> dict[str, Any]:
        if len(rel) < 2:
            return {"action": "double_click", "status": "error", "error": "coordinate 需要两个元素"}
        ax, ay = self._rel_to_screen((rel[0], rel[1]))
        self.win.ensure_foreground()
        pyautogui.doubleClick(ax, ay)
        return {"action": "double_click", "status": "ok", "abs": (ax, ay)}

    def drag(self, start: list[float], end: list[float]) -> dict[str, Any]:
        if len(start) < 2 or len(end) < 2:
            return {"action": "drag", "status": "error", "error": "start/end 需要两个元素"}
        sx, sy = self._rel_to_screen((start[0], start[1]))
        ex, ey = self._rel_to_screen((end[0], end[1]))
        self.win.ensure_foreground()
        pyautogui.moveTo(sx, sy, duration=0.15)
        pyautogui.dragTo(ex, ey, duration=0.3, button="left")
        return {"action": "drag", "status": "ok", "start": (sx, sy), "end": (ex, ey)}

    def key(self, keys: list[str]) -> dict[str, Any]:
        if not keys:
            return {"action": "key", "status": "error", "error": "keys 为空"}
        self.win.ensure_foreground()
        for k in keys:
            pyautogui.keyDown(k)
        for k in reversed(keys):
            pyautogui.keyUp(k)
        return {"action": "key", "status": "ok", "keys": keys}

    def scroll(self, pixels: int) -> dict[str, Any]:
        self.win.ensure_foreground()
        pyautogui.scroll(int(pixels))
        return {"action": "scroll", "status": "ok", "pixels": int(pixels)}

    def type_text(self, text: str = "") -> dict[str, Any]:
        """输入文本（通过剪贴板 + Ctrl+V，支持中文）。"""
        if not text:
            return {"action": "type", "status": "error", "error": "text 为空"}
        import pyperclip
        self.win.ensure_foreground()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        return {"action": "type", "status": "ok", "text": text}

    def wait(self, time_s: float) -> dict[str, Any]:
        # wait 不在此 sleep，交由主循环统一计算等待时间（避免重复 sleep）
        return {"action": "wait", "status": "ok", "waited": float(time_s)}

    def terminate(self, status: str = "success") -> dict[str, Any]:
        """结束任务（由主循环读取 status 决定是否退出）。"""
        return {"action": "terminate", "status": "ok", "terminate_status": status}

    def answer(self, text: str = "") -> dict[str, Any]:
        """模型向用户回答一段文本（无 GUI 动作）。"""
        return {"action": "answer", "status": "ok", "text": text}

    # ------------------------------------------------------------------ #
    #  PvZ 语义动作（name=pvz_action）
    # ------------------------------------------------------------------ #
    def place_plant(self, card_index: int, row: int, col: int) -> dict[str, Any]:
        """种植：点卡片 → sleep → 点格子（executor 内部两步）。

        卡片位置优先用 OpenCV 实时扫描（传送带/变布局适配）：
        若注入了卡片扫描器且检测到卡片，用扫描到的卡片实际坐标；
        否则回退到配置的固定坐标 card_center。
        """
        if not (0 <= row < self.layout.rows and 0 <= col < self.layout.cols):
            return {
                "action": "place_plant", "status": "error",
                "error": f"row/col 越界: 行{row}列{col}（合法 行0~{self.layout.rows-1} 列0~{self.layout.cols-1}）",
            }
        try:
            # 1. 按模式定位卡片："opencv"=实时扫描（失败回退固定坐标）；"fixed"=直接用预定坐标
            card_x = card_y = None
            if self.card_position_mode == "opencv" and self._card_scanner is not None:
                try:
                    import numpy as np
                    from PIL import Image, ImageGrab
                    left, top, right, bottom = self.win.client_rect
                    if right - left > 0 and bottom - top > 0:
                        shot = ImageGrab.grab((left, top, right, bottom))
                        res = self._card_scanner.scan(shot)
                        pos = res.card_positions.get(card_index)
                        if pos is not None:
                            card_x, card_y = pos
                except Exception:
                    card_x = card_y = None  # 扫描失败回退固定坐标

            if card_x is None:
                card_x, card_y = self.card_center(card_index)
            cx, cy = self._to_screen(card_x, card_y)
            self._click_screen(cx, cy)
            time.sleep(0.2)

            gx, gy = self.grid_center(row, col)
            ax, ay = self._to_screen(gx, gy)
            self._click_screen(ax, ay)
            return {
                "action": "place_plant", "status": "ok",
                "card_index": card_index, "row": row, "col": col,
                "card_abs": (cx, cy), "grid_abs": (ax, ay),
            }
        except Exception as exc:
            return {"action": "place_plant", "status": "error", "error": str(exc)}

    def shovel(self, row: int, col: int) -> dict[str, Any]:
        """铲除：点铲子 → sleep → 点格子。"""
        if not (0 <= row < self.layout.rows and 0 <= col < self.layout.cols):
            return {
                "action": "shovel", "status": "error",
                "error": f"row/col 越界: 行{row}列{col}（合法 行0~{self.layout.rows-1} 列0~{self.layout.cols-1}）",
            }
        try:
            sx, sy = self._to_screen(*self.layout.shovel_pos)
            self._click_screen(sx, sy)
            time.sleep(0.2)

            gx, gy = self.grid_center(row, col)
            ax, ay = self._to_screen(gx, gy)
            self._click_screen(ax, ay)
            return {"action": "shovel", "status": "ok", "row": row, "col": col, "grid_abs": (ax, ay)}
        except Exception as exc:
            return {"action": "shovel", "status": "error", "error": str(exc)}

    def click_card(self, card_index: int) -> dict[str, Any]:
        """只选中卡片（暂不放置）。"""
        try:
            cx, cy = self._to_screen(*self.card_center(card_index))
            self._click_screen(cx, cy)
            return {"action": "click_card", "status": "ok", "card_index": card_index, "card_abs": (cx, cy)}
        except Exception as exc:
            return {"action": "click_card", "status": "error", "error": str(exc)}

    def select_seeds(self, seeds: list[int]) -> dict[str, Any]:
        """选卡界面：OpenCV 检测植物库卡片，点击选中，再点开始按钮。

        seeds 是植物库卡片的索引列表（0-based，从左到右），
        如 seeds=[0,1] 选植物库第 0、1 张卡（豌豆射手、向日葵）。
        系统用 SelectScanner 检测植物库卡片实际位置后精确点击（自动加入卡槽）。
        """
        try:
            if not seeds:
                return {"action": "select_seeds", "status": "error", "error": "seeds 为空"}
            if self._select_scanner is None:
                return {"action": "select_seeds", "status": "error",
                        "error": "未配置选卡扫描器（select_scan.enabled=false）"}

            # 1. 截图并扫描选卡界面
            import numpy as np
            from PIL import ImageGrab
            left, top, right, bottom = self.win.client_rect
            if right - left <= 0 or bottom - top <= 0:
                return {"action": "select_seeds", "status": "error", "error": "客户区尺寸无效"}
            shot = ImageGrab.grab((left, top, right, bottom))
            res = self._select_scanner.scan(shot)
            if not res.cards:
                return {"action": "select_seeds", "status": "error",
                        "error": "未检测到植物库卡片，可能不在选卡界面或布局异常"}

            self.win.ensure_foreground()
            # 2. 点击每张要选的卡片（点击自动加入卡槽）
            for i in seeds:
                if i < len(res.cards):
                    vx, vy = res.cards[i]
                    ax, ay = self._to_screen(vx, vy)
                    pyautogui.click(ax, ay)
                    time.sleep(0.25)

            # 3. 点击开始按钮
            clicked_start = False
            if res.start_button:
                bx, by = res.start_button
                ax, ay = self._to_screen(bx, by)
                pyautogui.click(ax, ay)
                clicked_start = True

            return {
                "action": "select_seeds", "status": "ok",
                "seeds": seeds, "count": len(seeds),
                "clicked_start": clicked_start,
                "card_positions": res.cards,
            }
        except Exception as exc:
            return {"action": "select_seeds", "status": "error", "error": str(exc)}

    # ------------------------------------------------------------------ #
    #  统一分派
    # ------------------------------------------------------------------ #
    def execute_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 tool_call 的 name 分派到对应动作处理器。

        兼容两种形态：
        - 旧 ``pvz_action`` / ``computer_use``：arguments 带 ``action`` 字段；
        - 原生 function-call 工具名：name 即动作（如 ``place_plant``）。

        与阳光收集线程的互斥：GUI 动作（点击/按键/拖拽）在整个执行期间
        持有 mouse_lock，保证与阳光线程不同时操作鼠标——尤其是 place_plant
        这种"点卡片→点格子"多步动作，中途不能插入阳光点击（会取消选卡）。
        wait/terminate/answer 无鼠标操作，不持锁。
        """
        action = arguments.get("action") or name

        # 控制类：终止/回答/等待（无 GUI 操作，不持锁）
        if action in ("terminate", "answer", "wait"):
            if action == "terminate":
                return self.terminate(arguments.get("status", "success"))
            if action == "answer":
                return self.answer(arguments.get("text", ""))
            return self.wait(arguments.get("time", 1.0))

        with self.mouse_lock:
            # 每次实际鼠标操作前，先在当前位置空点一次左键——
            # 防止鼠标"拿着"东西（如已选中的卡片/铲子）导致后续动作失效。
            # 若当前无选中物，空点无害；若有，则释放选择，确保后续动作正常。
            self._release_held_item()
            return self._dispatch(name, arguments)

    def _release_held_item(self) -> None:
        """在鼠标当前位置做一次右键点击，释放可能"拿着"的选中物。

        适用场景：
        - place_plant 点卡片前，若上次动作后卡片仍被选中（鼠标"拿着"卡片），
          空点会取消选中，避免点格子时误放置到错误位置。
        - shovel 点铲子前，释放可能选中的其他工具。
        - 通用 GUI 点击前，确保无残留的选中/拖拽状态。
        """
        try:
            self.win.ensure_foreground()
            px, py = pyautogui.position()
            pyautogui.rightClick(px, py)
            time.sleep(0.01)  # 给游戏响应释放选择
        except Exception:
            pass  # 空点失败不阻塞后续动作

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """实际分派（调用方已持有 mouse_lock）。"""
        action = arguments.get("action", "")

        if name == "pvz_action":
            # 纯参数动作：直接把参数字典传给处理器（place_plant/shovel/click_card/select_seeds）
            handler = getattr(self, action, None)
            if handler is None:
                return {"action": action, "status": "error", "error": f"未知 PvZ 动作: {action}"}
            try:
                return handler(
                    **{
                        k: v for k, v in arguments.items()
                        if k not in ("action",)
                    }
                )
            except TypeError as exc:
                return {"action": action, "status": "error", "error": f"参数错误: {exc}"}

        coord = arguments.get("coordinate")
        if action in ("left_click", "right_click", "double_click", "mouse_move"):
            if not coord or len(coord) < 2:
                return {"action": action, "status": "error", "error": "coordinate 需要 [x, y] 两个元素"}
            handler = getattr(self, action, None)
            if handler is None:
                return {"action": action, "status": "error", "error": f"未知动作: {action}"}
            try:
                return handler((coord[0], coord[1]))
            except Exception as exc:
                return {"action": action, "status": "error", "error": f"执行失败: {exc}"}

        if action == "drag":
            start = arguments.get("start_coordinate") or arguments.get("start")
            end = arguments.get("end_coordinate") or arguments.get("end")
            if not start or not end or len(start) < 2 or len(end) < 2:
                return {"action": "drag", "status": "error", "error": "drag 需要 start_coordinate/end_coordinate"}
            return self.drag((start[0], start[1]), (end[0], end[1]))

        if action == "key":
            keys = arguments.get("keys") or arguments.get("key")
            return self.key(keys if isinstance(keys, list) else [keys])

        if action == "scroll":
            return self.scroll(arguments.get("pixels", 0))

        if action == "type":
            return self.type_text(arguments.get("text", ""))

        # 原生 function-call 工具名（name 即动作）
        return self._native_dispatch(name, arguments)

    def _native_dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """原生 function-call 工具名分发（调用方已持有 mouse_lock）。

        PvZ 语义动作参数直通处理器；通用 GUI 动作做坐标/参数归一化。
        """
        # PvZ 语义动作：card_index/row/col/seeds 直接透传
        if name in ("place_plant", "shovel", "click_card", "select_seeds"):
            handler = getattr(self, name, None)
            if handler is None:
                return {"action": name, "status": "error", "error": f"未知 PvZ 动作: {name}"}
            try:
                return handler(
                    **{
                        k: v for k, v in arguments.items()
                        if k not in ("action",)
                    }
                )
            except TypeError as exc:
                return {"action": name, "status": "error", "error": f"参数错误: {exc}"}

        # 通用 GUI 动作：坐标/参数归一化
        if name in ("left_click", "right_click", "double_click", "mouse_move"):
            coord = arguments.get("coordinate")
            if not coord or len(coord) < 2:
                return {"action": name, "status": "error", "error": "coordinate 需要 [x, y] 两个元素"}
            handler = getattr(self, name, None)
            if handler is None:
                return {"action": name, "status": "error", "error": f"未知动作: {name}"}
            try:
                return handler((coord[0], coord[1]))
            except Exception as exc:
                return {"action": name, "status": "error", "error": f"执行失败: {exc}"}

        if name == "drag":
            start = arguments.get("start") or arguments.get("start_coordinate")
            end = arguments.get("end") or arguments.get("end_coordinate")
            if not start or not end or len(start) < 2 or len(end) < 2:
                return {"action": "drag", "status": "error", "error": "drag 需要 start/end"}
            return self.drag((start[0], start[1]), (end[0], end[1]))

        if name == "key":
            keys = arguments.get("keys") or arguments.get("key")
            return self.key(keys if isinstance(keys, list) else [keys])

        if name == "scroll":
            return self.scroll(arguments.get("pixels", 0))

        if name == "type":
            return self.type_text(arguments.get("text", ""))

        return {"action": name, "status": "error", "error": f"未知动作: {name}"}
