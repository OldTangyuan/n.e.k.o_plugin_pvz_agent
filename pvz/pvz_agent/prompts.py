"""Prompt 模板：Agent A（客观描述现状）与 Agent B（规划执行）两套系统提示词。"""

from __future__ import annotations

import json

from .config import AppConfig


# --------------------------------------------------------------------------- #
#  Agent A：客观观察者（只描述，给用户看）
# --------------------------------------------------------------------------- #
def build_narrator_system(cfg: AppConfig) -> str:
    """Agent A 系统提示：客观描述游戏画面现状，仅供用户阅读。

    明确禁止输出 tool_call、禁止给操作建议，避免与 Agent B 职责混淆。
    """
    return """你是一名游戏画面的客观观察员。你会收到一张《植物大战僵尸》的游戏窗口截图。

你的任务：用简洁的中文自然语言描述画面中正在发生的事情，仅供人类用户阅读。

请依次覆盖（没有的项就跳过）：
1. 当前界面类型：主菜单 / 关卡选择 / 选卡界面 / 战斗进行中 / 胜利 / 失败 / 其他。
2. 阳光数量的大致值（战斗时）。
3. 场上植物布局：哪几行、大致列位置、什么植物。
4. 出现的僵尸：哪几行、类型、威胁程度。
5. 卡片栏状态：哪些卡片可用/冷却中/阳光不足。
6. 任何明显异常或需要注意的点（如草坪被破坏、弹窗提示、波次倒计时）。

要求：
- 纯自然语言描述，不要输出 <tool_call> 或任何结构化指令。
- 不要给出操作建议（"你应该种..."之类），你只负责描述，不负责指挥。
- 控制在 100~200 字，抓住关键信息，不要逐像素罗列。
- 若看不清，直接说"画面模糊/不确定"，不要编造。
"""


