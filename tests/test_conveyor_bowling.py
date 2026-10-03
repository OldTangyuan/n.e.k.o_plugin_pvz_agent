"""1-5 坚果保龄球（冒险模式传送带关）单元测试（0.4.3，0.4.4-0.4.6 扩展）.

背景：1-5 是冒险模式内的传送带关——实测 game_mode=0、Board+0x5550=5、
无阳光机制、卡槽被传送带喂满坚果。旧判定的两个坑：
- 卡槽非空恒判普通关 → 阳光自愈苟活，且 putplant 给用过的坚果开 30s 冷却
  （卡既不消失也不能再用，占着卡槽越种弹药越少）；
- 爆炸坚果（type=49，AsmVsZombies 枚举 48=模仿者、49=爆炸坚果）不在
  PLANT_NAMES，被防崩溃守卫拦截"无法种植"。

修复：
- level_is_conveyor 增加冒险传送带例外（mode=0 且 adventure_level∈{5,35}，
  不看卡槽；5=1-5 实机验证，35=4-5 同系列推算）；
- PLANT_NAMES/PLANT_SUN_COST 补 49；
- 传送带关维持 putplant 直注（用户选定路线），直注调用后由
  _consume_seed_card 写 type=-1 模拟游戏原生"用后消失"——传送带按原生
  节奏向空槽补新坚果（实测存活内存里游戏自己消耗过的槽就是 -1）；
- collect_belt 在冒险传送带关禁用（0.4.6：扫描点与卡栏重叠，点击只会
  误拾栏内卡；1-5 传送带自动补卡，无需收取），状态文本同步不再引导。

只测纯逻辑，不依赖真实游戏进程；非 Windows 跳过。
"""

from __future__ import annotations

import sys
import unittest.mock as mock
from types import SimpleNamespace

import pytest

# 非 Windows 跳过（executor 模块顶部无条件 import ctypes.wintypes）。
# guard 必须在 pvz_memory import 之前；sys.platform 条件是 Ruff E402 允许的
# 前置语句（与 test_service.py 同模式）。pvz 导入路径由 tests/conftest.py 设置。
if sys.platform != "win32":
    pytest.skip(
        "PvZ Agent 插件测试依赖 Windows 运行时（executor 模块 import ctypes.wintypes）",
        allow_module_level=True,
    )

from pvz_memory import executor as pvz_executor  # noqa: E402
from pvz_memory.executor import PvZExecutor  # noqa: E402
from pvz_memory.injector import PvZCodeInjector  # noqa: E402
from pvz_memory.offsets import PLANT_NAMES, PLANT_SUN_COST, PvZOffsets  # noqa: E402
from pvz_memory.reader import GameState, SeedInfo, level_is_conveyor  # noqa: E402


def _seed(plant_type: int = 3) -> SeedInfo:
    return SeedInfo(
        index=0, plant_type=plant_type,
        name=PLANT_NAMES.get(plant_type, "坚果"), sun_cost=0,
        cd=0, initial_cd=0, is_usable=True, imitator_type=-1,
    )


# --------------------------------------------------------------------------- #
#  level_is_conveyor：1-5 例外 + 原有语义回归
# --------------------------------------------------------------------------- #

def test_bowling_is_conveyor_even_with_full_seed_bar() -> None:
    """1-5：mode=0 + adventure_level=5 + 卡槽被坚果喂满 → 传送带关。"""
    state = SimpleNamespace(
        game_mode=0, adventure_level=5,
        seeds=[_seed(3), _seed(49), _seed(3)],
    )
    assert level_is_conveyor(state) is True


def test_bowling_exception_requires_level_5() -> None:
    """其他冒险关（adventure_level 不在保龄球集合）不例外：卡槽非空仍判普通关。"""
    state = SimpleNamespace(game_mode=0, adventure_level=4, seeds=[_seed(3)])
    assert level_is_conveyor(state) is False
    state = SimpleNamespace(game_mode=0, adventure_level=15, seeds=[])
    assert level_is_conveyor(state) is False


def test_bowling2_4_5_also_conveyor() -> None:
    """4-5 坚果保龄球2（adventure_level=35，关卡号=(章-1)*10+关 推算）同判传送带。"""
    state = SimpleNamespace(game_mode=0, adventure_level=35, seeds=[_seed(3)])
    assert level_is_conveyor(state) is True


def test_bowling_exception_needs_adventure_mode() -> None:
    """只有冒险模式（mode=0）才走关卡号例外，其他模式维持原判定。"""
    state = SimpleNamespace(game_mode=2, adventure_level=5, seeds=[_seed(3)])
    assert level_is_conveyor(state) is False


def test_missing_adventure_level_keeps_old_semantics() -> None:
    """无 adventure_level 盖章（旧调用方/读失败）→ 原判定不变。"""
    state = SimpleNamespace(game_mode=0, seeds=[])
    assert level_is_conveyor(state) is False
    state = SimpleNamespace(game_mode=2, seeds=[])
    assert level_is_conveyor(state) is True
    state = SimpleNamespace(game_mode=2, seeds=[_seed(3)])
    assert level_is_conveyor(state) is False


