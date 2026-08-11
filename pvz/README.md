# PvZ 双 Agent 自动化

给 `xzx.py` 配上双 Agent，使其能**自动完成《植物大战僵尸》关卡**（纯 VLM，不读内存）。

## 架构

```
主循环 (pvz_agent/main.py)
   │
   ├─ 拍一张客户区截图（同一帧供两个 Agent 复用）
   ├─ 并发调用 Agent B (planner) 与 Agent A (narrator)（两个线程同时发 VLM 请求）
   │    ├─ Agent B 截图 + 文本历史 → <tool_call> 动作 → executor 执行
   │    └─ Agent A 截图 → 中文现状描述（仅打印给用户，不传给 B）
   ├─ 执行反馈回填给 Agent B 的历史
   └─ sleep(节拍 × speed)
用户调控 (pvz_agent/controller.py)：后台 stdin 线程解析命令 + F12 急停热键
```

- **两个 Agent 互相独立**：A 的描述绝不喂给 B，B 的动作决策不喂给 A，二者唯一共享的是同一帧截图与系统级配置。
- **A/B 并发调用**：两个 VLM 请求同时发出，单轮总耗时 = max(A, B) 而非 A+B，A 的描述耗时被 B 的规划+执行掩盖。
- **纯 VLM**：不读游戏内存、不做代码注入。模型靠看截图估阳光/冷却/僵尸，坐标由 executor 按虚拟画布换算，无需模型猜像素。

## 快速开始

1. 复制 `.env.example` 为 `.env`，填写远程 OpenAI 兼容接口：
   ```
   VLM_BASE_URL=https://api.example.com/v1
   VLM_MODEL=your-model-name
   VLM_API_KEY=sk-xxxxxxxx
   ```
2. 启动游戏（杂交版/原版均可，窗口标题需含"植物大战僵尸"/"pvz"等关键词，可改 `config.json`）。
3. 运行：
   ```bash
   LLM_PvZ_Player-main\venv\Scripts\python.exe -m pvz_agent.main
   ```

## 用户命令

| 命令 | 作用 |
|------|------|
| `pause` / `resume` | 暂停/恢复自动执行（暂停时仍可 `describe`/`screenshot` 观察） |
| `describe` | 立即要一份 Agent A 的现状描述 |
| `screenshot` | 截图保存 |
| `speed <N>` | 设置每轮间隔倍率（如 `speed 2` = 间隔翻倍） |
| `goal <文本>` | 设定/修改当前目标 |
| `h` | 帮助 |
| `quit` | 退出 |
| **F12** | 全局急停热键（无人值守安全网，pyautogui FAILSAFE 兜底） |

## 模块

| 模块 | 职责 |
|------|------|
| `config.py` | 配置加载（`.env` 密钥 + `config.json` 布局/行为） |
| `window.py` | 窗口查找/激活/客户区实时坐标/截图 |
| `vlm.py` | VLM 客户端（openai SDK，图片消息 + 重试） |
| `parser.py` | `<tool_call>` 提取 + JSON 解析 |
| `executor.py` | 语义动作（place_plant/shovel 等）+ 通用 GUI 动作 → pyautogui |
| `prompts.py` | 两套 system prompt（观察者 / 规划执行者） |
| `narrator.py` | **Agent A**：截图 → 现状描述 |
| `planner.py` | **Agent B**：截图+历史 → 动作，维护文本历史 |
| `controller.py` | 用户调控：命令 + 急停热键 |
| `sun.py` | **OpenCV 自动收集阳光**（无需 LLM 判断） |
| `grid_scan.py` | **OpenCV 网格扫描**：检测植物/僵尸坐标注入 Agent B |
| `grid_scan_debug.py` | **网格扫描精度检测插件**：可视化标注 |
| `main.py` | 主循环调度与组装 |

## 坐标体系