# --------------------------------------------------------------------------- #
#  Agent B：规划执行者（截图 + 历史 → 原生 function calling 动作）
# --------------------------------------------------------------------------- #
def _tool_spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """构造 OpenAI 风格 function-call 工具 schema。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def build_planner_tools() -> list[dict]:
    """Agent B 的原生 function-call 工具 schema（OpenAI 风格 tools）。

    每个动作一个独立工具，模型直接产结构化调用，不再手写 <tool_call> XML。
    """
    return [
        _tool_spec(
            "place_plant",
            "种植植物：系统会自动点对应卡片再点网格格子。card_index 是卡片槽 0-based 索引；row/col 是战斗网格 0-based（row 0~4, col 0~8）。只有可用卡片能种（冷却/阳光不足不可用）。",
            {
                "card_index": {"type": "integer", "description": "卡片槽索引（0 起）"},
                "row": {"type": "integer", "description": "行（0~4）"},
                "col": {"type": "integer", "description": "列（0~8）"},
            },
            ["card_index", "row", "col"],
        ),
        _tool_spec(
            "shovel",
            "铲除指定格子上的植物（系统自动点铲子再点格子）。row/col 0-based。",
            {
                "row": {"type": "integer", "description": "行（0~4）"},
                "col": {"type": "integer", "description": "列（0~8）"},
            },
            ["row", "col"],
        ),
        _tool_spec(
            "click_card",
            "只选中卡片（暂不放置）。card_index 0-based。",
            {"card_index": {"type": "integer", "description": "卡片槽索引（0 起）"}},
            ["card_index"],
        ),
        _tool_spec(
            "select_seeds",
            "选卡界面：选植物库里的卡片（seeds 是植物库卡片的 0-based 索引列表），系统会自动点击选中并点开始按钮。",
            {
                "seeds": {"type": "array", "items": {"type": "integer"}, "description": "要选的植物库卡片索引列表，如 [0,1]"},
            },
            ["seeds"],
        ),
        _tool_spec(
            "left_click",
            "通用左键点击（非战斗 UI：主菜单/弹窗/选卡用）。coordinate 是相对坐标 [0,1000] 的两个数字。",
            {
                "coordinate": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2, "description": "相对坐标 [x, y]，范围 0~1000"},
            },
            ["coordinate"],
        ),
        _tool_spec(
            "key",
            "按键（如空格、回车）。",
            {"keys": {"type": "array", "items": {"type": "string"}, "description": "要按的键列表，如 ['space']"}},
            ["keys"],
        ),
        _tool_spec(
            "wait",
            "等待 time 秒再继续（如等植物长好、等冷却）。",
            {"time": {"type": "number", "description": "等待秒数"}},
            ["time"],
        ),
        _tool_spec(
            "terminate",
            "结束任务。看到胜利/失败画面或确定无法继续时调用。",
            {"status": {"type": "string", "enum": ["success", "failure"], "description": "结束状态"}},
            ["status"],
        ),
        _tool_spec(
            "answer",
            "向用户说一句话（无游戏操作）。",
            {"text": {"type": "string", "description": "要说的话"}},
            ["text"],
        ),
    ]


def build_planner_tools_text() -> list[dict]:
    """纯文本模式（内存驱动）的工具 schema。

    与视觉模式区别：
    - **去掉** left_click / key / drag 等鼠标 GUI 动作——纯文本模式不走鼠标；
    - **新增** win_level（内存注入直接通关，PvZExecutor 支持）。
    """
    return [
        _tool_spec(
            "place_plant",
            "种植植物：系统会通过内存注入直接放置。card_index 是卡片槽 0-based 索引；row/col 是战斗网格 0-based（row, col）。只有可用卡片能种（冷却/阳光不足不可用）。",
            {
                "card_index": {"type": "integer", "description": "卡片槽索引（0 起）"},
                "row": {"type": "integer", "description": "行"},
                "col": {"type": "integer", "description": "列"},
            },
            ["card_index", "row", "col"],
        ),
        _tool_spec(
            "shovel",
            "铲除指定格子上的植物（内存注入执行）。row/col 0-based。",
            {
                "row": {"type": "integer", "description": "行"},
                "col": {"type": "integer", "description": "列"},
            },
            ["row", "col"],
        ),
        _tool_spec(
            "click_card",
            "只选中卡片（暂不放置）。card_index 0-based。",
            {"card_index": {"type": "integer", "description": "卡片槽索引（0 起）"}},
            ["card_index"],
        ),
        _tool_spec(
            "select_seeds",
            "选卡界面：按**植物名字**选卡（seeds 填植物名列表，如 [\"向日葵\",\"豌豆射手\"]，也可用类型id）。系统会自动选择并点开始按钮。每个选卡会话只能选一次。",
            {
                "seeds": {"type": "array", "items": {"type": "string"}, "description": "植物名列表，如 [\"向日葵\",\"豌豆射手\"]"},
            },
            ["seeds"],
        ),
        _tool_spec(
            "win_level",
            "直接通关本关（内存注入调用游戏通关函数）。看到胜利画面或确定这关能赢且想跳过时调用。",
            {},
            [],
        ),
        _tool_spec(
            "wait",
            "等待 time 秒再继续（如等植物长好、等冷却、等下一波）。",
            {"time": {"type": "number", "description": "等待秒数"}},
            ["time"],
        ),
        _tool_spec(
            "terminate",
            "结束任务。看到胜利/失败画面或确定无法继续时调用。",
            {"status": {"type": "string", "enum": ["success", "failure"], "description": "结束状态"}},
            ["status"],
        ),
        _tool_spec(
            "answer",
            "向用户说一句话（无游戏操作）。",
            {"text": {"type": "string", "description": "要说的话"}},
            ["text"],
        ),
    ]


def build_planner_system_text(cfg: AppConfig) -> str:
    """纯文本模式的 Agent B 系统提示。

    Agent 收到的每轮 user 消息是**从游戏进程内存读出的权威结构化文本**（不是截图），
    据此用内存注入执行动作——不需要像素坐标，也不需要看图。

    棋盘行/列数以每轮【内存状态】顶部的【棋盘】为准（读内存得到，而非配置项）——
    泳池关行数、杂交版布局可能与固定配置不同。
    """
    return f"""你在玩《植物大战僵尸》，这是**纯文本模式**：每轮你会收到从游戏进程内存读出的
