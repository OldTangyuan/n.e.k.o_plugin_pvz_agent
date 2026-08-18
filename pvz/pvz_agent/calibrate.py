"""交互式格子校准工具：移动鼠标确认落点、截图叠网格预览、采集真实点拟合写回 config.json。

用法（游戏窗口在战斗界面时运行）:
  python -m pvz_agent.calibrate grid       # 校准战斗格子
  python -m pvz_agent.calibrate cards      # 只校准卡片栏
  python -m pvz_agent.calibrate            # 全部

核心命令（终端输入）:
  m <row> <col>    把鼠标移到 格子(row,col) 的【预测中心】——你看落点是否对准真实格子中心
  p                截图并叠加 5x9 网格线显示，直观对比预测网格 vs 真实游戏网格
  g <row> <col>    采集当前鼠标位置为 格子(row,col) 的真实中心
  mcard <index>    把鼠标移到 战斗卡槽 卡片[index] 的预测中心
  card <index>     采集当前鼠标位置为 卡片[index] 的真实中心
  mshovel          把鼠标移到 铲子按钮 的预测中心
  shovel           采集当前鼠标位置为 铲子按钮 的真实中心
  fit              用已采点最小二乘拟合，写回 config.json（带合理性校验）
  test <row> <col> 拟合后把鼠标移到该格，验证是否对准（可再微调）
  list             显示已采集的点
  undo             删除最后一个采集点
  q                退出

典型流程:
  1. 先 m 0 0 看预测落点，再 m 0 8 / m 4 0 / m 4 8 / m 2 4 大致确认偏差方向。
  2. 对每个关键格：把鼠标移到真实中心 → g <row> <col> 采集。
  3. 采完 4~5 个点后 fit 写回。
  4. 用 test <row> <col> 逐个验证；不准就 g 重新采对应点再 fit。
  5. 卡片栏：mcard 0 看落点 → 手动挪到真实卡中心 card 0 采集 → 同理 card 1/2 → fit。
  6. 铲子：mshovel 看落点 → 手动挪到真实铲子 shovel 采集 → fit。

坐标系说明：pyautogui 用【屏幕绝对坐标】（含窗口在屏幕上的偏移）。程序移动
鼠标与读取鼠标位置都用绝对坐标，二者一致所以点击落点正确。命令会同时显示
屏幕绝对坐标与窗口内相对坐标（= 绝对 - 窗口左上角）。PvZ 草坪是 5 行 x 9 列
（row 0~4, col 0~8）。拟合结果有合理性校验，超范围拒绝写回。
"""

from __future__ import annotations

import json
import os
import sys
import time

import pyautogui
from PIL import ImageDraw

from .config import CONFIG_FILE, load_config
from .executor import Executor
from .window import Capturer, find_target_windows, pick_single

# 校准关键点（5x9，四角 + 中心）
KEY_GRID = [(0, 0), (0, 8), (4, 0), (4, 8), (2, 4)]
KEY_CARD = [0, 1, 2]


def _write_config(mutator) -> None:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    mutator(cfg)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """最小二乘 y = k*x + b，返回 (k, b)。"""
    n = len(xs)
    if n < 2:
        return 1.0, 0.0
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return 1.0, 0.0
    k = (n * sxy - sx * sy) / denom
    b = (sy - k * sx) / n
    return k, b


def _show_grid_preview(ex: Executor, cfg) -> None:
    """截图 + 叠加 5x9 网格线 + 保存并打印路径（用户可打开查看偏差）。"""
    cap = Capturer(ex.win)
    try:
        img = cap.grab_pil()
    except Exception as exc:
        print(f"[预览] 截图失败: {exc}")
        return
    draw = ImageDraw.Draw(img)
    rows, cols = cfg.layout.rows, cfg.layout.cols
    # 画每格中心十字 + 网格线
    for r in range(rows):
        for c in range(cols):
            vx, vy = ex.grid_center(r, c)
            ax, ay = ex._to_screen(vx, vy)
            # 相对客户区坐标（画在截图上）
            left, top, _, _ = ex.win.client_rect
            rx, ry = ax - left, ay - top
            draw.line([(rx - 15, ry), (rx + 15, ry)], fill=(255, 0, 0), width=2)
            draw.line([(rx, ry - 15), (rx, ry + 15)], fill=(255, 0, 0), width=2)
            draw.text((rx + 5, ry - 20), f"{r},{c}", fill=(0, 200, 255))
    # 网格外框
    for r in range(rows):
        vx0, vy0 = ex.grid_center(r, 0)
        vx1, vy1 = ex.grid_center(r, cols - 1)
        left, top, _, _ = ex.win.client_rect
        ax0, ay0 = ex._to_screen(vx0, vy0)
        ax1, ay1 = ex._to_screen(vx1, vy1)
        draw.line([(ax0 - left, ay0 - top), (ax1 - left, ay1 - top)], fill=(0, 255, 0), width=1)
    os.makedirs("debug", exist_ok=True)
    path = os.path.join("debug", f"grid_preview_{time.strftime('%Y%m%d_%H%M%S')}.png")
    img.save(path)
    print(f"[预览] 已保存网格叠加图: {path}")
    print("[预览] 红色十字 = 预测格子中心，绿线 = 每行中心连线。")
    print("[预览] 请打开该图，对比红色十字是否落在真实格子的中心。")


