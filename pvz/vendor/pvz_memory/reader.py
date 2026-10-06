"""PvZ 游戏状态读取器 — 读取内存并格式化为结构化文本.

这是 pvz_memory 模块的核心输出层: 从 PvZ 进程内存读取完整游戏状态,
格式化为 LLM 可理解的结构化文本, 注入到 LLM/VLM Agent 的 prompt 中.

读取内容:
- 基础信息: 阳光/波数/场景/游戏界面/暂停状态/时钟
- 种子卡片: 类型/冷却/可用性/位置
- 植物阵型: 类型/位置/血量/状态/是否睡觉
- 僵尸情报: 类型/位置/血量/状态/减速/冻结
- 收集物: 阳光/金币等掉落物位置
- 场地物品: 墓碑/弹坑/梯子/钉耙
- 割草机: 是否存活

稳定性设计:
- 每个实体段独立 try/except, 单段数据异常不击穿整个读取
- GameState.last_error 记录最近一次读取的失败原因, 便于诊断准确性
- 日志使用标准库 logging, 模块 logger 名为 "pvz_memory.reader"
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory import PvZMemory, PvZMemoryError
from .offsets import (
    ITEM_NAMES,
    PLACE_ITEM_NAMES,
    PLANT_NAMES,
    PLANT_POT_TYPES,
    PLANT_PUMPKIN_TYPE,
    PLANT_SUN_COST,
    ZOMBIE_NAMES,
    GameUI,
)

logger = logging.getLogger(__name__)


def _as_int(value: Any, default: int = -1) -> int:
    """宽容转 int（None/垃圾值 → default；保留 0，绝不用 `or` 折叠合法零值）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ================================================================== #
#  传送带关权威判定（0.4.8 重构）
#
#  旧逻辑"mode≠0 且卡槽全空 → 传送带"是瞬态启发式：传送带开局几十秒内
#  就把卡栏喂满（实测僵王关 -1,-1 → …→ 8/10 槽有卡），第一次读到有卡就
#  被误判普通关——小游戏僵王博士的复仇全程判错。现改为**权威白名单**：
#
#  来源：PvZ 反编译 enum GameMode（BobVarioa/pvz1-decompiled ConstEnums.h，
#  从 0 起数）。锚点验证：实机小游戏"僵王博士的复仇"读数 game_mode=35
#  = GAMEMODE_CHALLENGE_FINAL_BOSS，逐位吻合。冒险关卡号 Board+0x5550
#  （=(章-1)*10+关）已有 1-5=5 实机验证。
# ================================================================== #

# 传送带关（卡栏由传送带自动喂卡、用后消失）的 GameMode 白名单
BELT_GAME_MODES = frozenset({
    17,   # 坚果保龄球（小游戏版）GAMEMODE_CHALLENGE_WALLNUT_BOWLING
    33,   # 坚果保龄球2（小游戏版）GAMEMODE_CHALLENGE_WALLNUT_BOWLING_2
    35,   # 僵王博士的复仇（小游戏版）GAMEMODE_CHALLENGE_FINAL_BOSS
})
# 冒险模式里的传送带关（game_mode=0 时按关卡号识别，关卡号=(章-1)*10+关）
# 0.4.14 用户规则：冒险共 5 个大关，**每个大关的第 10 关都是传送带关**
# （1-10 实测曾被误判普通关）；0.4.15 用户补充：2-5/3-5/5-5 也是传送带。
# 1-5 坚果保龄球为实机验证过的补充。旧条目 35（4-5）是纯推断、用户枚举
# 未列入——保持普通关：把普通关误判成传送带会免阳光，比漏判危害大得多
# （如 4-5 实测是传送带，把 35 加回本集合即可）。
BELT_ADVENTURE_LEVELS = frozenset({
    5,    # 1-5 坚果保龄球——实机验证
    10,   # 1-10——用户规则（实测曾误判普通关）
    15,   # 2-5——0.4.15 用户补充
    20,   # 2-10——用户规则
    25,   # 3-5——0.4.15 用户补充
    30,   # 3-10——用户规则
    40,   # 4-10——用户规则
    45,   # 5-5——0.4.15 用户补充
    50,   # 5-10 僵王博士——用户规则，与此前实机推断一致
})

# 行为语义（0.4.9 用户决策，统一单档）：所有传送带关**免阳光、无冷却、
# 无任何阳光/冷却提示**。僵王关实测虽有阳光经济（天降阳光、卷心菜 100☀），
# 但种植走 PutPlant 直注（游戏函数不扣阳光），插件统一按 0 费用注入、
# 不开冷却、文本不提阳光——防止模型把"阳光 0"当成种不了而干等。
# 0.4.8 曾给僵王类保留"有阳光"档（真实扣费+硬拦），实机阳光=0 时把模型
# 卡死（"需要 100 阳光，当前只有 0"），已按统一语义回退。


def level_is_conveyor(state: Any) -> bool:
    """传送带关判定：GameMode/冒险关卡号权威白名单（0.4.8）.

    - mode ∈ {17, 33, 35}（坚果保龄球1/2、僵王博士小游戏）→ 传送带；
    - mode=0（冒险）且 Board+0x5550 ∈ {5, 10, 15, 20, 25, 30, 40, 45, 50}
      （1-5 保龄球 + 各大关第 10 关 + 2-5/3-5/5-5，0.4.14/0.4.15 用户规则）→ 传送带；
    - 其余一律普通关（生存/禅境/图鉴/解谜/未知模式都**不是**传送带）。

    为什么不再用"卡槽全空"启发式：传送带关开局卡栏很快被喂满，瞬态信号
    只在头几十秒成立，之后恒误判普通关（僵王关全程判错的根因）；而生存
    模式选卡前卡栏也全空，反向误判。白名单对原版/中文版全量关卡权威。
    传送带关行为语义见上方常量注释（0.4.9 统一免阳光/无冷却）。
    """
    try:
        mode = _as_int(getattr(state, "game_mode", -1))
        if mode in BELT_GAME_MODES:
            return True
        if mode == 0 and _as_int(getattr(state, "adventure_level", -1)) in BELT_ADVENTURE_LEVELS:
            return True
    except Exception:
        return False
    return False


# ================================================================== #
#  数据类 — 各类实体的结构化表示
# ================================================================== #

