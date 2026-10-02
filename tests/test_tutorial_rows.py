"""教学关草皮行单元测试（0.4.2）.

背景：教学关只在草皮行配割草机（三行草皮关实测割草机行=[1,2,3]，单行关=[2]，
均为全草坪坐标），而旧实现 a) 割草机行按数组下标猜（row=i）、b) 【棋盘】行数
取割草机数组长度。结果三行草皮关被告知"3 行 (row 0~2)"，模型把草皮行当
row 0~2 全种到草皮上方一行，草皮末行（僵尸实际所在行）无人防守。

修复（本文件覆盖）：
- reader 读割草机对象内真实行号（lm_row=0x14，越界/非递增退回下标），
  已发射的割草机保留（行号仍有效，草皮行推导关末稳定）；
- reader 推导草皮行盖章 state._plantable_rows（普通关=连续从 0 → None）；
- executor 把种到非草皮行的请求重定向到最近草皮行（占用/升级检查之前）；
- 【棋盘】行在教学关回退全草坪行数并明示草皮行。

只测纯逻辑，不依赖真实游戏进程；非 Windows 跳过（executor import ctypes.wintypes）。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest.mock as mock
from pathlib import Path
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
from pvz_memory.offsets import PvZOffsets  # noqa: E402
from pvz_memory.reader import LawnMowerInfo, PvZStateReader, SeedInfo  # noqa: E402

# memory_engine.py 按文件路径加载（不经包导入，避免 plugin.sdk 依赖）；
# 兼容仓库布局与宿主安装布局——两种布局下 pvz/ 都是 tests/ 的祖父/父目录。
_pvz_dir = Path(__file__).resolve().parent.parent / "pvz"
_ENGINE_PATH = _pvz_dir / "pvz_agent" / "memory_engine.py"
_spec = importlib.util.spec_from_file_location("memory_engine_under_test", _ENGINE_PATH)
_memory_engine = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("memory_engine_under_test", _memory_engine)
_spec.loader.exec_module(_memory_engine)
MemoryGameEngine = _memory_engine.MemoryGameEngine


# --------------------------------------------------------------------------- #
#  构造工具
# --------------------------------------------------------------------------- #

class _FakeMem:
    """最小内存桩：按 reader 的地址算术预填割草机数组。"""

    def __init__(self, mower_rows: list[int], alive: list[bool] | None = None):
        self.offsets = PvZOffsets()
        self.main_object = 0x1000
        off = self.offsets
        self._ints: dict[int, int] = {}
        self._bools: dict[int, bool] = {}
        self._ptrs: dict[int, int] = {}
        base = 0x2000
        self._ints[self.main_object + off.lawn_mower_count_max] = len(mower_rows)
        self._ptrs[self.main_object + off.lawn_mower_array] = base
        for i, r in enumerate(mower_rows):
            a = base + i * off.lawn_mower_struct_size
            self._ints[a + off.lm_row] = r
            self._bools[a + off.lm_dead] = bool(alive) and not alive[i]

    def read_pointer(self, addr: int) -> int:
        return self._ptrs.get(addr, 0)

    def read_int(self, addr: int) -> int:
        return self._ints.get(addr, 0)

    def read_bool(self, addr: int) -> bool:
        return self._bools.get(addr, False)


def _bare_reader(mem: _FakeMem) -> PvZStateReader:
    r = PvZStateReader.__new__(PvZStateReader)
    r._mem = mem
    r._guide_dir = None
    return r


def _bare_executor(planting_mode: str = "putplant") -> PvZExecutor:
    ex = PvZExecutor.__new__(PvZExecutor)
    ex._mem = mock.Mock(name="PvZMemory")
    ex._mem.offsets = PvZOffsets()
    ex._mem.main_object = 0x1000
    ex._mem.read_pointer.return_value = 0x2000  # seed_array
    ex._injector = mock.Mock(name="PvZCodeInjector")
    ex._planting_mode = planting_mode
    ex._direct_plants = 0
    ex._last_clock = -1
    ex._get_rect = lambda: (0, 0, 800, 600)
    return ex


def _seed(index: int = 0, plant_type: int = 0) -> SeedInfo:
    return SeedInfo(
        index=index,
        plant_type=plant_type,
        name="豌豆射手",
        sun_cost=100,
        cd=0,
        initial_cd=750,
        is_usable=True,
        imitator_type=-1,
    )


def _mower(row: int, alive: bool = True) -> LawnMowerInfo:
    return LawnMowerInfo(index=0, row=row, is_alive=alive)


# --------------------------------------------------------------------------- #
#  reader：割草机真实行号（lm_row=0x14，0.4.2 实测标定）
# --------------------------------------------------------------------------- #

def test_mower_rows_read_from_object_not_index() -> None:
    """教学关三行草皮：割草机行 [1,2,3]（下标 0..2），已发射的也保留。"""
    reader = _bare_reader(_FakeMem([1, 2, 3], alive=[True, True, False]))
    mowers = reader._read_lawn_mowers()
    assert [m.row for m in mowers] == [1, 2, 3]
    assert [m.is_alive for m in mowers] == [True, True, False]


def test_mower_rows_normal_level_unchanged() -> None:
    reader = _bare_reader(_FakeMem([0, 1, 2, 3, 4], alive=[True] * 5))
    assert [m.row for m in reader._read_lawn_mowers()] == [0, 1, 2, 3, 4]


def test_mower_rows_garbage_falls_back_to_index() -> None:
    """行值越界/非递增 → 该版本 0x14 不是行字段，退回下标（普通关无损）。"""
    reader = _bare_reader(_FakeMem([316000, 326000, 336000]))
    assert [m.row for m in reader._read_lawn_mowers()] == [0, 1, 2]
    reader = _bare_reader(_FakeMem([3, 1, 2]))
    assert [m.row for m in reader._read_lawn_mowers()] == [0, 1, 2]


# --------------------------------------------------------------------------- #
#  reader：草皮行推导盖章
# --------------------------------------------------------------------------- #

def test_plantable_rows_stamped_for_tutorial() -> None:
    state = SimpleNamespace(lawn_mowers=[_mower(1), _mower(2), _mower(3)])
    PvZStateReader._stamp_plantable_rows(state)
    assert state._plantable_rows == [1, 2, 3]


def test_plantable_rows_none_for_normal_levels() -> None:
    for rows in ([0, 1, 2, 3, 4], [0, 1, 2, 3, 4, 5], []):
        state = SimpleNamespace(lawn_mowers=[_mower(r) for r in rows])
        PvZStateReader._stamp_plantable_rows(state)
        assert state._plantable_rows is None


def test_plantable_rows_single_grass_tutorial() -> None:
    state = SimpleNamespace(lawn_mowers=[_mower(2)])
    PvZStateReader._stamp_plantable_rows(state)
    assert state._plantable_rows == [2]


# --------------------------------------------------------------------------- #
#  executor：非草皮行重定向（占用/升级检查之前生效）
# --------------------------------------------------------------------------- #

def _putplant_env(monkeypatch, plantable, plants=(), sun: int = 9999):
    ex = _bare_executor(planting_mode="putplant")
    ex._injector.supports_mouse = False
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: False)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_occupied", lambda self, r, c: True)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    ex._mem.read_int.side_effect = [0, 750]
    ex._mem.read_bool.return_value = True
    state = SimpleNamespace(
        seeds=[_seed()], sun=sun, plants=list(plants), game_clock=100, scene=0,
    )
    if plantable is not None:
        state._plantable_rows = plantable
    return ex, state


def test_place_plant_redirects_to_nearest_grass_row(monkeypatch) -> None:
    """单行草皮教学关（row 2）：模型种 row 0 → 改种 row 2 并回填说明。"""
    ex, state = _putplant_env(monkeypatch, plantable=[2])
    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 0, "col": 3}, state, result)

    ex._injector.put_plant.assert_called_once_with(2, 3, 0, imitater=False, sun_cost=100)
    assert result["grid"] == (2, 3)
    assert "改种到最近的草皮行 2" in result["warning"]


def test_place_plant_redirect_clamps_to_grass_range(monkeypatch) -> None:
    """三行草皮关（row 1~3）：row 0→1，row 4→3，行内草皮行原样通过。"""
    for req, expect in ((0, 1), (4, 3), (2, 2)):
        ex, state = _putplant_env(monkeypatch, plantable=[1, 2, 3])
        result: dict = {"action": "place_plant", "status": "ok"}
        ex._place_plant({"card_index": 0, "row": req, "col": 3}, state, result)
        assert ex._injector.put_plant.call_args[0][0] == expect
        if req != expect:
            assert "warning" in result
        else:
            assert "warning" not in result


def test_place_plant_redirect_applies_before_occupancy_check(monkeypatch) -> None:
    """重定向后的格子被占 → 报"已有植物"而不是先撞原行的占用判定。"""
    occupied = SimpleNamespace(row=2, col=3, name="向日葵")
    ex, state = _putplant_env(monkeypatch, plantable=[2], plants=[occupied])
    with pytest.raises(ValueError, match="行2列3 已有 向日葵"):
        ex._place_plant({"card_index": 0, "row": 0, "col": 3}, state, {})
    ex._injector.put_plant.assert_not_called()


def test_place_plant_normal_level_untouched(monkeypatch) -> None:
    """普通关无 _plantable_rows 盖章 → 行为零变化（回归）。"""
    ex, state = _putplant_env(monkeypatch, plantable=None)
    result: dict = {"action": "place_plant", "status": "ok"}
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, state, result)
    ex._injector.put_plant.assert_called_once_with(1, 2, 0, imitater=False, sun_cost=100)
    assert "warning" not in result


# --------------------------------------------------------------------------- #
#  memory_engine：【棋盘】行在教学关回退全草坪行数并明示草皮行
# --------------------------------------------------------------------------- #

def _bare_engine(grid_dims_result):
    eng = MemoryGameEngine.__new__(MemoryGameEngine)
    eng._reader = SimpleNamespace(format_state=lambda s: "TEXT-BODY")
    eng.grid_dims = lambda fallback=None: grid_dims_result  # type: ignore[method-assign]
    return eng


def test_grid_line_declares_grass_rows_for_tutorial() -> None:
    eng = _bare_engine((3, 9))  # count_max=3（旧实现会谎报 3 行）
    state = SimpleNamespace(last_error=None, _plantable_rows=[1, 2, 3])
    out = eng.read_state_text(state, fallback_grid=(5, 9))
    assert "【棋盘】5 行 x 9 列（row 0~4 / col 0~8" in out
    assert "只有 row 1、2、3 有草皮可种" in out
    assert out.endswith("TEXT-BODY")


def test_grid_line_single_grass_tutorial_without_fallback() -> None:
    eng = _bare_engine((1, 9))
    state = SimpleNamespace(last_error=None, _plantable_rows=[2])
    out = eng.read_state_text(state)  # 无 fallback → 全草坪行数取 5
    assert "【棋盘】5 行 x 9 列（row 0~4 / col 0~8" in out
    assert "只有 row 2 有草皮可种" in out


def test_grid_line_normal_level_unchanged() -> None:
    eng = _bare_engine((5, 9))
    state = SimpleNamespace(last_error=None, _plantable_rows=None)
    out = eng.read_state_text(state)
    assert "【棋盘】5 行 x 9 列（row 0~4 / col 0~8，0-based）" in out
    assert "教学关" not in out