def _move_to_cell(ex, cfg, row: int, col: int) -> None:
    """把鼠标移到格子(row,col)的预测中心。

    坐标系说明：pyautogui 用的是【屏幕绝对坐标】（含窗口在屏幕上的偏移）。
    程序移动鼠标和读取鼠标位置都是绝对坐标，二者一致，因此点击落点正确。
    但"绝对坐标"不等于"窗口内坐标"——窗口内坐标 = 绝对坐标 - 窗口左上角。
    """
    if not (0 <= row < cfg.layout.rows and 0 <= col < cfg.layout.cols):
        print(f"  row/col 越界: 合法 行0~{cfg.layout.rows-1} 列0~{cfg.layout.cols-1}")
        return
    vx, vy = ex.grid_center(row, col)
    ax, ay = ex._to_screen(vx, vy)
    left, top, _, _ = ex.win.client_rect
    ex.win.ensure_foreground()
    pyautogui.moveTo(ax, ay, duration=0.3)
    print(f"  已移动鼠标到 格子({row},{col}) 预测中心")
    print(f"    屏幕绝对坐标: ({ax},{ay})")
    print(f"    窗口内相对坐标: ({ax-left},{ay-top})  （窗口左上角 {left},{top}）")
    print(f"  若落点不在真实格子中心 → 手动把鼠标挪到真实中心，然后 g {row} {col} 采集")


def _fit_grid(ex, cfg, grid_real: list) -> None:
    """用已采点拟合 grid 参数并写回。"""
    if len(grid_real) < 3:
        print(f"  采集点不足（{len(grid_real)}<3），无法拟合。请先采集至少 3 个格子。")
        return
    left, top, right, bottom = ex.win.client_rect
    cw = max(right - left, 1)
    ch = max(bottom - top, 1)
    scale_x = cw / cfg.layout.canvas_w
    scale_y = ch / cfg.layout.canvas_h

    # 列方向
    col_xs = [c for _, c, _, _ in grid_real]
    col_sx = [x for _, _, x, _ in grid_real]
    k_col, b_col = _linear_fit(col_xs, col_sx)
    col_w1 = k_col / scale_x if abs(scale_x) > 1e-9 else cfg.layout.col_width
    col_w2 = (b_col - left) / scale_x if abs(scale_x) > 1e-9 else cfg.layout.col_width
    new_col_w = round((col_w1 + col_w2) / 2, 1)

    # 行方向
    row_xs = [r for r, _, _, _ in grid_real]
    row_sy = [y for _, _, _, y in grid_real]
    k_row, b_row = _linear_fit(row_xs, row_sy)
    new_row_h = round(k_row / scale_y, 1) if abs(scale_y) > 1e-9 else cfg.layout.grid_row_h
    new_grid_top = round((b_row - top) / scale_y, 1) if abs(scale_y) > 1e-9 else cfg.layout.grid_top

    print(f"\n拟合结果: grid_top={new_grid_top}  grid_row_h={new_row_h}  col_width={new_col_w}")
    print(f"  （原值: grid_top={cfg.layout.grid_top} grid_row_h={cfg.layout.grid_row_h} col_width={cfg.layout.col_width}）")

    if not (0 <= new_grid_top <= cfg.layout.canvas_h and 10 <= new_row_h <= 300 and 30 <= new_col_w <= 200):
        print("⚠ 拟合结果超出合理范围，疑似采到错误位置。已取消写回，请检查采集点后重试。")
        return

    def mutate(c):
        c["layout"]["grid_top"] = new_grid_top
        c["layout"]["grid_row_h"] = new_row_h
        c["layout"]["col_width"] = new_col_w
    _write_config(mutate)
    print("已写回 config.json。")


