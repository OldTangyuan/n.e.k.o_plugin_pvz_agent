# pvz/vendor/ — 插件内置依赖

本目录的依赖随插件分发，`service.py` 在导入时按需挂到 `sys.path`，宿主无需安装。

| 目录 | 内容 | 挂载方式 |
|---|---|---|
| `cv2/` | OpenCV（vision 模式扫描用） | 无条件插入 `sys.path` 最前 |
| `pvz_memory/` | PvZ 内存读取/注入（纯 ctypes，零第三方依赖） | 无条件 |
| `pywin32/` | `win32gui.pyd` / `win32process.pyd` / `win32con.py` | 宿主缺 win32gui 时按绝对路径 spec 加载 |
| `pyautogui_stack/` | pyautogui 及其依赖（pygetwindow/**pyrect**/pyscreeze/pymsgbox/pytweening/mouseinfo/pyperclip） | 宿主缺 pyautogui 时按依赖序 spec 加载 |
| `pillow/` | Pillow 11.3.0 完整 PIL | 宿主 PIL 残缺（命名空间包）时 spec 加载 |
| `openai_stack/` | openai 2.8.1 及配套依赖（httpx/httpcore/h11/anyio/sniffio/distro/jiter/certifi/pydantic/pydantic_core/tqdm/idna/annotated_types/typing_extensions/typing_inspection） | 宿主缺 openai 时按依赖序 spec 加载（pydantic 与 pydantic_core 成套） |

## 挂载机制（v0.2.8 起）

service.py 导入时用 `find_spec`/试导入探测宿主环境；缺失/残缺的库按依赖序用
`importlib.util.spec_from_file_location` **按绝对路径加载并注册进 `sys.modules`**。
import 系统永远先查 `sys.modules`，因此不依赖 sys.path 的查找顺序——宿主子进程
如何重排或清理导入路径都不影响（v0.2.7 的 sys.path 挂载方案在 Steam 版宿主
的插件子进程中未生效，v0.2.8 改为本机制）。健康宿主探测全部命中，内置副本
不参与导入。

## 为什么是"按需兜底"而不是无条件挂载

- `pyd`/`dll` 与解释器版本强相关（本目录的 pywin32/pydantic_core 取自 CPython 3.11）。
  健康宿主优先使用自带的同名库；只有缺失/加载失败时才用内置副本，避免版本错配。
- `openai_stack` 是成套拷贝（pydantic 纯 Python 部分 + pydantic_core 必须同版本），
  兜底挂载时插入 `sys.path` 最前，保证插件子进程内成套自洽。

## 已知限制

- `pywin32/` 只含 `win32gui`/`win32process`/`win32con` 三个缺失模块；`win32api`、
  `pywintypes311.dll` 等仍使用宿主自带副本（Steam 版宿主的 `resources/bin` 里有）。
  若宿主连这些也缺，需补全 pywin32。
- vision 模式（`mode="vision"`）还需要 numpy<2（Steam 版宿主内置 numpy 2.x 与
  vendored cv2 不兼容）；text 模式（默认）完全不依赖 cv2/numpy，不受影响。

## 来源与许可

- pywin32（win32gui/win32process/win32con）— PSF License，取自同版本 Python 3.11 环境
- pyautogui / pygetwindow / pyscreeze / pymsgbox / pytweening / mouseinfo / pyperclip — BSD-3
- openai 2.8.1 — Apache-2.0；httpx — BSD-3；anyio — MIT；pydantic — MIT
