"""PvZ 内存偏移量表 — 版本检测与多版本偏移定义.

参考项目:
- pvztoolkit (data.cpp): 版本检测 + 偏移表
- AsmVsZombies (avz_pvz_struct.h): 结构体字段偏移
- AvZLib: 僵尸位置预测、波次计时等高级功能偏移

偏移体系:
- PvzBase: 全局基址 [0x6a9ec0]，所有版本的入口
- PvzBase → MainObject: 不同版本偏移不同
- MainObject → 各实体数组: 不同版本偏移不同
- 实体内部字段偏移: 跨版本基本一致
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

# ================================================================== #
#  版本常量 — 与 pvztoolkit data.h 对应
# ================================================================== #

class PvZVersion(IntEnum):
    """PvZ 版本标识."""

    NOT_FOUND = 0
    OPEN_ERROR = -1
    UNSUPPORTED = 1

    V1_0_0_1051_EN = 1001
    V1_2_0_1065_EN = 1002
    V1_0_4_7924_ES = 1003
    V1_0_7_3556_ES = 1004
    V1_0_7_3467_RU = 1005

    GOTY_1_2_0_1073_EN = 2001
    GOTY_1_2_0_1096_EN = 2002
    GOTY_1_2_0_1093_DE_ES_FR_IT = 2003
    GOTY_1_1_0_1056_ZH = 2004       # ← 中文年度版（主要目标）
    GOTY_1_1_0_1056_JA = 2005
    GOTY_1_1_0_1056_ZH_2012_06 = 2006
    GOTY_1_1_0_1056_ZH_2012_07 = 2007


# PE 时间戳 → 版本映射（来自 pvztoolkit pvz.cpp FindPvZ）
_PE_TIMESTAMP_MAP: dict[int, PvZVersion] = {
    0x49ECF563: PvZVersion.V1_0_0_1051_EN,
    0x4A37D6AF: PvZVersion.V1_2_0_1065_EN,
    0x4A5B7963: PvZVersion.V1_0_4_7924_ES,
    0x4C237519: PvZVersion.V1_0_7_3556_ES,
    0x4CE4C3D6: PvZVersion.V1_0_7_3467_RU,
    0x4C2E3453: PvZVersion.GOTY_1_2_0_1073_EN,
    0x4D02B058: PvZVersion.GOTY_1_2_0_1096_EN,
    0x4CA31BAA: PvZVersion.GOTY_1_2_0_1093_DE_ES_FR_IT,
    0x4C563DE1: PvZVersion.GOTY_1_1_0_1056_ZH,
    0x4CC8E5F8: PvZVersion.GOTY_1_1_0_1056_JA,
    0x4FCD7BE2: PvZVersion.GOTY_1_1_0_1056_ZH_2012_06,
    0x5003D437: PvZVersion.GOTY_1_1_0_1056_ZH_2012_07,
}


# PvZ 窗口类名和已知标题
PVZ_WINDOW_CLASS = "MainWindow"
PVZ_WINDOW_TITLES = [
    "Plants vs. Zombies",
    "Plants vs. Zombies 1.2.0.1073",
    "Plants vs. Zombies 1.2.0.1073 RELEASE",
    "Plants vs. Zombies GOTY",
    "Pflanzen gegen Zombies 1.2.0.1093",
    "Plantas contra Zombis 1.2.0.1093",
    "Plantes contre Zombies 1.2.0.1093",
    "Piante contro zombi 1.2.0.1093",
]

# 全局基址 — 所有版本共用
PVZ_BASE_ADDRESS = 0x6A9EC0


# ================================================================== #
#  场景类型
# ================================================================== #

class SceneType(IntEnum):
    """游戏场景类型."""

    DAY = 0       # 白天
    NIGHT = 1     # 黑夜
    POOL = 2      # 泳池
    FOG = 3       # 雾夜
    ROOF = 4      # 天台
    MOON = 5      # 月夜（僵王战场地）


# ================================================================== #
#  游戏界面状态
# ================================================================== #

class GameUI(IntEnum):
    """游戏界面状态."""

    MAIN_MENU = 1      # 主界面
    SELECT_CARD = 2    # 选卡界面
    IN_GAME = 3        # 战斗界面


# ================================================================== #
#  植物类型枚举 — 来自 AsmVsZombies avz_types.h
# ================================================================== #

PLANT_NAMES: dict[int, str] = {
    0: "豌豆射手", 1: "向日葵", 2: "樱桃炸弹", 3: "坚果",
    4: "土豆地雷", 5: "寒冰射手", 6: "大嘴花", 7: "双发射手",
    8: "小喷菇", 9: "阳光菇", 10: "大喷菇", 11: "墓碑吞噬者",
    12: "魅惑菇", 13: "胆小菇", 14: "寒冰菇", 15: "毁灭菇",
    16: "荷叶", 17: "倭瓜", 18: "三发射手", 19: "缠绕海藻",
    20: "火爆辣椒", 21: "地刺", 22: "火炬树桩", 23: "高坚果",
    24: "水兵菇", 25: "路灯花", 26: "仙人掌", 27: "三叶草",
    28: "裂荚射手", 29: "杨桃", 30: "南瓜头", 31: "磁力菇",
    32: "卷心菜投手", 33: "花盆", 34: "玉米投手", 35: "咖啡豆",
    36: "大蒜", 37: "叶子保护伞", 38: "金盏花", 39: "西瓜投手",
    40: "机枪射手", 41: "双子向日葵", 42: "忧郁菇", 43: "香蒲",
    44: "冰西瓜投手", 45: "吸金磁", 46: "地刺王", 47: "玉米加农炮",
    48: "模仿者",
}

# 植物类型 → 阳光消耗
PLANT_SUN_COST: dict[int, int] = {
    0: 100, 1: 50, 2: 150, 3: 50, 4: 25, 5: 175, 6: 150, 7: 200,
    8: 0, 9: 25, 10: 75, 11: 75, 12: 75, 13: 25, 14: 75, 15: 125,
    16: 25, 17: 50, 18: 325, 19: 25, 20: 125, 21: 100, 22: 175,
    23: 125, 24: 0, 25: 25, 26: 125, 27: 100, 28: 125, 29: 125,
    30: 125, 31: 100, 32: 100, 33: 25, 34: 100, 35: 75, 36: 50,
    37: 100, 38: 50, 39: 300, 40: 250, 41: 150, 42: 150, 43: 225,
    44: 200, 45: 50, 46: 125, 47: 500, 48: 0,
}

# 升级植物 → 基础植物 映射
# 升级卡必须点在已有基础植物上才能生效，不能种在空地。
PLANT_UPGRADE_MAP: dict[int, int] = {
    40: 7,    # 机枪射手 ← 双发射手
    41: 1,    # 双子向日葵 ← 向日葵
    42: 10,   # 忧郁菇 ← 大喷菇
    43: 8,    # 香蒲 ← 小喷菇
    44: 39,   # 冰西瓜投手 ← 西瓜投手
    45: 31,   # 吸金磁 ← 磁力菇
    46: 21,   # 地刺王 ← 地刺
    47: 34,   # 玉米加农炮 ← 玉米投手（需两个相邻，特殊处理）
}


# ================================================================== #
#  僵尸类型枚举 — 来自 AsmVsZombies avz_types.h
# ================================================================== #

ZOMBIE_NAMES: dict[int, str] = {
    0: "普僵", 1: "旗帜", 2: "路障", 3: "撑杆", 4: "铁桶",
    5: "读报", 6: "铁门", 7: "橄榄", 8: "舞王", 9: "伴舞",
    10: "鸭子", 11: "潜水", 12: "冰车", 13: "雪橇", 14: "海豚",
    15: "小丑", 16: "气球", 17: "矿工", 18: "跳跳", 19: "雪人",
    20: "蹦极", 21: "扶梯", 22: "投篮", 23: "白眼巨人", 24: "小鬼",
    25: "僵王博士", 26: "豌豆僵尸", 27: "坚果僵尸", 28: "辣椒僵尸",
    29: "机枪僵尸", 30: "倭瓜僵尸", 31: "高坚果僵尸", 32: "红眼巨人",
}


# ================================================================== #
#  场地物品类型 — 来自 AsmVsZombies APlaceItemType
# ================================================================== #

PLACE_ITEM_NAMES: dict[int, str] = {
    1: "墓碑", 2: "弹坑", 3: "梯子", 4: "传送门(圆)",
    5: "传送门(方)", 6: "脑子", 7: "花瓶", 8: "松鼠",
    9: "禅境工具", 10: "臭臭", 11: "钉耙", 12: "IZ脑子",
}


# ================================================================== #
#  物品类型（收集物）
# ================================================================== #

ITEM_SUN = 1         # 阳光
ITEM_SUN_SMALL = 2   # 小阳光
ITEM_COIN_SILVER = 3 # 银币
ITEM_COIN_GOLD = 4   # 金币
ITEM_DIAMOND = 5     # 钻石

ITEM_NAMES: dict[int, str] = {
    1: "阳光", 2: "小阳光", 3: "银币", 4: "金币",
    5: "钻石", 6: "花盆", 8: "巧克力", 9: "肥料",
    10: "杀虫剂", 11: "树肥",
}


# ================================================================== #
#  偏移数据类 — 每个版本一份
# ================================================================== #

@dataclass
class PvZOffsets:
    """PvZ 内存偏移量集合.

    偏移链规则:
    - PvzBase 在全局地址 [PVZ_BASE_ADDRESS]
    - PvzBase → MainObject: pvz_base + main_object
    - MainObject → 实体数组: main_obj + entity_array
    - 实体数组[i]: array_base + struct_size * i
    - 实体字段: entity_base + field_offset
    """

    # ---- 版本信息 ----
    version: PvZVersion = PvZVersion.NOT_FOUND
    version_name: str = "Unknown"

    # ---- PvzBase 级偏移（从 [PVZ_BASE_ADDRESS] 指向的对象开始） ----
    main_object: int = 0      # PvzBase → MainObject 指针偏移
    game_ui: int = 0          # PvzBase → GameUi
    game_mode: int = 0        # PvzBase → GameMode/LevelId
    tick_ms: int = 0          # PvzBase → TickMs (帧时长)

    # ---- MainObject 级偏移 ----
    # 僵尸
    zombie_array: int = 0
    zombie_count_max: int = 0
    zombie_limit: int = 0
    zombie_count: int = 0

    # 植物
    plant_array: int = 0
    plant_count_max: int = 0
    plant_count: int = 0

    # 种子/卡片
    seed_array: int = 0

    # 收集物
    item_array: int = 0
    item_count_max: int = 0

    # 场地物品
    grid_item_array: int = 0
    grid_item_count_max: int = 0

    # 子弹
    projectile_array: int = 0
    projectile_count_max: int = 0

    # 割草机
    lawn_mower_array: int = 0
    lawn_mower_count_max: int = 0
    lawn_mower_count: int = 0

    # 鼠标/光标
    mouse: int = 0
    mouse_extra: int = 0

    # 游戏状态
    game_paused: int = 0
    scene: int = 0
    sun: int = 0
    game_clock: int = 0
    wave: int = 0
    total_wave: int = 0
    refresh_countdown: int = 0
    huge_wave_countdown: int = 0
    initial_countdown: int = 0
    zombie_refresh_hp: int = 0
    level_end_countdown: int = 0
    adventure_level: int = 0

    # 出怪列表
    zombie_list: int = 0
    zombie_type_list: int = 0

    # 格子类型
    grid_type_list: int = 0
    row_type: int = 0

    # ---- 实体内部字段偏移（跨版本基本一致） ----

    # 僵尸字段
    z_type: int = 0x24
    z_state: int = 0x28
    z_row: int = 0x1c
    z_abscissa: int = 0x2c      # float
    z_ordinate: int = 0x30      # float
    z_speed: int = 0x34         # float
    z_hp: int = 0xC8
    z_one_hp: int = 0xD0        # 一类饰品（路障/铁桶等）
    z_two_hp: int = 0xDC        # 二类饰品（铁门等）
    z_is_eat: int = 0x51        # 是否在啃食
    z_at_wave: int = 0x6c       # 所在波数
    z_slow_countdown: int = 0xAC
    z_fixation_countdown: int = 0xB0  # 黄油固定
    z_freeze_countdown: int = 0xB4
    z_is_disappeared: int = 0xEC
    z_animation_code: int = 0x118
    z_state_countdown: int = 0x68

    # 植物字段
    p_type: int = 0x24
    p_row: int = 0x1c
    p_col: int = 0x28
    p_hp: int = 0x40
    p_hp_max: int = 0x44
    p_state: int = 0x3c
    p_state_countdown: int = 0x54
    p_shoot_countdown: int = 0x90
    p_is_sleeping: int = 0x143
    p_is_crushed: int = 0x142
    p_is_disappeared: int = 0x141

    # 种子/卡片字段（相对卡片起始地址）
    # 卡片容器: SeedArray 指向的内存
    #   offset 0x24 = 卡片数量 Count
    #   offset 0x28 起 = 第 0 张卡片
    #   每张卡片大小 = seed_card_size
    seed_count: int = 0x24
    seed_card_offset: int = 0x28    # 第 0 张卡片起始偏移
    seed_card_size: int = 0x50      # 每张卡片大小 (来自 pvztoolkit slot_seed_struct_size)
    sc_type: int = 0x34             # 卡片内: 种子类型
    sc_cd: int = 0x24               # 卡片内: 冷却
    sc_initial_cd: int = 0x28       # 卡片内: 初始冷却
    sc_imitator_type: int = 0x38    # 卡片内: 模仿者类型
    sc_x: int = 0x08                # 卡片内: 横坐标
    sc_y: int = 0x0c                # 卡片内: 纵坐标
    sc_width: int = 0x10            # 卡片内: 宽度
    sc_height: int = 0x14           # 卡片内: 高度
    sc_usable: int = 0x48           # 卡片内: 是否可用

    # 收集物字段
    i_abscissa: int = 0x24          # float
    i_ordinate: int = 0x28          # float
    i_type: int = 0x58
    i_is_disappeared: int = 0x38
    i_is_collected: int = 0x50

    # 场地物品字段
    gi_type: int = 0x08
    gi_col: int = 0x10
    gi_row: int = 0x14
    gi_value: int = 0x18            # 弹坑倒计时/墓碑量/钉耙倒计时
    gi_is_disappeared: int = 0x20

    # 割草机字段
    lm_dead: int = 0x30

    # 子弹字段
    pr_type: int = 0x5c
    pr_is_disappeared: int = 0x50

    # ---- 实体结构体大小（用于数组遍历） ----
    zombie_struct_size: int = 0x15C
    plant_struct_size: int = 0x14C
    item_struct_size: int = 0xD8
    grid_item_struct_size: int = 0xEC
    lawn_mower_struct_size: int = 0x48
    projectile_struct_size: int = 0x94

    # ---- 全局基址变量地址（版本相关） ----
    # 指向 PvzBase 对象的全局变量地址。原版为 0x6A9EC0，GOTY 各版本不同
    # （pvztoolkit data.cpp 的 lawn 字段）。读取指针链第一级 [pvz_base] → 对象。
    pvz_base: int = 0x6A9EC0


# ================================================================== #
#  版本偏移表 — 从 pvztoolkit data.cpp 提取
# ================================================================== #

def _make_original_offsets(version: PvZVersion = PvZVersion.V1_0_0_1051_EN,
                           version_name: str = "1.0.0.1051 EN") -> PvZOffsets:
    """原版系列偏移 (1.0.0.1051 / 1.2.0.1065) — AsmVsZombies 基准."""
    return PvZOffsets(
        version=version,
        version_name=version_name,
        # PvzBase 级
        main_object=0x768,
        game_ui=0x7FC,
        game_mode=0x7F8,
        tick_ms=0x454,
        # MainObject 级
        zombie_array=0x90,
        zombie_count_max=0x94,
        zombie_limit=0x98,
        zombie_count=0xA0,
        plant_array=0xAC,
        plant_count_max=0xB0,
        plant_count=0xBC,
        seed_array=0x144,
        item_array=0xE4,
        item_count_max=0xE8,
        grid_item_array=0x11C,
        grid_item_count_max=0x120,
        projectile_array=0xC8,
        projectile_count_max=0xCC,
        lawn_mower_array=0x100,
        lawn_mower_count_max=0x104,
        lawn_mower_count=0x110,
        mouse=0x138,
        mouse_extra=0x13C,
        game_paused=0x164,
        scene=0x554C,
        sun=0x5560,
        game_clock=0x5568,
        wave=0x557C,
        total_wave=0x5564,
        refresh_countdown=0x559C,
        huge_wave_countdown=0x55A4,
        initial_countdown=0x55A0,
        zombie_refresh_hp=0x5594,
        level_end_countdown=0x5604,
        adventure_level=0x5550,
        zombie_list=0x6B4,
        zombie_type_list=0x54D4,
        grid_type_list=0x168,
        row_type=0x5D8,
        # 实体大小
        zombie_struct_size=0x15C,
        plant_struct_size=0x14C,
        # 全局基址 (pvztoolkit lawn)
        pvz_base=0x6A9EC0,
    )


def _make_goty_base_offsets(version: PvZVersion, version_name: str,
                            pvz_base: int, game_ui: int, game_mode: int) -> PvZOffsets:
    """GOTY 系列偏移 — MainObject 级全部为原版 + 0x18.

    数据来源: pvztoolkit data.cpp (data_goty_*)。所有 GOTY 版本的
    MainObject 级偏移相同 (+0x18)，仅 PvzBase 级 game_ui/game_mode
    与全局基址 pvz_base 因版本而异:
      - GOTY 1.2.0.1073/1096 EN, GOTY 1.1.0.1056 JA: game_ui=0x91C, game_mode=0x918
      - GOTY 1.1.0.1056 ZH, ZH 2012-06/07:              game_ui=0x920, game_mode=0x91C
    """
    return PvZOffsets(
        version=version,
        version_name=version_name,
        # PvzBase 级
        main_object=0x868,       # 0x768 + 0x100
        game_ui=game_ui,
        game_mode=game_mode,
        tick_ms=0x4B4,           # 0x454 + 0x60
        # MainObject 级 — 均为原版 + 0x18
        zombie_array=0xA8,       # 0x90 + 0x18
        zombie_count_max=0xAC,   # 0x94 + 0x18
        zombie_limit=0xB0,       # 0x98 + 0x18
        zombie_count=0xB8,       # 0xA0 + 0x18
        plant_array=0xC4,        # 0xAC + 0x18
        plant_count_max=0xC8,    # 0xB0 + 0x18
        plant_count=0xD4,        # 0xBC + 0x18
        seed_array=0x15C,        # 0x144 + 0x18
        item_array=0xFC,         # 0xE4 + 0x18
        item_count_max=0x100,    # 0xE8 + 0x18
        grid_item_array=0x134,   # 0x11C + 0x18
        grid_item_count_max=0x138,  # 0x120 + 0x18
        projectile_array=0xE0,   # 0xC8 + 0x18
        projectile_count_max=0xE4,  # 0xCC + 0x18
        lawn_mower_array=0x118,  # 0x100 + 0x18
        lawn_mower_count_max=0x11C,  # 0x104 + 0x18
        lawn_mower_count=0x128,  # 0x110 + 0x18
        mouse=0x150,             # 0x138 + 0x18
        mouse_extra=0x154,       # 0x13C + 0x18
        game_paused=0x17C,       # 0x164 + 0x18
        scene=0x5564,            # 0x554C + 0x18
        sun=0x5578,              # 0x5560 + 0x18
        game_clock=0x5580,       # 0x5568 + 0x18
        wave=0x5594,             # 0x557C + 0x18
        total_wave=0x557C,       # 0x5564 + 0x18
        refresh_countdown=0x55B4,  # 0x559C + 0x18
        huge_wave_countdown=0x55BC,  # 0x55A4 + 0x18
        initial_countdown=0x55B8,  # 0x55A0 + 0x18
        zombie_refresh_hp=0x55AC,  # 0x5594 + 0x18
        level_end_countdown=0x561C,  # 0x5604 + 0x18
        adventure_level=0x5568,  # 0x5550 + 0x18
        zombie_list=0x6CC,       # 0x6B4 + 0x18
        zombie_type_list=0x54EC,  # 0x54D4 + 0x18
        grid_type_list=0x180,    # 0x168 + 0x18
        row_type=0x5F0,          # 0x5D8 + 0x18
        # 实体大小 — GOTY 僵尸更大
        zombie_struct_size=0x168,
        plant_struct_size=0x14C,
        # TODO: 以下 GOTY 结构体大小需实测确认
        item_struct_size=0xF0,         # 0xD8 + 0x18?
        grid_item_struct_size=0x104,   # 0xEC + 0x18?
        lawn_mower_struct_size=0x60,   # 0x48 + 0x18?
        projectile_struct_size=0xAC,   # 0x94 + 0x18?
        # 全局基址 (pvztoolkit lawn)
        pvz_base=pvz_base,
    )


# ================================================================== #
#  版本偏移注册表
# ================================================================== #

_OFFSET_REGISTRY: dict[PvZVersion, PvZOffsets] = {
    PvZVersion.V1_0_0_1051_EN: _make_original_offsets(),
    PvZVersion.V1_2_0_1065_EN: _make_original_offsets(PvZVersion.V1_2_0_1065_EN, "1.2.0.1065 EN"),
    PvZVersion.GOTY_1_2_0_1073_EN: _make_goty_base_offsets(
        PvZVersion.GOTY_1_2_0_1073_EN, "GOTY 1.2.0.1073 EN", 0x729670, 0x91C, 0x918),
    PvZVersion.GOTY_1_2_0_1096_EN: _make_goty_base_offsets(
        PvZVersion.GOTY_1_2_0_1096_EN, "GOTY 1.2.0.1096 EN", 0x731C50, 0x91C, 0x918),
    PvZVersion.GOTY_1_1_0_1056_ZH: _make_goty_base_offsets(
        PvZVersion.GOTY_1_1_0_1056_ZH, "GOTY 1.1.0.1056 ZH", 0x7794F8, 0x920, 0x91C),
    PvZVersion.GOTY_1_1_0_1056_JA: _make_goty_base_offsets(
        PvZVersion.GOTY_1_1_0_1056_JA, "GOTY 1.1.0.1056 JA", 0x7578F8, 0x91C, 0x918),
    PvZVersion.GOTY_1_1_0_1056_ZH_2012_06: _make_goty_base_offsets(
        PvZVersion.GOTY_1_1_0_1056_ZH_2012_06, "GOTY 1.1.0.1056 ZH (2012-06)", 0x755E0C, 0x920, 0x91C),
    PvZVersion.GOTY_1_1_0_1056_ZH_2012_07: _make_goty_base_offsets(
        PvZVersion.GOTY_1_1_0_1056_ZH_2012_07, "GOTY 1.1.0.1056 ZH (2012-07)", 0x757E0C, 0x920, 0x91C),
}


def get_offsets(version: PvZVersion) -> PvZOffsets | None:
    """根据版本号获取偏移量表."""
    return _OFFSET_REGISTRY.get(version)


def detect_version_from_timestamp(timestamp: int) -> PvZVersion:
    """根据 PE 时间戳检测 PvZ 版本."""
    return _PE_TIMESTAMP_MAP.get(timestamp, PvZVersion.UNSUPPORTED)


def get_all_known_timestamps() -> dict[int, PvZVersion]:
    """返回所有已知的 PE 时间戳 → 版本映射."""
    return dict(_PE_TIMESTAMP_MAP)
