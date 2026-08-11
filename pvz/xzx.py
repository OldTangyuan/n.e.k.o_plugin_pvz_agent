import win32gui
import win32con
import win32api
import ctypes
from PIL import ImageGrab
import pyautogui
import os
import time
import re

# ---------- 配置 ----------
NORM_WIDTH = 800    # 虚拟画布宽度（用于 vm 命令）
NORM_HEIGHT = 600   # 虚拟画布高度

# ---------- DPI 感知 ----------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except:
    pass

# ---------- 窗口匹配规则 ----------
MATCH_KEYWORDS = [
    "plant vs zombie", "植物大战僵尸", "pvz",
    "杂交版", "plants vs. zombies", "plants vs zombies"
]
EXCLUDE_TITLE_KEYWORDS = ["edge", "microsoft edge", "文件资源管理器", "file explorer", "explorer"]
EXCLUDE_CLASS_PREFIXES = ["CabinetWClass", "ExploreWClass", "Chrome_WidgetWin"]

def is_excluded(title, class_name):
    if title:
        for kw in EXCLUDE_TITLE_KEYWORDS:
            if kw in title.lower():
                return True
    if class_name:
        for prefix in EXCLUDE_CLASS_PREFIXES:
            if class_name.startswith(prefix):
                return True
    return False

def is_target(title):
    if not title:
        return False
    lower = title.lower()
    for kw in MATCH_KEYWORDS:
        if kw in lower:
            return True
    return False

def enum_callback(hwnd, hwnd_list):
    if not win32gui.IsWindowVisible(hwnd):
        return True
    title = win32gui.GetWindowText(hwnd)
    class_name = win32gui.GetClassName(hwnd)
    if not is_excluded(title, class_name) and is_target(title):
        hwnd_list.append(hwnd)
    return True

def find_target_windows():
    hwnd_list = []
    win32gui.EnumWindows(enum_callback, hwnd_list)
    return hwnd_list

def get_client_origin(hwnd):
    """返回客户区左上角屏幕坐标 (left, top) 及客户区宽高 (width, height)"""
    rect = win32gui.GetClientRect(hwnd)
    left_top = win32gui.ClientToScreen(hwnd, (rect[0], rect[1]))
    right_bottom = win32gui.ClientToScreen(hwnd, (rect[2], rect[3]))
    return left_top[0], left_top[1], rect[2], rect[3]

def activate_window(hwnd):
    """将窗口恢复、置顶并激活"""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    win32gui.BringWindowToTop(hwnd)
    time.sleep(0.2)  # 等待窗口激活

def capture_window(hwnd, save_dir="screenshots"):
    """截取客户区并保存"""
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    activate_window(hwnd)
    left, top, width, height = get_client_origin(hwnd)
    bbox = (left, top, left + width, top + height)
    img = ImageGrab.grab(bbox)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    title = win32gui.GetWindowText(hwnd)
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip() or f"hwnd_{hwnd}"
    filename = f"{safe_title}_{timestamp}.png"
    filepath = os.path.join(save_dir, filename)
    img.save(filepath)
    print(f"截图已保存: {filepath}")
    return filepath

def click_at(hwnd, rel_x, rel_y):
    """点击窗口内相对坐标 (rel_x, rel_y)"""
    activate_window(hwnd)
    left, top, width, height = get_client_origin(hwnd)
    if rel_x < 0 or rel_y < 0 or rel_x > width or rel_y > height:
        print(f"警告: 坐标 ({rel_x},{rel_y}) 超出客户区范围 ({width}x{height})")
    abs_x = left + rel_x
    abs_y = top + rel_y
    pyautogui.click(abs_x, abs_y)
    print(f"已点击屏幕绝对坐标 ({abs_x}, {abs_y})  [相对 ({rel_x}, {rel_y})]")

