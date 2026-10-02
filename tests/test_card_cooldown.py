"""putplant 直注路线的游戏原生冷却单元测试。

背景：PutPlant 只创建植物对象，不触发 SeedCard 的任何 UI 逻辑。0.4.1 及以前
的"冷却写回"把总时长写进 0x24（已冷却时长）——语义正好相反（该字段从 0 逐帧
递增，pvzclass SeedCard.CoolDown），游戏既不画冷却遮罩也不倒计时。

修复后（等价 pvzclass SeedCard::EnterCoolDown）：
    Enable(0x48)=0 + Interval(0x28)=时长 + CoolDown(0x24)=0 + Active(0x49)=1
之后冷却遮罩/倒计时/就绪闪光全部由游戏自己完成。

本文件只测纯逻辑（注入写入序列、执行器开冷却流程、reader 的剩余时间换算），
不依赖真实游戏进程；非 Windows 跳过（与 test_service.py 同策略，executor
模块顶部无条件 import ctypes.wintypes）。
"""

from __future__ import annotations

import sys
import time
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

import pytest

if sys.platform != "win32":
    pytest.skip(
        "PvZ Agent 插件测试依赖 Windows 运行时（executor 模块 import ctypes.wintypes）",
        allow_module_level=True,
    )

# 让 pvz_memory 可导入：兼容本仓库布局（<root>/pvz）与宿主安装布局
# （plugin/plugins/pvz_agent/pvz），两种布局下 pvz/ 都是本文件的祖父/父目录之一。
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent, _HERE.parent.parent):
    _pvz_dir = _candidate / "pvz"
    if (_pvz_dir / "vendor" / "pvz_memory").is_dir():
        for _p in (str(_pvz_dir), str(_pvz_dir / "vendor")):
            if _p not in sys.path:
                sys.path.insert(0, _p)
        break

from pvz_memory import executor as pvz_executor  # noqa: E402
from pvz_memory.injector import PvZCodeInjector  # noqa: E402
from pvz_memory.offsets import PvZOffsets  # noqa: E402
from pvz_memory.reader import SeedInfo  # noqa: E402


# --------------------------------------------------------------------------- #
#  构造工具
# --------------------------------------------------------------------------- #

def _bare_injector() -> PvZCodeInjector:
    """跳过 __init__（不连真实进程）的注入器，记录全部写入。"""
    inj = PvZCodeInjector.__new__(PvZCodeInjector)
    writes: list[tuple[int, bytes]] = []
    inj._write_bytes = lambda addr, data: writes.append((addr, bytes(data)))  # type: ignore[method-assign]
    inj.writes = writes  # type: ignore[attr-defined]
    return inj


def _bare_executor(planting_mode: str = "putplant") -> pvz_executor.PvZExecutor:
    """跳过 __init__（不连真实进程/窗口）的执行器。"""
    ex = pvz_executor.PvZExecutor.__new__(pvz_executor.PvZExecutor)
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


# --------------------------------------------------------------------------- #
#  injector.start_card_cooldown：写入序列 = 游戏原生 EnterCoolDown 语义
# --------------------------------------------------------------------------- #

def test_start_card_cooldown_writes_native_fields() -> None:
    inj = _bare_injector()
    base = 0x123450
    inj.start_card_cooldown(base, 750)

    assert inj.writes == [
        (base + 0x48, b"\x00"),             # Enable = false（不可点击）
        (base + 0x28, (750).to_bytes(4, "little")),  # CoolDownInterval = 总时长
        (base + 0x24, (0).to_bytes(4, "little")),    # CoolDown = 0（从零计）
        (base + 0x49, b"\x01"),             # Active = true（正在冷却）
    ]


def test_start_card_cooldown_clamps_duration() -> None:
    inj = _bare_injector()
    inj.start_card_cooldown(0x400, 0)
    interval = dict(inj.writes)[0x400 + 0x28]
    assert int.from_bytes(interval, "little") == 1  # 非法时长夹到 >= 1 厘秒


# --------------------------------------------------------------------------- #
#  SeedInfo：0x24 是"已冷却时长"，剩余 = 总时长 - 已冷却
# --------------------------------------------------------------------------- #

def test_seed_remaining_counts_down_from_total() -> None:
    s = _seed()
    s.cd, s.initial_cd = 200, 750
    assert s.cd_remaining == 550          # 真实剩余
    assert s.cd_progress == pytest.approx(200 / 750)  # 进度 = 已冷却占比


def test_seed_remaining_zero_when_ready_or_finished() -> None:
    s = _seed()
    assert s.cd_remaining == 0            # 就绪
    s.cd, s.initial_cd = 750, 750
    assert s.cd_remaining == 0            # 冷却走完的边界


def test_seed_remaining_falls_back_on_dirty_snapshot() -> None:
    s = _seed()
    s.cd, s.initial_cd = 800, 0           # 快照脏值：总时长不可信
    assert s.cd_remaining == 800          # 退回旧口径原样显示


# --------------------------------------------------------------------------- #
#  executor._start_game_cooldown：开启 + 回读验证
# --------------------------------------------------------------------------- #

