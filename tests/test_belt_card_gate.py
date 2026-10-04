"""传送带关"已送达卡"实时复核闸门测试（0.4.12）.

背景：0.4.9 传送带关统一免阳光/无冷却时跳过了全部闸门，而 place_plant
拿到的 state 是**轮首快照**——同轮连续种植会把卡消耗掉（sc_type→-1）
而快照不更新；卡槽被传送带刷新后快照也不更新。模型照快照引用旧卡序号
就会"预支"传送带还没送到的植物（用户实测僵王关种出未交付的卡）。

0.4.12：传送带关种植前实时复核卡槽（_read_card_live_type）——
-1 = 确认空槽/刚消耗 → 拒绝；-2 = 读失败/越界 → 拒绝（宁可拦下不凭空直注）；
与快照不一致 → 拒绝并提示按最新卡片列表重选；一致 → 放行。
普通关不走该闸门（冷却/阳光/占用闸门照旧）。

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
from pvz_memory.offsets import PvZOffsets  # noqa: E402
from pvz_memory.reader import PvZStateReader, SeedInfo  # noqa: E402


def _seed(plant_type: int, index: int = 0) -> SeedInfo:
    name = {32: "卷心菜投手", 20: "火爆辣椒", 33: "花盆"}.get(
        plant_type, f"未知({plant_type})")
    return SeedInfo(index=index, plant_type=plant_type, name=name, sun_cost=0,
                    cd=0, initial_cd=750, is_usable=True, imitator_type=-1)


def _belt_state() -> SimpleNamespace:
    return SimpleNamespace(
        seeds=[_seed(32)], sun=0, plants=[],
        game_clock=100, scene=0, game_mode=35, adventure_level=-1,
    )


def _bare_executor() -> PvZExecutor:
    ex = PvZExecutor.__new__(PvZExecutor)
    ex._mem = mock.Mock(name="PvZMemory")
    ex._mem.offsets = PvZOffsets()
    ex._mem.main_object = 0x1000
    ex._mem.read_pointer.return_value = 0x2000
    ex._injector = mock.Mock(name="PvZCodeInjector")
    ex._injector.supports_mouse = False
    ex._planting_mode = "putplant"
    ex._direct_plants = 0
    ex._last_clock = -1
    ex._get_rect = lambda: (0, 0, 800, 600)
    return ex


def _allow_belt(monkeypatch, live_type: int) -> None:
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_has_plant_type",
                        lambda self, r, c, t: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_start_game_cooldown",
                        lambda self, s, i, p: None)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_read_card_live_type",
                        lambda self, i: live_type)


def test_belt_rejects_consumed_card(monkeypatch) -> None:
    """同轮连续种植：卡已被上一发消耗（实时读 = -1）→ 拒绝重复种植。"""
    ex = _bare_executor()
    _allow_belt(monkeypatch, -1)
    with pytest.raises(ValueError, match="刚被用掉"):
        ex._place_plant({"card_index": 0, "row": 1, "col": 2}, _belt_state(),
                        {"action": "place_plant"})
    ex._injector.put_plant.assert_not_called()


def test_belt_rejects_when_card_refreshed(monkeypatch) -> None:
    """卡槽被传送带刷新成别的植物（实时 20 ≠ 快照 32）→ 拒绝并提示重选。"""
    ex = _bare_executor()
    _allow_belt(monkeypatch, 20)
    with pytest.raises(ValueError, match="已被传送带刷新"):
        ex._place_plant({"card_index": 0, "row": 1, "col": 2}, _belt_state(),
                        {"action": "place_plant"})
    ex._injector.put_plant.assert_not_called()


def test_belt_rejects_when_status_unreadable(monkeypatch) -> None:
    """实时读失败（-2 无法确认）→ 拒绝，绝不凭空直注。"""
    ex = _bare_executor()
    _allow_belt(monkeypatch, -2)
    with pytest.raises(ValueError, match="无法实时读取"):
        ex._place_plant({"card_index": 0, "row": 1, "col": 2}, _belt_state(),
                        {"action": "place_plant"})
    ex._injector.put_plant.assert_not_called()


def test_belt_allows_when_live_matches(monkeypatch) -> None:
    """实时卡槽 = 快照卡 → 放行（0 费用直注 + 消耗卡槽）。"""
    ex = _bare_executor()
    _allow_belt(monkeypatch, 32)
    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, _belt_state(), result)
    ex._injector.put_plant.assert_called_once_with(1, 2, 32, imitater=False, sun_cost=0)
    ex._injector.clear_seed_card.assert_called_once()
    assert "阳光" not in str(result)          # 免阳光零提示语义不变


def test_normal_level_skips_live_gate(monkeypatch) -> None:
    """普通关不走实时卡槽闸门（冷却/阳光/占用闸门照旧）。"""
    ex = _bare_executor()
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: False)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_has_plant_type",
                        lambda self, r, c, t: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_start_game_cooldown",
                        lambda self, s, i, p: None)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_read_card_live_type",
                        lambda self, i: -2)   # 若被误用会拒绝 → 测试失败
    state = _belt_state()
    state.game_mode = 0
    state.sun = 9999
    state.seeds = [SeedInfo(index=0, plant_type=0, name="豌豆射手", sun_cost=100,
                            cd=0, initial_cd=750, is_usable=True, imitator_type=-1)]
    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, state, result)
    ex._injector.put_plant.assert_called_once_with(1, 2, 0, imitater=False, sun_cost=100)


def test_empty_slot_guard_precedes_live_check(monkeypatch) -> None:
    """空槽守卫（快照 plant_type=-1）先行，实时复核都不用发生。"""
    ex = _bare_executor()
    live_probe = mock.Mock(return_value=32)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_read_card_live_type", live_probe)
    state = _belt_state()
    state.seeds = [_seed(-1)]
    with pytest.raises(ValueError, match="空槽位"):
        ex._place_plant({"card_index": 0, "row": 1, "col": 2}, state,
                        {"action": "place_plant"})
    live_probe.assert_not_called()
    ex._injector.put_plant.assert_not_called()


# --------------------------------------------------------------------------- #
#  format_state：空槽明示 + 基座/南瓜可种性标注
# --------------------------------------------------------------------------- #

def _stub_reader():
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
    return r


def _fmt_state(seeds, plants) -> SimpleNamespace:
    return SimpleNamespace(
        game_clock=100, wave=1, total_wave=8, refresh_countdown=0,
        huge_wave_countdown=0, level_end_countdown=0, scene_name="白天",
        sun=0, seeds=seeds, plants=plants, zombies=[], lawn_mowers=[],
        game_mode=35, adventure_level=-1, game_ui=3, in_battle=True,
        is_paused=False, scene=0, items=[], grid_items=[],
        _plantable_rows=None,
    )


def _plant(row, col, plant_type, name):
    return SimpleNamespace(row=row, col=col, plant_type=plant_type, name=name,
                           is_sleeping=False, is_crushed=False,
                           hp=300, hp_max=300, state=0)


def test_format_state_belt_lists_empty_slots() -> None:
    """空槽明示：模型能无歧义区分可种 index 与未送卡 index。"""
    seeds = [_seed(32),
             SeedInfo(index=1, plant_type=-1, name="未知(-1)", sun_cost=0,
                      cd=0, initial_cd=0, is_usable=False, imitator_type=-1),
             SeedInfo(index=2, plant_type=-1, name="未知(-1)", sun_cost=0,
                      cd=0, initial_cd=0, is_usable=False, imitator_type=-1)]
    text = _stub_reader().format_state(_fmt_state(seeds, []))
    assert "空槽 [1 ,2]" in text
    assert "传送带还没把卡送到" in text


def test_format_state_annotates_empty_pot() -> None:
    """空花盆标注"空基座——优先往这里种"。"""
    plants = [_plant(1, 2, 33, "花盆")]
    text = _stub_reader().format_state(_fmt_state([], plants))
    assert "空基座——优先往这里种" in text


def test_format_state_annotates_filled_pot() -> None:
    """花盆里已有植物：标注"满"，模型不再往里塞。"""
    plants = [_plant(1, 2, 33, "花盆"), _plant(1, 2, 32, "卷心菜投手")]
    text = _stub_reader().format_state(_fmt_state([], plants))
    assert "基座里已种卷心菜投手" in text
    assert "空基座" not in text


def test_format_state_annotates_empty_pumpkin() -> None:
    """只有空南瓜的格子标注"可往里种"。"""
    plants = [_plant(0, 3, 30, "南瓜头")]
    text = _stub_reader().format_state(_fmt_state([], plants))
    assert "空南瓜——可往里种" in text
