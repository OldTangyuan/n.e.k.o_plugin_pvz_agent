"""PvZ 内存读取 + 注入 + 动作执行模块.

通过 ReadProcessMemory 读取 PvZ 进程内存, 获取完整游戏状态
(阳光/卡片/植物/僵尸/收集物/场地物品/割草机), 格式化为 LLM
可理解的结构化文本; 通过代码注入直接调用游戏内部函数实现
种植/铲除/选卡/自动收集阳光等动作.

本模块是从 LLM_PvZ_Player 项目独立抽取的:
- 零第三方运行时依赖 (仅标准库 ctypes/struct/logging)
- 公共 API 与原项目 vlm_game_agent.pvz 完全兼容
- 读逻辑支持多版本; 代码注入仅支持原版 V1_0_0_1051_EN

架构:
    PvZMemory      → 底层内存读取 (ctypes ReadProcessMemory)
    PvZOffsets     → 版本偏移量表
    PvZStateReader → 高层游戏状态读取 + 文本格式化
    PvZCodeInjector → 代码注入 (hack 开关 + 调用游戏内部函数)
    PvZExecutor    → 高层动作映射 (种植/铲除/选卡)

用法::

    from pvz_memory import PvZMemory, PvZStateReader, PvZExecutor, setup_logging

    setup_logging()

    mem = PvZMemory()
    if mem.connect():
        reader = PvZStateReader(mem)
        game_text = reader.read_and_format()   # 游戏状态文本
        executor = PvZExecutor(mem)            # 动作执行 (注入为主)
        result = executor.execute("place_plant", {"card_index": 0, "row": 0, "col": 3}, reader.read_state())
        executor.close()
        mem.disconnect()
"""

from __future__ import annotations

import logging

from .executor import PvZExecutor
from .injector import (
    HACK_AUTO_COLLECTED,
    HACK_BLOCK_MAIN_LOOP,
    HACK_PLACED_ANYWHERE,
    HACK_UNLOCK_SUN_LIMIT,
    SUPPORTED_INJECT_VERSIONS,
    HackInfo,
    PvZCodeInjector,
    get_inject_addresses,
)
from .memory import PvZMemory, PvZMemoryError
from .offsets import (
    ITEM_NAMES,
    PLACE_ITEM_NAMES,
    PLANT_NAMES,
    PLANT_SUN_COST,
    PVZ_BASE_ADDRESS,
    ZOMBIE_NAMES,
    GameUI,
    PvZOffsets,
    PvZVersion,
    SceneType,
    detect_version_from_timestamp,
    get_all_known_timestamps,
    get_offsets,
)
from .reader import (
    GameState,
    GridItemInfo,
    ItemInfo,
    LawnMowerInfo,
    PlantInfo,
    PvZStateReader,
    SeedInfo,
    ZombieInfo,
)

__all__ = [
    # 核心
    "PvZMemory",
    "PvZMemoryError",
    "PvZStateReader",
    "GameState",
    # 偏移
    "PvZOffsets",
    "PvZVersion",
    "detect_version_from_timestamp",
    "get_offsets",
    "get_all_known_timestamps",
    # 枚举
    "GameUI",
    "SceneType",
    # 名称映射
    "PLANT_NAMES",
    "ZOMBIE_NAMES",
    "ITEM_NAMES",
    "PLACE_ITEM_NAMES",
    "PLANT_SUN_COST",
    # 常量
    "PVZ_BASE_ADDRESS",
    # 数据类
    "SeedInfo",
    "PlantInfo",
    "ZombieInfo",
    "ItemInfo",
    "GridItemInfo",
    "LawnMowerInfo",
    # 注入器
    "PvZCodeInjector",
    "HackInfo",
    "HACK_BLOCK_MAIN_LOOP",
    "HACK_AUTO_COLLECTED",
    "HACK_UNLOCK_SUN_LIMIT",
    "HACK_PLACED_ANYWHERE",
    "SUPPORTED_INJECT_VERSIONS",
    "get_inject_addresses",
    # 执行器
    "PvZExecutor",
    # 日志辅助
    "setup_logging",
]


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """为 pvz_memory 包配置一次性控制台 handler. 幂等.

    不在 import 时自动调用 (库不污染根 logger); 未配置时 logging 自带
    lastResort handler 保证 WARNING 及以上级别可见.

    Args:
        level: 日志级别, 默认 INFO.

    Returns:
        pvz_memory 根 logger (子模块 logger 会传播到它).
    """
    logger = logging.getLogger("pvz_memory")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False  # 避免向上冒泡到 root 重复打印
    return logger
