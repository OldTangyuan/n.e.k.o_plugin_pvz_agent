"""植物叠种（基座/外壳槽位）规则单元测试（0.4.10）.

背景：0.4.9 及以前，占用判定"见植物就拦"——模型在屋顶关往**花盆**里种
卷心菜投手被误报"不能叠种"，屋顶/水池关根本没法玩。0.4.10 按游戏原生
"一格三槽位"语义重写：

- 基座槽：花盆(33)/荷叶(16)——最多 1 个，只能落在完全空格；
- 植物槽：普通植物——最多 1 株，花盆/荷叶上、空南瓜里都可以种；
- 外壳槽：南瓜壳(30)——最多 1 个，可以围在已有植物/基座外面。

对应槽位被占才拦截；普通关与传送带关同规则。升级植物走独立分支
（必须点在基础植物上），不受影响。

只测纯逻辑，不依赖真实游戏进程；非 Windows 跳过。
"""

from __future__ import annotations

import sys
import unittest.mock as mock
from types import SimpleNamespace

import pytest

if sys.platform != "win32":
    pytest.skip(
        "PvZ Agent 插件测试依赖 Windows 运行时（executor 模块 import ctypes.wintypes）",
        allow_module_level=True,
    )

from pvz_memory import executor as pvz_executor  # noqa: E402
from pvz_memory.executor import PvZExecutor  # noqa: E402
from pvz_memory.offsets import PLANT_NAMES, PvZOffsets  # noqa: E402
from pvz_memory.reader import SeedInfo  # noqa: E402


def _seed(plant_type: int) -> SeedInfo:
    return SeedInfo(
        index=0, plant_type=plant_type,
        name=PLANT_NAMES.get(plant_type, str(plant_type)), sun_cost=0,
        cd=0, initial_cd=750, is_usable=True, imitator_type=-1,
    )


def _plant(row: int, col: int, plant_type: int) -> SimpleNamespace:
    return SimpleNamespace(
        row=row, col=col, plant_type=plant_type,
        name=PLANT_NAMES.get(plant_type, str(plant_type)),
    )


def _state(plants: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        seeds=[_seed(32)], sun=9999, plants=plants,
        game_clock=100, scene=0, game_mode=0, adventure_level=1,
    )


def _bare_executor() -> PvZExecutor:
    ex = PvZExecutor.__new__(PvZExecutor)
    ex._mem = mock.Mock(name="PvZMemory")
    ex._mem.offsets = PvZOffsets()
    ex._mem.main_object = 0x1000
    ex._injector = mock.Mock(name="PvZCodeInjector")
    ex._injector.supports_mouse = True
    ex._planting_mode = "putplant"
    ex._direct_plants = 0
    ex._last_clock = -1
    ex._get_rect = lambda: (0, 0, 800, 600)
    return ex


def _allow(monkeypatch) -> None:
    """放行所有外部副作用：判定为普通关、种后验证成功、冷却写入为空操作。"""
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: False)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_has_plant_type",
                        lambda self, r, c, t: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_start_game_cooldown",
                        lambda self, s, i, p: None)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)


ROW, COL = 1, 2

PEASHOOTER, SUNFLOWER, POT, PUMPKIN, CABBAGE = 0, 1, 33, 30, 32


# --------------------------------------------------------------------------- #
#  允许：基座叠种 / 南瓜包围 / 空南瓜内种植
# --------------------------------------------------------------------------- #

def test_plant_on_empty_flower_pot_allowed(monkeypatch) -> None:
    """屋顶核心场景：空花盆上叠种卷心菜投手 → 放行。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": ROW, "col": COL},
                    _state([_plant(ROW, COL, POT)]), result)
    ex._injector.put_plant.assert_called_once_with(ROW, COL, CABBAGE,
                                                   imitater=False, sun_cost=0)


def test_plant_on_empty_lily_pad_allowed(monkeypatch) -> None:
    """水池核心场景：空荷叶上叠种豌豆射手 → 放行。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    state = _state([_plant(ROW, COL, 16)])
    state.seeds = [_seed(PEASHOOTER)]
    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": ROW, "col": COL}, state, result)
    ex._injector.put_plant.assert_called_once_with(ROW, COL, PEASHOOTER,
                                                   imitater=False, sun_cost=0)