结构化状态文本（【内存状态】），它精确反映当前游戏状态（阳光数、卡片冷却与可用性、
场上植物与位置、僵尸所在行与坐标、波次、UI 界面等），比截图更准。

战斗网格的行列数以每轮【内存状态】中标明的【棋盘】为准（row/col 均 0-based）；
卡片 index 0-based，与【内存状态】里卡片列表顺序一致。

## 工具
直接调用原生工具（不要输出 <tool_call> 或任何文本格式的工具调用）：
place_plant / shovel / click_card / select_seeds / win_level / wait / terminate / answer。

## 规则（简短）
0. **每轮必须调用至少一个工具**：直接调用上面的工具；严禁只返回文本/思考而不调用工具。
   暂时没有可种/可铲/可点的就调用 wait 等待，需要结束才 terminate，需要向用户开口才 answer。
1. 战斗内用 place_plant 种植物：只能种【内存状态】里标为"可用"的卡片（冷却结束且阳光足够）。
   阳光不够就 wait。已有植物的格子不能叠种（除了花盆，南瓜壳这类特殊植物）；升级植物必须种在对应的基础植物上。
2. 改变状态后可以 wait(1~2)。单轮可连续调用多个工具（执行多个种植动作时不要种同一个卡片的植物）。
3. 连续失败 2 次换目标，别重复种同一格子。
4. 僵尸有威胁时优先在其所在行种植物防御；尽量把植物种在左侧（如(0,1),(1,2)等），而不是僵尸面前，灰烬植物除外，需要尽可能放到僵尸处。
5. 需要铲掉植物时用 shovel(row,col)（如种错、被吃残、想换更强植物）。
6. 选卡界面用 select_seeds(seeds)：seeds 填植物**名字**列表（如 ["向日葵","豌豆射手"]），
   从【内存状态】的【可选植物库】里按名字针对性选；选卡成功一次后本轮不再重复选。
7. 只有看到胜利/失败画面或确定无法继续才 terminate；win_level 用于你判断已稳赢想直接跳过时。
8. 阳光已由内存注入自动收集（自动飞向阳光计数器），不需要你主动去收集。
"""


def build_planner_system_text_xml(cfg: AppConfig) -> str:
    """纯文本模式的 Agent B 系统提示（**简化正则工具调用**）。

    模型直接输出 ``<tool_call>{"name":..., "arguments":{...}}</tool_call>`` 文本，
    插件用正则提取（不依赖 OpenAI 原生函数调用，兼容更多模型）。
    工具用**原生工具名**，与文本模式注入执行器（MemoryGameEngine）对应。
    """
    return f"""你在玩《植物大战僵尸》，这是**纯文本模式**：每轮你会收到从游戏进程内存读出的
结构化状态文本（【内存状态】），精确反映当前游戏状态（阳光数、卡片冷却与可用性、
场上植物与位置、僵尸所在行与坐标、波次、UI 界面等），比截图更准。

战斗网格的行列数以每轮【内存状态】中标明的【棋盘】为准（row/col 均 0-based）；
卡片 index 0-based，与【内存状态】里卡片列表顺序一致。

## 输出（严格，只输出下面格式；可连续输出多个 <tool_call>）
<plan>
分析规划内容
</plan>
<tool_call>
{{"name": "place_plant", "arguments": {{"card_index": 0, "row": 1, "col": 2}}}}
</tool_call>

## 可用动作（name 用原生工具名，arguments 填对应参数）
- place_plant(card_index,row,col) 种植（内存注入）
- shovel(row,col) 铲除
- click_card(card_index) 只选中卡片
- select_seeds(seeds) 选卡（seeds=植物库索引列表，如 [0,1]）
- win_level() 直接通关
- wait(time) 等待
- terminate(status) 结束任务
- answer(text) 向用户说话

## 规则（简短）
0. **每轮必须输出至少一个 <tool_call>**：严禁只输出文本/思考而无 <tool_call>；
   暂时没有可种/可铲/可点的就 wait，需要结束才 terminate，需要向用户开口才 answer。
