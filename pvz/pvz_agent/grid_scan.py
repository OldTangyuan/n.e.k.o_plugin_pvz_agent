"""网格扫描器：OpenCV 检测 5x9 网格中哪格有植物/僵尸，输出 (row,col) 坐标。

作为 Agent B 的辅助信息：把植物/僵尸坐标注入 user_text，让 VLM 基于精确位置决策，
而不是仅靠看图猜格子。

识别策略（v6，用户指定：植物精确坐标 + 僵尸精确格 + 空地精确）：
1. **遮挡检测**：先检测屏幕中部是否有大物体（暂停窗口/结算面板/选卡界面等）。
   中部区域草坪色占比过低 → 判定被遮挡，跳过本轮扫描（不输出坐标），
   避免把暂停窗口/弹窗误当成僵尸/植物。
2. **植物（精确坐标，中心采样，逻辑稳定不改）**：格子中心有黄棕暖色主体
   （向日葵/土豆雷等）warm_plant_min，或草坪占比低于 lawn_contain_max（含物）→ plant。
3. **僵尸（精确格）**：僵尸衣裤/身体 = 蓝灰（H90~130）或暗棕（暗非绿）。
   单格蓝灰 ≥ zombie_blue_min 或 暗棕 ≥ zombie_dark_min → 僵尸格。
   植物蓝灰几乎全为 0，僵尸绝大多数 ≥0.03；低蓝灰僵尸靠身体暗色兜底。
   已在 235218/235254/223255 三张用户标注真值图验证：僵尸精确格 12/12 命中 0 误报。
4. **空地（精确，只排除植物）**：非植物、非僵尸格的草坪即空地。
   **僵尸所在地仍可种植物（防御），所以僵尸行内的空格也算空地**；
   有植物的格不算空地（已种过不能重种）。
5. 输出：植物精确坐标 + 僵尸精确格 + 空地精确坐标。

精度说明：只判"有无"，不识别具体植物/僵尸类型（OpenCV 难以鲁棒区分，且无必要）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .config import LayoutConfig

# data/grass.png 模板路径（与模块同级项目根的 data/ 目录）
_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "grass.png")
# 夜晚草坪模板（夜晚场景 V 低至 ~70、H 偏蓝绿，与白天不同）
_NIGHT_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "night_glass.png")


@dataclass
class GridScanConfig:
    """网格扫描配置（config.json → "grid_scan"）。

    识别流程（v5，用户指定：植物给精确坐标，僵尸只报行号）：
    1. 遮挡检测：中部区域草坪色占比低于 occlusion_lawn_max → 暂停窗口/大物体，
       跳过扫描（不输出坐标）。
    2. 植物（精确坐标，中心采样）：格子中心有黄棕暖色主体（向日葵/土豆雷）
       warm_plant_min，或草坪占比低于 lawn_contain_max（含物）→ plant。
       空格 = 草坪主导 且 无暖色 且 无蓝灰特征（阴影/网格线也算空，不误判植物）。
    3. 僵尸（只报行号，整行宽松，基于真实截图校准）：
       僵尸衣裤/身体 = 蓝灰（H 90~130）或 暗棕橙（暗非绿）。行内任一格：
       - 蓝灰占比 ≥ zombie_blue_min → 僵尸行；
       - 或 暗棕占比 ≥ zombie_dark_min 且 蓝灰 ≥ zombie_dark_blue_min → 僵尸行；
       - 或 相邻两格蓝灰均 ≥ zombie_cross_min（跨界僵尸）→ 僵尸行。
       已在 235218/235254/223255 三张真值图验证：僵尸行全部精确命中 0 误报。
    """

    enabled: bool = True
    sample_radius: int = 20             # 格子中心采样半径（虚拟画布像素，聚焦主体）

    # 草坪 HSV 范围（data/grass.png 模板校准：模板 H 58~71 / S≈236 / V≈197）
    lawn_h_min: int = 40
    lawn_h_max: int = 75
    lawn_s_min: int = 150
    lawn_v_min: int = 120
    lawn_min_ratio: float = 0.85        # 中心草坪占比 ≥ 此 → 候选空格（阴影也算空）
    lawn_contain_max: float = 0.60      # 中心草坪占比 < 此 → 必含物（植物/僵尸）

    # 植物判定（中心采样，精确坐标）
    warm_plant_min: float = 0.15        # 黄棕暖色占比 ≥ 此 → 植物（向日葵等主体）
    ice_plant_min: float = 0.08         # 冰蓝占比 ≥ 此 → 植物（寒冰射手等蓝植，排除误判僵尸；真僵尸冰蓝<0.07）

    # 僵尸判定（精确格 + 行号，特征经真实截图校准）
    # 僵尸衣裤 = 蓝灰（H90~130 蓝，低饱和至中饱和，非亮）
    zombie_blue_min: float = 0.03       # 单格蓝灰占比 ≥ 此 → 僵尸格
    zombie_blue_h_lo: int = 90
    zombie_blue_h_hi: int = 130
    zombie_blue_s_min: int = 60
    zombie_blue_v_max: int = 180
    # 僵尸身体 = 暗棕橙（暗且非草坪绿）
    zombie_dark_min: float = 0.15       # 单格暗棕占比 ≥ 此 → 僵尸格
    zombie_dark_blue_min: float = 0.01  # 暗棕判定时需伴随的蓝灰下限
    zombie_cross_min: float = 0.02      # 相邻两格蓝灰均 ≥ 此 → 跨界僵尸行

    # 遮挡检测（暂停窗口/结算面板等大物体）
    occlusion_check: bool = True
    occlusion_region: list = field(default_factory=lambda: [160, 150, 640, 480])  # 屏幕中部区域
    occlusion_lawn_max: float = 0.75    # 中部草坪占比低于此 → 有遮挡，跳过扫描

    debug: bool = False                 # 保存检测标注图到 debug/


@dataclass
class GridScanResult:
    """一次扫描结果。"""

    plants: list[tuple[int, int]] = field(default_factory=list)   # [(row,col),...]
    zombies: list[tuple[int, int]] = field(default_factory=list)
    empty: list[tuple[int, int]] = field(default_factory=list)
    raw: list[dict] = field(default_factory=list)   # 每格详情，供调试
    occluded: bool = False              # 是否被大物体遮挡（暂停窗口等），跳过扫描
    zombie_rows: list[int] = field(default_factory=list)   # 有僵尸的行号（0-based，兼容）
    zombie_cells: list[tuple[int, int]] = field(default_factory=list)  # 僵尸精确格（0-based）

    def to_text(self) -> str:
        """格式化为给 Agent B 的辅助文本。"""
        if self.occluded:
            return ""
        parts = []
        if self.plants:
            coords = ", ".join(f"({r},{c})" for r, c in self.plants)
            parts.append(f"植物: {coords}")
        if self.zombie_cells:
            coords = ", ".join(f"({r},{c})" for r, c in self.zombie_cells)
            parts.append(f"僵尸: {coords}")
        elif self.zombie_rows:
            rows = ", ".join(f"行{r}" for r in self.zombie_rows)
            parts.append(f"僵尸: {rows}")
        if self.empty:
            coords = ", ".join(f"({r},{c})" for r, c in self.empty)
            parts.append(f"空地: {coords}")
        if not parts:
            return ""
        return "；".join(parts)


class GridScanner:
    """5x9 网格扫描器（植物精确坐标 + 僵尸行号）。

    分类原则（用户指定）：
    - 植物：精确 (row,col)，中心采样有黄棕暖色主体或草坪不主导。
    - 僵尸：只报行号，整行宽松检测（行内任一格蓝灰衣裤或暗棕身体达阈值即报行）。
    - 空草坪：中心草坪占比高且无暖色/无蓝灰（阴影/网格线也算空，不误判植物）。
    """

    def __init__(self, layout: LayoutConfig, cfg: GridScanConfig | None = None) -> None:
        self.layout = layout
        self.cfg = cfg or GridScanConfig()
        self._lawn_bounds_cache: tuple[int, int, int, int] | None = None   # (h_min, h_max, s_min, v_min)
        self._night_bounds_cache: tuple[int, int, int, int] | None = None  # 夜晚草坪 HSV 范围

    # ------------------------------------------------------------------ #
    #  草坪色参考（grass.png 白天 + night_glass.png 夜晚 模板校准）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _template_stats() -> tuple[int, int, int, int] | None:
        """读取 data/grass.png 白天模板，返回 (H_min, H_max, S_min, V_min)。

        模板缺失/损坏时返回 None（调用方回退到配置默认范围）。
        """
        if not os.path.isfile(_TEMPLATE_PATH):
            return None
        try:
            img = Image.open(_TEMPLATE_PATH).convert("RGB")
            a = np.array(img)
            hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV).reshape(-1, 3)
            return int(hsv[:, 0].min()), int(hsv[:, 0].max()), int(hsv[:, 1].min()), int(hsv[:, 2].min())
        except Exception:
            return None

    @staticmethod
    def _night_template_stats() -> tuple[int, int, int, int] | None:
        """读取 data/night_glass.png 夜晚草坪模板，返回 (H_min, H_max, S_min, V_min)。

        夜晚草坪特征：暗蓝绿（H≈90~110、S≈200~240、V≈60~80），与白天亮绿完全不同。
        """
        if not os.path.isfile(_NIGHT_TEMPLATE_PATH):
            return None
        try:
            img = Image.open(_NIGHT_TEMPLATE_PATH).convert("RGB")
            a = np.array(img)
            hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV).reshape(-1, 3)
            return int(hsv[:, 0].min()), int(hsv[:, 0].max()), int(hsv[:, 1].min()), int(hsv[:, 2].min())
        except Exception:
            return None

    def _lawn_bounds(self) -> tuple[int, int, int, int]:
        """白天草坪色 HSV 范围。

        优先用 data/grass.png 模板校准（色相外扩容差，适配战斗中阴影/网格线的明暗波动）；
        模板缺失时回退到配置默认值。
        """
        if self._lawn_bounds_cache is not None:
            return self._lawn_bounds_cache
        ref = self._template_stats()
        if ref is None:
            c = self.cfg
            self._lawn_bounds_cache = (c.lawn_h_min, c.lawn_h_max, c.lawn_s_min, c.lawn_v_min)
            return self._lawn_bounds_cache
        th_min, th_max, ts_min, tv_min = ref
        # 模板色相外扩容差：战斗中草坪受阴影/网格线影响，色相/饱和度/亮度均有波动
        h_min = max(0, th_min - 15)
        h_max = min(179, th_max + 15)
        s_min = max(100, ts_min - 100)
        v_min = max(100, tv_min - 40)
        self._lawn_bounds_cache = (h_min, h_max, s_min, v_min)
        return self._lawn_bounds_cache

    def _night_lawn_bounds(self) -> tuple[int, int, int, int]:
        """夜晚草坪 HSV 范围（night_glass.png 模板校准）。

        夜晚草坪是暗蓝绿（主体 H≈90~110、S≈200~240、V≈60~80）。
        用主体的 5%~95% 分位校准，避免模板边缘抗锯齿像素把范围撑大。
        """
        if self._night_bounds_cache is not None:
            return self._night_bounds_cache
        ref = self._night_template_stats()
        if ref is None:
            # 模板缺失时用夜晚草坪主体范围（实测）
            self._night_bounds_cache = (88, 112, 150, 45)
            return self._night_bounds_cache
        img = None
        try:
            img = Image.open(_NIGHT_TEMPLATE_PATH).convert("RGB")
            a = np.array(img)
            hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV).reshape(-1, 3)
            H, S, V = hsv[:, 0], hsv[:, 1], hsv[:, 2]
            # 用主体分位（排除边缘/抗锯齿的极端像素）
            h_lo, h_hi = np.percentile(H, 5), np.percentile(H, 95)
            s_lo, s_hi = np.percentile(S, 5), np.percentile(S, 95)
            v_lo, v_hi = np.percentile(V, 5), np.percentile(V, 95)
            # 外扩少量容差（夜晚光照波动）
            h_min = max(0, int(h_lo) - 8)
            h_max = min(179, int(h_hi) + 8)
            s_min = max(60, int(s_lo) - 60)
            v_min = max(30, int(v_lo) - 15)
            self._night_bounds_cache = (h_min, h_max, s_min, v_min)
        except Exception:
            self._night_bounds_cache = (88, 112, 150, 45)
        return self._night_bounds_cache

    def _lawn_mask(self, H: np.ndarray, S: np.ndarray, V: np.ndarray) -> np.ndarray:
        """草坪掩膜 = 白天草坪 OR 夜晚草坪（双模板）。

        夜晚场景草坪变暗偏蓝（V~70、H~99），单独用白天范围（V≥107）会完全失效。
        合并两个范围：白天亮绿 OR 夜晚暗蓝绿。
        """
        dh0, dh1, ds0, dv0 = self._lawn_bounds()
        day = (H >= dh0) & (H <= dh1) & (S >= ds0) & (V >= dv0)
        nh0, nh1, ns0, nv0 = self._night_lawn_bounds()
        night = (H >= nh0) & (H <= nh1) & (S >= ns0) & (V >= nv0)
        return day | night

    # ------------------------------------------------------------------ #
    #  主入口
    # ------------------------------------------------------------------ #
    def scan(self, pil_img: Image.Image) -> GridScanResult:
        """扫描 PIL 截图，返回植物/僵尸坐标。

        先做遮挡检测：屏幕中部草坪占比过低（暂停窗口/大物体）→ 返回空结果并标记 occluded，
        不进行网格分类。
        """
        if not self.cfg.enabled:
            return GridScanResult()

        # 归一化到虚拟画布
        norm = pil_img.convert("RGB").resize(
            (self.layout.canvas_w, self.layout.canvas_h), Image.LANCZOS
        )
        arr = np.array(norm)
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)

        result = GridScanResult()

        # 1. 遮挡检测：屏幕中部是否有大物体（暂停窗口/结算面板等）
        if self._is_occluded(hsv):
            result.occluded = True
            return result

        # 2.5 场景检测：夜晚场景用夜晚专用分类（夜晚草坪 V 低偏蓝，白天逻辑失效）
        is_night = self._is_night_scene(hsv)

        # 2. 僵尸：精确格 + 行号（夜晚场景用夜晚僵尸判定）
        result.zombie_cells = self._zombie_cells(arr, is_night=is_night)
        result.zombie_rows = sorted({r for r, c in result.zombie_cells})
        zombie_cell_set = set(result.zombie_cells)

        # 3. 逐格分类（植物精确坐标；夜晚场景用夜晚专用分支）
        for r in range(self.layout.rows):
            for c in range(self.layout.cols):
                cx, cy = self._grid_center(r, c)
                if is_night:
                    cls = self._classify_cell_night(arr, cx, cy)
                else:
                    cls = self._classify_cell(arr, cx, cy)
                result.raw.append({"row": r, "col": c, "class": cls, "center": (cx, cy)})
                if cls == "plant":
                    # 单格有明显僵尸特征（蓝灰衣裤/暗棕身体）的格不列为植物，避免僵尸身体被误判
                    if (r, c) not in zombie_cell_set:
                        result.plants.append((r, c))
                elif cls == "zombie":
                    # 僵尸精确格由 zombie_cells 报告，不进入 plants
                    pass
                else:
                    # 空地：只排除"有植物"的格。僵尸所在地仍可种植物（防御），
                    # 所以僵尸行内的空格也算空地。有僵尸特征的格由 zombie_cells 单独报告。
                    if (r, c) not in zombie_cell_set:
                        result.empty.append((r, c))

        if self.cfg.debug:
            self._save_debug(norm, result)

        return result

    # ------------------------------------------------------------------ #
    #  遮挡检测
    # ------------------------------------------------------------------ #
    def _is_occluded(self, hsv: np.ndarray) -> bool:
        """屏幕中部区域草坪色占比低于阈值 → 判定被大物体遮挡。

        正常战斗画面中部几乎全是草坪（实测 94%~95%）；暂停窗口/结算面板/选卡界面
        会把中部大块面积盖住，草坪占比显著下降。
        """
        if not self.cfg.occlusion_check:
            return False
        x0, y0, x1, y1 = self.cfg.occlusion_region
        sub = hsv[y0:y1, x0:x1].reshape(-1, 3)
        H, S, V = sub[:, 0], sub[:, 1], sub[:, 2]
        lawn = self._lawn_mask(H, S, V).mean()
        return lawn < self.cfg.occlusion_lawn_max

    # ------------------------------------------------------------------ #
    #  单格分类（植物精确坐标：中心采样）
    # ------------------------------------------------------------------ #
    def _classify_cell(self, rgb: np.ndarray, cx: int, cy: int) -> str:
        """返回 'plant' / 'zombie' / 'empty'（单格视角）。

        判定原则（用户指定：植物给精确坐标，僵尸只需知道在第几行）：
        1. **空草坪**：中心采样草坪占比高 且 无黄棕 且 无蓝灰特征 → 空格。
           （阴影/网格线只是"变暗的草坪"，仍算空；真植物/僵尸中心有明确颜色主体。）
        2. **植物**：中心采样含黄棕暖色主体（向日葵等）或草坪不主导（含物）→ plant。
        3. **僵尸**：中心采样含蓝灰衣裤像素占比高 → zombie（调用方聚合成行号）。
        """
        pts = self._center_pixels(rgb, cx, cy)
        if pts is None:
            return "empty"
        R, G, B = pts[:, 0], pts[:, 1], pts[:, 2]

        # 草坪占比（中心采样，白天 OR 夜晚）
        hsv = cv2.cvtColor(pts.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_RGB2HSV).reshape(-1, 3)
        H, S, V = hsv[:, 0], hsv[:, 1], hsv[:, 2]
        lawn_ratio = self._lawn_mask(H, S, V).mean()

        # 特征：黄棕暖色（向日葵/植物主体）/ 蓝灰衣裤（僵尸）/ 冰蓝（寒冰射手等蓝植）
        warm_ratio = ((R > G + 15) & (R > 60) & (B < R)).mean()
        blue_ratio = ((H >= self.cfg.zombie_blue_h_lo) & (H <= self.cfg.zombie_blue_h_hi) &
                      (S >= self.cfg.zombie_blue_s_min) & (V < self.cfg.zombie_blue_v_max)).mean()
        ice_ratio = ((B > R + 15) & (B > G + 10)).mean()

        # 1. 空草坪：草坪占比高 且 无暖色主体 → 空。
        #    （夜晚草坪是暗蓝绿，blue/ice 可能高，但只要草坪占比高且无暖色主体就是空格；
        #      寒冰射手等植物草坪占比低，不会被误判为空。）
        if lawn_ratio >= self.cfg.lawn_min_ratio and warm_ratio < self.cfg.warm_plant_min:
            return "empty"

        # 2. 植物：暖色主体（向日葵/土豆雷）或 冰蓝主体（寒冰射手/蓝植）或 草坪不主导（含物）
        #    —— 植物判定优先于僵尸，避免寒冰射手（蓝色植物）被误判为僵尸。
        if warm_ratio >= self.cfg.warm_plant_min or ice_ratio >= self.cfg.ice_plant_min or \
           lawn_ratio < self.cfg.lawn_contain_max:
            return "plant"

        # 3. 僵尸：蓝灰衣裤占比高（非植物格）
        if blue_ratio >= self.cfg.zombie_blue_min:
            return "zombie"

        # 4. 兜底：其余算空
        return "empty"

    # ------------------------------------------------------------------ #
    #  夜晚场景检测与专用分类
    # ------------------------------------------------------------------ #
    def _is_night_scene(self, hsv: np.ndarray) -> bool:
        """检测是否为夜晚场景。

        夜晚草坪是暗蓝绿（H≈90~110、V≈60~80），白天草坪是亮绿（H≈40~75、V≥120）。
        用屏幕中部草坪区域的夜晚/白天特征占比对比判断。
        """
        x0, y0, x1, y1 = self.cfg.occlusion_region
        sub = hsv[y0:y1, x0:x1].reshape(-1, 3)
        H, S, V = sub[:, 0], sub[:, 1], sub[:, 2]
        night_mask = (H >= 85) & (H <= 130) & (S >= 150) & (V < 95)
        day_mask = (H >= 40) & (H <= 75) & (S >= 150) & (V >= 120)
        return bool(night_mask.mean() > day_mask.mean())

    def _classify_cell_night(self, rgb: np.ndarray, cx: int, cy: int) -> str:
        """夜晚场景专用单格分类（返回 'plant'/'zombie'/'empty'）。

        基于真实夜晚图 003639 真值校准（20/21 精确）：
        - 夜晚草坪是暗蓝绿，白天逻辑（V≥107）失效，需用夜晚特征。
        - **僵尸** = 非草坪暖色（肤色 H<40）且暖色像素暗（肤色暗）且非草坪面积合理。
        - **植物/墓碑** = 非草坪亮色高 或 非草坪暖色高（向日葵/墓碑）。
        - **空格** = 纯草坪 或 非草坪蓝色变化 或 暖色亮（夜晚草坪高亮变化）。
        """
        Hh, Ww = rgb.shape[:2]
        half_w = self.layout.col_width // 2
        half_h = self.layout.grid_row_h // 2
        x0, x1 = max(0, cx - half_w), min(Ww, cx + half_w)
        y0, y1 = max(0, cy - half_h), min(Hh, cy + half_h)
        cell = rgb[y0:y1, x0:x1]
        if cell.size < 256:
            return "empty"
        hsv = cv2.cvtColor(cell.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_RGB2HSV).reshape(-1, 3)
        H, S, V = hsv[:, 0], hsv[:, 1], hsv[:, 2]
        lawn = self._lawn_mask(H, S, V)
        nl = ~lawn
        warm_px = nl & (H < 40)

        nl_area = float(nl.mean())
        nl_warm = float(warm_px.mean())
        warm_v = float(np.median(V[warm_px])) if warm_px.sum() > 0 else 0.0
        nl_blue = float((nl & (H >= 85) & (H <= 150)).mean())
        nl_green = float((nl & (H >= 40) & (H < 85)).mean())
        nl_bright = float((nl & (V >= 110)).mean())
        lawn_ratio = float(lawn.mean())

        # 1. 纯草坪空格：草坪占比极高 且 无亮色
        if lawn_ratio >= 0.95 and nl_bright < 0.02:
            return "empty"
        # 2. 亮植物：非草坪亮色高
        if nl_bright >= 0.15:
            return "plant"
        # 3. 空格：非草坪蓝色变化（夜晚蓝绿草坪）且 无暖色
        if nl_blue >= 0.15 and nl_warm < 0.04:
            return "empty"
        # 4. 空格：非草坪暖色但暖色像素亮（夜晚草坪高亮变化，非肤色）
        if nl_warm >= 0.04 and warm_v >= 90 and nl_area < 0.35:
            return "empty"
        # 5. 僵尸：非草坪暖色（肤色）且 暖色像素暗 且 非草坪面积合理
        if nl_warm >= 0.04 and warm_v < 90 and nl_area < 0.45:
            return "zombie"
        # 6. 植物：非草坪暖色高（墓碑/向日葵）或 绿植
        if nl_warm >= 0.04 or nl_green >= 0.015:
            return "plant"
        # 7. 兜底：非草坪面积大 → 占用格（植物），否则空格
        if nl_area >= 0.30:
            return "plant"
        return "empty"

    # ------------------------------------------------------------------ #
    #  整行僵尸行号判定（宽松，蓝灰 + 暗棕特征）
    # ------------------------------------------------------------------ #
    def zombie_rows(self, rgb: np.ndarray) -> list[int]:
        """检测有僵尸的行（0-based），只报行号不报精确坐标。

        僵尸特征（基于真实截图校准，僵尸衣裤/身体）：
        - **蓝灰**：H 90~130（蓝），S≥60，V<180，非草坪——僵尸衣裤。
        - **暗棕**：V<90，S≥80，非草坪绿——僵尸身体暗部。
        行内任一格：
        - 蓝灰占比 ≥ zombie_blue_min → 僵尸行；
        - 或 暗棕占比 ≥ zombie_dark_min 且 蓝灰 ≥ zombie_dark_blue_min → 僵尸行；
        - 或 相邻两格蓝灰均 ≥ zombie_cross_min（跨界僵尸）→ 僵尸行。
        """
        rows: set[int] = set()
        for r in range(self.layout.rows):
            blues = []
            darks = []
            for c in range(self.layout.cols):
                cx, cy = self._grid_center(r, c)
                blue, dark = self._cell_zombie_feats(rgb, cx, cy)
                blues.append(blue)
                darks.append(dark)

            max_blue = max(blues)
            max_dark = max(darks)
            # 跨界：相邻两格蓝灰都达到下限
            cross = any(blues[c] >= self.cfg.zombie_cross_min and blues[c + 1] >= self.cfg.zombie_cross_min
                        for c in range(self.layout.cols - 1))

            if max_blue >= self.cfg.zombie_blue_min or \
               (max_dark >= self.cfg.zombie_dark_min and max_blue >= self.cfg.zombie_dark_blue_min) or \
               cross:
                rows.add(r)
        return sorted(rows)

    def _cell_zombie_feats(self, rgb: np.ndarray, cx: int, cy: int) -> tuple[float, float]:
        """整格采样，返回 (蓝灰占比, 暗棕占比)。"""
        Hh, Ww = rgb.shape[:2]
        half_w = self.layout.col_width // 2
        half_h = self.layout.grid_row_h // 2
        x0, x1 = max(0, cx - half_w), min(Ww, cx + half_w)
        y0, y1 = max(0, cy - half_h), min(Hh, cy + half_h)
        cell = rgb[y0:y1, x0:x1]
        if cell.size < 256:
            return 0.0, 0.0
        hsv = cv2.cvtColor(cell.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_RGB2HSV).reshape(-1, 3)
        H, S, V = hsv[:, 0], hsv[:, 1], hsv[:, 2]
        lawn = self._lawn_mask(H, S, V)
        # 蓝灰衣裤（非草坪）
        blue = ((H >= self.cfg.zombie_blue_h_lo) & (H <= self.cfg.zombie_blue_h_hi) &
                (S >= self.cfg.zombie_blue_s_min) & (V < self.cfg.zombie_blue_v_max) & ~lawn).mean()
        # 暗棕身体（暗 + 非草坪绿）
        dark = ((V < 90) & (S >= 80) & ~((H >= 35) & (H <= 85))).mean()
        return float(blue), float(dark)

    def _zombie_cells(self, rgb: np.ndarray, is_night: bool = False) -> list[tuple[int, int]]:
        """返回僵尸精确格列表（蓝灰衣裤或暗棕身体达阈值，且非冰蓝植物）。

        判定（基于多张真值图校准）：
        - 蓝灰 ≥ zombie_blue_min（植物蓝灰几乎全为 0，僵尸绝大多数 ≥0.03）；
        - 或 暗棕 ≥ zombie_dark_min（低蓝灰僵尸靠身体暗色兜底）。
        - 排除冰蓝植物（寒冰射手等蓝植，冰蓝 ≥ ice_plant_min → 植物非僵尸）。

        夜晚场景（is_night=True）：夜晚草坪本身蓝灰高、冰蓝排除失效，
        改用夜晚分类器（非草坪暖色=肤色）判僵尸格。
        """
        cells: list[tuple[int, int]] = []
        for r in range(self.layout.rows):
            for c in range(self.layout.cols):
                cx, cy = self._grid_center(r, c)
                if is_night:
                    if self._classify_cell_night(rgb, cx, cy) == "zombie":
                        cells.append((r, c))
                    continue
                blue, dark = self._cell_zombie_feats(rgb, cx, cy)
                ice = self._cell_ice_ratio(rgb, cx, cy)
                if (blue >= self.cfg.zombie_blue_min or dark >= self.cfg.zombie_dark_min) and \
                   ice < self.cfg.ice_plant_min:
                    cells.append((r, c))
        return cells

    def _cell_ice_ratio(self, rgb: np.ndarray, cx: int, cy: int) -> float:
        """整格采样，返回冰蓝占比（寒冰射手等蓝色植物特征：B 显著高于 R/G）。"""
        Hh, Ww = rgb.shape[:2]
        half_w = self.layout.col_width // 2
        half_h = self.layout.grid_row_h // 2
        x0, x1 = max(0, cx - half_w), min(Ww, cx + half_w)
        y0, y1 = max(0, cy - half_h), min(Hh, cy + half_h)
        cell = rgb[y0:y1, x0:x1]
        if cell.size < 256:
            return 0.0
        R = cell[:, :, 0].astype(int)
        G = cell[:, :, 1].astype(int)
        B = cell[:, :, 2].astype(int)
        return float(((B > R + 15) & (B > G + 10)).mean())

    def _center_pixels(self, rgb: np.ndarray, cx: int, cy: int, radius: int | None = None) -> np.ndarray | None:
        """格子中心圆形采样（半径默认 sample_radius）。

        中心采样聚焦格子中央的植物/僵尸主体，避免整格采样把草坪边缘稀释。
        """
        rr = radius or self.cfg.sample_radius
        Hh, Ww = rgb.shape[:2]
        pts = []
        for y in range(max(0, cy - rr), min(Hh, cy + rr + 1)):
            for x in range(max(0, cx - rr), min(Ww, cx + rr + 1)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= rr * rr:
                    pts.append((x, y))
        if len(pts) < 16:
            return None
        return np.array([rgb[y, x] for x, y in pts], dtype=np.int16)

    # ------------------------------------------------------------------ #
    #  坐标
    # ------------------------------------------------------------------ #
    def _grid_center(self, row: int, col: int) -> tuple[int, int]:
        """与 executor 一致：x = (col+1)*80, y = grid_top + row*grid_row_h。"""
        x = (col + 1) * self.layout.col_width
        y = self.layout.grid_top + row * self.layout.grid_row_h
        return x, y

    # ------------------------------------------------------------------ #
    #  调试标注
    # ------------------------------------------------------------------ #
    def _save_debug(self, norm: Image.Image, result: GridScanResult) -> None:
        try:
            os.makedirs("debug", exist_ok=True)
            draw = ImageDraw.Draw(norm)
            for r, c in result.plants:
                cx, cy = self._grid_center(r, c)
                draw.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], outline=(0, 220, 0), width=3)
                draw.text((cx + 5, cy - 25), f"P({r},{c})", fill=(0, 220, 0))
            # 空地：蓝色小方块标记（可种植区，已排除植物/僵尸格）
            for r, c in result.empty:
                cx, cy = self._grid_center(r, c)
                draw.rectangle([cx - 6, cy - 6, cx + 6, cy + 6], outline=(0, 120, 255), width=2)
            # 僵尸：精确格红圈
            for r, c in result.zombie_cells:
                cx, cy = self._grid_center(r, c)
                draw.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], outline=(255, 0, 0), width=3)
                draw.text((cx + 5, cy - 25), f"Z({r},{c})", fill=(255, 0, 0))
            path = os.path.join("debug", f"gridscan_{time.strftime('%Y%m%d_%H%M%S')}.png")
            norm.save(path)
            print(f"[网格扫描] 标注图已保存: {path}")
        except Exception as exc:
            print(f"[网格扫描] 标注图保存失败: {exc}")
