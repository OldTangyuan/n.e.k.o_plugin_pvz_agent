"""网格扫描精度检测插件：扫描当前游戏画面，标注植物/僵尸，可视化检查准确度。

用法（游戏处于战斗界面时运行）:
  python -m pvz_agent.grid_scan_debug
  python -m pvz_agent.grid_scan_debug --table
  python -m pvz_agent.grid_scan_debug --pants 0.10 --warm 0.15

功能:
1. 截图 → GridScanner 扫描 5x9 网格，输出每格分类（植物/空）与僵尸行号。
2. 把结果画在截图上：绿色圈=植物，红色横条=僵尸行，保存到 debug/gridscan_*.png。
3. 打印结果，方便肉眼核对与调阈值。

参数（均可选，默认读 config.json）:
  --blue <n>    单格蓝灰占比下限（僵尸判定，大=更严格）
  --dark <n>    暗棕占比下限（僵尸判定辅助，大=更严格）
  --warm <n>    植物黄棕暖色占比下限（大=更少判植物）
  --table       打印完整 5x9 分类表
  --no-occlusion  跳过遮挡检测（测试用）
"""

from __future__ import annotations

import sys
import time

from .config import GridScanConfig, load_config
from .grid_scan import GridScanner
from .window import Capturer, find_target_windows, pick_single


def _parse_args(args: list[str]) -> dict:
    opts = {}
    i = 0
    while i < len(args):
        if args[i] == "--blue" and i + 1 < len(args):
            opts["zombie_blue_min"] = float(args[i + 1])
            i += 1
        elif args[i] == "--dark" and i + 1 < len(args):
            opts["zombie_dark_min"] = float(args[i + 1])
            i += 1
        elif args[i] == "--warm" and i + 1 < len(args):
            opts["warm_plant_min"] = float(args[i + 1])
            i += 1
        elif args[i] in ("--table", "--debug-save"):
            opts[args[i][2:].replace("-", "_")] = True
        elif args[i] == "--no-occlusion":
            opts["occlusion_check"] = False
        i += 1
    return opts


def main() -> None:
    opts = _parse_args(sys.argv[1:])

    cfg = load_config()
    handles = find_target_windows(cfg.window_title_keywords)
    win = pick_single(handles)
    cap = Capturer(win)

    # 扫描配置：允许命令行覆盖阈值
    scan_cfg = GridScanConfig(
        enabled=True,
        zombie_blue_min=opts.get("zombie_blue_min", cfg.grid_scan.zombie_blue_min),
        zombie_dark_min=opts.get("zombie_dark_min", cfg.grid_scan.zombie_dark_min),
        warm_plant_min=opts.get("warm_plant_min", cfg.grid_scan.warm_plant_min),
        occlusion_check=opts.get("occlusion_check", cfg.grid_scan.occlusion_check),
        debug=True,  # 强制保存标注图
    )
    scanner = GridScanner(cfg.layout, scan_cfg)

    print(f"窗口: '{win.title}' 客户区: {win.size}")
    print(f"扫描配置: blue>={scan_cfg.zombie_blue_min} dark>={scan_cfg.zombie_dark_min} "
          f"warm>={scan_cfg.warm_plant_min} 遮挡检测={scan_cfg.occlusion_check}")
    print("正在扫描网格...")
    time.sleep(3)
    try:
        img = cap.grab_pil()
    except Exception as exc:
        print(f"截图失败: {exc}")
        return

    result = scanner.scan(img)

    if result.occluded:
        print("\n[遮挡检测] 屏幕中部草坪占比过低，疑似暂停窗口/结算面板/选卡界面，跳过扫描。")
        print("          请回到战斗界面后重试，或用 --no-occlusion 强制扫描。")
        return

    print("\n检测结果:")
    print(f"  植物 ({len(result.plants)}): {[(r, c) for r, c in result.plants]}")
    print(f"  僵尸行: {result.zombie_rows}")

    if opts.get("table"):
        print("\n5x9 分类表 (P=植物 Z=僵尸行 .=空):")
        grid = [["."] * cfg.layout.cols for _ in range(cfg.layout.rows)]
        for r, c in result.plants:
            grid[r][c] = "P"
        for r in result.zombie_rows:
            for c in range(cfg.layout.cols):
                if grid[r][c] == ".":
                    grid[r][c] = "Z"
        print("     " + "  ".join(str(c) for c in range(cfg.layout.cols)))
        for r, row in enumerate(grid):
            print(f"  r{r}  " + "  ".join(row))

    print("\n标注图已保存到 debug/gridscan_*.png（绿圈=植物，红横条=僵尸行）")
    print("打开标注图核对：绿圈应罩住植物，红横条应盖住有僵尸的行。")
    print("僵尸漏检：调低 --rowpants；空格误判植物：调高 --warm。")


if __name__ == "__main__":
    main()