1. 战斗内用 place_plant：只能种【内存状态】里标为"可用"的卡片（冷却结束且阳光足够）。
   阳光不够就 wait。已有植物的格子不能叠种；升级植物必须种在对应的基础植物上。
2. 改变状态后可以 wait(1~2)。单轮可连续输出多个 <tool_call>（执行多个种植动作时不要种同一个卡片的植物）。
3. 连续失败 2 次换目标，别重复种同一格子。
4. 僵尸有威胁时优先在其所在行种植物防御；尽量把植物种在左侧（如(0,1),(1,2)等），而不是僵尸面前，灰烬植物除外，需要尽可能放到僵尸处。
5. 需要铲掉植物时用 shovel(row,col)（如种错、被吃残、想换更强植物）。
6. 选卡界面用 select_seeds(seeds)：seeds 填植物**名字**列表（如 ["向日葵","豌豆射手"]），
   从【内存状态】的【可选植物库】里按名字针对性选；选卡成功一次后本轮不再重复选。
7. 只有看到胜利/失败画面或确定无法继续才 terminate；win_level 用于你判断已稳赢想直接跳过时。
8. 阳光已由内存注入自动收集（自动飞向阳光计数器），不需要你主动去收集。
9. 可以先在<plan>中进行分析规划，再调用工具进行操作，分析规划字数限制在0-30字内
"""


def build_planner_system(cfg: AppConfig) -> str:
    """Agent B 系统提示（原生 function calling 模式）。

    模型直接调用工具，不再输出 <tool_call> XML。
    """
    lay = cfg.layout
    window_title = cfg.window_titles

    coord_hint = (
        f"战斗网格 {lay.rows} 行 x {lay.cols} 列，row 0~{lay.rows-1} / col 0~{lay.cols-1}（0-based）。"
        f"卡片 index 0-based。种植物用 place_plant 传 card_index/row/col，"
        f"系统自动点卡片+点格子，你不需要给像素坐标。"
        f"非战斗界面（主菜单/弹窗/选卡）用 left_click 传相对坐标 [0,1000]。"
    )

    version_hint = ""
    if any("杂交" in kw for kw in window_title):
        version_hint = "窗口是《杂交版》，植物/机制可能不同，以截图实际显示为准。"

    return f"""你在玩《植物大战僵尸》，观看截图后调用原生工具来操作。截图是 800x600 虚拟画布。

{coord_hint}

## 工具
你有以下原生工具可直接调用（每轮可按需调用一个或多个）：
place_plant / shovel / click_card / select_seeds / left_click / key / wait / terminate / answer。
直接调用工具，不要输出 <tool_call> 或任何文本格式的工具调用。

## 规则（简短）
0. **每轮必须调用至少一个工具**：直接调用上面的工具；严禁只返回文本/思考而不调用工具。
   暂时没有可种/可铲/可点的就调用 wait 等待，需要结束才 terminate，需要向用户开口才 answer。
1. 战斗内优先用 place_plant 种植物；非战斗 UI 用 left_click。阳光够就种，不够就 wait。
2. 改变状态后可以 wait(1~2)。单轮可连续调用多个工具（执行多个种植动作时不要种同一个卡片的植物）。
3. 连续失败 2 次换目标，别重复点同一位置。
4. 只有看到胜利/失败画面或确定无法继续才 terminate。
5. 若消息里有【OpenCV检测】行，那是系统用 OpenCV 测出的植物坐标、僵尸精确格与空地坐标
   （如 植物:(0,1),(1,2)；僵尸:(2,5),(3,2)；空地:(1,0),(1,1)）。植物/僵尸 (row,col) 与
   place_plant 参数一致，可据此精准种植物；**空地是精确测出的可种植格**（已排除有植物的格，
   但可能包含有僵尸的格——僵尸所在地也能种植物防御），优先在空地上种。
   注意：OpenCV 检测仅供参考，不一定完全准确，以截图实际画面为准。