def test_start_game_cooldown_uses_standard_table_and_verifies() -> None:
    ex = _bare_executor()
    ex._mem.read_int.side_effect = [0, 750]   # 回读: 0x24 CoolDown=0, 0x28 Interval=750
    ex._mem.read_bool.return_value = True     # 回读: 0x49 Active=1
    state = SimpleNamespace(seeds=[_seed(), _seed(), _seed(index=2)])

    ex._start_game_cooldown(state, 2, plant_type=0)

    card_addr = 0x2000 + 0x28 + 2 * 0x50
    ex._injector.start_card_cooldown.assert_called_once_with(card_addr, 750)


def test_start_game_cooldown_skips_when_seed_array_unavailable() -> None:
    ex = _bare_executor()
    ex._mem.read_pointer.return_value = 0
    state = SimpleNamespace(seeds=[_seed()])

    ex._start_game_cooldown(state, 0, plant_type=0)

    ex._injector.start_card_cooldown.assert_not_called()


def test_start_game_cooldown_survives_verification_mismatch() -> None:
    """回读不一致（卡槽数组被迁移）只记日志，不抛错。"""
    ex = _bare_executor()
    ex._mem.read_int.side_effect = [123, 0]   # cd 非零 + interval 为 0 → 验证失败
    ex._mem.read_bool.return_value = False
    state = SimpleNamespace(seeds=[_seed()])

    ex._start_game_cooldown(state, 0, plant_type=0)  # 不应抛异常

    ex._injector.start_card_cooldown.assert_called_once()


# --------------------------------------------------------------------------- #
#  putplant 全流程：PutPlant 确认后 → 封卡 + 开游戏原生冷却
# --------------------------------------------------------------------------- #

def test_place_plant_putplant_starts_native_cooldown(monkeypatch) -> None:
    ex = _bare_executor(planting_mode="putplant")
    ex._injector.supports_mouse = False
    seed = _seed(index=0)
    state = SimpleNamespace(seeds=[seed], sun=9999, plants=[], game_clock=100, scene=0)

    monkeypatch.setattr(pvz_executor.PvZExecutor, "_conveyor_verdict", lambda self, s: False)
    monkeypatch.setattr(pvz_executor.PvZExecutor, "_cell_occupied", lambda self, r, c: True)
    monkeypatch.setattr(pvz_executor.time, "sleep", lambda s: None)
    ex._mem.read_int.side_effect = [0, 750]
    ex._mem.read_bool.return_value = True

    result: dict = {"action": "place_plant", "status": "ok"}  # execute() 预置的骨架
    ex._place_plant({"card_index": 0, "row": 1, "col": 2}, state, result)

    ex._injector.put_plant.assert_called_once_with(1, 2, 0, imitater=False, sun_cost=100)
    # 游戏原生冷却已开：豌豆射手 7.5s，写向卡槽 0 的地址
    card_addr = 0x2000 + 0x28 + 0 * 0x50
    ex._injector.start_card_cooldown.assert_called_once_with(card_addr, 750)
    # 不再有任何插件侧模拟冷却（0.4.1 的 monotonic 封卡表已移除）
    assert not hasattr(ex, "_card_ready_at")
    assert result["status"] == "ok"
    assert result["direct"] is True


def test_place_plant_putplant_same_round_replant_blocked_by_native_cd(monkeypatch) -> None:
    """同轮连种：动作间 execute_tool_call 会重读内存，游戏原生 cd>0 即拦截。

    直注后的 0.3s 验证等待足够游戏把 0x24 从 0 起跳（每帧 +1 厘秒），
    因此 0.4.1 的插件侧 monotonic 封卡表没有存在必要。
    """
    ex = _bare_executor(planting_mode="putplant")
    ex._injector.supports_mouse = False
    seed = _seed(index=0)
    seed.cd, seed.initial_cd = 30, 750  # 上一动作刚种下，游戏已把 0x24 起跳到 30
    state = SimpleNamespace(seeds=[seed], sun=9999, plants=[], game_clock=100, scene=0)

    with pytest.raises(ValueError, match="还剩 7.2s"):
        ex._place_plant({"card_index": 0, "row": 1, "col": 2}, state, {})
    ex._injector.put_plant.assert_not_called()
    ex._injector.start_card_cooldown.assert_not_called()


def test_place_plant_rejects_on_memory_cooldown(monkeypatch) -> None:
    """游戏原生冷却生效后，reader 读到 cd>0（已冷却起跳）同样拦截。"""
    ex = _bare_executor(planting_mode="putplant")
    ex._injector.supports_mouse = False
    seed = _seed(index=0)
    seed.cd, seed.initial_cd = 200, 750  # 游戏冷却进行中（已冷却 2s / 总 7.5s）
    state = SimpleNamespace(seeds=[seed], sun=9999, plants=[], game_clock=100, scene=0)

    with pytest.raises(ValueError, match="还剩 5.5s"):
        ex._place_plant({"card_index": 0, "row": 1, "col": 2}, state, {})
    ex._injector.put_plant.assert_not_called()