def test_pumpkin_wraps_existing_plant(monkeypatch) -> None:
    """南瓜壳套在已有植物外面 → 放行（先种植物再围南瓜是正常玩法）。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    state = _state([_plant(ROW, COL, PEASHOOTER)])
    state.seeds = [_seed(PUMPKIN)]
    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": ROW, "col": COL}, state, result)
    ex._injector.put_plant.assert_called_once_with(ROW, COL, PUMPKIN,
                                                   imitater=False, sun_cost=0)


def test_plant_inside_empty_pumpkin_allowed(monkeypatch) -> None:
    """只有空南瓜的格子：普通植物种进去 → 放行。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": ROW, "col": COL},
                    _state([_plant(ROW, COL, PUMPKIN)]), result)
    ex._injector.put_plant.assert_called_once_with(ROW, COL, CABBAGE,
                                                   imitater=False, sun_cost=0)


def test_pumpkin_wraps_planted_pot(monkeypatch) -> None:
    """南瓜壳围在"花盆+植物"外 → 放行（三槽位刚好排满）。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    state = _state([_plant(ROW, COL, POT), _plant(ROW, COL, CABBAGE)])
    state.seeds = [_seed(PUMPKIN)]
    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": ROW, "col": COL}, state, result)
    ex._injector.put_plant.assert_called_once()


# --------------------------------------------------------------------------- #
#  拦截：槽位被占
# --------------------------------------------------------------------------- #

def test_plant_on_occupied_flower_pot_rejected(monkeypatch) -> None:
    """花盆里已种了植物：再种 → 拦（一个基座只能种一株）。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    state = _state([_plant(ROW, COL, POT), _plant(ROW, COL, CABBAGE)])
    with pytest.raises(ValueError, match="一个基座只能种一株"):
        ex._place_plant({"card_index": 0, "row": ROW, "col": COL}, state,
                        {"action": "place_plant"})
    ex._injector.put_plant.assert_not_called()


def test_pot_on_pot_rejected(monkeypatch) -> None:
    """花盆上再放花盆 → 拦（一格一个基座）。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    state = _state([_plant(ROW, COL, POT)])
    state.seeds = [_seed(POT)]
    with pytest.raises(ValueError, match="只能种在空格上"):
        ex._place_plant({"card_index": 0, "row": ROW, "col": COL}, state,
                        {"action": "place_plant"})
    ex._injector.put_plant.assert_not_called()


def test_pot_on_existing_plant_rejected(monkeypatch) -> None:
    """植物下面垫花盆 → 拦（基座只能落空格）。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    state = _state([_plant(ROW, COL, PEASHOOTER)])
    state.seeds = [_seed(POT)]
    with pytest.raises(ValueError, match="只能种在空格上"):
        ex._place_plant({"card_index": 0, "row": ROW, "col": COL}, state,
                        {"action": "place_plant"})
    ex._injector.put_plant.assert_not_called()


def test_double_pumpkin_rejected(monkeypatch) -> None:
    """已有南瓜的格子再围南瓜 → 拦（一格一个外壳）。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    state = _state([_plant(ROW, COL, PUMPKIN), _plant(ROW, COL, PEASHOOTER)])
    state.seeds = [_seed(PUMPKIN)]
    with pytest.raises(ValueError, match="一格只能围一个南瓜壳"):
        ex._place_plant({"card_index": 0, "row": ROW, "col": COL}, state,
                        {"action": "place_plant"})
    ex._injector.put_plant.assert_not_called()


def test_normal_on_normal_rejected(monkeypatch) -> None:
    """普通关回归：格里有植物再种同类 → 拦（原有语义保持）。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    state = _state([_plant(ROW, COL, SUNFLOWER)])
    state.seeds = [_seed(SUNFLOWER)]
    with pytest.raises(ValueError, match="不能叠种"):
        ex._place_plant({"card_index": 0, "row": ROW, "col": COL}, state,
                        {"action": "place_plant"})
    ex._injector.put_plant.assert_not_called()


def test_full_cell_rejects_everything(monkeypatch) -> None:
    """花盆+植物+南瓜 三槽全满：再种任何东西都拦。"""
    ex = _bare_executor()
    _allow(monkeypatch)
    full = [_plant(ROW, COL, POT), _plant(ROW, COL, CABBAGE), _plant(ROW, COL, PUMPKIN)]
    for seed_type in (PEASHOOTER, POT, PUMPKIN):
        state = _state(full)
        state.seeds = [_seed(seed_type)]
        with pytest.raises(ValueError):
            ex._place_plant({"card_index": 0, "row": ROW, "col": COL}, state,
                            {"action": "place_plant"})
    ex._injector.put_plant.assert_not_called()