@dataclass
class SeedInfo:
    """种子/卡片信息."""
    index: int          # 卡片槽位 (0-based)
    plant_type: int     # 植物类型 ID
    name: str           # 植物名称
    sun_cost: int       # 阳光消耗
    cd: int             # 已冷却时长 (厘秒, 游戏内从 0 逐帧递增到 initial_cd)
    initial_cd: int     # 冷却总时长 (厘秒)
    is_usable: bool     # 是否可用
    imitator_type: int  # 模仿者实际类型 (-1=非模仿者)
    x: int = 0          # 卡片横坐标
    y: int = 0          # 卡片纵坐标
    width: int = 0      # 卡片宽度
    height: int = 0     # 卡片高度

    @property
    def is_ready(self) -> bool:
        return self.is_usable and self.cd == 0

    @property
    def cd_remaining(self) -> int:
        """剩余冷却（厘秒）。

        游戏内存 0x24 是"已冷却时长"（pvzclass SeedCard.CoolDown：从 0 逐帧
        递增，超过 0x28 总时长时游戏解除冷却并回写 0），因此剩余 =
        总时长 - 已冷却。0x28 读到脏值（< 已冷却）时退回旧口径原样显示。
        """
        if self.cd <= 0:
            return 0
        if self.initial_cd >= self.cd:
            return self.initial_cd - self.cd
        return self.cd

    @property
    def cd_progress(self) -> float:
        """冷却进度 0.0~1.0（已冷却时长占比），1.0=就绪."""
        if self.initial_cd <= 0:
            return 1.0
        return max(0.0, min(1.0, self.cd / self.initial_cd))


@dataclass
class PlantInfo:
    """植物信息."""
    index: int          # 数组下标
    plant_type: int     # 植物类型
    name: str           # 植物名称
    row: int            # 行 (0-based)
    col: int            # 列 (0-based)
    hp: int             # 当前血量
    hp_max: int         # 最大血量
    state: int          # 状态码
    is_sleeping: bool   # 是否睡觉
    is_crushed: bool    # 是否被压扁
    shoot_countdown: int = 0   # 射击倒计时

    @property
    def hp_ratio(self) -> float:
        if self.hp_max <= 0:
            return 0.0
        return self.hp / self.hp_max


@dataclass
class ZombieInfo:
    """僵尸信息."""
    index: int          # 数组下标
    zombie_type: int    # 僵尸类型
    name: str           # 僵尸名称
    row: int            # 行 (0-based)
    abscissa: float     # 横坐标 (像素级, 越小越靠右接近房屋)
    speed: float        # 横向速度
    hp: int             # 本体血量
    one_hp: int         # 一类饰品血量 (路障/铁桶)
    two_hp: int         # 二类饰品血量 (铁门)
    state: int          # 状态码
    is_eat: bool        # 是否在啃食
    at_wave: int        # 所在波数
    slow_countdown: int = 0
    fixation_countdown: int = 0   # 黄油固定
    freeze_countdown: int = 0

    @property
    def total_hp(self) -> int:
        return self.hp + self.one_hp + self.two_hp

    @property
    def is_hammering(self) -> bool:
        """巨人是否举锤."""
        return self.state == 70

    @property
    def is_dead(self) -> bool:
        return self.state in (1, 2, 3)

    @property
    def col_estimate(self) -> float:
        """将横坐标估算为列号 (0~8)，保留一位小数。

        经验证 (verify_grid_formula.py):
        - grid_to_pixel 返回: col 0→x=80, col 1→x=160, ..., col 8→x=720
        - 即 x = (col+1) * 80, 反推 col = x/80 - 1
        - 僵尸 abscissa 是锚点 (通常脚部中心)，不是 sprite 视觉中心
        - 僵尸 sprite 较宽，视觉上看可能延伸到下一列

        公式: col = (abscissa - 40) / 80
        例: abscissa=661.6 → col=7.8 (在 col 7 偏右，距 col 7 中心仅 21.6px)
        """
        if not math.isfinite(self.abscissa) or self.abscissa <= 0:
            # 防 NaN/Inf 出现 "nan" 文本
            return 0.0
        return min(8.0, max(0.0, round((self.abscissa - 40) / 80, 1)))


@dataclass
class ItemInfo:
    """收集物信息."""
    index: int
    item_type: int
    name: str
    x: float            # 横坐标
    y: float            # 纵坐标
    is_collected: bool

    @property
    def is_sun(self) -> bool:
        return self.item_type in (1, 2)


@dataclass
class GridItemInfo:
    """场地物品信息."""
    index: int
    item_type: int
    name: str
    row: int
    col: int
    value: int          # 弹坑倒计时/墓碑冒出量等


@dataclass
class LawnMowerInfo:
    """割草机信息."""
    index: int
    row: int            # 推断的行号
    is_alive: bool


@dataclass
class GameState:
    """完整游戏状态."""
    # 基础
    game_ui: int = 0
    game_mode: int = 0
    adventure_level: int = -1   # 冒险关卡序号（Board+0x5550，1-1=1 … 5-10=50；非冒险/读失败=-1）
    sun: int = 0
    scene: int = -1
    scene_name: str = "未知"
    is_paused: bool = False

    # 波次
    wave: int = 0
    total_wave: int = 0
    game_clock: int = 0
    refresh_countdown: int = 0
    huge_wave_countdown: int = 0
    level_end_countdown: int = 0

    # 实体列表
    seeds: list[SeedInfo] = field(default_factory=list)
    plants: list[PlantInfo] = field(default_factory=list)
    zombies: list[ZombieInfo] = field(default_factory=list)
    items: list[ItemInfo] = field(default_factory=list)
    grid_items: list[GridItemInfo] = field(default_factory=list)
    lawn_mowers: list[LawnMowerInfo] = field(default_factory=list)

    # 诊断信息: 最近一次读取的失败原因, 空串表示无错误
    last_error: str = ""

    # 是否在战斗中
    @property
    def in_battle(self) -> bool:
        # game_ui==SELECT_CARD(2): 选卡界面，一定不在战斗中。
        #   生存模式过完一大波后进入新一轮选卡，场上残留上一轮的 plants，
        #   但 game_ui 已回到 2，此时应走选卡分支而非战斗分支。
        # game_ui==IN_GAME(3): 正常战斗，一定在战斗中。
        # game_ui!=2/3 但 plants>0: 教学关等特殊场景（game_ui 仍是其他值但实际在战斗，
        #   场上有要铲的教学植物）。用 plants 而非 zombies 判定，因为选卡界面会预加载
        #   僵尸到屏幕外等待（zombies>0 但 plants=0），不能误判为战斗中。
        if self.game_ui == GameUI.SELECT_CARD:
            return False
        if self.game_ui == GameUI.IN_GAME:
            return True
        return bool(self.plants)