- 所有 PvZ 语义坐标基于 **800x600 虚拟画布**（`config.json` → `layout`）。
- 战斗网格 5 行 x 9 列，`place_plant(card_index, row, col)` 0-based（row 0~4, col 0~8）。
- executor 用 `scale_x = 实际宽/800`、`scale_y = 实际高/600` **分轴**缩放到客户区——自适应原版/杂交版不同窗口尺寸。
- 非战斗 UI（主菜单/弹窗/选卡）用 `computer_use.left_click` 相对坐标 [0,1000]，模型看图估算。

## 已知局限

- 不读内存 → 阳光/冷却/僵尸血量只能看图估算，节奏比读内存方案慢。
- 战斗胜利判定依赖 Agent B 识别胜利画面并输出 `terminate`；必要时用户可 `pause` 手动收尾。
- 高频实时小游戏（传送带/保龄球）AI 可能跟不上，prompt 已提示模型判断难赢时用 `terminate(failure)` 放弃。

## 自动收集阳光（OpenCV，独立线程，无需 LLM）

`sun.py` 用**独立后台线程**按 `scan_interval`（默认 0.35s）高频截图→检测→点击收集草坪上掉落的阳光，不经过 LLM，不阻塞 Agent 主循环：

- **识别**（基于真实阳光贴图 `太阳.gif` 校准）：截图归一化到 800x600 虚拟画布 → HSV 掩膜 → 形态学 → 轮廓过滤 → 圆心即点击点。
- **主体特征 = 低饱和奶油黄**：真实 PvZ 阳光整体是极亮的低饱和奶油黄（S 均值≈49，RGB≈(255,255,160)，V≈255），不是高饱和金黄。掩膜范围 H(19~40) + S(`s_min`~`s_max`) + V(`v_min`)。纯黄文字 (255,200,60) 的 S≈195 高于 `s_max`，被排除。
- **三重形状过滤**（剔除文字/横幅）：
  1. **长宽比** ≤ 2.2 — 阳光近正方形，文字横条/长条被剔；
  2. **圆度** ≥ `min_circularity` — 面积/外接圆面积，字母碎片/散点被剔；
  3. **实心率** ≥ 0.4 — 轮廓面积/外接矩形面积，空心/稀疏文字被剔。
- **深黄表情特征**：阳光中心有一块高饱和深黄表情（眼睛/嘴，S≈255），中心圆（0.55×R）内高饱和像素占比在 5%~55% 之间——**纯色文字内部均匀，无"低饱和主体+高饱和表情"的混合结构，被剔除**。
- **排除种植栏**：检测区域限定在 `layout.grid_top`（网格顶边）以下的草坪。
- **防重复**：每个阳光位置有 1.5s 点击冷却，同一阳光不会被反复点击。
- **鼠标互斥**：与 Agent B 共享一把 `mouse_lock`——Agent B 的每个动作（尤其 `place_plant` 的点卡片→点格子两步）整个持锁执行，阳光线程在 Agent B 动作期间不插队，避免取消卡片选中或抢鼠标。

配置在 `config.json` 的 `"sun"`：
```json
"sun": {
  "enabled": true,          // 总开关
  "thread_enabled": true,   // 独立线程高频扫描（false 则退回主循环同步收集）
  "scan_interval": 0.35,    // 扫描间隔（秒），越小收得越快、CPU 越高
  "min_radius": 9.0,        // 阳光最小半径（虚拟画布像素）
  "max_radius": 42.0,       // 最大半径（含光晕）
  "min_circularity": 0.5,   // 最小圆度
  "max_click_per_scan": 8,  // 每次扫描最多点击数
  "click_gap": 0.2,         // 连续点击间隔
  "s_min": 25,              // 阳光主体最小饱和度（低饱和奶油黄特征）
  "s_max": 170,             // 阳光主体最大饱和度（纯黄文字 S≈195 被排除）
  "v_min": 200,             // 阳光主体最小亮度（阳光极亮 V≈255）
  "debug": false            // true 时保存检测调试图到 debug/
}
```
若检测不准（漏收/误收），先开 `"debug": true` 跑一轮，看 `debug/sun_*.png` 里圈出的位置对不对，再微调 `min_radius` / `max_radius` / `min_circularity`。收得太慢可调小 `scan_interval`。

## OpenCV 网格扫描（植物精确坐标 + 僵尸行号辅助）

