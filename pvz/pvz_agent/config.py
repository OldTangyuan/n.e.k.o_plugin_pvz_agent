"""配置加载：.env（密钥/URL） + config.json（布局/行为） → 全局 settings 对象。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 项目根目录 = pvz_agent 的上一级（即 C:\...\pvz）
BASE_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = BASE_DIR / ".env"
CONFIG_FILE = BASE_DIR / "config.json"
ENV_EXAMPLE = BASE_DIR / ".env.example"


# --------------------------------------------------------------------------- #
#  配置数据结构
# --------------------------------------------------------------------------- #
@dataclass
class VLMConfig:
    """VLM 接口配置（密钥类，来自 .env / 环境变量）。"""

    base_url: str = ""
    model: str = ""
    api_key: str = ""
    max_output_tokens: int = 512
    temperature: float = 0.3
    retries: int = 2
    retry_delay: float = 1.0
    timeout: float = 120.0
    thinking: str = ""                   # 推理控制：""(默认)/"disabled"(关闭思维链，加快响应)
    tool_choice: str = "required"        # 原生工具选择策略："auto"(可不用工具)/"required"(强制每轮调用)/"none"


@dataclass
class LayoutConfig:
    """虚拟画布坐标（基于 800x600 基准，executor 按窗口比例分轴缩放）。

    参考参考项目 pvz/executor.py 的常量：
    - COL_WIDTH = 80；SHOVEL = (755, 50)
    - 格子 x = (col+1)*80；格子 y = 50 + row*100 + 40
    """

    canvas_w: int = 800
    canvas_h: int = 600
    rows: int = 5                       # 草坪行数（PvZ 标准 5 行，row 0~4）
    cols: int = 9                       # 草坪列数（PvZ 标准 9 列，col 0~8）
    col_width: int = 80                 # 每列宽（虚拟画布，精确值：x = (col+1)*80）
    grid_top: int = 120                 # 网格顶行中心 y（精确值：row0 中心 y=120）
    grid_row_h: int = 100               # 每行高台阶（精确值：y = 120 + row*100）
    # 战斗卡槽（工具栏）坐标。基于 800x600 虚拟画布。
    # 卡片 x = card_left + idx*card_step，y = card_top（已含半高偏移，即卡片中心 y）。
    # 若点击偏左/偏右，微调 card_left；卡间距不准则调 card_step。
    card_left: int = 90                 # 首张卡（index 0）中心 x
    card_step: int = 53                 # 相邻卡片中心水平间距
    card_top: int = 50                  # 卡片中心 y（与铲子 y=50 同属工具栏）
    shovel_pos: tuple[int, int] = (755, 50)   # 铲子按钮中心（虚拟画布）
    select_area: dict = field(default_factory=lambda: {
        "start_x": 0.4, "start_y": 0.3,   # 选卡界面卡片区模板起点（相对画布比例）
        "end_x": 0.95, "end_y": 0.7,
        "slots": 10,
    })


@dataclass
class AgentConfig:
    """Agent 行为配置（config.json）。"""

    tick_interval: float = 2.0          # 每轮基本间隔（秒）
    max_history_rounds: int = 1         # Agent B 保留最近文本轮数（1 = 只留上轮反馈，最快）
    include_image: bool = True
    narrator_on: bool = False           # Agent A 每轮自动描述默认关（需要时 describe 手动调）
    screenshot_dir: str = "screenshots"
    jpeg_quality: int = 70              # 截图发给 VLM 的 JPEG 压缩质量（0~95，低=快但糊）
    image_format: str = "jpeg"          # 发给 VLM 的图片格式: "png" / "jpeg"


@dataclass
class SunConfig:
    """OpenCV 自动收集阳光配置（config.json → "sun"）。

    region 默认用 layout.grid_top 限定草坪区域（排除顶部种植栏/阳光计数器）。
    min/max_radius 是虚拟画布（800x600）像素半径。
    """

    enabled: bool = True                # 是否启用自动收集
    thread_enabled: bool = True         # 独立线程高频扫描（False 则主循环同步收集）
    scan_interval: float = 0.35         # 独立线程扫描间隔（秒）
    min_radius: float = 9.0             # 阳光最小半径（虚拟画布像素）
    max_radius: float = 42.0            # 阳光最大半径（含光晕）
    min_circularity: float = 0.4        # 最小圆度（放宽，覆盖边缘变形的阳光）
    max_click_per_scan: int = 8         # 每次扫描最多点击收集数量
    click_gap: float = 0.2              # 连续点击间隔（秒）
    s_min: int = 25                     # 阳光主体最小饱和度（奶油黄特征）
    s_max: int = 170                    # 阳光主体最大饱和度（纯黄文字 S≈195 被排除）
    v_min: int = 200                    # 阳光主体最小亮度（阳光极亮 V≈255）
    debug: bool = False                 # 保存检测标注调试图


@dataclass
class GridScanConfig:
    """网格扫描（植物/僵尸检测）配置（config.json → "grid_scan"）。

    识别流程（v5，用户指定：植物给精确坐标，僵尸只报行号）：
    1. 遮挡检测：屏幕中部草坪占比低于 occlusion_lawn_max → 暂停窗口/大物体，跳过扫描。
    2. 植物（精确坐标，中心采样）：中心黄棕暖色 ≥ warm_plant_min 或草坪占比 <
       lawn_contain_max → plant。空格 = 草坪主导 且 无暖色 且 无蓝灰（阴影也算空）。
    3. 僵尸（只报行号，整行宽松）：僵尸衣裤=蓝灰（H90~130）、身体=暗棕（暗非绿）。
       行内任一格蓝灰 ≥ zombie_blue_min、或暗棕 ≥ zombie_dark_min 且伴随蓝灰、
       或相邻两格蓝灰均 ≥ zombie_cross_min（跨界）→ 该行为僵尸行。
    """

    enabled: bool = True                # 是否启用扫描并注入 Agent B
    sample_radius: int = 20             # 格子中心采样半径（聚焦主体，避免草坪稀释）

    # 草坪 HSV 范围（data/grass.png 模板校准 + 真实空草坪外扩）
    lawn_h_min: int = 40                # 草坪色相下限
    lawn_h_max: int = 75                # 草坪色相上限
    lawn_s_min: int = 150               # 草坪饱和度下限
    lawn_v_min: int = 120               # 草坪亮度下限
    lawn_min_ratio: float = 0.85        # 中心草坪占比 ≥ 此 → 候选空格（阴影也算空）
    lawn_contain_max: float = 0.60      # 中心草坪占比 < 此 → 必含物（植物/僵尸）

    # 植物判定（中心采样，精确坐标）
    warm_plant_min: float = 0.15        # 黄棕暖色占比 ≥ 此 → 植物（向日葵等主体）
    ice_plant_min: float = 0.11         # 冰蓝占比 ≥ 此 → 植物（寒冰射手等蓝色植物，排除误判僵尸）

    # 僵尸判定（只报行号，整行宽松；特征经真实截图校准）
    zombie_blue_h_lo: int = 90          # 僵尸衣裤蓝灰色相下限（蓝）
    zombie_blue_h_hi: int = 130         # 僵尸衣裤蓝灰色相上限
    zombie_blue_s_min: int = 60         # 僵尸衣裤最小饱和度
    zombie_blue_v_max: int = 180        # 僵尸衣裤最大亮度（非亮蓝）
    zombie_blue_min: float = 0.03       # 单格蓝灰占比 ≥ 此 → 僵尸格/行
    zombie_dark_min: float = 0.15       # 单格暗棕占比 ≥ 此 且 有蓝灰 → 僵尸行
    zombie_dark_blue_min: float = 0.01  # 暗棕判定时需伴随的蓝灰下限
    zombie_cross_min: float = 0.02      # 相邻两格蓝灰均 ≥ 此 → 跨界僵尸行

    # 遮挡检测（暂停窗口/结算面板等大物体）
    occlusion_check: bool = True        # 是否启用遮挡检测
    occlusion_region: list = field(default_factory=lambda: [160, 150, 640, 480])  # 屏幕中部
    occlusion_lawn_max: float = 0.75    # 中部草坪占比低于此 → 有遮挡，跳过扫描

    debug: bool = False                 # 保存检测标注图到 debug/


@dataclass
class CardScanConfig:
    """卡片栏扫描配置（config.json → "card_scan"）。

    识别流程（用户指定：OpenCV 检测卡片，替代固定坐标）：
    1. 非木色段检测：工具栏背景是木色，卡片是非木色独立段（宽 40~75px）。
    2. 固定UI过滤：铲子/阳光计数等连续大段（内部无木色间隙）不是卡片，剔除。
    3. 状态判定：可用（彩色+亮度高+价格区亮）/ 不可用（冷却/阳光不足，变暗）。
    4. 输出可用/不可用卡片给 Agent B。
    """

    enabled: bool = True                # 是否启用卡片扫描
    bar_top: int = 3                    # 卡片栏顶部 y
    bar_bottom: int = 78                # 卡片栏底部 y
    max_cards: int = 9                  # 最多检测卡片数
    # 卡片框尺寸（虚拟画布像素）
    card_w_min: int = 30
    card_w_max: int = 70
    card_h_min: int = 50
    card_h_max: int = 80
    # 卡片检测（统一"亮+彩色"，覆盖普通关+传送带）
    card_bright_min: int = 45           # 卡片最小亮度(V)（冷却卡可暗到 V~53）
    card_sat_min: int = 45              # 卡片最小饱和度(S)
    card_col_ratio: float = 0.18        # 列投影卡片判定阈值
    card_w_est: int = 50                # 卡片框估算宽度(px)
    # 卡片状态阈值
    avail_bright_min: float = 115.0     # 整体亮度 ≥ 此 且 价格区亮 → 可用
    cost_bright_min: float = 120.0      # 底部价格区亮度 ≥ 此 → 可用
    unavail_bright_max: float = 100.0   # 整体亮度 ≤ 此 且 有卡片 → 不可用
    colorful_min: float = 0.20          # 卡片需有 ≥ 此彩色度
    min_gap_width: int = 5              # 宽段切分的木色间隙最小宽度（真卡片间隙5~9px，固定UI噪声2px）
    # 木色背景（工具栏底）
    wood_r_min: int = 70
    wood_r_max: int = 140
    wood_g_max: int = 70
    wood_b_max: int = 40
    debug: bool = False                 # 保存标注图到 debug/


@dataclass
class SelectScanConfig:
    """选卡界面扫描配置（config.json → "select_scan"）。

    用于 select_seeds：OpenCV 检测植物库卡片位置与开始按钮，
    替代固定模板坐标。杂交版/不同版本的选卡布局不同，实时检测最可靠。
    """

    enabled: bool = True                # 是否启用选卡扫描
    wall_top: int = 120                 # 植物库区域顶部 y
    wall_bottom: int = 480              # 植物库区域底部 y
    bright_min: int = 70                # 卡片最小亮度
    sat_min: int = 40                   # 卡片最小饱和度
    btn_top: int = 500                  # 开始按钮检测区域顶部 y
    btn_bottom: int = 600               # 开始按钮检测区域底部 y
    btn_bright_min: int = 100           # 按钮最小亮度
    debug: bool = False                 # 保存标注图到 debug/


@dataclass
class AppConfig:
    """全局配置。"""

    vlm: VLMConfig = field(default_factory=VLMConfig)
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    sun: SunConfig = field(default_factory=SunConfig)
    grid_scan: GridScanConfig = field(default_factory=GridScanConfig)
    card_scan: CardScanConfig = field(default_factory=CardScanConfig)
    select_scan: SelectScanConfig = field(default_factory=SelectScanConfig)
    window_title_keywords: list[str] = field(default_factory=lambda: [
        "plant vs zombie", "植物大战僵尸", "pvz",
        "杂交版", "plants vs. zombies", "plants vs zombies",
    ])
    save_capture: bool = False          # 是否每轮都存盘截图


# --------------------------------------------------------------------------- #
#  加载
# --------------------------------------------------------------------------- #
def _load_env(path: Path) -> dict[str, str]:
    """读取简单 .env 文件（KEY=VALUE，忽略 # 注释与空行）。"""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[配置] 警告: {path.name} JSON 解析失败，使用默认值: {exc}")
        return {}