def test_game_state_has_adventure_level_field() -> None:
    assert GameState().adventure_level == -1


# --------------------------------------------------------------------------- #
#  爆炸坚果（type=49）入册
# --------------------------------------------------------------------------- #

def test_explosive_nut_is_named_and_free() -> None:
    assert PLANT_NAMES[49] == "爆炸坚果"
    assert PLANT_SUN_COST[49] == 0


# --------------------------------------------------------------------------- #
#  executor：传送带关强制 MouseClick 原生路线
# --------------------------------------------------------------------------- #

def _bare_executor(planting_mode: str = "putplant", supports_mouse: bool = True):
    ex = PvZExecutor.__new__(PvZExecutor)
    ex._mem = mock.Mock(name="PvZMemory")
    ex._mem.offsets = PvZOffsets()
    ex._mem.main_object = 0x1000
    ex._mem.read_pointer.return_value = 0x2000  # seed_array
    ex._injector = mock.Mock(name="PvZCodeInjector")
    ex._injector.supports_mouse = supports_mouse
    ex._injector.grid_to_pixel.return_value = (100, 200)
    ex._planting_mode = planting_mode
    ex._direct_plants = 0
    ex._last_clock = -1
    ex._get_rect = lambda: (0, 0, 800, 600)
    return ex


def _bowling_state():
    return SimpleNamespace(
        seeds=[_seed(3), _seed(49)], sun=0, plants=[],
        game_clock=100, scene=0, game_mode=0, adventure_level=5,
    )


def test_conveyor_putplant_consumes_card(monkeypatch) -> None:
    """1-5 + putplant：直注成功后卡槽写 -1（游戏原生"用后消失"语义）。"""
    ex = _bare_executor(planting_mode="putplant", supports_mouse=True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_occupied", lambda self, r, c: True)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    ex._mem.read_int.return_value = -1  # 消耗回读: sc_type = -1

    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, _bowling_state(), result)

    ex._injector.put_plant.assert_called_once_with(1, 2, 3, imitater=False, sun_cost=0)
    card_addr = 0x2000 + 0x28 + 0 * 0x50
    ex._injector.clear_seed_card.assert_called_once_with(card_addr)
    ex._injector.start_card_cooldown.assert_not_called()  # 传送带关不开冷却
    ex._injector.mouse_click.assert_not_called()          # 不走 MouseClick（用户选定 putplant）
    assert "卡牌已消耗" in result["detail"]


def test_conveyor_putplant_explosive_nut_passes_guard(monkeypatch) -> None:
    """爆炸坚果（type=49）已入册：防崩溃守卫放行 + 照常消耗卡槽。"""
    ex = _bare_executor(planting_mode="putplant", supports_mouse=False)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_occupied", lambda self, r, c: True)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    ex._mem.read_int.return_value = -1

    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 1, "row": 1, "col": 2}, _bowling_state(), result)

    ex._injector.put_plant.assert_called_once_with(1, 2, 49, imitater=False, sun_cost=0)
    card_addr = 0x2000 + 0x28 + 1 * 0x50
    ex._injector.clear_seed_card.assert_called_once_with(card_addr)


def test_conveyor_consume_survives_seed_array_unavailable(monkeypatch) -> None:
    """卡槽数组读不到：消耗跳过（只记日志），种植结果不受影响。"""
    ex = _bare_executor(planting_mode="putplant", supports_mouse=False)
    ex._mem.read_pointer.return_value = 0
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_occupied", lambda self, r, c: True)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)

    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, _bowling_state(), result)

    ex._injector.clear_seed_card.assert_not_called()
    ex._injector.put_plant.assert_called_once()


def test_conveyor_consume_even_when_nut_rolled_away(monkeypatch) -> None:
    """坚果落地即滚/爆：占位验证 False 也要消耗卡槽（0.4.5 实测漏消耗教训）。"""
    ex = _bare_executor(planting_mode="putplant", supports_mouse=True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_occupied", lambda self, r, c: False)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    ex._mem.read_int.return_value = -1

    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, _bowling_state(), result)

    ex._injector.put_plant.assert_called_once()
    card_addr = 0x2000 + 0x28 + 0 * 0x50
    ex._injector.clear_seed_card.assert_called_once_with(card_addr)
    assert "warning" in result          # 占位未确认的提示保留
    assert "卡牌已消耗" not in result.get("detail", "")


def test_normal_level_putplant_unchanged(monkeypatch) -> None:
    """普通关 + putplant 配置：仍走 PutPlant + 原生冷却，不消耗卡槽（回归）。"""
    ex = _bare_executor(planting_mode="putplant", supports_mouse=True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: False)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_occupied", lambda self, r, c: True)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    ex._mem.read_int.side_effect = [0, 750]
    ex._mem.read_bool.return_value = True
    state = SimpleNamespace(
        seeds=[SeedInfo(index=0, plant_type=0, name="豌豆射手", sun_cost=100,
                        cd=0, initial_cd=750, is_usable=True, imitator_type=-1)],
        sun=9999, plants=[], game_clock=100, scene=0,
    )

    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, state, result)

    ex._injector.put_plant.assert_called_once()
    ex._injector.start_card_cooldown.assert_called_once()
    ex._injector.clear_seed_card.assert_not_called()