`grid_scan.py` 用 OpenCV 检测 5x9 网格中的植物与僵尸，输出给 Agent B 作为辅助信息：

```
【OpenCV检测】植物: (0,0), (2,4), (3,6)；僵尸: 行2, 行3
```

- **识别策略（v5，用户指定：植物给精确坐标，僵尸只需行号）**：
  1. **遮挡检测**：先看屏幕中部草坪占比——暂停窗口/结算面板/选卡界面等大物体会盖住中部大块草坪，占比过低直接跳过扫描，避免把弹窗误判成僵尸/植物。
  2. **植物（精确坐标，中心采样）**：格子中心有黄棕暖色主体（向日葵等）或草坪占比低（含物）→ `(row,col)` 精确坐标。空格 = 中心草坪占比高且无暖色/无蓝灰——**阴影/网格线只是变暗的草坪，仍算空，不再误判植物**。
  3. **僵尸（只报行号，整行宽松）**：僵尸衣裤 = **蓝灰**（H90~130 蓝、S≥60、V<180、非草坪）、身体 = **暗棕**（暗且非草坪绿）。行内任一格蓝灰占比 ≥ `zombie_blue_min`、或暗棕 ≥ `zombie_dark_min` 且伴随蓝灰、或相邻两格蓝灰均 ≥ `zombie_cross_min`（**跨界僵尸**）→ 该行为僵尸行。**僵尸姿态/被弹幕遮挡的变化不影响整行检出，放弃不稳定的僵尸精确坐标**。
  4. 僵尸行的格子若有明确蓝灰特征，不列为植物（避免僵尸身体被误判成相邻两个植物）。
- 已在 3 张用户标注真值图验证：235218 僵尸行 [2,3]、235254 僵尸行 [1,2,3,4]、223255 僵尸行 [2,3] **全部精确命中 0 误报**；植物 5/5~6/6 精确命中。
- **不识别具体植物/僵尸类型**（OpenCV 难鲁棒区分），只判"有无"。
- 植物坐标与 executor 完全一致（`x=(col+1)*80, y=grid_top+row*grid_row_h`）。

配置 `config.json` → `"grid_scan"`：
```json
"grid_scan": {
  "enabled": true,
  "sample_radius": 20,        // 格子中心采样半径（聚焦主体）
  "lawn_h_min": 40,           // 草坪 HSV 范围（由 data/grass.png 模板校准）
  "lawn_h_max": 75,
  "lawn_s_min": 150,
  "lawn_v_min": 120,
  "lawn_min_ratio": 0.85,     // 中心草坪占比 ≥ 此 → 候选空格（阴影也算空）
  "lawn_contain_max": 0.60,   // 中心草坪占比 < 此 → 必含物
  "warm_plant_min": 0.15,     // 黄棕暖色占比 ≥ 此 → 植物（向日葵等主体）
  "zombie_blue_h_lo": 90,     // 僵尸衣裤蓝灰色相范围（蓝）
  "zombie_blue_h_hi": 130,
  "zombie_blue_s_min": 60,
  "zombie_blue_v_max": 180,
  "zombie_blue_min": 0.03,    // 单格蓝灰占比 ≥ 此 → 僵尸格/行
  "zombie_dark_min": 0.15,    // 单格暗棕占比 ≥ 此 且 有蓝灰 → 僵尸行
  "zombie_dark_blue_min": 0.01,
  "zombie_cross_min": 0.02,   // 相邻两格蓝灰均 ≥ 此 → 跨界僵尸行
  "occlusion_check": true,    // 遮挡检测（暂停窗口/结算面板跳过）
  "occlusion_region": [160, 150, 640, 480],
  "occlusion_lawn_max": 0.75, // 中部草坪占比低于此 → 有遮挡
  "debug": false              // true 时保存标注图
}
```

### 检测精度插件

