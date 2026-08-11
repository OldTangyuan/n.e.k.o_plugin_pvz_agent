# PVZ 游玩助手 —— 让猫娘自己玩《植物大战僵尸》

这个插件让猫娘能自己玩《植物大战僵尸》：她会周期性收到游戏画面，自己看战局、说出
她接下来的操作（发给你听），然后自己动手打下去。你只需要开着游戏，对她说一句
"去玩植物大战僵尸吧"。

---

## 一、快速开始

1. **打开游戏**：启动《植物大战僵尸》（原版 / 杂交版均可）。游戏窗口标题需含
   "植物大战僵尸" / "pvz" / "杂交版" 等关键词（可在插件配置改 `window_title_keywords`）。

2. **配置 AI 决策**（必需）：猫娘要自己看画面、定策略，需要一个能联网的 AI 服务。
   在插件目录 `pvz/.env` 填写（把 `pvz/.env.example` 复制为 `pvz/.env` 后填写）：
   ```
   VLM_BASE_URL=https://api.example.com/v1
   VLM_MODEL=your-model-name
   VLM_API_KEY=sk-xxxx
   ```

3. **对猫娘说一句**：在对话里说"去玩植物大战僵尸吧"，或点插件面板的「开始游玩」。
   之后猫娘会自己看画面、报策略、操作，不用你再管。

> 插件面板「快速开始」页有实时状态（游玩状态 / AI 决策 / 游戏窗口）+ 开始 / 暂停 / 停止
> 按钮，可以直接操作。

---

## 二、猫娘怎么玩

开始游玩后，插件每隔几秒截一张游戏画面推给猫娘，猫娘会：

- 直接说出她现在的打法，比如"我在第 2 行种一棵豌豆射手""这波僵尸多，我多种几棵"；
- 自己操作游戏，不会反复问你要怎么办；
- 你也可以随时在对话里引导她调整，比如"这波先攒阳光""寒冰射手守第二行"。

推给猫娘的是**高质量原图**，不会夹带后台决策用的扫描辅助信息；你随时可以用
对话里的工具让她扫一下战局（植物 / 僵尸 / 卡片位置）。

| 你说的 | 猫娘会 |
|---|---|
| "去玩植物大战僵尸吧" | 开始游玩，自己看画面操作 |
| "这波先攒阳光" / "寒冰射手守第二行" | 调整打法继续玩 |
| "暂停一下" / "停吧" | 暂停 / 停止游玩 |

游戏判定本关结束时，猫娘会告诉你结果并停下，你说一声就能开下一关。

---

## 三、猫娘可用的工具（@llm_tool）

| 工具 | 参数 | 作用 |
|---|---|---|
| `pvz_status` | — | 游玩状态（是否在玩 / 目标 / 窗口 / 已执行动作数） |
| `pvz_screenshot` | — | 立即截一帧游戏画面 |
| `pvz_scan` | — | 扫描当前战局：植物坐标 / 僵尸行 / 空地 / 可用卡片 |
| `pvz_start` | `goal`? `restart`? | 开始游玩（暂停时自动恢复；终止后可重启） |
| `pvz_pause` / `pvz_resume` / `pvz_stop` | — | 暂停 / 恢复 / 停止游玩 |
| `pvz_goal` | `goal` | 设定/修改游玩目标 |
| `pvz_instruction` | `instruction` | 下发一条打法引导（如"先种豌豆射手"） |

---

## 四、配置（plugin.toml → `[pvz_agent]`）

| 项 | 默认 | 说明 |
|---|---|---|
| `auto_start` | false | 插件启动时是否自动开始游玩 |
| `window_title_keywords` | 见文件 | 找游戏窗口的标题关键词 |
| `screenshot_feed_enabled` | true | 被动推画面：周期把最新截图推进猫娘视野 |
| `screenshot_feed_interval` | 8.0 | 被动推画面间隔（秒），画面没变不重复推 |
| `screenshot_nudge_enabled` | true | 主动催猫娘行动：截图 + 短触发文本 |
| `screenshot_nudge_interval` | 5.0 | 主动催行动间隔（秒） |
| `screenshot_nudge_text` | "【PVZ】最新战况。…" | 催行动时附带的短文本 |
| `screenshot_max_edge_px` | 0 | 给猫娘的截图最长边像素（0 = 原图不缩放） |
| `screenshot_jpeg_quality` | 95 | 推送 JPEG 质量 |
| `screenshot_max_bytes` | 163840 | 单帧 JPEG 字节预算（超限才自动降质） |
| `sun_auto_collect` | true | 后台自动收阳光（猫娘不用操心） |
| `scan_grid_enabled` / `scan_cards_enabled` | true | 战局扫描开关（辅助决策） |
| `notify_on_terminate` | true | 本关结束时推送通报 |
| `notify_window_lost` | true | 游戏窗口丢失时推送通报并停止 |

**配置边界**：AI 服务密钥在 `pvz/.env`（单来源）。校准数值（布局坐标、OpenCV 阈值）在
`pvz/config.json`，一般不需要动；只有画面点击位置明显偏了才需要按下文校准。

---

## 五、校准（进阶，一般不用）

布局 / 卡片坐标有偏差时，在 pvz 目录下用核心校准工具（会写 `pvz/config.json`）：

```bash
cd plugin/plugins/pvz_agent/pvz
<你环境的 python> -m pvz_agent.calibrate grid   # 校准战斗格子（5x9）
<你环境的 python> -m pvz_agent.calibrate cards  # 校准卡片栏
```

---

## 六、排障

- **状态一直显示"未找到窗口"**：确认游戏已打开、没最小化，窗口标题含关键词。
- **AI 决策未就绪**：`pvz/.env` 还没填密钥，填好并重启插件。
- **点「开始游玩」报错**：先确认插件在运行，再确认游戏窗口和 AI 决策都就绪。
- **猫娘一直没动静**：确认插件在运行、已开始游玩；若面板显示 AI 决策未就绪则先配好。

---

## 七、测试

```bash
uv run pytest plugin/plugins/pvz_agent/tests/test_service.py -q
```
