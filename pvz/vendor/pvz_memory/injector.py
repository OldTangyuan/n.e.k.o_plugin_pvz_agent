"""PvZ 代码注入器 — 向 PvZ 进程注入 x86 机器码，直接调用游戏内部函数.

核心机制:
1. 写入 hack 字节 (WriteProcessMemory) — 修改游戏代码段实现功能开关
   (自动收集阳光 / 冻结主循环 / 解锁阳光上限 / 任意位置种植)
2. 注入 shellcode (VirtualAllocEx + WriteProcessMemory + CreateRemoteThread) — 调用游戏函数
3. 注入前暂停游戏主循环 (block_main_loop hack)，防止竞态条件

⚠️ 版本限制: 所有函数地址 / hack 地址 / 结构体偏移均来自 pvztoolkit data.cpp
   **仅维护 V1_0_0_1051_EN (原版 1.0.0.1051 EN)**。初始化时会校验连接版本,
   其他版本会抛 PvZMemoryError 拒绝操作, 避免用错误的地址写坏游戏内存。

独立模块说明:
- 零第三方运行时依赖 (仅标准库 ctypes/struct/logging)
- 日志使用标准库 logging, 模块 logger 名为 "pvz_memory.injector"
- 非 Windows 平台可安全 import (初始化时才会报错)

参考项目:
- pvztoolkit (code.cpp): Code 类 + asm_code_inject
- AsmVsZombies (avz_asm.cpp): AAsm 内联汇编调用
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import struct
import time
from dataclasses import dataclass, field

from .memory import PvZMemory, PvZMemoryError
from .offsets import PvZVersion

logger = logging.getLogger(__name__)

# ================================================================== #
#  Windows API
# ================================================================== #

_IS_WINDOWS = hasattr(ctypes, "windll")

if _IS_WINDOWS:
    _kernel32 = ctypes.windll.kernel32

    # 注入用
    _VirtualAllocEx = _kernel32.VirtualAllocEx
    _VirtualAllocEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
        wintypes.DWORD, wintypes.DWORD,
    ]
    _VirtualAllocEx.restype = ctypes.c_void_p

    _VirtualFreeEx = _kernel32.VirtualFreeEx
    _VirtualFreeEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
    ]
    _VirtualFreeEx.restype = wintypes.BOOL

    _WriteProcessMemory = _kernel32.WriteProcessMemory
    _WriteProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    _WriteProcessMemory.restype = wintypes.BOOL

    _CreateRemoteThread = _kernel32.CreateRemoteThread
    _CreateRemoteThread.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _CreateRemoteThread.restype = wintypes.HANDLE

    _WaitForSingleObject = _kernel32.WaitForSingleObject
    _WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _WaitForSingleObject.restype = wintypes.DWORD

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _GetExitCodeThread = _kernel32.GetExitCodeThread
    _GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _GetExitCodeThread.restype = wintypes.BOOL

    _OpenProcess = _kernel32.OpenProcess
    _OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _OpenProcess.restype = wintypes.HANDLE

    _GetLastError = _kernel32.GetLastError
    _GetLastError.argtypes = []
    _GetLastError.restype = ctypes.c_int
else:
    # 非 Windows: 保持同名模块属性存在 (值为 None)，测试可 patch
    _kernel32 = None
    _VirtualAllocEx = None
    _VirtualFreeEx = None
    _WriteProcessMemory = None
    _CreateRemoteThread = None
    _WaitForSingleObject = None
    _CloseHandle = None
    _GetExitCodeThread = None
    _OpenProcess = None
    _GetLastError = None

# 常量
MEM_COMMIT = 0x00001000
MEM_RELEASE = 0x00008000
PAGE_EXECUTE_READWRITE = 0x40
INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 0x00000102

# 进程权限 — 注入需要更多权限
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_CREATE_THREAD = 0x0002

# Windows 错误码名称 (写内存失败诊断)
_WIN_ERROR_NAMES = {
    5: "ERROR_ACCESS_DENIED",
    6: "ERROR_INVALID_HANDLE",
    87: "ERROR_INVALID_PARAMETER",
    299: "ERROR_PARTIAL_COPY",
    998: "ERROR_NOACCESS",
}


# ================================================================== #
#  Hack 定义 — 来自 pvztoolkit data.cpp V1_0_0_1051_EN
# ================================================================== #
# HACK 格式: {addr, hack_value, reset_value}
# enable: 将 hack_value 写入 addr; disable: 将 reset_value 写回 addr

@dataclass(frozen=True)
class HackInfo:
    """一个 hack 条目: 地址 + 修改值 + 原始值."""
    addr: int
    hack_value: bytes
    reset_value: bytes


# 注入前暂停游戏主循环 — 防止注入代码与游戏主循环并发执行
# pvztoolkit: "block_main_loop", 0x552014, hack=0xFE(jmp), reset=0xDB
HACK_BLOCK_MAIN_LOOP = HackInfo(0x00552014, b'\xFE', b'\xDB')

# 自动收集阳光 — 阳光出现后自动飞向计数器
# pvztoolkit: "auto_collected", 0x43158F, hack=0xEB(jmp short), reset=0x75(jnz)
HACK_AUTO_COLLECTED = HackInfo(0x0043158F, b'\xEB', b'\x75')

# 无阳光上限
# pvztoolkit: "unlock_sun_limit", 0x430A23, hack=0xEB, reset=0x7E
HACK_UNLOCK_SUN_LIMIT = HackInfo(0x00430A23, b'\xEB', b'\x7E')

# 植物可种在任何位置
# pvztoolkit: "placed_anywhere", 0x40FE30, hack=0x81, reset=0x84
HACK_PLACED_ANYWHERE = HackInfo(0x0040FE30, b'\x81', b'\x84')


# ================================================================== #
#  版本地址表 — 所有游戏内部地址按版本收敛 (8 个支持版本)
# ================================================================== #

@dataclass(frozen=True)
class InjectAddresses:
    """一个版本的注入地址集合.

    基础能力 (所有支持版本): put_plant / fade_out_level(通关) / 4 个 hack。
    高级能力 (仅原版 V1_0_0_1051_EN, 来自 AsmVsZombies): 模拟鼠标点击 /
    铲除 / 选卡 — 其他版本为 None, 调用时抛 PvZMemoryError。
    """
    version: PvZVersion
    version_name: str
    pvz_base: int               # 全局基址变量地址 (pvztoolkit lawn, 版本相关)
    board_offset: int           # PvzBase → Board*
    # ---- 基础能力 (所有支持版本) ----
    func_put_plant: int
    func_fade_out_level: int
    fade_call_convention: str   # "ecx_thiscall" | "eax_push" (GOTY ZH/JA 用 push)
    hacks: dict[str, HackInfo] = field(default_factory=dict)
    # ---- 高级能力 (仅原版) ----
    mouse_offset: int | None = None
    select_card_ui_offset: int | None = None
    func_shovel_plant: int | None = None
    func_mouse_down: int | None = None
    func_mouse_up: int | None = None
    func_release_mouse: int | None = None
    func_choose_card: int | None = None
    func_rock: int | None = None
    func_pick_random_seeds: int | None = None
    func_grid_to_abscissa: int | None = None
    func_grid_to_ordinate: int | None = None
    grid_x_result_addr: int | None = None
    grid_y_result_addr: int | None = None

    @property
    def supports_mouse(self) -> bool:
        """是否支持模拟鼠标点击类高级动作 (仅原版)."""
        return self.mouse_offset is not None


def _make_hacks(block_addr: int, block_reset: int, auto_addr: int,
                unlock_addr: int, placed_addr: int) -> dict[str, HackInfo]:
    """构造一个版本的 4 个 hack (地址/字节按版本).

    hack 值字节所有版本一致 (auto_collected=EB/75 等);
    block_main_loop 的 reset 原版=DB, GOTY=C8。
    """
    return {
        "block_main_loop": HackInfo(block_addr, b'\xFE', bytes([block_reset])),
        "auto_collected": HackInfo(auto_addr, b'\xEB', b'\x75'),
        "unlock_sun_limit": HackInfo(unlock_addr, b'\xEB', b'\x7E'),
        "placed_anywhere": HackInfo(placed_addr, b'\x81', b'\x84'),
    }


def _make_base_addrs(version, version_name, *, pvz_base, board_offset,
                     func_put_plant, func_fade_out_level, fade_call_convention,
                     block_addr, block_reset, auto_addr, unlock_addr, placed_addr,
                     **advanced) -> InjectAddresses:
    """构造一个版本的基础注入地址表 (高级能力默认 None, 用 **advanced 覆盖)."""
    return InjectAddresses(
        version=version, version_name=version_name,
        pvz_base=pvz_base, board_offset=board_offset,
        func_put_plant=func_put_plant,
        func_fade_out_level=func_fade_out_level,
        fade_call_convention=fade_call_convention,
        hacks=_make_hacks(block_addr, block_reset, auto_addr, unlock_addr, placed_addr),
        **advanced,
    )


V1_INJECT_ADDRESSES = _make_base_addrs(
    PvZVersion.V1_0_0_1051_EN, "1.0.0.1051 EN",
    pvz_base=0x6A9EC0, board_offset=0x768,
    func_put_plant=0x40D120, func_fade_out_level=0x40C3E0,
    fade_call_convention="ecx_thiscall",
    block_addr=0x00552014, block_reset=0xDB,
    auto_addr=0x0043158F, unlock_addr=0x00430A23, placed_addr=0x0040FE30,
    # 高级能力 (仅原版, 来自 AsmVsZombies)
    mouse_offset=0x320, select_card_ui_offset=0x774,
    func_shovel_plant=0x411060, func_mouse_down=0x539390, func_mouse_up=0x5392E0,
    func_release_mouse=0x40CD80, func_choose_card=0x486030, func_rock=0x486D20,
    func_pick_random_seeds=0x4859B0, func_grid_to_abscissa=0x41C680,
    func_grid_to_ordinate=0x41C740,
    grid_x_result_addr=0x6AA7C0, grid_y_result_addr=0x6AA7C4,
)

_ADDRESS_TABLES: dict[PvZVersion, InjectAddresses] = {
    PvZVersion.V1_0_0_1051_EN: V1_INJECT_ADDRESSES,
    PvZVersion.V1_2_0_1065_EN: _make_base_addrs(
        PvZVersion.V1_2_0_1065_EN, "1.2.0.1065 EN",
        pvz_base=0x6A9EC0, board_offset=0x768,
        func_put_plant=0x40D130, func_fade_out_level=0x40C3F0,
        fade_call_convention="ecx_thiscall",
        block_addr=0x00552244, block_reset=0xDB,
        auto_addr=0x004315EF, unlock_addr=0x00430A83, placed_addr=0x0040FE20),
    PvZVersion.GOTY_1_2_0_1073_EN: _make_base_addrs(
        PvZVersion.GOTY_1_2_0_1073_EN, "GOTY 1.2.0.1073 EN",
        pvz_base=0x729670, board_offset=0x868,
        func_put_plant=0x40FA10, func_fade_out_level=0x40ECD0,
        fade_call_convention="ecx_thiscall",
        block_addr=0x005D6C6E, block_reset=0xC8,
        auto_addr=0x004342F2, unlock_addr=0x0041E6F5, placed_addr=0x004127F0),
    PvZVersion.GOTY_1_2_0_1096_EN: _make_base_addrs(
        PvZVersion.GOTY_1_2_0_1096_EN, "GOTY 1.2.0.1096 EN",
        pvz_base=0x731C50, board_offset=0x868,
        func_put_plant=0x4105A0, func_fade_out_level=0x40F860,
        fade_call_convention="ecx_thiscall",
        block_addr=0x005DD25E, block_reset=0xC8,
        auto_addr=0x004352F2, unlock_addr=0x0041F4E5, placed_addr=0x00413350),
    PvZVersion.GOTY_1_1_0_1056_ZH: _make_base_addrs(
        PvZVersion.GOTY_1_1_0_1056_ZH, "GOTY 1.1.0.1056 ZH",
        pvz_base=0x7794F8, board_offset=0x868,
        func_put_plant=0x422610, func_fade_out_level=0x421A20,
        fade_call_convention="eax_push",
        block_addr=0x005CFD4E, block_reset=0xC8,
        auto_addr=0x0044C5F2, unlock_addr=0x0044BA56, placed_addr=0x00425954),
    PvZVersion.GOTY_1_1_0_1056_JA: _make_base_addrs(
        PvZVersion.GOTY_1_1_0_1056_JA, "GOTY 1.1.0.1056 JA",
        pvz_base=0x7578F8, board_offset=0x868,
        func_put_plant=0x412370, func_fade_out_level=0x411880,
        fade_call_convention="eax_push",
        block_addr=0x00627ADE, block_reset=0xC8,
        auto_addr=0x0043B3A2, unlock_addr=0x0043A806, placed_addr=0x004156B4),
    PvZVersion.GOTY_1_1_0_1056_ZH_2012_06: _make_base_addrs(
        PvZVersion.GOTY_1_1_0_1056_ZH_2012_06, "GOTY 1.1.0.1056 ZH (2012-06)",
        pvz_base=0x755E0C, board_offset=0x868,
        func_put_plant=0x418D70, func_fade_out_level=0x418140,
        fade_call_convention="ecx_thiscall",
        block_addr=0x0062941E, block_reset=0xC8,
        auto_addr=0x0043CC72, unlock_addr=0x0043C0C1, placed_addr=0x0041BD2E),
    PvZVersion.GOTY_1_1_0_1056_ZH_2012_07: _make_base_addrs(
        PvZVersion.GOTY_1_1_0_1056_ZH_2012_07, "GOTY 1.1.0.1056 ZH (2012-07)",
        pvz_base=0x757E0C, board_offset=0x868,
        func_put_plant=0x4199C0, func_fade_out_level=0x418D90,
        fade_call_convention="ecx_thiscall",
        block_addr=0x006271FE, block_reset=0xC8,
        auto_addr=0x0043D8C2, unlock_addr=0x0043CD11, placed_addr=0x0041C9AE),
}

#: 支持代码注入的版本 (pvztoolkit data.cpp 有完整注入数据的 8 个版本)
SUPPORTED_INJECT_VERSIONS: tuple[PvZVersion, ...] = tuple(_ADDRESS_TABLES)


def get_inject_addresses(version: PvZVersion) -> InjectAddresses | None:
    """获取某版本的注入地址表; 版本不支持注入时返回 None."""
    return _ADDRESS_TABLES.get(version)


# ================================================================== #
#  Shellcode 构建辅助
# ================================================================== #

def _build_put_plant_code(row: int, col: int, plant_type: int, imitater: bool,
                          addrs: InjectAddresses = V1_INJECT_ADDRESSES) -> bytearray:
    """构建 PutPlant(row, col, type, imitater) 的 x86 机器码.

    调用约定来自 pvztoolkit asm_put_plant (非 GOTY 版本):
        push imitator_type   ; -1 或 实际类型
        push type            ; 植物类型
        mov eax, row         ; 行号
        push col             ; 列号
        mov ebp, [PVZ_BASE]
        mov ebp, [ebp + BOARD_OFFSET]
        push ebp             ; Board*
        call PutPlant
        ret
    """
    code = bytearray()

    if imitater:
        # push plant_type (实际要模仿的植物类型)
        code += b'\x68' + struct.pack('<i', plant_type)
        # push 48 (模仿者卡片类型)
        code += b'\x68' + struct.pack('<I', 48)
    else:
        # push -1 (非模仿者)
        code += b'\x6A\xFF'
        # push plant_type
        code += b'\x68' + struct.pack('<i', plant_type)

    # mov eax, row
    code += b'\xB8' + struct.pack('<i', row)
    # push col
    code += b'\x68' + struct.pack('<i', col)
    # mov ebp, [PVZ_BASE]
    code += b'\x8B\x2D' + struct.pack('<I', addrs.pvz_base)
    # mov ebp, [ebp + BOARD_OFFSET]
    code += b'\x8B\xAD' + struct.pack('<I', addrs.board_offset)
    # push ebp
    code += b'\x55'
    # mov edx, FUNC_PUT_PLANT
    code += b'\xBA' + struct.pack('<I', addrs.func_put_plant)
    # call edx
    code += b'\xFF\xD2'
    # ret
    code += b'\xC3'

    return code


def _build_shovel_code(x: int, y: int, addrs: InjectAddresses = V1_INJECT_ADDRESSES) -> bytearray:
    """构建 ShovelPlant(x, y) 的 x86 机器码.

    调用约定来自 AsmVsZombies AAsm::ShovelPlant (avz_asm.cpp L243-256):
        push 6
        push 1
        mov ecx, y
        mov edx, x
        mov eax, [PVZ_BASE]
        mov eax, [eax + BOARD_OFFSET]  ; eax = Board*
        mov ebx, FUNC_SHOVEL_PLANT
        call ebx
        ret
    """
    code = bytearray()

    # push 6
    code += b'\x6A\x06'
    # push 1
    code += b'\x6A\x01'
    # mov ecx, y
    code += b'\xB9' + struct.pack('<i', y)
    # mov edx, x
    code += b'\xBA' + struct.pack('<i', x)
    # mov eax, [PVZ_BASE]
    code += b'\xA1' + struct.pack('<I', addrs.pvz_base)
    # mov eax, [eax + BOARD_OFFSET]
    code += b'\x8B\x80' + struct.pack('<I', addrs.board_offset)
    # mov ebx, FUNC_SHOVEL_PLANT
    code += b'\xBB' + struct.pack('<I', addrs.func_shovel_plant)
    # call ebx
    code += b'\xFF\xD3'
    # ret
    code += b'\xC3'

    return code


def _build_mouse_click_code(x: int, y: int, button: int = 1, with_ret: bool = True,
                            addrs: InjectAddresses = V1_INJECT_ADDRESSES) -> bytearray:
    """构建完整的 MouseClick(x, y, button) 的 x86 机器码.

    实现 AvZ AAsm::MouseClick 的逻辑 (avz_asm.cpp L153-159):
        MouseDown(x, y, key)
        MouseUp(x, y, key)

    MouseDown: push x, eax=y, ebx=key, ecx=MouseWindow*
    MouseUp:   push key, push x, eax=MouseWindow*, ebx=y

    Args:
        x: 游戏 x 坐标。
        y: 游戏 y 坐标。
        button: 1=左键, 2=右键。
        with_ret: 是否末尾添加 ret（批量拼接时设为 False）。
    """
    code = bytearray()

    # ---- MouseDown(x, y, button) ----
    # mov ecx, [PVZ_BASE]
    code += b'\x8B\x0D' + struct.pack('<I', addrs.pvz_base)
    # mov ecx, [ecx + MOUSE_OFFSET]  ; ecx = MouseWindow*
    code += b'\x8B\x89' + struct.pack('<I', addrs.mouse_offset)
    # push x
    code += b'\x68' + struct.pack('<i', x)
    # mov eax, y
    code += b'\xB8' + struct.pack('<i', y)
    # mov ebx, button
    code += b'\xBB' + struct.pack('<i', button)
    # mov edx, FUNC_MOUSE_DOWN
    code += b'\xBA' + struct.pack('<I', addrs.func_mouse_down)
    # call edx
    code += b'\xFF\xD2'

    # ---- MouseUp(x, y, button) ----
    # mov eax, [PVZ_BASE]
    code += b'\xA1' + struct.pack('<I', addrs.pvz_base)
    # mov eax, [eax + MOUSE_OFFSET]  ; eax = MouseWindow*
    code += b'\x8B\x80' + struct.pack('<I', addrs.mouse_offset)
    # push button
    code += b'\x68' + struct.pack('<i', button)
    # push x
    code += b'\x68' + struct.pack('<i', x)
    # mov ebx, y
    code += b'\xBB' + struct.pack('<i', y)
    # mov edx, FUNC_MOUSE_UP
    code += b'\xBA' + struct.pack('<I', addrs.func_mouse_up)
    # call edx
    code += b'\xFF\xD2'

    if with_ret:
        code += b'\xC3'

    return code


def _build_grid_to_x_code(row: int, col: int,
                          addrs: InjectAddresses = V1_INJECT_ADDRESSES) -> bytearray:
    """构建 GridToAbscissa(row, col) 的 x86 机器码，结果写入固定地址.

    调用约定来自 AvZ (avz_asm.cpp L339-352):
        mov ecx, [PVZ_BASE]
        mov ecx, [ecx + BOARD_OFFSET]  ; ecx = Board*
        mov eax, col
        mov esi, row
        call GridToAbscissa
        ; 返回值在 eax

    结果写入 PvzBase + 0x900（PvzBase 对象尾部安全区域）。
    """
    code = bytearray()

    # mov ecx, [PVZ_BASE]
    code += b'\x8B\x0D' + struct.pack('<I', addrs.pvz_base)
    # mov ecx, [ecx + BOARD_OFFSET]
    code += b'\x8B\x89' + struct.pack('<I', addrs.board_offset)
    # mov eax, col
    code += b'\xB8' + struct.pack('<i', col)
    # mov esi, row
    code += b'\xBE' + struct.pack('<i', row)
    # mov edx, FUNC_GRID_TO_ABSCISSA
    code += b'\xBA' + struct.pack('<I', addrs.func_grid_to_abscissa)
    # call edx
    code += b'\xFF\xD2'
    # mov [RESULT_ADDR], eax
    code += b'\xA3' + struct.pack('<I', addrs.grid_x_result_addr)
    # ret
    code += b'\xC3'

    return code


def _build_grid_to_y_code(row: int, col: int,
                          addrs: InjectAddresses = V1_INJECT_ADDRESSES) -> bytearray:
    """构建 GridToOrdinate(row, col) 的 x86 机器码，结果写入固定地址.

    调用约定来自 AvZ (avz_asm.cpp L355-369):
        mov ebx, [PVZ_BASE]
        mov ebx, [ebx + BOARD_OFFSET]  ; ebx = Board*
        mov ecx, col
        mov eax, row
        call GridToOrdinate
        ; 返回值在 eax
    """
    code = bytearray()

    # mov ebx, [PVZ_BASE]
    code += b'\x8B\x1D' + struct.pack('<I', addrs.pvz_base)
    # mov ebx, [ebx + BOARD_OFFSET]
    code += b'\x8B\x9B' + struct.pack('<I', addrs.board_offset)
    # mov ecx, col
    code += b'\xB9' + struct.pack('<i', col)
    # mov eax, row
    code += b'\xB8' + struct.pack('<i', row)
    # mov edx, FUNC_GRID_TO_ORDINATE
    code += b'\xBA' + struct.pack('<I', addrs.func_grid_to_ordinate)
    # call edx
    code += b'\xFF\xD2'
    # mov [RESULT_ADDR], eax
    code += b'\xA3' + struct.pack('<I', addrs.grid_y_result_addr)
    # ret
    code += b'\xC3'

    return code


def _build_fade_out_level_code(addrs: InjectAddresses = V1_INJECT_ADDRESSES) -> bytearray:
    """构建 FadeOutLevel(直接通关) 的 x86 机器码.

    调用约定因版本而异 (pvztoolkit PvZ::DirectWin):
    - "ecx_thiscall" (原版 / GOTY EN / 2012 版):
        mov ecx,[pvz]; mov ecx,[ecx+board]; call FadeOutLevel  (ECX = Board*)
    - "eax_push" (GOTY ZH/JA):
        mov eax,[pvz]; mov eax,[eax+board]; push eax; call FadeOutLevel
    """
    code = bytearray()
    if addrs.fade_call_convention == "eax_push":
        # mov eax, [PVZ_BASE]
        code += b'\xA1' + struct.pack('<I', addrs.pvz_base)
        # mov eax, [eax + BOARD_OFFSET]  ; eax = Board*
        code += b'\x8B\x80' + struct.pack('<I', addrs.board_offset)
        # push eax
        code += b'\x50'
    else:
        # mov ecx, [PVZ_BASE]
        code += b'\x8B\x0D' + struct.pack('<I', addrs.pvz_base)
        # mov ecx, [ecx + BOARD_OFFSET]  ; ecx = Board*
        code += b'\x8B\x89' + struct.pack('<I', addrs.board_offset)
    # mov edx, FUNC_FADE_OUT_LEVEL
    code += b'\xBA' + struct.pack('<I', addrs.func_fade_out_level)
    # call edx
    code += b'\xFF\xD2'
    # ret
    code += b'\xC3'
    return code


# ================================================================== #
#  代码注入器
# ================================================================== #

class PvZCodeInjector:
    """PvZ 代码注入执行器.

    向 PvZ 进程注入小型 x86 shellcode 并远程执行。

    核心流程 (参考 pvztoolkit PvZ::asm_code_inject):
        1. 暂停游戏主循环 (enable_hack BLOCK_MAIN_LOOP)
        2. VirtualAllocEx 分配可执行内存
        3. WriteProcessMemory 写入 shellcode
        4. CreateRemoteThread 远程执行
        5. WaitForSingleObject 等待完成
        6. VirtualFreeEx 释放内存
        7. 恢复游戏主循环 (disable_hack BLOCK_MAIN_LOOP)

    ⚠️ 仅支持原版 V1_0_0_1051_EN，其他版本初始化时抛 PvZMemoryError。
    """

    def __init__(self, memory: PvZMemory, auto_collect: bool = True) -> None:
        if not _IS_WINDOWS:
            raise PvZMemoryError("代码注入仅支持 Windows 平台")
        if not memory.is_connected():
            raise PvZMemoryError("PvZ 进程未连接，无法注入代码")

        # 版本守卫: 注入器地址只对支持列表有效。
        # 必须在 OpenProcess 之前抛错，避免无谓地打开/占用句柄。
        addrs = get_inject_addresses(memory.version)
        if addrs is None:
            supported = ", ".join(str(v) for v in SUPPORTED_INJECT_VERSIONS)
            raise PvZMemoryError(
                f"代码注入不支持版本 {memory.version_name} ({memory.version})。"
                f"支持: {supported}。请改用只读模式 (PvZStateReader)。"
            )
        self._addrs = addrs
        self._mem = memory

        # 重新打开进程，注入需要更多权限
        handle = self._open_process_for_injection()
        self._inject_handle = handle

        # hack 状态跟踪
        self._active_hacks: dict[str, bool] = {}

        # 默认开启自动收集阳光 — Agent 模式下无需手动点阳光
        if auto_collect:
            self.set_auto_collect(True)

        logger.info("[注入器] 初始化成功，注入句柄=0x%X", handle)

    @property
    def supports_mouse(self) -> bool:
        """当前版本是否支持模拟鼠标/铲除/选卡等高级操作 (仅原版)."""
        return self._addrs.supports_mouse

    def close(self) -> None:
        """释放注入句柄和恢复所有 hack. 幂等."""
        for name in list(self._active_hacks):
            try:
                self._disable_hack_by_name(name)
            except Exception:
                pass
        if self._inject_handle:
            _CloseHandle(self._inject_handle)
            self._inject_handle = 0

    def __enter__(self) -> "PvZCodeInjector":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Hack 机制 — 写入字节修改游戏行为
    # ------------------------------------------------------------------ #

    def enable_hack(self, name: str, hack: HackInfo) -> None:
        """启用一个 hack: 将 hack_value 写入目标地址."""
        self._write_bytes(hack.addr, hack.hack_value)
        self._active_hacks[name] = True
        logger.debug("[Hack] 启用 %s (0x%X)", name, hack.addr)

    def disable_hack(self, name: str, hack: HackInfo) -> None:
        """禁用一个 hack: 将 reset_value 写回目标地址."""
        self._write_bytes(hack.addr, hack.reset_value)
        self._active_hacks.pop(name, None)
        logger.debug("[Hack] 禁用 %s (0x%X)", name, hack.addr)

    def _disable_hack_by_name(self, name: str) -> None:
        """按名称禁用 hack (内部清理用)."""
        hack = self._addrs.hacks.get(name)
        if hack:
            self.disable_hack(name, hack)

    def _write_bytes(self, addr: int, data: bytes) -> None:
        """向 PvZ 进程指定地址写入字节 (hack 机制与内存修改用)."""
        handle = self._inject_handle
        if not handle:
            raise PvZMemoryError("注入句柄无效，请先初始化注入器")
        written = ctypes.c_size_t(0)
        success = _WriteProcessMemory(
            handle,
            ctypes.c_void_p(addr),
            data,
            len(data),
            ctypes.byref(written),
        )
        if not success or written.value != len(data):
            error_code = _GetLastError() if _IS_WINDOWS else 0
            err_name = _WIN_ERROR_NAMES.get(error_code)
            suffix = f" ({err_name})" if err_name else ""
            raise PvZMemoryError(
                f"WriteProcessMemory 写入 0x{addr:X} 失败: "
                f"written={written.value}/{len(data)}, error={error_code}{suffix}"
            )

    def write_int(self, addr: int, value: int) -> None:
        """向 PvZ 进程指定地址写入 32 位整数 (如修改阳光数量)."""
        self._write_bytes(addr, struct.pack('<i', value))

    # ------------------------------------------------------------------ #
    #  注入核心 — shellcode 执行
    # ------------------------------------------------------------------ #

    def _inject_and_execute(self, code: bytes, timeout_ms: int = 3000) -> int:
        """注入 shellcode 到 PvZ 进程并执行.

        在注入前后暂停/恢复游戏主循环，防止竞态条件。
        """
        # 1. 暂停游戏主循环
        self.enable_hack("block_main_loop", self._addrs.hacks["block_main_loop"])
        time.sleep(0.01)  # 等待主循环停下来

        try:
            result = self._raw_inject(code, timeout_ms)
        finally:
            # 2. 恢复游戏主循环
            self.disable_hack("block_main_loop", self._addrs.hacks["block_main_loop"])

        return result

    def _raw_inject(self, code: bytes, timeout_ms: int = 3000) -> int:
        """底层注入执行（不暂停主循环）."""
        handle = self._inject_handle
        if not handle:
            raise PvZMemoryError("注入句柄无效")

        code_size = len(code)

        # 分配可执行内存
        code_addr = _VirtualAllocEx(
            handle, None, code_size,
            MEM_COMMIT, PAGE_EXECUTE_READWRITE,
        )
        if not code_addr:
            error_code = _GetLastError() if _IS_WINDOWS else 0
            raise PvZMemoryError(f"VirtualAllocEx 失败: error={error_code}")

        try:
            # 写入 shellcode
            written = ctypes.c_size_t(0)
            if not _WriteProcessMemory(
                handle, ctypes.c_void_p(code_addr),
                code, code_size,
                ctypes.byref(written),
            ):
                raise PvZMemoryError("WriteProcessMemory 失败")
            if written.value != code_size:
                raise PvZMemoryError(f"写入不完整: {written.value}/{code_size}")

            # 创建远程线程执行
            thread = _CreateRemoteThread(
                handle, None, 0,
                ctypes.c_void_p(code_addr),
                None, 0, None,
            )
            if not thread:
                raise PvZMemoryError("CreateRemoteThread 失败")

            # 等待完成
            wait_result = _WaitForSingleObject(thread, timeout_ms)
            if wait_result == WAIT_TIMEOUT:
                _CloseHandle(thread)
                raise PvZMemoryError("远程线程执行超时")

            # 读取退出码
            exit_code = wintypes.DWORD(0)
            _GetExitCodeThread(thread, ctypes.byref(exit_code))
            _CloseHandle(thread)

            return exit_code.value

        finally:
            _VirtualFreeEx(handle, ctypes.c_void_p(code_addr), 0, MEM_RELEASE)

    # ------------------------------------------------------------------ #
    #  高层动作接口
    # ------------------------------------------------------------------ #

    def put_plant(self, row: int, col: int, plant_type: int, imitater: bool = False,
                  sun_cost: int = 0) -> None:
        """直接放置植物（不经过鼠标操作）.

        注意: PutPlant 内部函数只创建植物对象，不扣除阳光。
        如果需要扣阳光，传入 sun_cost 参数，会手动修改内存中的阳光值。

        Args:
            row: 行号 (0-based)。
            col: 列号 (0-based)。
            plant_type: 植物类型 ID（0=豌豆射手, 1=向日葵, ...）。
            imitater: 是否为模仿者。
            sun_cost: 阳光消耗，0 表示不扣。
        """
        logger.info("[注入] PutPlant row=%s, col=%s, type=%s, imitater=%s, cost=%s",
                    row, col, plant_type, imitater, sun_cost)
        code = _build_put_plant_code(row, col, plant_type, imitater, self._addrs)
        self._inject_and_execute(bytes(code))

        # 手动扣除阳光（PutPlant 内部函数不走 UI 逻辑，不扣阳光）
        if sun_cost > 0:
            self._deduct_sun(sun_cost)

        time.sleep(0.05)

    def shovel(self, x: int, y: int) -> None:
        """在游戏坐标位置执行铲除操作.

        Args:
            x: 游戏内 x 坐标（800x600 基准）。
            y: 游戏内 y 坐标（800x600 基准）。
        """
        self._require_mouse()
        logger.info("[注入] ShovelPlant (%s, %s)", x, y)
        code = _build_shovel_code(x, y, self._addrs)
        self._inject_and_execute(bytes(code))
        time.sleep(0.05)

    def mouse_click(self, x: int, y: int, button: int = 1) -> None:
        """在游戏坐标空间内模拟鼠标点击 (MouseDown + MouseUp).

        Args:
            x: 游戏内 x 坐标（800x600 基准）。
            y: 游戏内 y 坐标（800x600 基准）。
            button: 1=左键, 2=右键。
        """
        self._require_mouse()
        logger.info("[注入] MouseClick (%s, %s), button=%s", x, y, button)
        code = _build_mouse_click_code(x, y, button, addrs=self._addrs)
        self._inject_and_execute(bytes(code))
        time.sleep(0.05)

    def release_mouse(self) -> None:
        """释放游戏鼠标选中状态.

        当卡片处于选中状态时（如种植物后卡片仍高亮），调用此方法取消选中。
        AvZ 在铲除前也会先调用 ReleaseMouse。
        调用约定: eax = Board*, 无参数。
        """
        self._require_mouse()
        a = self._addrs
        code = bytearray()
        # mov eax, [PVZ_BASE]
        code += b'\xA1' + struct.pack('<I', a.pvz_base)
        # mov eax, [eax + BOARD_OFFSET]  ; eax = Board*
        code += b'\x8B\x80' + struct.pack('<I', a.board_offset)
        # mov edx, FUNC_RELEASE_MOUSE
        code += b'\xBA' + struct.pack('<I', a.func_release_mouse)
        # call edx
        code += b'\xFF\xD2'
        # ret
        code += b'\xC3'
        self._inject_and_execute(bytes(code))

    def win_level(self) -> None:
        """直接通关 — 调用游戏内部 FadeOutLevel 触发本关结束.

        用于跳过 AI 难以胜任的实时小游戏（如坚果保龄球、传送带关卡），
        这些关卡对实时性要求极高，截图→推理→执行周期跟不上。
        所有支持注入的版本均可使用 (GOTY ZH/JA 用 push 约定)。
        """
        logger.info("[注入] FadeOutLevel 直接通关")
        code = _build_fade_out_level_code(self._addrs)
        self._inject_and_execute(bytes(code))
        time.sleep(0.05)

    def choose_card(self, plant_type: int) -> None:
        """选卡界面: 把指定植物加入卡组.

        对应 AsmVsZombies AAsm::ChooseCard (avz_asm.cpp:258-275)。
        传入植物类型 (0~47)，内部计算卡片条目地址后调用游戏函数。
        必须在选卡界面 (game_ui=2) 调用。

        Args:
            plant_type: 植物类型 ID (0=豌豆射手, 1=向日葵, ...)。
        """
        self._require_mouse()
        logger.info("[注入] ChooseCard 植物类型=%s", plant_type)
        a = self._addrs
        code = bytearray()
        # mov eax, [PVZ_BASE]  (eax = PvzBase 对象指针，0x6a9ec0 处即对象本身)
        # 注意: [PVZ_BASE] 已经是 PvzBase 对象，不可再 mov eax,[eax] 二次解引用，
        # 否则会读到对象头部的 vtable 指针，后续 [eax+0x774] 越界访问 .text 段导致崩溃。
        code += b'\xA1' + struct.pack('<I', a.pvz_base)
        # mov eax, [eax + SELECT_CARD_UI_OFFSET]  ; eax = SelectCardUi_p
        code += b'\x8B\x80' + struct.pack('<I', a.select_card_ui_offset)
        # mov edx, plant_type
        code += b'\xBA' + struct.pack('<i', plant_type)
        # shl edx, 4
        code += b'\xC1\xE2\x04'
        # sub edx, plant_type  (edx = plant_type * 15)
        code += b'\x81\xEA' + struct.pack('<i', plant_type)
        # shl edx, 2  (edx = plant_type * 60 = plant_type * 0x3c)
        code += b'\xC1\xE2\x02'
        # add edx, 0xa4
        code += b'\x81\xC2\xA4\x00\x00\x00'
        # add edx, eax  (edx = SelectCardUi_p + 0xa4 + plant_type*0x3c)
        code += b'\x01\xC2'
        # push edx
        code += b'\x52'
        # mov ecx, FUNC_CHOOSE_CARD
        code += b'\xB9' + struct.pack('<I', a.func_choose_card)
        # call ecx
        code += b'\xFF\xD1'
        # ret
        code += b'\xC3'
        self._inject_and_execute(bytes(code))
        time.sleep(0.2)  # 等待选卡动画

    def pick_random_seeds(self) -> None:
        """选卡界面: 随机填满剩余空卡槽.

        对应 AsmVsZombies AAsm::PickRandomSeeds (avz_asm.cpp:887-897)。
        等价于点击选卡界面的"调试试玩"按钮，会随机填充未选的卡槽。
        用于选卡数量不足卡槽上限时补满。
        """
        self._require_mouse()
        logger.info("[注入] PickRandomSeeds 随机填满卡槽")
        a = self._addrs
        code = bytearray()
        # mov eax, [PVZ_BASE]
        code += b'\xA1' + struct.pack('<I', a.pvz_base)
        # mov eax, [eax + SELECT_CARD_UI_OFFSET]  ; eax = SelectCardUi_p
        code += b'\x8B\x80' + struct.pack('<I', a.select_card_ui_offset)
        # push eax
        code += b'\x50'
        # mov ecx, FUNC_PICK_RANDOM_SEEDS
        code += b'\xB9' + struct.pack('<I', a.func_pick_random_seeds)
        # call ecx
        code += b'\xFF\xD1'
        # ret
        code += b'\xC3'
        self._inject_and_execute(bytes(code))
        time.sleep(0.5)  # 等待随机选卡动画完成

    def rock(self) -> None:
        """选卡界面: 开始游戏 (等价点"一起摇摆吧！"按钮).

        对应 AsmVsZombies AAsm::Rock (avz_asm.cpp:111-123)。
        调用后游戏从选卡界面进入战斗。卡组未满时调用可能无效，
        建议先 pick_random_seeds 补满。
        """
        self._require_mouse()
        logger.info("[注入] Rock 开始游戏")
        a = self._addrs
        code = bytearray()
        # mov ebx, [PVZ_BASE]
        code += b'\x8B\x1D' + struct.pack('<I', a.pvz_base)
        # mov ebx, [ebx + SELECT_CARD_UI_OFFSET]  ; ebx = SelectCardUi_p
        code += b'\x8B\x9B' + struct.pack('<I', a.select_card_ui_offset)
        # mov esi, [PVZ_BASE]  (Rock 内部用 esi=PvzBase)
        code += b'\x8B\x35' + struct.pack('<I', a.pvz_base)
        # mov edi, 1
        code += b'\xBF\x01\x00\x00\x00'
        # mov ebp, 1
        code += b'\xBD\x01\x00\x00\x00'
        # mov eax, FUNC_ROCK
        code += b'\xB8' + struct.pack('<I', a.func_rock)
        # call eax
        code += b'\xFF\xD0'
        # ret
        code += b'\xC3'
        self._inject_and_execute(bytes(code))
        time.sleep(0.05)

    def grid_to_pixel(self, row: int, col: int) -> tuple[int, int]:
        """调用游戏内部 GridToAbscissa/Ordinate 获取格子中心像素坐标.

        这比用公式近似计算更准确，特别是屋顶场景的斜坡偏移。

        注意: GridToAbscissa/Ordinate 返回格子左上角坐标，需各 +40 得到
        格子中心 (与 AvZ AGridToCoordinate 一致)。

        Args:
            row: 行号 (0-based)。
            col: 列号 (0-based)。

        Returns:
            (x, y) 格子中心的游戏像素坐标 (800x600 基准)。
        """
        self._require_mouse()
        a = self._addrs
        # 注入 GridToAbscissa
        x_code = _build_grid_to_x_code(row, col, a)
        self._inject_and_execute(bytes(x_code))

        # 注入 GridToOrdinate
        y_code = _build_grid_to_y_code(row, col, a)
        self._inject_and_execute(bytes(y_code))

        # 读取结果
        try:
            x = self._mem.read_int(a.grid_x_result_addr) + 40  # 格子左边缘 → 中心
            y = self._mem.read_int(a.grid_y_result_addr) + 40  # 格子顶部 → 中心
        except Exception:
            # 读取失败，用近似公式兜底
            x = (col + 1) * 80
            y = self._approximate_grid_y(row)
            logger.warning("[注入] GridToOrdinate 读取失败，用近似值 (%s, %s)", x, y)

        return x, y

    def collect_sun_at(self, x: int, y: int) -> None:
        """在游戏坐标位置收集阳光/物品（等于模拟左键点击）."""
        self.mouse_click(int(x), int(y))

    def collect_all_sun(self, item_coords: list[tuple[int, int]]) -> int:
        """批量收集阳光.

        self._require_mouse()
        一次注入批量点击，减少注入次数。

        Args:
            item_coords: 阳光的游戏坐标列表。

        Returns:
            收集的数量。
        """
        self._require_mouse()
        code = bytearray()
        for x, y in item_coords[:8]:
            code += _build_mouse_click_code(x, y, 1, with_ret=False, addrs=self._addrs)

        if code:
            code += b'\xC3'  # ret
            self._inject_and_execute(bytes(code))

        return min(len(item_coords), 8)

    # ------------------------------------------------------------------ #
    #  Hack 开关接口
    # ------------------------------------------------------------------ #

    def set_auto_collect(self, on: bool) -> None:
        """开启/关闭自动收集阳光.

        开启后阳光出现会自动飞向计数器，无需手动点击。
        """
        hack = self._addrs.hacks["auto_collected"]
        if on:
            self.enable_hack("auto_collected", hack)
            logger.info("[注入] 自动收集阳光 已开启")
        else:
            self.disable_hack("auto_collected", hack)
            logger.info("[注入] 自动收集阳光 已关闭")

    def set_main_loop_blocked(self, on: bool) -> None:
        """开启/关闭游戏主循环冻结，用于 Agent 思考期间暂停游戏."""
        hack = self._addrs.hacks["block_main_loop"]
        if on:
            self.enable_hack("block_main_loop", hack)
            logger.info("[注入] 游戏主循环 已冻结")
        else:
            self.disable_hack("block_main_loop", hack)
            logger.info("[注入] 游戏主循环 已恢复")

    def set_unlock_sun_limit(self, on: bool) -> None:
        """开启/关闭阳光上限解锁."""
        hack = self._addrs.hacks["unlock_sun_limit"]
        if on:
            self.enable_hack("unlock_sun_limit", hack)
        else:
            self.disable_hack("unlock_sun_limit", hack)

    def set_placed_anywhere(self, on: bool) -> None:
        """开启/关闭植物任意放置."""
        hack = self._addrs.hacks["placed_anywhere"]
        if on:
            self.enable_hack("placed_anywhere", hack)
        else:
            self.disable_hack("placed_anywhere", hack)

    # ------------------------------------------------------------------ #
    #  内部工具
    # ------------------------------------------------------------------ #

    def _require_mouse(self) -> None:
        """校验当前版本支持模拟鼠标/选卡等高级动作 (仅原版)."""
        if not self._addrs.supports_mouse:
            raise PvZMemoryError(
                f"{self._addrs.version_name} 不支持模拟鼠标/铲除/选卡等高级操作, "
                f"仅原版 1.0.0.1051 EN 可用"
            )

    def _deduct_sun(self, cost: int) -> None:
        """手动扣除阳光.

        PutPlant 内部函数只创建植物对象，不走 UI 逻辑，不扣阳光。
        阳光地址: [[PVZ_BASE] + BOARD_OFFSET] + sun_offset
        """
        try:
            board_ptr = self._mem.read_pointer(self._addrs.pvz_base + self._addrs.board_offset)
            if board_ptr == 0:
                logger.warning("[注入] Board* 为空，无法扣阳光")
                return
            sun_addr = board_ptr + self._mem.offsets.sun
            current_sun = self._mem.read_int(sun_addr)
            new_sun = max(0, current_sun - cost)
            self.write_int(sun_addr, new_sun)
            logger.debug("[注入] 阳光 %s → %s (-%s)", current_sun, new_sun, cost)
        except Exception as exc:
            logger.warning("[注入] 扣除阳光失败: %s", exc)

    def _open_process_for_injection(self) -> int:
        """以注入权限打开 PvZ 进程.

        PvZMemory 只开了 PROCESS_VM_READ + PROCESS_QUERY_INFORMATION，
        注入需要 PROCESS_VM_WRITE + PROCESS_VM_OPERATION + PROCESS_CREATE_THREAD.
        失败时抛错（不回退到只读句柄——只读句柄无写权限，且会被 close() 误关）。
        """
        access = (
            PROCESS_VM_READ |
            PROCESS_VM_WRITE |
            PROCESS_VM_OPERATION |
            PROCESS_CREATE_THREAD |
            PROCESS_QUERY_INFORMATION
        )
        handle = _OpenProcess(access, False, self._mem._pid)
        if not handle:
            error_code = _GetLastError() if _IS_WINDOWS else 0
            raise PvZMemoryError(
                f"无法以注入权限打开 PvZ 进程 (PID={self._mem._pid})，"
                f"请以管理员权限运行。error={error_code}"
            )
        return handle

    @staticmethod
    def _approximate_grid_y(row: int) -> int:
        """近似计算格子 y 坐标 (仅作为 grid_to_pixel 读取失败时的兜底)."""
        return 50 + row * 100 + 40