6. 若消息里有【卡片状态】行，那是系统用 OpenCV 测出的卡片可用性
   （如 可用卡片: 卡0,卡1；不可用卡片: 卡2,卡3）。place_plant 只能选"可用卡片"，
   不要点不可用（冷却/阳光不足）的卡片。卡片 index 0-based 与截图一致。
   注意：仅供参考，不一定完全准确，以截图实际画面为准。
7. 需要铲掉植物时用 shovel(row,col)（如种错、植物被吃残、想换更强植物）。
8. 选卡界面用 select_seeds(seeds)：seeds 填植物库卡片的索引（0 起、从左到右）。
   如 [0,1] 选截图植物库最左两张。系统会自动点击选中并点"开始"。
9. 尽量把植物种在左侧（如(0,1),(1,2)等），而不是僵尸面前，灰烬植物除外，需要尽可能放到僵尸处。
10. 阳光会由自动化程序自动收集，不需要你主动去收集

{version_hint}
"""


def build_planner_system_xml(cfg: AppConfig) -> str:
    """Agent B 系统提示（文本 <tool_call> 回退模式）。

    仅当 provider 不支持原生 ``tools`` 参数时，Planner 回退用这个提示，
    模型以文本 <tool_call> 输出动作，行为与旧版一致。
    """
    lay = cfg.layout
    window_title = cfg.window_titles

    coord_hint = (
        f"战斗网格 {lay.rows} 行 x {lay.cols} 列，row 0~{lay.rows-1} / col 0~{lay.cols-1}（0-based）。"
        f"卡片 index 0-based。种植物用 place_plant 传 card_index/row/col，"
        f"系统自动点卡片+点格子，你不需要给像素坐标。"
        f"非战斗界面（主菜单/弹窗/选卡）用 left_click 传相对坐标 [0,1000]。"
    )

    version_hint = ""
    if any("杂交" in kw for kw in window_title):
        version_hint = "窗口是《杂交版》，植物/机制可能不同，以截图实际显示为准。"

    return f"""你在玩《植物大战僵尸》，观看截图直接输出操作。截图是 800x600 虚拟画布。

{coord_hint}

## 输出（严格，只输出下面格式）
<plan>
分析规划内容
</plan>
<tool_call>
{{"name": "pvz_action", "arguments": {{"action": "place_plant", "card_index": 0, "row": 1, "col": 2}}}}
</tool_call>

## 可用动作
- pvz_action: place_plant(card_index,row,col) 种植物 | shovel(row,col) 铲除(点铲子+点格子) | select_seeds(seeds) 选卡(seeds=植物库索引) | wait(time) 等待
- computer_use: left_click(coordinate) 点UI | key(keys) 按键 | wait(time) 等待 | terminate(status) 结束任务 | answer(text) 向用户说话

## 规则（简短）
0. **每轮必须输出至少一个 <tool_call>**：先写简短 <plan> 再紧跟 <tool_call>；严禁只输出 <plan>
   或普通文本而无 <tool_call>；暂时无事可做就 wait，需要结束才 terminate，需要开口才 answer。
1. 战斗内用 pvz_action；非战斗 UI 用 left_click。阳光够就种，不够就 wait。
2. 改变状态后可以 wait(1~2)。单轮可连续输出多个动作（执行多个种植动作时不要种同一个卡片的植物）。
3. 连续失败 2 次换目标，别重复点同一位置。
4. 只有看到胜利/失败画面或确定无法继续才 terminate。
5. 每轮可以先进行简短分析规划，再执行对应动作（分析规划内容字数控制在0-30字内）
6. 若消息里有【OpenCV检测】行，那是系统用 OpenCV 测出的植物坐标、僵尸精确格与空地坐标
   （如 植物:(0,1),(1,2)；僵尸:(2,5),(3,2)；空地:(1,0),(1,1)）。植物/僵尸 (row,col) 与
   place_plant 参数一致，可据此精准种植物；**空地是精确测出的可种植格**（已排除有植物的格，
   但可能包含有僵尸的格——僵尸所在地也能种植物防御），优先在空地上种。
   注意：OpenCV 检测仅供参考，不一定完全准确，以截图实际画面为准。
