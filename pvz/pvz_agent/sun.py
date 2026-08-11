"""OpenCV 自动收集阳光：独立线程高频扫描草坪上的阳光并点击，无需 LLM。

识别策略（只收"掉落阳光"，排除种植栏/阳光计数器）：
- 把截图按 800x600 虚拟画布做尺寸归一（原图缩放到虚拟尺寸再检测），
  这样 min/max_radius 与布局坐标全部统一在虚拟画布系，天然适配任意窗口尺寸。
- 检测区域限定在草坪内：从 grid_top-35（网格顶边往上提，覆盖靠近顶部的阳光）
  到画布底部；左侧排除 x<60 的暖色装饰带（会干扰检测，真阳光落在草坪格内 x≥80）。
  顶部种植栏/阳光计数器被排除在外，避免把"种植栏里的金色图标"误当阳光。
- 阳光是金黄色实心圆：转 HSV，用 H 通道 + 饱和度(S) + 亮度(V) 三层阈值筛出
  高饱和金黄像素 → 形态学闭运算 → findContours → 按面积/圆度过滤 → 圆心=收集点。
- 圆度阈值放宽（0.4）覆盖边缘/左上角变形的阳光，扩大拾取范围。

线程模型：
- 独立 daemon 线程按 scan_interval 高频截图→检测→点击，不阻塞主循环。
- 与 Agent B 通过共享 mouse_lock 互斥：同一时刻只有一方在点鼠标。
- 每个阳光位置有"已点冷却"（默认 1.5s），避免同一阳光被反复点击，
  也避免卡在边上的阳光导致线程空转。
"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np
import pyautogui
from PIL import Image

from .config import LayoutConfig, SunConfig
from .window import WindowHandle, Capturer

pyautogui.FAILSAFE = True


class SunCollector:
    """OpenCV 阳光检测与自动收集（独立线程）。"""

    def __init__(
        self,
        win: WindowHandle,
        layout: LayoutConfig,
        cfg: SunConfig,
        mouse_lock: threading.Lock | None = None,
    ) -> None:
        self.win = win
        self.layout = layout
        self.cfg = cfg
        self.mouse_lock = mouse_lock or threading.Lock()

        self._capturer = Capturer(win)  # 线程内自截图
        self._clicked: list[tuple[float, float, float]] = []  # (vx, vy, 时间戳)
        self._stop = threading.Event()

        self.total_collected: int = 0
        self.last_collect_count: int = 0
        self._count_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    #  生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动独立扫描线程（daemon）。"""
        if not self.cfg.enabled or not self.cfg.thread_enabled:
            return
        if getattr(self, "_thread", None) and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sun-collector")
        self._thread.start()
        print(f"[阳光] 自动收集线程已启动（间隔 {self.cfg.scan_interval}s，采集区：草坪 {self.layout.grid_top}~{self.layout.canvas_h}px）")

    def stop(self) -> None:
        self._stop.set()
        if getattr(self, "_thread", None) and self._thread.is_alive():
            self._thread.join(timeout=2)

    @property
    def alive(self) -> bool:
        return getattr(self, "_thread", None) is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ #
    #  线程主循环
    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                self._collect_once()
            except Exception as exc:
                # 截图失败等瞬时错误：打印一次，不刷屏
                if getattr(self, "_last_err", "") != str(exc):
                    print(f"[阳光] 扫描异常: {exc}")
                    self._last_err = str(exc)
            elapsed = time.perf_counter() - t0
            sleep = self.cfg.scan_interval - elapsed
            if sleep > 0:
                self._stop.wait(sleep)

    def _collect_once(self) -> int:
        """扫一次：截图→检测→点击。返回本次点击数。"""
        try:
            pil_img = self._capturer.grab_pil()
        except Exception:
            return 0

        norm = pil_img.convert("RGB").resize(
            (self.layout.canvas_w, self.layout.canvas_h), Image.LANCZOS
        )
        now = time.time()
        centers = self._detect_sun(norm)
        if not centers:
            self._prune_clicked(now)
            return 0

        fresh = [c for c in centers if not self._is_clicked_recently(c[0], c[1], now)]

        count = 0
        with self.mouse_lock:
            for vx, vy in fresh:
                if count >= self.cfg.max_click_per_scan:
                    break
                ax, ay = self._to_screen(vx, vy)
                try:
                    self.win.ensure_foreground()
                    pyautogui.click(ax, ay)
                except Exception:
                    continue
                with self._count_lock:
                    self.total_collected += 1
                    count += 1
                self._clicked.append((vx, vy, now))
                if count < len(fresh):
                    time.sleep(self.cfg.click_gap)

        self._prune_clicked(now)
        self.last_collect_count = count
        if count:
            print(f"[阳光] 收集 {count} 个（累计 {self.total_collected}）")
        return count

    # 兼容旧接口：主循环同步收集（thread_enabled=False 时可用）
    def collect(self, pil_img: Image.Image) -> int:
        if not self.cfg.enabled:
            return 0
        return self._collect_once_with_img(pil_img)

    def _collect_once_with_img(self, pil_img: Image.Image) -> int:
        norm = pil_img.convert("RGB").resize(
            (self.layout.canvas_w, self.layout.canvas_h), Image.LANCZOS
        )
        now = time.time()
        centers = self._detect_sun(norm)
        if not centers:
            self._prune_clicked(now)
            return 0
        fresh = [c for c in centers if not self._is_clicked_recently(c[0], c[1], now)]
        count = 0
        with self.mouse_lock:
            for vx, vy in fresh:
                if count >= self.cfg.max_click_per_scan:
                    break
                ax, ay = self._to_screen(vx, vy)
                try:
                    self.win.ensure_foreground()
                    pyautogui.click(ax, ay)
                except Exception:
                    continue
                with self._count_lock:
                    self.total_collected += 1
                    count += 1
                self._clicked.append((vx, vy, now))
                if count < len(fresh):
                    time.sleep(self.cfg.click_gap)
        self._prune_clicked(now)
        self.last_collect_count = count
        return count

    # ------------------------------------------------------------------ #
    #  检测（虚拟画布坐标）
    # ------------------------------------------------------------------ #
    def _detect_sun(self, norm_img: Image.Image) -> list[tuple[float, float]]:
        """返回检测到的阳光中心 (vx, vy)，坐标在虚拟画布系。

        形状过滤（剔除文字/横幅/大块等高对比非阳光）：
        1. 紧致度 circularity = 面积/外接圆面积 —— 阳光≈0.55~0.95，文字碎片很低。
        2. 长宽比 bounding_rect 宽/高 —— 阳光≈1，文字横条比值大。
        3. 实心率 fill_ratio = 轮廓面积/最小外接矩形面积 —— 阳光实心≈0.6+，横幅空心。
        4. 中心高光核（阳光结构特征）：真实阳光中央有更亮的高光内圆；
           纯色文字/字母块内部颜色均匀，无中心高光，被剔除。
        """
        cv_img = cv2.cvtColor(np.array(norm_img), cv2.COLOR_RGB2BGR)
        hsv = cv2.cvtColor(cv_img, cv2.COLOR_BGR2HSV)

        # 检测区域：顶部从卡片栏下方开始（y=85，比网格顶边再往上，覆盖靠近顶部的阳光），
        # 左侧排除装饰带（x<60 的暖色条带会干扰检测，真阳光落在草坪格内 x≥80）。
        top_y = max(0, int(self.layout.grid_top) - 35)
        bottom_y = self.layout.canvas_h
        left_x = 60
        if bottom_y - top_y <= 0:
            return []
        region_hsv = hsv[top_y:bottom_y, left_x:, :]
        offset_y = top_y
        offset_x = left_x

        mask = self._sun_mask(region_hsv)

        # 形态学闭运算连接碎裂像素（轻量），不做开运算侵蚀（保留内部纹理结构）
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        min_area = max(4, int(np.pi * (self.cfg.min_radius ** 2) * 0.4))
        max_area = int(np.pi * (self.cfg.max_radius ** 2) * 1.6)

        centers: list[tuple[float, float]] = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_area or area > max_area:
                continue

            # 长宽比：阳光近似正方形；文字横条/长条会被剔除
            x, y, w, h = cv2.boundingRect(cnt)
            if w <= 0 or h <= 0:
                continue
            aspect = max(w, h) / min(w, h) if min(w, h) > 0 else 99
            if aspect > 2.2:
                continue

            # 紧致度：面积 / 外接圆面积。阳光≈0.55~0.95；文字碎片/散点低。
            (cxp, cyp), radius = cv2.minEnclosingCircle(cnt)
            if radius <= 0:
                continue
            circularity = area / (np.pi * radius * radius)
            if circularity < self.cfg.min_circularity:
                continue

            # 实心率：轮廓面积 / 最小外接矩形面积。阳光是实心圆≈0.45~0.6；
            # 空心/稀疏文字低（<0.4）被剔除。阈值 0.4 避免小尺寸阳光边缘误判。
            fill_ratio = area / (w * h) if w * h > 0 else 0
            if fill_ratio < 0.35:
                continue

            # 深黄表情特征：阳光中心有高饱和表情（低饱和主体 + 高饱和表情混合）；
            # 纯色文字内部均匀，无此混合特征，被剔除。
            # 注意：hsv 是整幅图坐标，minEnclosingCircle 返回裁剪区坐标，
            #       需加 offset 转回整图坐标后再采样。
            if not self._has_core_texture(hsv, cxp + offset_x, cyp + offset_y, radius):
                continue

            centers.append((cxp + offset_x, cyp + offset_y))

        if self.cfg.debug:
            self._save_debug(norm_img, mask, top_y, centers)

        return centers

    def _sun_mask(self, hsv) -> np.ndarray:
        """奶油黄阳光掩膜（基于真实阳光贴图特征）。

        真实阳光贴图：整体是低饱和奶油黄（S 均值≈49，RGB≈(255,255,160)），
        极亮（V≈255）。之前用高饱和金黄掩膜（S≥90）会把主体滤掉只剩轮廓，
        导致面积过小、圆度破碎而漏检。

        范围：H 金黄(19~40) + 中低饱和(s_min~s_max) + 极高亮(v_min)。
        纯黄文字 (255,200,60) 的 S≈195 高于 s_max，被排除；
        草坪绿背景 H≈50 不在此范围，被排除。
        """
        lower = np.array([19, self.cfg.s_min, self.cfg.v_min], dtype=np.uint8)
        upper = np.array([40, self.cfg.s_max, 255], dtype=np.uint8)
        return cv2.inRange(hsv, lower, upper)

    def _has_core_texture(self, hsv: np.ndarray, cx: float, cy: float, radius: float) -> bool:
        """检查候选中心是否有"深黄表情"特征（基于真实阳光贴图）。

        真实阳光 = 低饱和奶油黄圆盘 + 中心一块高饱和深黄表情（眼睛/嘴）。
        深黄表情的色相非常集中：H 26~29（纯黄），S≈255（实测中心高饱和
        像素 100% 落在 H 24~32 黄区）。

        文字/草坪干扰：纯色文字中心即使有高饱和像素，色相也分散（H 38~60
        绿黄混合，混入草皮绿），无一落在 H 24~32 纯黄区 → 被剔除。

        判定：中心圆（0.55×R）内"纯黄高饱和（H 24~32 且 S≥200）"像素占比
        在 3%~55% 之间才算阳光（实测真阳光 ≈12.7%）。
        """
        if radius < 4.0:
            return True  # 过小轮廓不做此校验，靠形状过滤兜底

        h, w = hsv.shape[:2]
        cx_i, cy_i = int(round(cx)), int(round(cy))
        r_px = max(3, int(radius * 0.55))

        y0, y1 = max(0, cy_i - r_px), min(h, cy_i + r_px + 1)
        x0, x1 = max(0, cx_i - r_px), min(w, cx_i + r_px + 1)

        total = 0
        yellow_high = 0
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                if (xx - cx_i) ** 2 + (yy - cy_i) ** 2 <= r_px * r_px:
                    total += 1
                    H, S = int(hsv[yy, xx, 0]), int(hsv[yy, xx, 1])
                    # 纯黄高饱和表情：色相 24~32（黄），饱和度 ≥200
                    if S >= 200 and 24 <= H <= 32:
                        yellow_high += 1
        if total < 12:
            return True
        ratio = yellow_high / total
        # 真阳光中心纯黄高饱和占比 ≈12.7%；纯色文字无纯黄表情 → 0%
        return 0.03 <= ratio <= 0.55

    # ------------------------------------------------------------------ #
    #  防重复
    # ------------------------------------------------------------------ #
    def _is_clicked_recently(self, vx: float, vy: float, now: float) -> bool:
        for px, py, ts in self._clicked:
            if now - ts > 1.5:
                continue
            if (px - vx) ** 2 + (py - vy) ** 2 <= 16 ** 2:
                return True
        return False

    def _prune_clicked(self, now: float) -> None:
        self._clicked = [(x, y, t) for (x, y, t) in self._clicked if now - t <= 1.5]

    # ------------------------------------------------------------------ #
    #  坐标换算
    # ------------------------------------------------------------------ #
    def _to_screen(self, vx: float, vy: float) -> tuple[int, int]:
        """虚拟画布坐标 → 屏幕绝对像素（与 executor 同规则）。"""
        left, top, right, bottom = self.win.client_rect
        cw = max(right - left, 1)
        ch = max(bottom - top, 1)
        scale_x = cw / self.layout.canvas_w
        scale_y = ch / self.layout.canvas_h
        return int(left + vx * scale_x), int(top + vy * scale_y)

    # ------------------------------------------------------------------ #
    #  调试
    # ------------------------------------------------------------------ #
    def _save_debug(self, norm_img: Image.Image, mask: np.ndarray, top_y: int, centers) -> None:
        try:
            import os
            os.makedirs("debug", exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            out = np.array(norm_img.convert("RGB"))
            overlay = out.copy()
            overlay[mask > 0] = (255, 60, 60)
            out = cv2.addWeighted(out, 0.5, overlay, 0.5, 0)
            for vx, vy in centers:
                cv2.circle(out, (int(vx), int(vy)), 8, (0, 200, 0), 2)
            cv2.imwrite(f"debug/sun_{stamp}.png", cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
            print(f"[阳光] 调试图已保存: debug/sun_{stamp}.png")
        except Exception:
            pass
