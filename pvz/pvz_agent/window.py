"""窗口查找/激活/客户区实时坐标/截图（借鉴 xzx.py 与参考项目 capture.py，独立实现）。"""

from __future__ import annotations

import base64
import ctypes
import io
import os
import time
from typing import Any, Callable

import win32api
import win32con
import win32gui
import win32process
from PIL import Image, ImageGrab

# 与 xzx.py 一致：进程设为系统 DPI 感知。
# 否则高 DPI 缩放下 GetClientRect/ClientToScreen 返回虚拟化坐标，
# 与 ImageGrab/pyautogui 的物理像素不一致，导致截图空图或点击偏移。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# 与 xzx.py 一致的排除规则，避免误匹配到浏览器/资源管理器/终端/命令行窗口。
# 终端类（cmd/PowerShell/Windows Terminal）标题多样，用类名精确排除最稳。
EXCLUDE_TITLE_KEYWORDS = [
    "edge", "microsoft edge", "文件资源管理器", "file explorer", "explorer",
    "cmd", "命令提示符", "powershell", "windows terminal", "windowsterminal",
    "terminal", "anaconda", "mintty", "git bash",
]
EXCLUDE_CLASS_PREFIXES = [
    "CabinetWClass", "ExploreWClass", "Chrome_WidgetWin",
    "ConsoleWindowClass",          # cmd / PowerShell
    "WindowsTerminal",             # Windows Terminal 宿主
    "CASCADIA_HOSTING_WINDOW_CLASS",
]


def _is_excluded(title: str, class_name: str) -> bool:
    if title:
        for kw in EXCLUDE_TITLE_KEYWORDS:
            if kw in title.lower():
                return True
    if class_name:
        for prefix in EXCLUDE_CLASS_PREFIXES:
            if class_name.startswith(prefix):
                return True
    return False


def _is_exact_title(title: str, titles: list[str]) -> bool:
    """精确标题匹配：窗口标题 strip 后与任一配置标题全等（忽略大小写）。

    不再做子串模糊匹配——模糊匹配容易误命中（如"植物大战僵尸"会命中
    "植物大战僵尸 修改器.txt"），要求配置里写明窗口的精确标题。
    """
    if not title:
        return False
    normalized = title.strip().casefold()
    return any(normalized == t.strip().casefold() for t in titles if t)


def _enum_callback(hwnd: int, ctx: dict) -> bool:
    if not win32gui.IsWindowVisible(hwnd):
        return True
    title = win32gui.GetWindowText(hwnd)
    class_name = win32gui.GetClassName(hwnd)
    if not _is_excluded(title, class_name) and _is_exact_title(title, ctx["titles"]):
        ctx["hwnds"].append(hwnd)
    return True