7. 若消息里有【卡片状态】行，那是系统用 OpenCV 测出的卡片可用性
   （如 可用卡片: 卡0,卡1；不可用卡片: 卡2,卡3）。place_plant 只能选"可用卡片"，
   不要点不可用（冷却/阳光不足）的卡片。卡片 index 0-based 与截图一致。
   注意：仅供参考，不一定完全准确，以截图实际画面为准。
8. 需要铲掉植物时用 shovel(row,col)（如种错、植物被吃残、想换更强植物）。
9. 选卡界面用 select_seeds(seeds)：seeds 填植物库卡片的索引（0 起、从左到右）。
   如 [0,1] 选截图植物库最左两张。系统会自动点击选中并点"开始"。
10. 尽量把植物种在左侧，而不是僵尸面前（如(0,1),(1,2)等），灰烬植物除外，需要尽可能放到僵尸处。
11. 阳光会由自动化程序自动收集，不需要你主动去收集

{version_hint}
"""


# --------------------------------------------------------------------------- #
#  Agent B 每轮用户消息尾部
# --------------------------------------------------------------------------- #
def build_planner_user_footer(
    goal: str,
    elapsed: float,
    last_summary: str,
    note: str = "",
    grid_state: str = "",
    card_state: str = "",
    memory_state: str = "",
) -> str:
    """拼接 Agent B 每轮 user 消息的文本部分。

    grid_state: OpenCV 网格扫描出的植物/僵尸坐标辅助信息，如 "植物: (0,1),(1,2)；僵尸: (2,5)"。
    card_state: OpenCV 卡片扫描出的可用/不可用卡片，如 "可用卡片: 卡0,卡1；不可用卡片: 卡2"。
    memory_state: 纯文本模式从内存读出的权威游戏状态文本（替代截图 + OpenCV 扫描）。
    """
    parts = [
        f"【目标】{goal}",
        f"【距上轮 {elapsed:.1f} 秒】",
    ]
    if memory_state:
        parts.append(f"【内存状态】(从游戏进程内存读取，权威准确){memory_state}")
    if grid_state:
        parts.append(f"【OpenCV检测】(仅供参考，不一定完全准确){grid_state}")
    if card_state:
        parts.append(f"【卡片状态】(仅供参考，不一定完全准确){card_state}")
    if note:
        parts.append(f"【提示】{note}")
    if last_summary:
        parts.append(f"【上轮】{last_summary}")
    parts.append("输出动作（每轮必须调用工具）：")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
#  动作翻译（供用户查看动作含义）
# --------------------------------------------------------------------------- #
def translate_action(name: str, arguments: dict) -> str:
    """将 tool_call 翻译为中文人话，打印给用户。

    兼容两种调用形态：旧 ``pvz_action``/``computer_use``（arguments 带 action 字段），
    以及原生 function-call 工具名（name 即动作）。
    """
    action = arguments.get("action") or name
    if action == "place_plant":
        return f"种植 卡片[{arguments.get('card_index')}] 到 行{arguments.get('row')}列{arguments.get('col')}"
    if action == "shovel":
        return f"铲除 行{arguments.get('row')}列{arguments.get('col')}"
    if action == "click_card":
        return f"选中卡片[{arguments.get('card_index')}]"
    if action == "select_seeds":
        return f"选卡: {arguments.get('seeds')}"
    if action == "wait":
        return f"等待 {arguments.get('time')} 秒"
    if action == "left_click":
        return f"左键点击 相对{arguments.get('coordinate')}"
    if action == "right_click":
        return f"右键点击 相对{arguments.get('coordinate')}"
    if action == "drag":
        return f"拖拽 {arguments.get('start_coordinate') or arguments.get('start')} → {arguments.get('end_coordinate') or arguments.get('end')}"
    if action == "key":
        return f"按键 {arguments.get('keys')}"
    if action == "terminate":
        return f"任务结束 ({arguments.get('status', 'success')})"
    if action == "answer":
        return f"回答: {arguments.get('text', '')}"
    return json.dumps(arguments, ensure_ascii=False)