def move_normalized(hwnd, raw_x, raw_y):
    """
    使用归一化坐标 (0~1000) 映射到虚拟画布 (NORM_WIDTH x NORM_HEIGHT)，
    然后移动鼠标到窗口内对应位置。
    """
    activate_window(hwnd)
    # 应用映射公式
    pixel_x = (raw_x / 1000.0) * NORM_WIDTH
    pixel_y = (raw_y / 1000.0) * NORM_HEIGHT
    # 钳位
    pixel_x = max(0, min(NORM_WIDTH, pixel_x))
    pixel_y = max(0, min(NORM_HEIGHT, pixel_y))
    # 转换为整数
    pixel_x = int(round(pixel_x))
    pixel_y = int(round(pixel_y))

    # 获取窗口实际客户区大小，并缩放坐标（若窗口实际尺寸 != NORM，则按比例映射）
    left, top, actual_w, actual_h = get_client_origin(hwnd)
    # 如果实际窗口尺寸与虚拟画布不同，将虚拟坐标按比例映射到实际尺寸
    # 这里有两种策略：直接使用虚拟坐标作为偏移（固定800x600），或按比例缩放。
    # 为更通用，我们按比例映射到实际窗口尺寸：
    scaled_x = (pixel_x / NORM_WIDTH) * actual_w
    scaled_y = (pixel_y / NORM_HEIGHT) * actual_h
    abs_x = left + int(round(scaled_x))
    abs_y = top + int(round(scaled_y))

    pyautogui.moveTo(abs_x, abs_y)
    print(f"已移动鼠标到屏幕绝对坐标 ({abs_x}, {abs_y})  [归一化 ({raw_x},{raw_y}) -> 虚拟 ({pixel_x},{pixel_y})]")

def main():
    print("正在扫描窗口...")
    hwnds = find_target_windows()
    if not hwnds:
        print("未找到匹配窗口。")
        return

    print(f"找到 {len(hwnds)} 个匹配窗口:")
    for i, hwnd in enumerate(hwnds):
        title = win32gui.GetWindowText(hwnd)
        print(f"  {i+1}. 句柄: {hwnd}, 标题: '{title}'")
    print("请输入序号选择要操作的窗口 (默认 1):", end=" ")
    choice = input().strip()
    if choice == "":
        idx = 0
    else:
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(hwnds):
                print("序号无效，默认使用第一个。")
                idx = 0
        except ValueError:
            print("输入非数字，默认使用第一个。")
            idx = 0
    hwnd = hwnds[idx]
    title = win32gui.GetWindowText(hwnd)
    print(f"已选择窗口: '{title}' (句柄 {hwnd})")

    # 初始截图
    capture_window(hwnd)

    print("\n命令说明:")
    print("  m x,y   - 点击窗口内相对坐标 (x,y)，原点为客户区左上角")
    print("  vm x,y  - 使用归一化坐标 (0~1000) 移动鼠标到窗口内对应位置")
    print("  s       - 重新截图并保存")
    print("  q       - 退出程序")
    print("  h       - 显示本帮助")
    print("示例: m 150,300  或  vm 500,500")

    while True:
        cmd = input("\n> ").strip().lower()
        if cmd == "q":
            print("退出程序。")
            break
        elif cmd == "h":
            print("命令说明:")
            print("  m x,y   - 点击窗口内相对坐标 (x,y)，原点为客户区左上角")
            print("  vm x,y  - 使用归一化坐标 (0~1000) 移动鼠标到窗口内对应位置")
            print("  s       - 重新截图并保存")
            print("  q       - 退出程序")
            print("  h       - 显示本帮助")
            continue
        elif cmd == "s":
            capture_window(hwnd)
            continue
        elif cmd.startswith("m") or cmd.startswith("vm"):
            # 提取数字和逗号
            parts = re.sub(r'[^\d,.\-]', '', cmd[2:]) if cmd.startswith("vm") else re.sub(r'[^\d,.\-]', '', cmd[1:])
            if not parts:
                print("格式错误，请使用: m x,y 或 vm x,y")
                continue
            coords = parts.split(',')
            if len(coords) != 2:
                print("格式错误，请使用: m x,y 或 vm x,y")
                continue
            try:
                x = float(coords[0].strip())
                y = float(coords[1].strip())
                x_int = int(round(x))
                y_int = int(round(y))
                if cmd.startswith("vm"):
                    # 检查范围
                    if not (0 <= x_int <= 1000 and 0 <= y_int <= 1000):
                        print("归一化坐标范围应为 0~1000，已自动钳位。")
                    move_normalized(hwnd, x_int, y_int)
                else:
                    click_at(hwnd, x_int, y_int)
            except ValueError:
                print("坐标必须为数字，请重试。")
        else:
            print("未知命令，输入 h 查看帮助。")

if __name__ == "__main__":
    pyautogui.FAILSAFE = True
    main()