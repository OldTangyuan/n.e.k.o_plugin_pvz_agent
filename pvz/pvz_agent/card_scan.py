"""卡片栏扫描：OpenCV 检测战斗工具栏的卡片位置与可用状态。

解决固定坐标问题：
- 原版/杂交版/传送带关卡的卡片位置和数量不同，固定 card_left/card_step 不可靠。
- 用 OpenCV（边缘检测 + 卡片框特征）找每张卡片的实际位置，自适应任意布局。
- 对每张卡片判状态：可用 / 不可用(冷却/阳光不足) / 空槽。
- 输出可用卡片 index 与位置给 Agent B，避免它点不可用/不存在的卡。

状态判定（基于真实截图校准）：
- 工具栏背景 = 木质棕 (96,32,0)。卡片框是亮色（植物图 + 底部阳光价格数字）。
- 可用 = 彩色植物图 + 整体亮度高 + 底部价格数字亮。
- 不可用(冷却/阳光不足) = 有卡片框但整体变暗（亮度 60~90）或价格区暗。
- 空槽 = 纯木色背景，无卡片内容。
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
class CardScanConfig:
    """卡片栏扫描配置（config.json → "card_scan"）。

    状态判定阈值基于真实战斗截图校准：
    - 可用卡片 亮度≈145~170，价格区亮度≈154~191。
    - 冷却/不可用 亮度≈63~76，价格区亮度≈79~83。
    - 空槽/纯背景 亮度≈57~60。
    """

    enabled: bool = True
    bar_top: int = 3                     # 卡片栏顶部 y（虚拟画布，顶部传送带/底部工具栏）
    bar_bottom: int = 78                 # 卡片栏底部 y
    max_cards: int = 9                   # 最多检测的卡片数
    # 卡片检测（统一"亮+彩色"，覆盖普通关+传送带）
    card_bright_min: int = 45            # 卡片最小亮度(V)（冷却卡可暗到 V~53，需放低到45）
    card_sat_min: int = 45               # 卡片最小饱和度(S)
    card_col_ratio: float = 0.18         # 列投影卡片判定阈值（冷却卡列密度~0.15）
    card_w_est: int = 50                 # 卡片框估算宽度(px)
    # 卡片框尺寸（虚拟画布像素）：宽约 48，高约 69
    card_w_min: int = 30
    card_w_max: int = 70
    card_h_min: int = 50
    card_h_max: int = 80
    # 卡片状态阈值
    avail_bright_min: float = 115.0      # 整体亮度 ≥ 此 且 价格区亮 → 可用
    cost_bright_min: float = 120.0       # 底部价格区亮度 ≥ 此 → 可用
    unavail_bright_max: float = 100.0    # 整体亮度 ≤ 此 且 有卡片 → 不可用
    colorful_min: float = 0.20           # 卡片需有 ≥ 此彩色度（排除空槽/纯灰铲子）
    cost_std_min: float = 70.0           # 卡片底部价格数字的最小对比度(真卡片std≈80, 固定UI≈40-61)
    min_gap_width: int = 5               # 宽段切分的木色间隙最小宽度(真卡片间隙5~9px, 固定UI噪声2px)
    # 木色背景（工具栏底）
    wood_r_min: int = 70
    wood_r_max: int = 140
    wood_g_max: int = 70
    wood_b_max: int = 40
    debug: bool = False


@dataclass
class CardScanResult:
    """一次卡片扫描结果。"""

    cards: list[dict] = field(default_factory=list)   # [{index, center, state}]
    available: list[int] = field(default_factory=list)  # 可用卡片 index
    unavailable: list[int] = field(default_factory=list)  # 不可用(冷却/阳光不足)
    empty: list[int] = field(default_factory=list)     # 空槽 index
    card_positions: dict = field(default_factory=dict)  # index → (x, y) 虚拟画布坐标

    def to_text(self) -> str:
        """格式化为给 Agent B 的辅助文本。"""
        parts = []
        if self.available:
            parts.append(f"可用卡片: {', '.join(f'卡{i}' for i in self.available)}")
        if self.unavailable:
            parts.append(f"不可用卡片(冷却/阳光不足): {', '.join(f'卡{i}' for i in self.unavailable)}")
        if not parts:
            return ""
        return "；".join(parts)


class CardScanner:
    """战斗卡片栏扫描器（OpenCV 边缘检测找卡片框 + 状态判定）。"""

    def __init__(self, layout: LayoutConfig, cfg: CardScanConfig | None = None) -> None:
        self.layout = layout
        self.cfg = cfg or CardScanConfig()

    # ------------------------------------------------------------------ #
    #  主入口
    # ------------------------------------------------------------------ #
    def scan(self, pil_img: Image.Image) -> CardScanResult:
        """扫描 PIL 截图，返回卡片位置与状态。"""
        if not self.cfg.enabled:
            return CardScanResult()

        # 归一化到虚拟画布
        norm = pil_img.convert("RGB").resize(
            (self.layout.canvas_w, self.layout.canvas_h), Image.LANCZOS
        )
        arr = np.array(norm)
        result = CardScanResult()

        # 1. 检测卡片框（边缘检测，自适应任意数量/位置）
        boxes = self._detect_card_boxes(arr)
        if not boxes:
            return result

        # 2. 按 x 排序（从左到右 = index 顺序）
        boxes.sort(key=lambda b: b[0])

        # 3. 对每张卡片判状态
        for idx, (x, y, w, h) in enumerate(boxes):
            if idx >= self.cfg.max_cards:
                break
            cx = x + w // 2
            cy = y + h // 2
            state = self._classify_card(arr, (x, y, x + w, y + h))
            result.cards.append({"index": idx, "center": (cx, cy), "state": state})
            result.card_positions[idx] = (cx, cy)
            if state == "available":
                result.available.append(idx)
            elif state == "unavailable":
                result.unavailable.append(idx)
            else:
                result.empty.append(idx)

        if self.cfg.debug:
            self._save_debug(norm, result)
        return result

    # ------------------------------------------------------------------ #
    #  检测卡片框（非木色段 + 网格/价格数字过滤）
    # ------------------------------------------------------------------ #
    def _detect_card_boxes(self, arr: np.ndarray) -> list[tuple[int, int, int, int]]:
        """返回所有卡片框 [(x, y, w, h)]。

        方法（统一"亮+彩色"检测，覆盖普通关 + 传送带）：
        普通关卡片在底部工具栏（y≈40），传送带卡片在顶部传送带（y≈43），
        两者都是"亮 + 有饱和度(植物图)"的色块，且间距均匀。
        1. 扫描顶部(y0~110)和底部(y0~110 用 bar_top/bar_bottom)两个候选区。
        2. 用"亮+彩色"掩膜做列投影，找卡片列段（等间距）。
        3. 过滤孤立UI（间距不规则）。

        传送带卡片无价格数字但仍是卡片样式，状态判定不依赖价格数字。
        """
        candidates = []
        # 顶部候选区（传送带）
        top_y0, top_y1 = self.cfg.bar_top, self.cfg.bar_bottom
        candidates.extend(self._scan_region(arr, top_y0, top_y1))
        return candidates

    def _scan_region(self, arr: np.ndarray, y0: int, y1: int) -> list[tuple[int, int, int, int]]:
        """扫描一个横向区域，找等间距的"亮+彩色"卡片。"""
        region = arr[y0:y1, :, :]
        if region.size == 0:
            return []
        hsv = cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
        mask = ((hsv[:, :, 2] > self.cfg.card_bright_min) &
                (hsv[:, :, 1] > self.cfg.card_sat_min)).astype(np.uint8)

        # 列投影找卡片列段
        col = mask.mean(axis=0)
        segs = []
        start = None
        for x, v in enumerate(col > self.cfg.card_col_ratio):
            if v and start is None:
                start = x
            elif not v and start is not None:
                segs.append((start, x - 1))
                start = None
        if start is not None:
            segs.append((start, len(col) - 1))
        segs = [(s, e) for s, e in segs if e - s >= 25]

        # 卡片中心
        xs = [(s + e) // 2 for s, e in segs]

        # 过滤孤立UI，保留间距一致的真卡片序列
        xs = self._filter_even_spacing(sorted(xs))

        # 生成卡片框
        boxes = []
        for cx in xs:
            box_w = self.cfg.card_w_est
            x0_box = max(0, cx - box_w // 2)
            boxes.append((x0_box, y0, box_w, y1 - y0))
        return boxes

    def _filter_even_spacing(self, xs: list[int]) -> list[int]:
        """保留间距均匀的连续卡片序列（剔除孤立UI/装饰）。

        真卡片等间距（普通关 ~59px / 传送带 ~99px）。
        找出"间距一致(±6px)的最长连续段"，只保留这段的卡片。
        不补全：宁可漏个别冷却卡，也不把固定UI误当卡片。
        """
        if len(xs) < 2:
            return xs
        xs = sorted(xs)
        best = []
        cur = [xs[0]]
        for i in range(1, len(xs)):
            gap = xs[i] - xs[i - 1]
            if 30 <= gap <= 120:
                if len(cur) >= 2:
                    prev_gap = cur[-1] - cur[-2]
                    if abs(gap - prev_gap) <= 6:
                        cur.append(xs[i])
                    else:
                        if len(cur) > len(best):
                            best = cur
                        cur = [xs[i - 1], xs[i]]
                else:
                    cur.append(xs[i])
            else:
                if len(cur) > len(best):
                    best = cur
                cur = [xs[i]]
        if len(cur) > len(best):
            best = cur
        return best

    # ------------------------------------------------------------------ #
    #  卡片状态判定
    # ------------------------------------------------------------------ #
    def _classify_card(self, arr: np.ndarray, box: tuple[int, int, int, int]) -> str:
        """返回 'available' / 'unavailable' / 'empty'。"""
        x0, y0, x1, y1 = box
        card = arr[max(0, y0):min(arr.shape[0], y1), max(0, x0):min(arr.shape[1], x1), :]
        if card.size == 0:
            return "empty"

        bright = card.mean()
        # 底部价格区（卡片下半部，阳光价格数字）
        h = card.shape[0]
        cost_area = card[int(h * 0.6):, :, :]
        cost_bright = cost_area.mean() if cost_area.size else 0

        # 彩色度（植物图色彩）
        colorful = self._colorful_ratio(card)

        # 木色占比（空槽背景）
        R = card[:, :, 0].astype(int)
        G = card[:, :, 1].astype(int)
        B = card[:, :, 2].astype(int)
        wood_ratio = ((R >= self.cfg.wood_r_min) & (R <= self.cfg.wood_r_max) &
                      (G <= self.cfg.wood_g_max) & (B <= self.cfg.wood_b_max)).mean()

        # 判定
        if wood_ratio > 0.6 and bright < 90:
            return "empty"  # 纯木色背景 = 空槽
        if colorful >= self.cfg.colorful_min and bright >= self.cfg.avail_bright_min and \
           cost_bright >= self.cfg.cost_bright_min:
            return "available"
        if colorful >= self.cfg.colorful_min and bright <= self.cfg.unavail_bright_max:
            return "unavailable"  # 有卡片但变暗（冷却/阳光不足）
        # 中间态：有彩色但不够亮 → 不可用
        if colorful >= self.cfg.colorful_min:
            return "unavailable"
        return "empty"

    @staticmethod
    def _colorful_ratio(card: np.ndarray) -> float:
        """彩色像素占比（植物图色彩丰富度）。"""
        hsv = cv2.cvtColor(card, cv2.COLOR_RGB2HSV)
        return float(((hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 80)).mean())

    # ------------------------------------------------------------------ #
    #  调试标注
    # ------------------------------------------------------------------ #
    def _save_debug(self, norm: Image.Image, result: CardScanResult) -> None:
        try:
            os.makedirs("debug", exist_ok=True)
            draw = ImageDraw.Draw(norm)
            for card in result.cards:
                idx = card["index"]
                cx, cy = card["center"]
                state = card["state"]
                color = (0, 220, 0) if state == "available" else ((255, 165, 0) if state == "unavailable" else (120, 120, 120))
                draw.text((cx + 5, cy - 15), f"卡{idx}:{state[:2]}", fill=color)
                draw.rectangle([cx - 24, 5, cx + 24, 73], outline=color, width=2)
            path = os.path.join("debug", f"cards_{time.strftime('%Y%m%d_%H%M%S')}.png")
            norm.save(path)
            print(f"[卡片扫描] 标注图已保存: {path}")
        except Exception as exc:
            print(f"[卡片扫描] 标注图保存失败: {exc}")