def _move_to_card(ex, cfg, index: int) -> None:
    """把鼠标移到卡片[index]的预测中心（战斗卡槽）。"""
    vx, vy = ex.card_center(index)
    ax, ay = ex._to_screen(vx, vy)
    left, top, _, _ = ex.win.client_rect
    ex.win.ensure_foreground()
    pyautogui.moveTo(ax, ay, duration=0.3)
    print(f"  已移动鼠标到 卡片[{index}] 预测中心")
    print(f"    屏幕绝对坐标: ({ax},{ay})")
    print(f"    窗口内相对坐标: ({ax-left},{ay-top})")
    print(f"  若落点不在真实卡片中心 → 手动挪到真实中心，然后 card {index} 采集")


def _move_to_shovel(ex, cfg) -> None:
    """把鼠标移到铲子按钮的预测中心。"""
    vx, vy = cfg.layout.shovel_pos
    ax, ay = ex._to_screen(vx, vy)
    left, top, _, _ = ex.win.client_rect
    ex.win.ensure_foreground()
    pyautogui.moveTo(ax, ay, duration=0.3)
    print("  已移动鼠标到 铲子按钮 预测中心")
    print(f"    屏幕绝对坐标: ({ax},{ay})")
    print(f"    窗口内相对坐标: ({ax-left},{ay-top})")
    print("  若落点不在真实铲子中心 → 手动挪到真实中心，然后 shovel 采集")


def _fit_cards(ex, cfg, card_real: list) -> None:
    if len(card_real) < 2:
        print(f"  卡片采集点不足（{len(card_real)}<2）。")
        return
    left, top, right, bottom = ex.win.client_rect
    cw = max(right - left, 1)
    ch = max(bottom - top, 1)
    scale_x = cw / cfg.layout.canvas_w
    scale_y = ch / cfg.layout.canvas_h

    # x 方向：card_left + idx*card_step
    idxs = [i for i, _, _ in card_real]
    xs = [x for _, x, _ in card_real]
    k_step, b_left = _linear_fit(idxs, xs)
    new_step = round(k_step / scale_x, 1) if abs(scale_x) > 1e-9 else cfg.layout.card_step
    new_left = round((b_left - left) / scale_x, 1) if abs(scale_x) > 1e-9 else cfg.layout.card_left

    # y 方向：card_top（取采集点的 y 平均值，转回虚拟画布）
    ys = [y for _, _, y in card_real]
    avg_y = sum(ys) / len(ys)
    new_top = round((avg_y - top) / scale_y, 1) if abs(scale_y) > 1e-9 else cfg.layout.card_top

    print(f"\n卡片拟合: card_left={new_left}  card_step={new_step}  card_top={new_top}")
    print(f"  （原值: card_left={cfg.layout.card_left} card_step={cfg.layout.card_step} card_top={cfg.layout.card_top}）")
    if not (0 <= new_left <= cfg.layout.canvas_w and 20 <= new_step <= 120 and 0 <= new_top <= cfg.layout.canvas_h):
        print("⚠ 卡片拟合超出合理范围，已取消写回。")
        return
    def mutate(c):
        c["layout"]["card_left"] = new_left
        c["layout"]["card_step"] = new_step
        c["layout"]["card_top"] = new_top
    _write_config(mutate)
    print("已写回 config.json。")