def load_config() -> AppConfig:
    """读取 .env + config.json 合并为 AppConfig。

    优先级：.env 中已设置的环境变量 > .env 文件 > 默认值。
    api_key 未配置时打印指引并退出（不静默带默认密钥）。
    """
    env = _load_env(ENV_FILE)
    jcfg = _load_json(CONFIG_FILE)

    def env_get(key: str, default: str = "") -> str:
        # 环境变量优先，其次 .env 文件
        return os.environ.get(key) or env.get(key, default)

    # ---- VLM（密钥类来自 .env）----
    vlm = VLMConfig(
        base_url=env_get("VLM_BASE_URL", ""),
        model=env_get("VLM_MODEL", ""),
        api_key=env_get("VLM_API_KEY", ""),
        max_output_tokens=jcfg.get("vlm", {}).get("max_output_tokens", 4096),
        temperature=jcfg.get("vlm", {}).get("temperature", 0.3),
        retries=jcfg.get("vlm", {}).get("retries", 3),
        retry_delay=jcfg.get("vlm", {}).get("retry_delay", 2.0),
        timeout=jcfg.get("vlm", {}).get("timeout", 300.0),
        thinking=jcfg.get("vlm", {}).get("thinking", ""),
        tool_choice=jcfg.get("vlm", {}).get("tool_choice", "required"),
    )

    # ---- 布局（config.json）----
    lay = jcfg.get("layout", {})
    layout = LayoutConfig(
        canvas_w=lay.get("canvas_w", 800),
        canvas_h=lay.get("canvas_h", 600),
        rows=lay.get("rows", 5),
        cols=lay.get("cols", 9),
        col_width=lay.get("col_width", 80),
        grid_top=lay.get("grid_top", 120),
        grid_row_h=lay.get("grid_row_h", 100),
        card_left=lay.get("card_left", 90),
        card_step=lay.get("card_step", 53),
        card_top=lay.get("card_top", 50),
        shovel_pos=tuple(lay.get("shovel_pos", [755, 50])),
        select_area=lay.get("select_area", LayoutConfig().select_area),
    )

    # ---- Agent（config.json）----
    ag = jcfg.get("agent", {})
    agent = AgentConfig(
        tick_interval=ag.get("tick_interval", 2.0),
        max_history_rounds=ag.get("max_history_rounds", 1),
        include_image=ag.get("include_image", True),
        narrator_on=ag.get("narrator_on", False),
        screenshot_dir=ag.get("screenshot_dir", "screenshots"),
        jpeg_quality=ag.get("jpeg_quality", 70),
        image_format=ag.get("image_format", "jpeg"),
    )

    # ---- 阳光自动收集（config.json → "sun"）----
    sn = jcfg.get("sun", {})
    sun = SunConfig(
        enabled=sn.get("enabled", True),
        thread_enabled=sn.get("thread_enabled", True),
        scan_interval=sn.get("scan_interval", 0.35),
        min_radius=sn.get("min_radius", 9.0),
        max_radius=sn.get("max_radius", 42.0),
        min_circularity=sn.get("min_circularity", 0.5),
        max_click_per_scan=sn.get("max_click_per_scan", 8),
        click_gap=sn.get("click_gap", 0.2),
        s_min=sn.get("s_min", 25),
        s_max=sn.get("s_max", 170),
        v_min=sn.get("v_min", 200),
        debug=sn.get("debug", False),
    )

    # ---- 网格扫描（config.json → "grid_scan"）----
    gs = jcfg.get("grid_scan", {})
    grid_scan = GridScanConfig(
        enabled=gs.get("enabled", True),
        sample_radius=gs.get("sample_radius", 16),
        lawn_h_min=gs.get("lawn_h_min", 40),
        lawn_h_max=gs.get("lawn_h_max", 75),
        lawn_s_min=gs.get("lawn_s_min", 150),
        lawn_v_min=gs.get("lawn_v_min", 120),
        lawn_min_ratio=gs.get("lawn_min_ratio", 0.85),
        lawn_contain_max=gs.get("lawn_contain_max", 0.60),
        warm_plant_min=gs.get("warm_plant_min", 0.15),
        ice_plant_min=gs.get("ice_plant_min", 0.11),
        zombie_blue_h_lo=gs.get("zombie_blue_h_lo", 90),
        zombie_blue_h_hi=gs.get("zombie_blue_h_hi", 130),
        zombie_blue_s_min=gs.get("zombie_blue_s_min", 60),
        zombie_blue_v_max=gs.get("zombie_blue_v_max", 180),
        zombie_blue_min=gs.get("zombie_blue_min", 0.03),
        zombie_dark_min=gs.get("zombie_dark_min", 0.15),
        zombie_dark_blue_min=gs.get("zombie_dark_blue_min", 0.01),
        zombie_cross_min=gs.get("zombie_cross_min", 0.02),
        occlusion_check=gs.get("occlusion_check", True),
        occlusion_region=gs.get("occlusion_region", [160, 150, 640, 480]),
        occlusion_lawn_max=gs.get("occlusion_lawn_max", 0.75),
        debug=gs.get("debug", False),
    )

    # ---- 卡片扫描（config.json → "card_scan"）----
    cs = jcfg.get("card_scan", {})
    card_scan = CardScanConfig(
        enabled=cs.get("enabled", True),
        bar_top=cs.get("bar_top", 3),
        bar_bottom=cs.get("bar_bottom", 78),
        max_cards=cs.get("max_cards", 9),
        card_w_min=cs.get("card_w_min", 30),
        card_w_max=cs.get("card_w_max", 70),
        card_h_min=cs.get("card_h_min", 50),
        card_h_max=cs.get("card_h_max", 80),
        card_bright_min=cs.get("card_bright_min", 45),
        card_sat_min=cs.get("card_sat_min", 45),
        card_col_ratio=cs.get("card_col_ratio", 0.18),
        card_w_est=cs.get("card_w_est", 50),
        avail_bright_min=cs.get("avail_bright_min", 115.0),
        cost_bright_min=cs.get("cost_bright_min", 120.0),
        unavail_bright_max=cs.get("unavail_bright_max", 100.0),
        colorful_min=cs.get("colorful_min", 0.20),
        min_gap_width=cs.get("min_gap_width", 5),
        wood_r_min=cs.get("wood_r_min", 70),
        wood_r_max=cs.get("wood_r_max", 140),
        wood_g_max=cs.get("wood_g_max", 70),
        wood_b_max=cs.get("wood_b_max", 40),
        debug=cs.get("debug", False),
    )

    # ---- 选卡扫描（config.json → "select_scan"）----
    cs2 = jcfg.get("select_scan", {})

    app = AppConfig(
        vlm=vlm,
        layout=layout,
        agent=agent,
        sun=sun,
        grid_scan=grid_scan,
        card_scan=card_scan,
        select_scan=SelectScanConfig(
            enabled=cs2.get("enabled", True),
            wall_top=cs2.get("wall_top", 120),
            wall_bottom=cs2.get("wall_bottom", 480),
            bright_min=cs2.get("bright_min", 70),
            sat_min=cs2.get("sat_min", 40),
            btn_top=cs2.get("btn_top", 500),
            btn_bottom=cs2.get("btn_bottom", 600),
            btn_bright_min=cs2.get("btn_bright_min", 100),
            debug=cs2.get("debug", False),
        ),
        window_title_keywords=jcfg.get("window_title_keywords", AppConfig().window_title_keywords),
        save_capture=jcfg.get("save_capture", False),
    )

    # ---- api_key 校验 ----
    if not app.vlm.api_key:
        print("[配置] 未找到 VLM_API_KEY。")
        print("请在项目根目录复制 .env.example 为 .env，并填写：")
        print("  VLM_BASE_URL=你的OpenAI兼容接口地址（如 https://api.example.com/v1）")
        print("  VLM_MODEL=你的模型名")
        print("  VLM_API_KEY=你的密钥")
        raise SystemExit(1)

    if not app.vlm.base_url:
        app.vlm.base_url = env_get("VLM_BASE_URL", "https://api.openai.com/v1")
    if not app.vlm.model:
        print("[配置] 未找到 VLM_MODEL。请在 .env 中指定模型名。")
        raise SystemExit(1)

    return app