# ================================================================== #
#  游戏状态读取器
# ================================================================== #

class PvZStateReader:
    """从 PvZ 内存读取游戏状态并格式化为文本.

    用法::

        mem = PvZMemory()
        mem.connect()

        reader = PvZStateReader(mem)
        state = reader.read_state()
        text = reader.format_state(state)
        # 将 text 注入 LLM prompt
    """

    # 场景名称映射
    SCENE_NAMES: dict[int, str] = {
        0: "白天", 1: "黑夜", 2: "泳池", 3: "雾夜",
        4: "天台", 5: "月夜",
    }

    def __init__(self, memory: PvZMemory, guide_dir: str | Path | None = None) -> None:
        self._mem = memory
        self._guide_dir = Path(guide_dir) if guide_dir else None

    def conveyor_verdict(self, state: Any) -> bool:
        """传送带判定：**逐轮纯推导**（level_is_conveyor），无任何实例级缓存。

        结果盖章 state._is_conveyor 供 executor 共享。传送带关行为语义
        （0.4.9 统一免阳光/无冷却/无提示）见白名单常量注释。
        为什么不用粘性缓存：service 有多个读取入口
        （文本循环、pvz_scan、逐动作 execute 重读）且运行时进程长期存活，
        实例缓存在跨关/重启混载时会分叉——实测 20:54 文本循环判普通关、
        execute 却按陈旧缓存当传送带种（无阳光无冷却）。纯推导对同一内存
        状态恒等，天然一致。
        """
        verdict = level_is_conveyor(state)
        try:
            state._is_conveyor = verdict
        except Exception:
            pass
        self._dump_bank_if_changed(state, verdict)
        return verdict

    def _dump_bank_if_changed(self, state: Any, verdict: bool) -> None:
        """关卡/卡槽变化时记一行判定依据（bank头作传送带标定数据，尽力而为）。

        签名只取 game_mode + 卡槽类型——bank头里含逐帧变化的内部计数
        （实测第 3 个 int 在 0/1/10 间跳动），按全头做 key 会每秒刷屏；
        0.4.2 起只在真正换关/卡槽变动时记一行，并把 game_mode=0 的含义
        写进日志（0=冒险/普通关，是"非传送带"的证据而非判定结果）。
        """
        try:
            mode = _as_int(getattr(state, "game_mode", -1))
            bar = tuple(_as_int(getattr(s, "plant_type", -1))
                        for s in (getattr(state, "seeds", []) or [])[:10])
            key = (mode, bar)
            if key != getattr(self, "_last_bank_key", None):
                self._last_bank_key = key
                bank = self._read_bank_header()
                kind = ("传送带关（不计阳光/无冷却，卡牌用后由游戏原生消耗）"
                        if verdict else "普通关（非传送带，计阳光）")
                logger.info(
                    "[PvZ] 传送带判定: game_mode=%s 冒险关卡号=%s 卡槽=%s bank头=%s → %s",
                    mode, getattr(state, "adventure_level", "?"), list(bar), bank, kind,
                )
        except Exception:
            pass

    def _read_bank_header(self) -> list[int] | None:
        """卡槽组（SeedBank）头部 10 个 int——传送带标定用，尽力而为。"""
        try:
            if not (self._mem and self._mem.main_object):
                return None
            off = self._mem.offsets
            base = self._mem.read_pointer(self._mem.main_object + off.seed_array)
            if not base:
                return None
            return [self._mem.read_int(base + i * 4) for i in range(10)]
        except Exception:
            return None

    @staticmethod
    def _note_error(state: GameState, tag: str, exc: Exception) -> None:
        """把某个实体段的读取失败记录到 state.last_error（截断防膨胀）."""
        msg = f"{tag}: {exc}"
        state.last_error = (state.last_error + "; " + msg) if state.last_error else msg
        state.last_error = state.last_error[:500]

    def read_state(self) -> GameState:
        """读取完整游戏状态."""
        state = GameState()

        if not self._mem.is_connected():
            state.last_error = "未连接到 PvZ 进程"
            return state

        try:
            # 刷新 MainObject 指针
            self._mem.refresh_main_object()
        except PvZMemoryError as e:
            self._note_error(state, "刷新 MainObject", e)
            return state

        # 基础信息
        state.game_ui = self._mem.get_game_ui()
        state.game_mode = self._mem.get_game_mode()
        # 冒险关卡序号（0.4.3）：供 level_is_conveyor 识别冒险模式内的
        # 传送带关（1-5 坚果保龄球，实测 Board+0x5550=5）。越界视为读失败。
        try:
            adv = self._mem.read_int(
                self._mem.main_object + self._mem.offsets.adventure_level
            )
            state.adventure_level = adv if 0 <= adv <= 100 else -1
        except (PvZMemoryError, OSError, ValueError):
            state.adventure_level = -1

        if not self._mem.main_object:
            return state

        # 注意: game_ui 不可靠。教学关 / 小游戏等特殊场景 game_ui 可能仍是
        # SELECT_CARD(2) 而非 IN_GAME(3)，但场上已有植物、铲子、僵尸，
        # 实际处于"战斗中"。这里改为只要有 MainObject 就读取详细状态，
        # 让 format_state 依据是否有实体来决定输出战斗信息，避免教学关
        # 拿不到 plants 精确坐标、模型只能靠视觉数格子导致列号偏移。

        # 战斗中的详细状态
        state.sun = self._mem.get_sun()
        state.scene = self._mem.get_scene()
        state.scene_name = self.SCENE_NAMES.get(state.scene, "未知")
        state.is_paused = self._mem.is_game_paused()

        state.wave = self._mem.get_wave()
        state.total_wave = self._mem.get_total_wave()
        state.game_clock = self._mem.get_game_clock()
        state.refresh_countdown = self._mem.get_refresh_countdown()
        state.huge_wave_countdown = self._mem.get_huge_wave_countdown()
        state.level_end_countdown = self._mem.get_level_end_countdown()

        # 读取各类实体 — 每段独立容错，单段异常不击穿整个读取
        try:
            state.seeds = self._read_seeds()
        except (PvZMemoryError, OSError, ValueError) as e:
            logger.debug("[PvZReader] 读取种子失败: %s", e)
            self._note_error(state, "读取种子", e)

        try:
            state.plants = self._read_plants()
        except (PvZMemoryError, OSError, ValueError) as e:
            logger.debug("[PvZReader] 读取植物失败: %s", e)
            self._note_error(state, "读取植物", e)

        try:
            state.zombies = self._read_zombies()
        except (PvZMemoryError, OSError, ValueError) as e:
            logger.debug("[PvZReader] 读取僵尸失败: %s", e)
            self._note_error(state, "读取僵尸", e)

        try:
            state.items = self._read_items()
        except (PvZMemoryError, OSError, ValueError) as e:
            logger.debug("[PvZReader] 读取收集物失败: %s", e)
            self._note_error(state, "读取收集物", e)

        try:
            state.grid_items = self._read_grid_items()
        except (PvZMemoryError, OSError, ValueError) as e:
            logger.debug("[PvZReader] 读取场地物品失败: %s", e)
            self._note_error(state, "读取场地物品", e)

        try:
            state.lawn_mowers = self._read_lawn_mowers()
        except (PvZMemoryError, OSError, ValueError) as e:
            logger.debug("[PvZReader] 读取割草机失败: %s", e)
            self._note_error(state, "读取割草机", e)

        # 传送带判定每轮无条件执行（盖章 state._is_conveyor 供 executor
        # 共享）——动态证据（卡槽类型变化）依赖逐轮观测，不能只在文本
        # 构建且卡槽为空时才调用，否则冒险传送带关（game_mode=0、卡槽
        # 首观测非空）永远识别不出。
        try:
            self.conveyor_verdict(state)
        except Exception as exc:
            logger.debug("[PvZReader] 传送带判定异常: %s", exc)

        # 草皮行推导（教学关特殊布局）：每轮无条件重算盖章，与传送带判定
        # 同理——多个读取入口共享同一 state 结论，纯推导不缓存不分叉。
        try:
            self._stamp_plantable_rows(state)
        except Exception as exc:
            logger.debug("[PvZReader] 草皮行推导异常: %s", exc)

        return state

    # ------------------------------------------------------------------ #
    #  种子/卡片读取
    # ------------------------------------------------------------------ #

    def _read_seeds(self) -> list[SeedInfo]:
        """读取种子卡片列表."""
        off = self._mem.offsets
        mo = self._mem.main_object

        # 读取种子数组指针
        seed_array = self._mem.read_pointer(mo + off.seed_array)
        if not seed_array:
            return []

        # 读取卡片数量
        count = self._mem.read_int(seed_array + off.seed_count)

        seeds: list[SeedInfo] = []
        for i in range(min(count, 10)):  # 最多 10 张卡
            card_addr = seed_array + off.seed_card_offset + i * off.seed_card_size

            try:
                plant_type = self._mem.read_int(card_addr + off.sc_type)
                cd = self._mem.read_int(card_addr + off.sc_cd)
                initial_cd = self._mem.read_int(card_addr + off.sc_initial_cd)
                imitator_type = self._mem.read_int(card_addr + off.sc_imitator_type)
                is_usable = self._mem.read_bool(card_addr + off.sc_usable)
                x = self._mem.read_int(card_addr + off.sc_x)
                y = self._mem.read_int(card_addr + off.sc_y)
                width = self._mem.read_int(card_addr + off.sc_width)
                height = self._mem.read_int(card_addr + off.sc_height)
            except PvZMemoryError:
                break

            # 确定植物名称（模仿者显示实际模仿的植物）。
            # 注意: 只有 plant_type==48 才是模仿者卡。杂交版等改版新增的
            # plant_type>48 是全新植物, 不归为模仿者, 按普通未知植物处理
            # （避免其 imitator_type<0 被误标成"模仿?"）。
            if plant_type == 48:
                # 模仿者卡片
                actual_type = imitator_type if imitator_type >= 0 else plant_type
                name = f"模仿{PLANT_NAMES.get(actual_type, '?')}"
                sun_cost = PLANT_SUN_COST.get(actual_type, 0)
            else:
                name = PLANT_NAMES.get(plant_type, f"未知({plant_type})")
                sun_cost = PLANT_SUN_COST.get(plant_type, 0)
                imitator_type = -1

            seeds.append(SeedInfo(
                index=i,
                plant_type=plant_type,
                name=name,
                sun_cost=sun_cost,
                cd=cd,
                initial_cd=initial_cd,
                is_usable=is_usable,
                imitator_type=imitator_type,
                x=x, y=y, width=width, height=height,
            ))

        return seeds

    # ------------------------------------------------------------------ #
    #  植物读取
    # ------------------------------------------------------------------ #

    def _read_plants(self) -> list[PlantInfo]:
        """读取场上植物列表."""
        off = self._mem.offsets
        mo = self._mem.main_object

        plant_array = self._mem.read_pointer(mo + off.plant_array)
        if not plant_array:
            return []

        count_max = self._mem.read_int(mo + off.plant_count_max)

        plants: list[PlantInfo] = []
        for i in range(min(count_max, 200)):
            addr = plant_array + i * off.plant_struct_size

            try:
                is_disappeared = self._mem.read_bool(addr + off.p_is_disappeared)
            except PvZMemoryError:
                break

            if is_disappeared:
                continue

            try:
                plant_type = self._mem.read_int(addr + off.p_type)
                row = self._mem.read_int(addr + off.p_row)
                col = self._mem.read_int(addr + off.p_col)
                hp = self._mem.read_int(addr + off.p_hp)
                hp_max = self._mem.read_int(addr + off.p_hp_max)
                state = self._mem.read_int(addr + off.p_state)
                is_sleeping = self._mem.read_bool(addr + off.p_is_sleeping)
                is_crushed = self._mem.read_bool(addr + off.p_is_crushed)
                shoot_cd = self._mem.read_int(addr + off.p_shoot_countdown)
            except PvZMemoryError:
                continue

            name = PLANT_NAMES.get(plant_type, f"未知({plant_type})")

            plants.append(PlantInfo(
                index=i,
                plant_type=plant_type,
                name=name,
                row=row,
                col=col,
                hp=hp,
                hp_max=hp_max,
                state=state,
                is_sleeping=is_sleeping,
                is_crushed=is_crushed,
                shoot_countdown=shoot_cd,
            ))

        return plants

    # ------------------------------------------------------------------ #
    #  僵尸读取
    # ------------------------------------------------------------------ #

    def _read_zombies(self) -> list[ZombieInfo]:
        """读取场上僵尸列表."""
        off = self._mem.offsets
        mo = self._mem.main_object

        zombie_array = self._mem.read_pointer(mo + off.zombie_array)
        if not zombie_array:
            return []

        count_max = self._mem.read_int(mo + off.zombie_count_max)

        zombies: list[ZombieInfo] = []
        for i in range(min(count_max, 500)):
            addr = zombie_array + i * off.zombie_struct_size

            try:
                is_disappeared = self._mem.read_bool(addr + off.z_is_disappeared)
            except PvZMemoryError:
                break

            if is_disappeared:
                continue

            try:
                zombie_type = self._mem.read_int(addr + off.z_type)
                row = self._mem.read_int(addr + off.z_row)
                abscissa = self._mem.read_float(addr + off.z_abscissa)
                speed = self._mem.read_float(addr + off.z_speed)
                hp = self._mem.read_int(addr + off.z_hp)
                one_hp = self._mem.read_int(addr + off.z_one_hp)
                two_hp = self._mem.read_int(addr + off.z_two_hp)
                state = self._mem.read_int(addr + off.z_state)
                is_eat = self._mem.read_bool(addr + off.z_is_eat)
                at_wave = self._mem.read_int(addr + off.z_at_wave)
                slow_cd = self._mem.read_int(addr + off.z_slow_countdown)
                fixation_cd = self._mem.read_int(addr + off.z_fixation_countdown)
                freeze_cd = self._mem.read_int(addr + off.z_freeze_countdown)
            except PvZMemoryError:
                continue

            # 跳过已死亡的僵尸
            if state in (1, 2, 3):
                continue

            name = ZOMBIE_NAMES.get(zombie_type, f"未知({zombie_type})")

            zombies.append(ZombieInfo(
                index=i,
                zombie_type=zombie_type,
                name=name,
                row=row,
                abscissa=abscissa,
                speed=speed,
                hp=hp,
                one_hp=one_hp,
                two_hp=two_hp,
                state=state,
                is_eat=is_eat,
                at_wave=at_wave,
                slow_countdown=slow_cd,
                fixation_countdown=fixation_cd,
                freeze_countdown=freeze_cd,
            ))

        # 按行、横坐标排序，方便阅读
        zombies.sort(key=lambda z: (z.row, z.abscissa))
        return zombies

    # ------------------------------------------------------------------ #
    #  收集物读取
    # ------------------------------------------------------------------ #

    def _read_items(self) -> list[ItemInfo]:
        """读取收集物列表（阳光、金币等）."""
        off = self._mem.offsets
        mo = self._mem.main_object

        item_array = self._mem.read_pointer(mo + off.item_array)
        if not item_array:
            return []

        count_max = self._mem.read_int(mo + off.item_count_max)

        items: list[ItemInfo] = []
        for i in range(min(count_max, 200)):
            addr = item_array + i * off.item_struct_size

            try:
                is_disappeared = self._mem.read_bool(addr + off.i_is_disappeared)
            except PvZMemoryError:
                break

            if is_disappeared:
                continue

            try:
                item_type = self._mem.read_int(addr + off.i_type)
                x = self._mem.read_float(addr + off.i_abscissa)
                y = self._mem.read_float(addr + off.i_ordinate)
                is_collected = self._mem.read_bool(addr + off.i_is_collected)
            except PvZMemoryError:
                continue

            if is_collected:
                continue

            name = ITEM_NAMES.get(item_type, f"物品({item_type})")

            items.append(ItemInfo(
                index=i,
                item_type=item_type,
                name=name,
                x=x, y=y,
                is_collected=is_collected,
            ))

        return items

    # ------------------------------------------------------------------ #
    #  场地物品读取
    # ------------------------------------------------------------------ #

    def _read_grid_items(self) -> list[GridItemInfo]:
        """读取场地物品（墓碑、弹坑、梯子等）."""
        off = self._mem.offsets
        mo = self._mem.main_object

        gi_array = self._mem.read_pointer(mo + off.grid_item_array)
        if not gi_array:
            return []

        count_max = self._mem.read_int(mo + off.grid_item_count_max)

        result: list[GridItemInfo] = []
        for i in range(min(count_max, 200)):
            addr = gi_array + i * off.grid_item_struct_size

            try:
                is_disappeared = self._mem.read_bool(addr + off.gi_is_disappeared)
            except PvZMemoryError:
                break

            if is_disappeared:
                continue

            try:
                item_type = self._mem.read_int(addr + off.gi_type)
                col = self._mem.read_int(addr + off.gi_col)
                row = self._mem.read_int(addr + off.gi_row)
                value = self._mem.read_int(addr + off.gi_value)
            except PvZMemoryError:
                continue

            name = PLACE_ITEM_NAMES.get(item_type, f"场地({item_type})")

            result.append(GridItemInfo(
                index=i,
                item_type=item_type,
                name=name,
                row=row,
                col=col,
                value=value,
            ))

        return result

    # ------------------------------------------------------------------ #
    #  割草机读取
    # ------------------------------------------------------------------ #

    def _read_lawn_mowers(self) -> list[LawnMowerInfo]:
        """读取割草机状态.

        行号读割草机对象内的真实行（lm_row=0x14）——0.4.2 实测教学关只在
        草皮行配割草机且行号为全草坪坐标（三行草皮关=[1,2,3]），普通关
        [0..N-1]，两者都与数组下标不同，旧实现按 row=i 猜在教学关失准。
        已发射的割草机保留在列表（is_alive=False，对象内的行号依然有效），
        草皮行推导在关末依旧稳定；展示层自行过滤 alive。
        校验：行值必须全在 0..9 且按下标严格递增（数组按行排列），不满足
        视为该版本对象布局不同，退回按数组下标当行号（普通关不受影响）。
        """
        off = self._mem.offsets
        mo = self._mem.main_object

        lm_array = self._mem.read_pointer(mo + off.lawn_mower_array)
        if not lm_array:
            return []

        count_max = self._mem.read_int(mo + off.lawn_mower_count_max)

        rows: list[int] = []
        alive_flags: list[bool] = []
        for i in range(min(count_max, 12)):
            addr = lm_array + i * off.lawn_mower_struct_size

            try:
                is_dead = self._mem.read_bool(addr + off.lm_dead)
                row_raw = self._mem.read_int(addr + off.lm_row)
            except PvZMemoryError:
                break

            rows.append(row_raw if 0 <= row_raw <= 9 else i)
            alive_flags.append(not is_dead)

        # 行值序列非法（越界/非递增）→ 该版本 0x14 不是行字段，退回下标
        if any(rows[i] >= rows[i + 1] for i in range(len(rows) - 1)):
            rows = list(range(len(rows)))

        return [
            LawnMowerInfo(index=i, row=rows[i], is_alive=alive_flags[i])
            for i in range(len(rows))
        ]

    @staticmethod
    def _stamp_plantable_rows(state: Any) -> None:
        """由割草机行推导草皮行并盖章 ``state._plantable_rows``（0.4.2）.

        普通关/泳池关每行一台割草机，行集合连续且从 0 起（[0..N-1]）→
        无特殊布局，盖 None；教学关只在草皮行配割草机（实测 [1,2,3] /
        单行 [2]），推导出非 0 起始的行集合 → 盖行列表，供 executor 把
        种到无草皮行的请求重定向到最近草皮行、供【棋盘】行明示草皮行。
        """
        mowers = getattr(state, "lawn_mowers", []) or []
        rows = sorted({lm.row for lm in mowers})
        plantable = rows if rows and rows != list(range(len(rows))) else None
        state._plantable_rows = plantable

    # ================================================================== #
    #  格式化输出 — 生成 LLM 可理解的结构化文本
    # ================================================================== #

    def format_state(self, state: GameState) -> str:
        """将游戏状态格式化为结构化文本, 用于注入 LLM prompt.

        格式设计原则:
        - 信息密度高, 避免 LLM 解析歧义
        - 行内标记关键状态 (冷却/血量/异常)
        - 僵尸按行分组, 便于战略决策
        """
        # 注意: 这里**不再**把"战斗中卡片全空"强行改判为选卡界面。
        # 该旧判定会把传送带关卡（种子栏开局为空/全无效）误判成选卡界面，
        # 让模型反复调用 select_seeds（传送带关没有选卡 UI，永远无效），
        # 整关死锁不种植物。传送带关的正确呈现是：战斗中 + 卡片栏空 +
        # collect_belt 提示（见下方卡片段）。生存模式换轮过渡期 game_ui
        # 很快回到 SELECT_CARD(2)，即便误显示一两轮战斗状态也无碍。
        if not state.in_battle:
            ui_names = {1: "主界面", 2: "选卡界面", 3: "战斗界面"}
            label = ui_names.get(state.game_ui, "未知")
            # 教学关等特殊场景 game_ui=2 但实际在战斗，此时 in_battle=True，
            # 不会走到这里；真正非战斗时才显示 UI 标签。
            lines = [f"游戏状态: {label} (非战斗)"]
            # 选卡界面：告诉 AI 卡槽总数，避免少选导致随机补满不想要的卡。
            # pick_random_seeds 只填满 AI 没选的空槽，所以少选 = 被动接受随机卡。
            if state.game_ui == GameUI.SELECT_CARD:
                slot_count = self._mem.get_card_slot_count()
                lines.append(
                    f"🎴 卡槽: {slot_count} 个。用 select_seeds 选卡，"
                    f"选不满的槽位会被随机填充，建议选满 {slot_count} 张。"
                )
                # 可选植物库（名 + 阳光价）：让模型按**名字**针对性选卡，而不是盲选数字。
                if PLANT_NAMES:
                    catalog = "  ".join(
                        f"{PLANT_NAMES[t]}({PLANT_SUN_COST.get(t, 0)}☀)"
                        for t in sorted(PLANT_NAMES)
                        if PLANT_NAMES.get(t)
                    )
                    lines.append(f"🌿 可选植物库（select_seeds 按名字选，如 向日葵/豌豆射手）: {catalog}")
                # 场景信息
                if state.scene_name and state.scene_name != "未知":
                    lines.append(f"🗺 场景: {state.scene_name}")
                # 本关将出现的僵尸类型（去重）
                if state.zombies:
                    seen: set[int] = set()
                    zombie_types: list[str] = []
                    for z in state.zombies:
                        if z.zombie_type not in seen:
                            seen.add(z.zombie_type)
                            zombie_types.append(z.name)
                    if zombie_types:
                        lines.append(f"🧟 本关僵尸: {', '.join(zombie_types)}")
                # 波次信息
                if state.total_wave > 0:
                    lines.append(f"🌊 总波次: {state.total_wave}")
                # 扫描图鉴目录，列出可用图鉴
                guide_list = self._scan_guide_dir()
                if guide_list:
                    lines.append(f"📖 可用图鉴: {', '.join(guide_list)}")
                    lines.append("  (不确定植物/僵尸特性时，可用 view_guide 查看图鉴)")
            return "\n".join(lines)

        lines: list[str] = []

        # ---- 基础信息 ----
        # 传送带判定逐轮纯推导并盖章（幂等）——本函数开头重算一次，保证
        # 后文（关卡类型/阳光/卡片状态/空卡栏提示）读到同一轮的一致结论，
        # 不依赖调用方是否已先盖章（read_state 正常会盖，这里双保险）。
        belt = self.conveyor_verdict(state)
        if belt:
            # 0.4.9 起传送带关**统一免阳光**（用户决策）：僵王关实测虽有
            # 阳光经济，但种植走 PutPlant 直注，插件不扣阳光也不显示任何
            # 阳光/冷却提示——防止模型把"阳光 0"当成种不了而干等。
            lines.append("🏷 关卡类型: 传送带关——植物由传送带供给，**不计阳光、无冷却**（卡可用就直接种）")
            lines.append("☀ 阳光: 不适用（传送带关没有阳光机制，绝不要因阳光数字 wait）")
        else:
            lines.append(f"☀ 阳光: {state.sun}")
        # 游戏时钟（厘秒 → mm:ss），帮助模型判断节奏
        if state.game_clock > 0:
            _cs = state.game_clock // 100
            lines.append(f"🕐 游戏时间: {_cs // 60}:{_cs % 60:02d}")
        wave_display = state.wave + 1  # 1-indexed 显示
        is_huge = (state.wave + 1) % 10 == 0 or state.wave + 1 == state.total_wave
        wave_tag = " 🚩大波" if is_huge else ""
        lines.append(f"🌊 波次: {wave_display}/{state.total_wave}{wave_tag}")
        lines.append(f"🗺 场景: {state.scene_name}")

        # 刷新倒计时 (厘秒→秒)
        if state.refresh_countdown > 0:
            lines.append(f"⏱ 下波倒计时: {state.refresh_countdown / 100:.1f}s")
        if state.huge_wave_countdown > 0:
            lines.append(f"🚩大波倒计时: {state.huge_wave_countdown / 100:.1f}s")
        if state.level_end_countdown > 0:
            lines.append(f"🏆通关倒计时: {state.level_end_countdown / 100:.1f}s")

        # 不输出"游戏已暂停"信息。
        # Agent 使用注入冻结主循环来暂停游戏，此时 game_paused 可能仍为 False；
        # 若用户手动 Esc 暂停，game_paused 为 True，但模型不应该去按空格取消暂停
        # （Agent 自己管理暂停/恢复），告诉模型只会误导它去操作暂停菜单。

        lines.append("")

        # ---- 种子卡片 ----
        # 状态判定优先级: 冷却中 > 就绪 > 阳光不足 > 锁定/禁用
        # 关键: cd>0 即在冷却（0x24 是"已冷却时长"，从 0 逐帧递增到 0x28 总
        # 时长，超过即解除），显示真实剩余秒数（总时长-已冷却），不因
        # is_usable 不可靠而吞掉冷却信息。
        lines.append("📋 卡片:")
        # 过滤无效占位卡（plant_type<0）：它们不是真卡（传送带空槽/未初始化），
        # 若按原样显示成 "未知(-1) (0☀) ✅"，模型会误以为有可用卡去 place_plant。
        valid_seeds = [s for s in state.seeds if s.plant_type >= 0]
        if valid_seeds:
            for s in valid_seeds:
                if belt:
                    # 传送带关统一语义（0.4.9）：有卡即可种——不显示冷却
                    # （喂卡节奏由传送带管理）、不显示☀费用/☀不足（免阳光），
                    # 防止模型误判"阳光不足/冷却中不能种"而干等。
                    lines.append(f"  [{s.index}] {s.name} ✅")
                    continue
                if s.cd > 0:
                    # 冷却中: 显示剩余秒数（0x28 总时长 - 0x24 已冷却时长）
                    status = f"⏳{s.cd_remaining / 100:.1f}s"
                elif state.sun >= s.sun_cost:
                    status = "✅"
                elif s.sun_cost > 0:
                    status = "☀不足"
                else:
                    # cd==0、免费、仍不可用 → 多半是被禁用/锁定
                    status = "🔒"
                lines.append(
                    f"  [{s.index}] {s.name} ({s.sun_cost}☀) {status}"
                )
            if belt:
                # 0.4.12：把空槽也点明——模型曾经引用"快照里有效、实际已消耗"
                # 的卡序号去"预支"传送带还没送到的植物。列出空槽序号让它
                # 无歧义地知道哪些 index 能种。
                empty_idx = [str(s.index) for s in state.seeds if s.plant_type < 0]
                if empty_idx:
                    lines.append(
                        f"  (空槽 [{' ,'.join(empty_idx)}]：传送带还没把卡送到这些格——"
                        "不能种；只种上面对应 ✅ 的卡)"
                    )
        else:
            if self.conveyor_verdict(state):
                # 所有传送带关的卡栏都由传送带**自动喂卡**（1-5 坚果保龄球、
                # 僵王博士的复仇实测一致），没有"待收取队列"。collect_belt 的
                # 扫描点与卡栏重叠，点击只会误拾栏内卡——executor 已对全部
                # 传送带关禁用，文本也不再引导模型调用（0.4.7 实证它会照旧反复调）。
                lines.append(
                    "  (空——传送带会自动补卡，无需收取)"
                )
                lines.append(
                    "  👉 传送带关：空槽由传送带自动补卡（collect_belt 已禁用且不必要）——"
                    "先种卡栏已有的卡；全部空槽时本轮 wait 等待补卡后重读"
                )
            else:
                lines.append(
                    "  (空——等待游戏发卡或教程关自动给卡；这不是传送带关，不要 collect_belt，"
                    "有卡后会显示在这里)"
                )
        if not belt and state.seeds and state.sun <= 0 and any(
            getattr(s, "plant_type", -1) < 0 for s in state.seeds
        ):
            # 卡槽有空位 + 阳光归零：多半是传送带关漏判（阳光不会增长）。
            # 不点破结论，只给行为指令——防止模型把"阳光 0"当成死局干等。
            lines.append(
                "  ⚠ 阳光为 0 且卡槽有空位：阳光可能不会增长——只要卡是 ✅ 就直接"
                " place_plant（执行层会自动处理阳光），**绝不要因此 wait**"
            )

        lines.append("")

        # ---- 植物阵型 ----
        lines.append("🌱 植物:")
        if state.plants:
            # 按行分组显示
            plants_by_row: dict[int, list[PlantInfo]] = {}
            for p in state.plants:
                plants_by_row.setdefault(p.row, []).append(p)

            for row in sorted(plants_by_row.keys()):
                row_plants = sorted(plants_by_row[row], key=lambda p: p.col)
                parts: list[str] = []
                for p in row_plants:
                    tags = ""
                    if p.is_sleeping:
                        tags += "💤"
                    if p.is_crushed:
                        tags += "💥"
                    # 只显示玩家能看到的损伤程度，不显示具体血量
                    if p.hp < p.hp_max and p.hp_max > 0:
                        ratio = p.hp_ratio
                        if ratio < 0.3:
                            tags += "🔴"   # 濒危
                        elif ratio < 0.6:
                            tags += "🟡"   # 受损
                        # 内存权威值：附加精确血量 (当前/最大)，帮助判断是否要铲/补
                        tags += f"[{int(p.hp)}/{int(p.hp_max)}]"
                    # 玉米炮状态
                    if p.state == 35:
                        tags += " 空"
                    elif p.state == 36:
                        tags += " 装"
                    elif p.state == 37:
                        tags += " ✅"
                    elif p.state == 38:
                        tags += " 发"
                    # 基座/外壳可种性标注（0.4.12）：屋顶空花盆、水池空荷叶是
                    # 唯一种植点——明示"可种"并把已占用的标出来，防止模型
                    # 绕开空基座或往已种过的基座里硬塞。
                    if p.plant_type in PLANT_POT_TYPES or p.plant_type == PLANT_PUMPKIN_TYPE:
                        inner = [q for q in state.plants
                                 if q.row == p.row and q.col == p.col
                                 and q.plant_type not in PLANT_POT_TYPES
                                 and q.plant_type != PLANT_PUMPKIN_TYPE]
                        if p.plant_type == PLANT_PUMPKIN_TYPE:
                            if not inner:
                                tags += "(空南瓜——可往里种)"
                        elif inner:
                            tags += f"(基座里已种{'、'.join(q.name for q in inner)}，满)"
                        else:
                            tags += "(空基座——优先往这里种)"
                    parts.append(f"({p.col}){p.name}{tags}")
                lines.append(f"  行{row}: {', '.join(parts)}")
        else:
            lines.append("  (无植物)")

        lines.append("")

        # ---- 僵尸情报 ----
        lines.append("🧟 僵尸:")
        if state.zombies:
            # 按行分组
            zombies_by_row: dict[int, list[ZombieInfo]] = {}
            for z in state.zombies:
                zombies_by_row.setdefault(z.row, []).append(z)

            for row in sorted(zombies_by_row.keys()):
                row_zombies = sorted(zombies_by_row[row], key=lambda z: -z.abscissa)
                parts: list[str] = []
                for z in row_zombies:
                    tags = ""
                    if z.is_eat:
                        tags += "啃"
                    if z.freeze_countdown > 0:
                        tags += "🧊"
                    elif z.slow_countdown > 0:
                        tags += "🐌"
                    if z.fixation_countdown > 0:
                        tags += "🧈"
                    if z.is_hammering:
                        tags += "🔨"

                    # 只显示玩家能看到的装备状态，不显示具体血量数字
                    accessories: list[str] = []
                    if z.two_hp > 0:
                        accessories.append("有门")
                    if z.one_hp > 0:
                        # 根据僵尸类型显示对应装备
                        if z.zombie_type in (2,):  # 路障
                            accessories.append("有帽")
                        elif z.zombie_type in (4,):  # 铁桶
                            accessories.append("有桶")
                        else:
                            accessories.append("有饰")

                    acc_str = f"({','.join(accessories)})" if accessories else ""
                    # 内存权威值：精确血量 本体/饰品/铁门，帮助判断威胁与是否值得用灰烬
                    _hp_parts = [str(int(z.hp))]
                    if z.one_hp > 0:
                        _hp_parts.append(f"饰{int(z.one_hp)}")
                    if z.two_hp > 0:
                        _hp_parts.append(f"门{int(z.two_hp)}")
                    hp_str = f"hp:{'/'.join(_hp_parts)}"
                    # 波次归属：非当前波的僵尸 = 提前/后续波，提示威胁时机
                    wave_str = f" 波{z.at_wave}" if z.at_wave and z.at_wave != state.wave else ""
                    # 速度：快僵尸（跑尸等）标记
                    speed_str = " ⚡快" if z.speed > 0.4 else ""
                    col_est = z.col_estimate
                    parts.append(f"{z.name}[列≈{col_est:.1f}]{hp_str}{acc_str}{wave_str}{speed_str}{tags}")
                lines.append(f"  行{row}: {', '.join(parts)}")
        else:
            lines.append("  (当前无僵尸)")

        lines.append("")

        # ---- 收集物 ----
        sun_items = [it for it in state.items if it.is_sun]
        if sun_items:
            lines.append(f"🌞 待收集阳光: {len(sun_items)}个")

        # ---- 场地物品 ----
        if state.grid_items:
            gi_parts = []
            for gi in state.grid_items:
                if gi.item_type == 3:  # 梯子
                    gi_parts.append(f"梯({gi.row},{gi.col})")
                elif gi.item_type == 1:  # 墓碑
                    gi_parts.append(f"碑({gi.row},{gi.col})")
                elif gi.item_type == 2:  # 弹坑
                    gi_parts.append(f"坑({gi.row},{gi.col})")
                elif gi.item_type == 11:  # 钉耙
                    gi_parts.append(f"耙({gi.row},{gi.col})")
                else:
                    gi_parts.append(f"{gi.name}({gi.row},{gi.col})")
            lines.append("📦 场地: " + ", ".join(gi_parts))

        # ---- 割草机 ----
        alive_mowers = [lm for lm in state.lawn_mowers if lm.is_alive]
        if alive_mowers:
            rows = [str(lm.row) for lm in alive_mowers]
            lines.append(f"🚜 割草机: 行{','.join(rows)}")

        return "\n".join(lines)

    def read_and_format(self) -> str:
        """一步完成: 读取状态 + 格式化文本."""
        state = self.read_state()
        return self.format_state(state)

    def _scan_guide_dir(self) -> list[str]:
        """扫描图鉴目录，返回所有 .md 文件的相对路径列表（去掉 .md 后缀）.

        支持多级目录，路径用 / 分隔，如 "植物/向日葵"、"僵尸/铁桶"。
        """
        if not self._guide_dir or not self._guide_dir.is_dir():
            return []
        results: list[str] = []
        for f in sorted(self._guide_dir.rglob("*.md")):
            rel = f.relative_to(self._guide_dir).with_suffix("")
            results.append(str(rel).replace("\\", "/"))
        return results