def _fit_shovel(ex, cfg, shovel_real: list) -> None:
    """写回铲子按钮位置（单点，直接取采集值）。"""
    if not shovel_real:
        print("  铲子采集点为空，跳过。")
        return
    x, y = shovel_real[-1]
    left, top, _, _ = ex.win.client_rect
    vx = (x - left) / (cfg.layout.canvas_w / max(ex.win.size[0], 1)) if ex.win.size[0] else 0
    vy = (y - top) / (cfg.layout.canvas_h / max(ex.win.size[1], 1)) if ex.win.size[1] else 0
    new_pos = [round(vx, 1), round(vy, 1)]
    print(f"\n铲子拟合: shovel_pos={new_pos}（原值 {list(cfg.layout.shovel_pos)}）")
    if not (0 <= new_pos[0] <= cfg.layout.canvas_w and 0 <= new_pos[1] <= cfg.layout.canvas_h):
        print("⚠ 铲子位置超出合理范围，已取消写回。")
        return
    def mutate(c):
        c["layout"]["shovel_pos"] = new_pos
    _write_config(mutate)
    print("已写回 config.json。")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    cfg = load_config()
    handles = find_target_windows(cfg.window_titles)
    win = pick_single(handles)
    ex = Executor(win, cfg.layout)

    print(f"窗口: '{win.title}' 客户区尺寸: {win.size}")
    print(f"草坪: {cfg.layout.rows}行 x {cfg.layout.cols}列（row 0~{cfg.layout.rows-1}, col 0~{cfg.layout.cols-1}）")
    print("\n命令说明（输入 help 看完整帮助）:")
    print("  m <row> <col>  移动鼠标到格子预测中心（看落点是否对准）")
    print("  p              截图叠加网格线预览")
    print("  g <row> <col>  采集当前鼠标位置为格子真实中心")
    print("  mcard <index>  移动鼠标到 卡片[index] 预测中心（战斗卡槽）")
    print("  card <index>   采集当前鼠标位置为 卡片[index] 真实中心")
    print("  mshovel        移动鼠标到 铲子按钮 预测中心")
    print("  shovel         采集当前鼠标位置为 铲子按钮 真实中心")
    print("  fit            拟合写回 config.json（格子/卡片/铲子）")
    print("  test <r> <c>   拟合后验证某格是否对准")
    print("  list / undo / q")

    grid_real: list = []
    card_real: list = []
    shovel_real: list = []

    while True:
        try:
            line = input("\ncal> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[退出]")
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("q", "quit", "exit"):
            print("[退出]")
            break
        elif cmd in ("h", "help", "?"):
            print(__doc__)
        elif cmd == "m" and len(args) >= 2:
            try:
                _move_to_cell(ex, cfg, int(args[0]), int(args[1]))
            except ValueError:
                print("  用法: m <row> <col>，如 m 2 4")
        elif cmd == "p":
            _show_grid_preview(ex, cfg)
        elif cmd == "g" and len(args) >= 2:
            try:
                r, c = int(args[0]), int(args[1])
                if not (0 <= r < cfg.layout.rows and 0 <= c < cfg.layout.cols):
                    print(f"  row/col 越界: 合法 行0~{cfg.layout.rows-1} 列0~{cfg.layout.cols-1}")
                    continue
                mx, my = pyautogui.position()
                left, top, _, _ = ex.win.client_rect
                grid_real.append((r, c, mx, my))
                print(f"  已采集 格子({r},{c})")
                print(f"    屏幕绝对: ({mx},{my})  窗口内相对: ({mx-left},{my-top})")
                print(f"    共 {len(grid_real)} 点")
            except ValueError:
                print("  用法: g <row> <col>，如 g 4 8")
        elif cmd == "mcard" and len(args) >= 1:
            try:
                _move_to_card(ex, cfg, int(args[0]))
            except ValueError:
                print("  用法: mcard <index>，如 mcard 0")
        elif cmd == "mshovel":
            _move_to_shovel(ex, cfg)
        elif cmd == "card" and len(args) >= 1:
            try:
                i = int(args[0])
                mx, my = pyautogui.position()
                left, top, _, _ = ex.win.client_rect
                card_real.append((i, mx, my))   # 存 x 和 y，便于拟合 card_top
                print(f"  已采集 卡片[{i}]")
                print(f"    屏幕绝对: ({mx},{my})  窗口内相对: ({mx-left},{my-top})")
                print(f"    共 {len(card_real)} 点")
            except ValueError:
                print("  用法: card <index>，如 card 0")
        elif cmd == "shovel":
            mx, my = pyautogui.position()
            left, top, _, _ = ex.win.client_rect
            shovel_real.append((mx, my))
            print("  已采集 铲子按钮")
            print(f"    屏幕绝对: ({mx},{my})  窗口内相对: ({mx-left},{my-top})")
        elif cmd == "fit":
            if mode in ("grid", "all"):
                _fit_grid(ex, cfg, grid_real)
            if mode in ("cards", "all"):
                _fit_cards(ex, cfg, card_real)
            if mode in ("all", "tools"):
                _fit_shovel(ex, cfg, shovel_real)
        elif cmd == "test" and len(args) >= 2:
            try:
                _move_to_cell(ex, cfg, int(args[0]), int(args[1]))
                print("  看落点是否对准。不准就 g 重新采该格，再 fit。")
            except ValueError:
                print("  用法: test <row> <col>")
        elif cmd == "list":
            print("  已采集格子点:")
            for r, c, x, y in grid_real:
                print(f"    ({r},{c}) → ({x},{y})")
            print("  已采集卡片点:")
            for i, x, y in card_real:
                print(f"    [{i}] → ({x},{y})")
            print("  已采集铲子点:")
            for x, y in shovel_real:
                print(f"    shovel → ({x},{y})")
        elif cmd == "undo":
            if shovel_real:
                shovel_real.pop()
                print(f"  已删除最后一个铲子点（剩 {len(shovel_real)}）")
            elif grid_real:
                grid_real.pop()
                print(f"  已删除最后一个格子点（剩 {len(grid_real)}）")
            elif card_real:
                card_real.pop()
                print(f"  已删除最后一个卡片点（剩 {len(card_real)}）")
            else:
                print("  没有可删除的点")
        else:
            print(f"  未知命令: {line}（输入 help 看帮助）")


if __name__ == "__main__":
    main()
