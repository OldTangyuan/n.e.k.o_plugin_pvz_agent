"""pytest 共享配置：把 pvz/ 与 pvz/vendor 加入 sys.path（先于测试模块加载）.

兼容仓库布局（<root>/pvz）与宿主安装布局（.../pvz_agent/pvz），两种布局下
pvz/ 都是 tests/ 的祖父/父目录。路径设置集中在这里，让各测试文件的 import
能保持在文件顶部（CI Ruff E402/I001；模板以 --ignore-noqa 运行，noqa 无效）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent, _HERE.parent.parent):
    _pvz_dir = _candidate / "pvz"
    if (_pvz_dir / "vendor" / "pvz_memory").is_dir():
        for _p in (str(_pvz_dir), str(_pvz_dir / "vendor")):
            if _p not in sys.path:
                sys.path.insert(0, _p)
        # 内置 openai 副本（pvz_agent.vlm 依赖 `from openai import OpenAI`）：
        # 运行时由 service.py 挂载；测试同样挂上，省得每个测试文件自己插路径
        _oai_dir = _pvz_dir / "vendor" / "openai_stack"
        if _oai_dir.is_dir() and str(_oai_dir) not in sys.path:
            sys.path.insert(0, str(_oai_dir))
        break