class WindowHandle:
    """封装目标窗口句柄，提供实时客户区坐标与前台激活。"""

    def __init__(self, hwnd: int, title: str) -> None:
        self.hwnd = hwnd
        self.title = title

    # ------------------------------------------------------------------ #
    #  客户区
    # ------------------------------------------------------------------ #
    @property
    def client_rect(self) -> tuple[int, int, int, int]:
        """客户区屏幕坐标 (left, top, right, bottom)，每次调用实时刷新。

        防止窗口被移动/缩放后坐标漂移——动作执行前必须重新读取。
        """
        rect = win32gui.GetClientRect(self.hwnd)
        left, top = win32gui.ClientToScreen(self.hwnd, (rect[0], rect[1]))
        right, bottom = win32gui.ClientToScreen(self.hwnd, (rect[2], rect[3]))
        return left, top, right, bottom

    @property
    def size(self) -> tuple[int, int]:
        """客户区宽高 (width, height)。"""
        left, top, right, bottom = self.client_rect
        return right - left, bottom - top

    # ------------------------------------------------------------------ #
    #  前台激活
    # ------------------------------------------------------------------ #
    def ensure_foreground(self) -> None:
        """将窗口恢复、置顶并激活（AttachThreadInput 绕过前台限制）。"""
        if win32gui.IsIconic(self.hwnd):
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)

        current_thread = win32api.GetCurrentThreadId()
        target_thread, _ = win32process.GetWindowThreadProcessId(self.hwnd)
        try:
            if current_thread != target_thread:
                win32process.AttachThreadInput(current_thread, target_thread, True)
                win32gui.SetForegroundWindow(self.hwnd)
                win32process.AttachThreadInput(current_thread, target_thread, False)
            else:
                win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            # 兜底：直接置顶
            try:
                win32gui.SetWindowPos(
                    self.hwnd, win32con.HWND_TOP,
                    0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
            except Exception:
                pass
        time.sleep(0.15)  # 等待窗口激活/重绘

    def is_visible(self) -> bool:
        try:
            return bool(win32gui.IsWindowVisible(self.hwnd))
        except Exception:
            return False

    def __repr__(self) -> str:
        return f"WindowHandle(hwnd={self.hwnd}, title='{self.title}')"


# --------------------------------------------------------------------------- #
#  查找与选择
# --------------------------------------------------------------------------- #
def find_target_windows(titles: list[str]) -> list[WindowHandle]:
    """按精确标题查找所有可见目标窗口（一次快照，不做轮询）。"""
    ctx = {"titles": titles, "hwnds": []}
    win32gui.EnumWindows(_enum_callback, ctx)
    return [WindowHandle(hwnd, win32gui.GetWindowText(hwnd)) for hwnd in ctx["hwnds"]]


class WindowNotFoundError(RuntimeError):
    """轮询等待窗口超时 / 被取消，未找到匹配的窗口。"""


def wait_for_window(
    titles_provider: Callable[[], list[str]],
    *,
    timeout: float | None = None,
    interval: float = 1.0,
    cancel: Any | None = None,
    on_try: Callable[[int, list[str]], None] | None = None,
) -> WindowHandle | None:
    """轮询等待目标窗口出现。

    - ``titles_provider``：每次轮询**重读**标题配置的可调用对象（返回精确标题列表）。
      运行中修改配置，下一轮轮询即生效，无需重启。
    - ``timeout``：最长等待秒数；``None`` = 无限等待（直到命中或被取消）。
    - ``interval``：两轮轮询间隔（秒）。
    - ``cancel``：``threading.Event``，被 set 时立即放弃并返回 ``None``。
    - ``on_try``：每轮未命中时回调 ``on_try(attempt, titles)``（用于打日志提示）。

    命中返回第一个匹配窗口；超时 / 取消返回 ``None``。
    """
    deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
    attempt = 0
    while True:
        if cancel is not None and cancel.is_set():
            return None
        titles = list(titles_provider() or [])
        handles = find_target_windows(titles)
        if handles:
            return handles[0]
        if deadline is not None and time.monotonic() >= deadline:
            return None
        if on_try is not None:
            on_try(attempt, titles)
        attempt += 1
        # 用 cancel.wait 等间隔，可被取消及时唤醒
        if cancel is not None:
            if cancel.wait(interval):
                return None
        else:
            time.sleep(interval)


def pick_single(handles: list[WindowHandle]) -> WindowHandle:
    """从候选窗口中挑选一个（自动用第一个，多个时打印让用户输入序号）。

    与 xzx.py 的交互逻辑保持一致。
    """
    if not handles:
        raise RuntimeError("未找到匹配的 PvZ 窗口，请确认游戏已启动且窗口标题包含关键词。")

    print(f"找到 {len(handles)} 个匹配窗口:")
    for i, h in enumerate(handles):
        print(f"  {i + 1}. 句柄: {h.hwnd}, 标题: '{h.title}'")

    if len(handles) == 1:
        return handles[0]

    choice = input("请输入序号选择要操作的窗口 (默认 1): ").strip()
    if choice == "":
        return handles[0]
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(handles):
            return handles[idx]
    except ValueError:
        pass
    print("序号无效，默认使用第一个。")
    return handles[0]


# --------------------------------------------------------------------------- #
#  截图
# --------------------------------------------------------------------------- #
class Capturer:
    """窗口客户区截图器：内存 PIL + base64 + 可选存盘。"""

    def __init__(self, win: WindowHandle, screenshot_dir: str = "screenshots") -> None:
        self.win = win
        self.screenshot_dir = screenshot_dir

    def grab_pil(self) -> Image.Image:
        """截取客户区并返回 PIL 图片。

        截图前先把窗口恢复/激活（最小化或后台时客户区尺寸可能为 0，
        导致抓到空图）。若仍取到无效尺寸则抛异常，由调用方跳过本轮。
        """
        self.win.ensure_foreground()
        time.sleep(0.1)  # 给窗口重绘留时间
        left, top, right, bottom = self.win.client_rect
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            raise RuntimeError(f"客户区尺寸无效 ({width}x{height})，窗口可能最小化或未就绪")
        bbox = (left, top, right, bottom)
        img = ImageGrab.grab(bbox)
        if img is None or img.size[0] <= 0 or img.size[1] <= 0:
            raise RuntimeError("截图结果为空图像")
        return img

    @staticmethod
    def to_base64(img: Image.Image, image_format: str = "png", jpeg_quality: int = 70) -> str:
        """PIL → base64 字符串（供 VLM 消息使用）。

        Args:
            img: PIL 图片。
            image_format: "png" 或 "jpeg"。JPEG 大幅减小图片 token/上传体积（更快）。
            jpeg_quality: JPEG 质量（0~95，仅 image_format="jpeg" 时生效）。
        """
        if img is None or img.size[0] <= 0 or img.size[1] <= 0:
            raise ValueError(f"无法编码空图像 (size={getattr(img, 'size', '?')})")
        buf = io.BytesIO()
        fmt = image_format.lower()
        if fmt == "jpeg":
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=jpeg_quality)
        else:
            img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def save(self, img: Image.Image, tag: str = "") -> str:
        """存盘并打印路径，返回文件路径。"""
        os.makedirs(self.screenshot_dir, exist_ok=True)
        safe = "".join(c for c in self.win.title if c.isalnum() or c in (" ", "-", "_")).strip() or "pvz"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        suffix = f"_{tag}" if tag else ""
        filepath = os.path.join(self.screenshot_dir, f"{safe}_{stamp}{suffix}.png")
        img.save(filepath)
        print(f"截图已保存: {filepath}")
        return filepath