# --------------------------------------------------------------------------- #
#  injector.clear_seed_card：写入序列
# --------------------------------------------------------------------------- #

def test_clear_seed_card_writes_minus_one() -> None:
    inj = PvZCodeInjector.__new__(PvZCodeInjector)
    writes: list[tuple[int, bytes]] = []
    inj._write_bytes = lambda addr, data: writes.append((addr, bytes(data)))  # type: ignore[method-assign]
    inj.clear_seed_card(0x123450)
    assert writes == [(0x123450 + 0x34, (-1 & 0xFFFFFFFF).to_bytes(4, "little"))]


# --------------------------------------------------------------------------- #
#  collect_belt：冒险传送带关禁用（0.4.6）+ 假名修复
# --------------------------------------------------------------------------- #

def test_collect_belt_refused_on_adventure_belt(monkeypatch) -> None:
    """1-5 类自动补卡传送带关：collect_belt 直接拒绝，一个鼠标点击都不发。"""
    ex = _bare_executor(planting_mode="putplant")
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)

    with pytest.raises(ValueError, match="已禁用"):
        ex._collect_belt({"count": 3}, _bowling_state(), {"action": "collect_belt"})
    ex._injector.mouse_click.assert_not_called()


def test_collect_belt_still_allowed_on_minigame_belt(monkeypatch) -> None:
    """非冒险传送带（小游戏类，mode=2）：collect_belt 照常可用（回归）。"""
    ex = _bare_executor(planting_mode="putplant")
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cancel_cursor", lambda self: None)
    # 第一次读数 2 张，点击扫描点一次后变 3 张 → 模拟成功收取
    counts = iter([2, 3])
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_valid_seed_count",
                        lambda self: next(counts, 3))
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_mem_read_valid_seeds",
                        lambda self: [(1, 3)])
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    state = SimpleNamespace(game_mode=2, adventure_level=-1, seeds=[_seed(3)])

    result: dict = {"action": "collect_belt"}
    ex._collect_belt({"count": 1}, state, result)

    assert "坚果" in result["detail"]        # 按植物类型取名（槽 1 旧 bug 会报"向日葵"）
    assert "向日葵" not in result["detail"]


def test_format_state_bowling_empty_bar_without_collect_hint(monkeypatch) -> None:
    """冒险传送带关空卡栏：提示自动补卡，不再引导 collect_belt。"""
    from pvz_memory.reader import PvZStateReader

    class _StubMem:
        offsets = PvZOffsets()
        main_object = 0x1000

        def read_int(self, addr: int) -> int:
            return 0

        def read_bool(self, addr: int) -> bool:
            return False

        def read_pointer(self, addr: int) -> int:
            return 0

    r = PvZStateReader.__new__(PvZStateReader)
    r._mem = _StubMem()
    r._guide_dir = None
    state = SimpleNamespace(
        game_clock=100, wave=1, total_wave=8, refresh_countdown=0,
        huge_wave_countdown=0, level_end_countdown=0, scene_name="白天",
        sun=0, seeds=[], plants=[], zombies=[], lawn_mowers=[],
        game_mode=0, adventure_level=5, game_ui=3, in_battle=True, is_paused=False, scene=0, items=[], grid_items=[], _plantable_rows=None, _is_conveyor=True,
    )
    text = r.format_state(state)
    assert "自动补卡" in text
    assert "立刻用 collect_belt 收取" not in text
    assert "全部空槽时本轮 wait" in text


def test_format_state_minigame_belt_keeps_collect_hint(monkeypatch) -> None:
    """非冒险传送带（小游戏类）：保留 collect_belt 引导（回归）。"""
    from pvz_memory.reader import PvZStateReader

    class _StubMem:
        offsets = PvZOffsets()
        main_object = 0x1000

        def read_int(self, addr: int) -> int:
            return 0

        def read_bool(self, addr: int) -> bool:
            return False

        def read_pointer(self, addr: int) -> int:
            return 0

    r = PvZStateReader.__new__(PvZStateReader)
    r._mem = _StubMem()
    r._guide_dir = None
    state = SimpleNamespace(
        game_clock=100, wave=1, total_wave=8, refresh_countdown=0,
        huge_wave_countdown=0, level_end_countdown=0, scene_name="白天",
        sun=0, seeds=[], plants=[], zombies=[], lawn_mowers=[],
        game_mode=2, adventure_level=-1, game_ui=3, in_battle=True, is_paused=False, scene=0, items=[], grid_items=[], _plantable_rows=None, _is_conveyor=True,
    )
    text = r.format_state(state)
    assert "立刻用 collect_belt 收取" in text
