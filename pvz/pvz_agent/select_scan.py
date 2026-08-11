"""选卡界面扫描：OpenCV 检测植物库卡片网格与开始按钮，供选卡动作定位。

解决固定模板坐标问题：
- 杂交版/不同版本的选卡界面布局不同，固定 start_x/end_y 模板点击选不中。
- 用 OpenCV 检测植物库卡片的实际网格位置（行/列），点击对应卡片加入卡槽。
- 检测底部"开始"按钮位置。

界面结构（基于真实选卡截图校准）：
- 顶部 y0~110：卡槽区（已选卡片显示，空槽为木色）。
- 中部 y120~480：植物库，可选植物卡片（网格排列，通常第一行有卡片，
  其他行为空槽/未解锁）。
- 底部：开始按钮（亮色块）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .config import LayoutConfig


@dataclass
class SelectScanConfig:
    """选卡界面扫描配置（config.json → "select_scan"）。"""

    enabled: bool = True
    # 植物库区域（虚拟画布坐标）
    wall_top: int = 120
    wall_bottom: int = 480
    # 卡片尺寸（植物库卡片宽约 53px，高约 73px）
    card_w_min: int = 30
    card_w_max: int = 70
    card_h_min: int = 50
    card_h_max: int = 90
    # 卡片判定：亮度 + 饱和度
    bright_min: int = 80
    sat_min: int = 40
    # 开始按钮检测区域
    btn_top: int = 500
    btn_bottom: int = 600
    btn_bright_min: int = 100
    debug: bool = False


@dataclass
class SelectScanResult:
    """一次选卡界面扫描结果。"""

    grid: list[list[tuple[int, int]]] = field(default_factory=list)   # 植物库卡片网格 [[(cx,cy),...]]
    cards: list[tuple[int, int]] = field(default_factory=list)        # 检测到的所有卡片 [(cx,cy)]
    start_button: tuple[int, int] | None = None                       # 开始按钮中心
    has_slots: bool = False                                           # 是否检测到顶部卡槽（确认在选卡界面）

    def to_text(self) -> str:
        """格式化描述（供调试）。"""
        return f"植物卡片 {len(self.cards)} 张, 开始按钮 {'有' if self.start_button else '无'}"


class SelectScanner:
    """选卡界面扫描器（植物库网格 + 开始按钮）。"""

    def __init__(self, layout: LayoutConfig, cfg: SelectScanConfig | None = None) -> None:
        self.layout = layout
        self.cfg = cfg or SelectScanConfig()

    # ------------------------------------------------------------------ #
    #  主入口
    # ------------------------------------------------------------------ #
    def scan(self, pil_img: Image.Image) -> SelectScanResult:
        """扫描 PIL 截图，返回植物库网格与开始按钮位置。"""
        if not self.cfg.enabled:
            return SelectScanResult()

        norm = pil_img.convert("RGB").resize(
            (self.layout.canvas_w, self.layout.canvas_h), Image.LANCZOS
        )
        arr = np.array(norm)
        result = SelectScanResult()

        # 1. 检测顶部卡槽（确认在选卡界面）
        result.has_slots = self._detect_slots(arr)

        # 2. 检测植物库卡片网格
        result.cards, result.grid = self._detect_card_grid(arr)

        # 3. 检测开始按钮
        result.start_button = self._detect_start_button(arr)

        if self.cfg.debug:
            self._save_debug(norm, result)
        return result

    # ------------------------------------------------------------------ #
    #  检测顶部卡槽（确认选卡界面）
    # ------------------------------------------------------------------ #
    def _detect_slots(self, arr: np.ndarray) -> bool:
        """顶部 y0~110 是否有卡槽区（亮色连续带）。"""
        top = arr[0:110, :, :]
        hsv = cv2.cvtColor(top, cv2.COLOR_RGB2HSV)
        bright = hsv[:, :, 2] > 60
        # 卡槽区 = 有大片亮色的连续段
        col = bright.mean(axis=0)
        segs = []
        start = None
        for x, v in enumerate(col > 0.5):
            if v and start is None:
                start = x
            elif not v and start is not None:
                segs.append((start, x - 1))
                start = None
        if start is not None:
            segs.append((start, len(col) - 1))
        wide = [(s, e) for s, e in segs if e - s > 100]
        return len(wide) >= 1

    # ------------------------------------------------------------------ #
    #  检测植物库卡片网格
    # ------------------------------------------------------------------ #
    def _detect_card_grid(self, arr: np.ndarray) -> tuple[list, list]:
        """检测植物库卡片位置。

        方法（列投影，基于真实选卡截图校准）：
        - 植物库卡片是"亮 + 有色彩(饱和>40)"的色块，等间距排列。
        - 对植物库区域逐行做列投影，找卡片列段 → 卡片中心。
        - 干扰（其他 UI 元素）通过与主行卡片数/间距对比剔除。

        返回 (所有卡片中心列表, 网格二维列表)。
        """
        y0, y1 = self.cfg.wall_top, self.cfg.wall_bottom
        wall = arr[y0:y1, :, :]
        hsv = cv2.cvtColor(wall, cv2.COLOR_RGB2HSV)
        mask = ((hsv[:, :, 2] > self.cfg.bright_min) & (hsv[:, :, 1] > self.cfg.sat_min)).astype(np.uint8)

        # 逐行做列投影，找每行的卡片列段
        rows_cards = []  # [(y中心, [x中心...])]
        row_h = 45
        for y in range(0, wall.shape[0], 8):
            slice_mask = mask[y:y + row_h, :]
            if slice_mask.size == 0:
                continue
            col = slice_mask.mean(axis=0)
            # 找卡片列段（列密度高）
            segs = []
            start = None
            for x, v in enumerate(col > 0.3):
                if v and start is None:
                    start = x
                elif not v and start is not None:
                    segs.append((start, x - 1))
                    start = None
            if start is not None:
                segs.append((start, len(col) - 1))
            cards = [(s + e) // 2 for s, e in segs if e - s > 15]
            if len(cards) >= 3:  # 至少 3 张才算卡片行
                cy = y + y0 + row_h // 2
                rows_cards.append((cy, cards))

        # 取卡片数最多的行作为植物库主行
        if not rows_cards:
            return [], []
        main_row = max(rows_cards, key=lambda rc: len(rc[1]))
        cy, xs = main_row

        # 过滤：只保留间距均匀的卡片序列（真卡片间距 ~53px 等距）
        # 干扰元素（其他 UI）间距不规则，被剔除。
        xs = self._filter_even_spacing(sorted(xs))

        cards = [(x, cy) for x in xs]
        grid = [sorted(cards, key=lambda p: p[0])]
        return cards, grid

    def _filter_even_spacing(self, xs: list[int]) -> list[int]:
        """保留间距均匀的连续卡片序列。

        真卡片等间距（~53px）。找出最长的等间距连续段。
        返回过滤后的 x 中心列表。
        """
        if len(xs) < 2:
            return xs
        # 计算相邻间距
        best = []
        cur = [xs[0]]
        for i in range(1, len(xs)):
            gap = xs[i] - xs[i - 1]
            # 间距合理（卡片间隔 ~40~70px）则延续；否则断
            if 35 <= gap <= 75:
                cur.append(xs[i])
            else:
                if len(cur) > len(best):
                    best = cur
                cur = [xs[i]]
        if len(cur) > len(best):
            best = cur
        return best

    def _cluster_grid(self, cards: list) -> list:
        """把卡片中心聚合成 行×列 网格。"""
        if not cards:
            return []
        # 按 y 排序聚类行（容忍 ±20px 行差）
        sorted_cards = sorted(cards, key=lambda p: (p[1], p[0]))
        rows = []
        for cx, cy in sorted_cards:
            placed = False
            for row in rows:
                if abs(row[0][1] - cy) <= 20:  # 同行
                    row.append((cx, cy))
                    placed = True
                    break
            if not placed:
                rows.append([(cx, cy)])
        # 每行按 x 排序
        grid = [sorted(row, key=lambda p: p[0]) for row in rows]
        return grid

    # ------------------------------------------------------------------ #
    #  检测开始按钮
    # ------------------------------------------------------------------ #
    def _detect_start_button(self, arr: np.ndarray) -> tuple[int, int] | None:
        """底部找开始按钮（亮色块，通常是宽矩形）。"""
        y0, y1 = self.cfg.btn_top, self.cfg.btn_bottom
        bottom = arr[y0:y1, :, :]
        hsv = cv2.cvtColor(bottom, cv2.COLOR_RGB2HSV)
        bright = hsv[:, :, 2] > self.cfg.btn_bright_min
        mask = bright.astype(np.uint8)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        # 找最亮的宽块（按钮）
        best = None
        best_area = 0
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = cv2.contourArea(cnt)
            if w > 60 and h > 20 and area > best_area:
                best_area = area
                best = (x + w // 2, y + y0 + h // 2)
        return best

    # ------------------------------------------------------------------ #
    #  调试
    # ------------------------------------------------------------------ #
    def _save_debug(self, norm: Image.Image, result: SelectScanResult) -> None:
        try:
            os.makedirs("debug", exist_ok=True)
            draw = ImageDraw.Draw(norm)
            for cx, cy in result.cards:
                draw.rectangle([cx - 26, cy - 36, cx + 26, cy + 36], outline=(0, 220, 0), width=2)
                draw.text((cx + 5, cy - 15), "卡", fill=(0, 220, 0))
            if result.start_button:
                bx, by = result.start_button
                draw.rectangle([bx - 60, by - 20, bx + 60, by + 20], outline=(255, 0, 0), width=2)
                draw.text((bx + 5, by - 15), "开始", fill=(255, 0, 0))
            path = os.path.join("debug", f"select_{time.strftime('%Y%m%d_%H%M%S')}.png")
            norm.save(path)
            print(f"[选卡扫描] 标注图已保存: {path}")
        except Exception as exc:
            print(f"[选卡扫描] 标注图保存失败: {exc}")
