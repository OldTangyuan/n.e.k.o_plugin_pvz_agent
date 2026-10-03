"""传送带关判定与行为单元测试（0.4.3 起逐步扩展，0.4.8 重构判定）.

背景：1-5 是冒险模式内的传送带关——实测 game_mode=0、Board+0x5550=5、
无阳光机制、卡槽被传送带喂满坚果。旧判定的两个坑：
- 卡槽非空恒判普通关 → 阳光自愈苟活，且 putplant 给用过的坚果开 30s 冷却
  （卡既不消失也不能再用，占着卡槽越种弹药越少）；
- 爆炸坚果（type=49，AsmVsZombies 枚举 48=模仿者、49=爆炸坚果）不在
  PLANT_NAMES，被防崩溃守卫拦截"无法种植"。

0.4.8 重构：判定改为 **GameMode/冒险关卡号权威白名单**——
- GameMode（PvzBase+0x7F8）∈ {17, 33, 35}：坚果保龄球1/2、僵王博士的小游戏
  （来源：PvZ 反编译 enum GameMode；锚点验证：僵王关实机读数 35 =
  GAMEMODE_CHALLENGE_FINAL_BOSS）；
- game_mode=0 且 adventure_level（Board+0x5550）∈ {5, 35, 50}：
  1-5 / 4-5 / 5-10。
旧"卡槽全空"瞬态启发式删除：传送带开局几十秒卡栏就被喂满，僵王关全程
被误判普通关的根因。

传送带行为统一语义（0.4.9 用户决策）：**免阳光、无冷却、零提示**——
僵王关实测虽有阳光经济，但种植走 PutPlant 直注不扣费，任何"阳光不足/
冷却中"提示都会误导模型干等；普通关闸门（冷却+阳光硬拦）保持不变。

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


def test_boss_minigame_is_conveyor() -> None:
    """僵王博士的复仇小游戏：实机 game_mode=35（=FINAL_BOSS）+ 卡栏已喂满 → 传送带关。

    这是 0.4.8 重构的直接动因：旧"卡槽全空"启发式在传送带喂满卡后恒误判
    普通关（实测卡槽=[32,20,32,14,20,33,33,39,...] 全程被判普通关）。
    """
    state = SimpleNamespace(
        game_mode=35,
        seeds=[_seed(32), _seed(20), _seed(14), _seed(33), _seed(39)],
    )
    assert level_is_conveyor(state) is True


def test_bowling_minigame_modes_are_conveyor() -> None:
    """小游戏版坚果保龄球 1/2（GameMode 17/33）判传送带。"""
    for mode in (17, 33):
        state = SimpleNamespace(game_mode=mode, seeds=[_seed(3)])
        assert level_is_conveyor(state) is True


def test_adventure_final_boss_5_10_is_conveyor() -> None:
    """冒险 5-10（最终 boss，adventure_level=50）也是传送带关。"""
    state = SimpleNamespace(game_mode=0, adventure_level=50, seeds=[_seed(33)])
    assert level_is_conveyor(state) is True


def test_non_belt_modes_stay_normal_even_with_empty_bar() -> None:
    """非传送带模式（生存/禅境/未知）即使卡栏全空也判普通关（旧瞬态启发式已删）。"""
    for mode in (1, 2, 4, 50, -1):
        state = SimpleNamespace(game_mode=mode, seeds=[])
        assert level_is_conveyor(state) is False
    # 有固定卡组的模式（如生存卡已选）同样普通关
    state = SimpleNamespace(game_mode=2, seeds=[_seed(3)])
    assert level_is_conveyor(state) is False


def test_bowling_exception_needs_adventure_mode() -> None:
    """只有冒险模式（mode=0）才走关卡号例外，其他模式维持原判定。"""
    state = SimpleNamespace(game_mode=2, adventure_level=5, seeds=[_seed(3)])
    assert level_is_conveyor(state) is False


def test_missing_adventure_level_defaults_normal() -> None:
    """adventure_level 缺失/读失败：保守判普通关，绝不误判传送带。"""
    state = SimpleNamespace(game_mode=0, seeds=[])
    assert level_is_conveyor(state) is False
    state = SimpleNamespace(game_mode=0, adventure_level=-1, seeds=[])
    assert level_is_conveyor(state) is False
    state = SimpleNamespace(game_mode=-1, seeds=[])
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


def test_boss_belt_plants_free_even_with_zero_sun(monkeypatch) -> None:
    """僵王类传送带（mode=35）统一免阳光：阳光=0 也放行，0 费用注入+消耗卡槽。

    0.4.8 回归教训：给僵王类保留"有阳光"档时，阳光=0 被硬拦
    （"需要 100 阳光，当前只有 0"），模型卡死——0.4.9 起统一免阳光。
    """
    ex = _bare_executor(planting_mode="putplant", supports_mouse=True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_occupied", lambda self, r, c: True)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    ex._mem.read_int.return_value = -1

    state = SimpleNamespace(
        seeds=[SeedInfo(index=0, plant_type=32, name="卷心菜投手", sun_cost=100,
                        cd=0, initial_cd=750, is_usable=True, imitator_type=-1)],
        sun=0, plants=[], game_clock=100, scene=0,
        game_mode=35, adventure_level=-1,
    )

    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, state, result)

    ex._injector.put_plant.assert_called_once_with(1, 2, 32, imitater=False, sun_cost=0)
    card_addr = 0x2000 + 0x28 + 0 * 0x50
    ex._injector.clear_seed_card.assert_called_once_with(card_addr)
    ex._injector.start_card_cooldown.assert_not_called()   # 传送带关一律不开冷却
    text = str(result)
    assert "阳光" not in text                              # 无任何阳光提示
    assert "warning" not in result


def test_boss_belt_ignores_stale_cooldown(monkeypatch) -> None:
    """传送带关统一无冷却语义：卡带陈旧 cd 读数也不拦、不提示（0.4.9）。"""
    ex = _bare_executor(planting_mode="putplant", supports_mouse=True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_occupied", lambda self, r, c: True)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    ex._mem.read_int.return_value = -1

    state = SimpleNamespace(
        seeds=[SeedInfo(index=0, plant_type=33, name="花盆", sun_cost=25,
                        cd=750, initial_cd=750, is_usable=False, imitator_type=-1)],
        sun=0, plants=[], game_clock=100, scene=0,
        game_mode=35, adventure_level=-1,
    )

    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, state, result)

    ex._injector.put_plant.assert_called_once_with(1, 2, 33, imitater=False, sun_cost=0)
    ex._injector.clear_seed_card.assert_called_once()
    text = str(result)
    assert "冷却" not in text                              # 无任何冷却提示


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
#  collect_belt：全部传送带关禁用（0.4.8）
# --------------------------------------------------------------------------- #

def test_collect_belt_refused_on_adventure_belt(monkeypatch) -> None:
    """1-5 类自动补卡传送带关：collect_belt 直接拒绝，一个鼠标点击都不发。"""
    ex = _bare_executor(planting_mode="putplant")
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)

    with pytest.raises(ValueError, match="已禁用"):
        ex._collect_belt({"count": 3}, _bowling_state(), {"action": "collect_belt"})
    ex._injector.mouse_click.assert_not_called()


def test_collect_belt_refused_on_boss_belt(monkeypatch) -> None:
    """僵王类传送带（mode=35）同样自动喂卡：collect_belt 拒绝（0.4.8）。"""
    ex = _bare_executor(planting_mode="putplant")
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: True)
    state = SimpleNamespace(game_mode=35, adventure_level=-1, seeds=[_seed(33)], sun=50)

    with pytest.raises(ValueError, match="已禁用"):
        ex._collect_belt({"count": 3}, state, {"action": "collect_belt"})
    ex._injector.mouse_click.assert_not_called()


def test_collect_belt_refused_on_non_belt(monkeypatch) -> None:
    """普通关：collect_belt 拒绝并给出正确出路（不误点卡片栏）。"""
    ex = _bare_executor(planting_mode="putplant")
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: False)
    state = SimpleNamespace(game_mode=2, adventure_level=-1, seeds=[_seed(3)], sun=100)

    with pytest.raises(ValueError, match="不是传送带关"):
        ex._collect_belt({"count": 3}, state, {"action": "collect_belt"})
    ex._injector.mouse_click.assert_not_called()


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


def test_format_state_boss_belt_unified_no_sun(monkeypatch) -> None:
    """僵王类传送带（mode=35）：统一免阳光文案——无真实阳光行、无☀费用、无冷却。"""
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
    # 卡片带陈旧 cd 与高额阳光费——传送带关都不得显示
    state = SimpleNamespace(
        game_clock=100, wave=1, total_wave=8, refresh_countdown=0,
        huge_wave_countdown=0, level_end_countdown=0, scene_name="白天",
        sun=0,
        seeds=[SeedInfo(index=0, plant_type=32, name="卷心菜投手", sun_cost=100,
                        cd=750, initial_cd=750, is_usable=False, imitator_type=-1)],
        plants=[], zombies=[], lawn_mowers=[],
        game_mode=35, adventure_level=-1, game_ui=3, in_battle=True, is_paused=False,
        scene=0, items=[], grid_items=[], _plantable_rows=None,
    )
    text = r.format_state(state)
    assert "传送带" in text
    assert "不计阳光" in text
    assert "阳光正常计费" not in text          # 0.4.8 的"有阳光"档文案已废
    assert "☀ 阳光: 0" not in text             # 不显示真实阳光数字
    assert "☀不足" not in text                 # 无阳光不足提示
    assert "⏳" not in text                    # 无冷却提示
    assert "(100☀)" not in text                # 不显示卡片阳光费用
    assert "[0] 卷心菜投手 ✅" in text
    assert "立刻用 collect_belt 收取" not in text   # 不引导收取（collect_belt 已禁用）


def test_format_state_bowling_minigame_belt_no_sun(monkeypatch) -> None:
    """小游戏坚果保龄球（mode=17）：无阳光传送带文案（不计阳光 + 自动补卡）。"""
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
        game_mode=17, adventure_level=-1, game_ui=3, in_battle=True, is_paused=False,
        scene=0, items=[], grid_items=[], _plantable_rows=None,
    )
    text = r.format_state(state)
    assert "不计阳光" in text
    assert "自动补卡" in text
    assert "立刻用 collect_belt 收取" not in text   # 不引导收取（collect_belt 已禁用）