```bash
python -m pvz_agent.grid_scan_debug              # 扫描当前画面，标注图存 debug/
python -m pvz_agent.grid_scan_debug --table      # 额外打印 5x9 分类表
python -m pvz_agent.grid_scan_debug --blue 0.03 --warm 0.15   # 调阈值测试
```
绿圈=植物，红横条=僵尸行。打开 `debug/gridscan_*.png` 核对：绿圈应罩住植物，红横条应盖住有僵尸的行。误检就调 `--warm`（高=更少判植物）、`--blue`（大=僵尸行判定更严格）。若提示"遮挡检测跳过"，说明当前画面不是战斗界面（暂停/结算/选卡）。

## 格子/卡片坐标校准（交互式，可确认落点）

格子坐标有偏差时，用交互式校准工具——**可移动鼠标到指定格看落点、可截图叠网格预览、可逐个采集拟合**：

```bash
python -m pvz_agent.calibrate grid       # 校准战斗格子（5x9）
python -m pvz_agent.calibrate cards      # 校准卡片栏
```

核心命令：
| 命令 | 作用 |
|------|------|
| `m <row> <col>` | 把鼠标移到 格子(row,col) 的**预测中心**——肉眼看落点是否对准真实格子中心 |
| `p` | **截图并叠加 5x9 网格线**保存到 `debug/grid_preview_*.png`，直观对比预测网格 vs 真实网格 |
| `g <row> <col>` | 采集**当前鼠标位置**为 格子(row,col) 的真实中心 |
| `mcard <index>` | 把鼠标移到**战斗卡槽 卡片[index]** 的预测中心 |
| `card <index>` | 采集当前鼠标位置为 卡片[index] 的真实中心 |
| `mshovel` | 把鼠标移到**铲子按钮**的预测中心 |
| `shovel` | 采集当前鼠标位置为 铲子按钮 的真实中心 |
| `fit` | 用已采点最小二乘拟合，写回 config.json（带合理性校验，超范围拒绝） |
| `test <row> <col>` | 拟合后把鼠标移到该格验证是否对准 |
| `list` / `undo` | 查看采集点 / 撤销最后一个点 |

典型流程：先 `m 0 0` / `m 0 8` / `m 4 0` / `m 4 8` 看偏差方向 → 逐个把鼠标挪到真实中心 `g` 采集 4~5 个关键格 → `fit` 写回 → `test` 逐个验证。**卡片栏**：`mcard 0` 看落点 → 挪到真实卡片中心 `card 0` 采集 → 同理 `card 1` `card 2` → `fit`（拟合并写回 `card_left`/`card_step`/`card_top`）。**铲子**：`mshovel` → 挪到真实铲子 `shovel` 采集 → `fit`。PvZ 草坪是 **5 行 x 9 列**（row 0~4, col 0~8）。

## 性能配置

`config.json` → `agent`：
```json
"tick_interval": 2.0,     // 每轮间隔（秒），越小 Agent B 响应越快
"max_history_rounds": 1,  // Agent B 保留的历史轮数（1=只留上轮反馈，最快）
"narrator_on": false,     // Agent A 每轮自动描述默认关（需要时 describe 手动调）
"jpeg_quality": 70,       // 截图 JPEG 压缩质量（发给 VLM 用）
"image_format": "jpeg"    // "jpeg" 压缩减小上传体积/推理时间，"png" 无损更清晰
```
- **JPEG 压缩**：VLM 收到的截图从全尺寸 PNG 改为 JPEG(质量70)，图片 token 和上传时间大幅下降。
- **A/B 并发**：两个 VLM 请求并行发出，单轮耗时 = max(A,B) 而非 A+B。
- **截图不累积**：`Planner` 历史只存文本，`VLMClient._build_messages` 会把历史里任何图片数组剥离成纯文本——同一时刻上下文里**至多一张图**（最新截图）。即使误传图片到历史也会被清除。
- 若仍慢：把 `narrator_on` 设 false（默认已关）、`max_history_rounds` 降到 1（默认已 1）、调小 `scan_interval`。

## 参考

- `xzx.py`：原手动窗口工具（保留不动）。
- `LLM_PvZ_Player-main/`：参考项目的窗口捕获/动作解析/prompt 模式（本项目仅借鉴，不 import 其内存读取与注入模块）。
