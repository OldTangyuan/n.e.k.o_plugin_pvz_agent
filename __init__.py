"""PVZ Agent 插件：让猫娘自己玩《植物大战僵尸》。

协作形态（观感 = 猫娘自己在玩）：
- **猫娘（主模型）**：插件周期性把最新游戏**纯截图**推进主模型视野；她看画面后
  用自然语言给出策略（说给用户听），需要调整打法时经 ``pvz_goal`` / ``pvz_instruction``
  下发引导（如"先种豌豆射手"），不提供精确坐标/步骤。
- **后台执行核心**：在后台循环里看截图、把猫娘的目标与引导翻译成具体操作实时执行
  （种到哪格、何时铲等），保证实时性——对外统一表现为"猫娘自己在操作"。
  插件通过 ``service.PvZAgentService`` 托管。

工具面：
- ``@llm_tool``：主聊天模型直接调用（观察 + 控制 + 调整打法）。
- ``@plugin_entry``：Agent 分析器 / HTTP trigger 的同一能力入口。
- 观察线程：周期推纯截图（feed：``ai_behavior="read"`` 不打断；nudge：截图+短触发，
  ``ai_behavior="respond"`` 唤起猫娘看画面并继续行动）。

生命周期：startup 读 ``[pvz_agent]`` 配置 + 自检 + 启观察线程；shutdown 停循环/阳光/观察。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib  # Python 3.11+
except ImportError:  # Python 3.10 及以下
    import tomli as tomllib  # type: ignore[no-redef]

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
)

from .neko_interface import PvZNekoInterface
from .service import PvZAgentService

JsonObject = dict[str, Any]


def _as_mapping(value: Any) -> JsonObject:
    return dict(value) if isinstance(value, Mapping) else {}


@neko_plugin
class PVZAgentPlugin(NekoPluginBase):
    """PVZ Agent 插件 facade——只做 SDK 接线，业务在 service。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        _log_level = (os.environ.get("NEKO_LOG_LEVEL") or "INFO").strip().upper()
        try:
            self.file_logger = self.enable_file_logging(log_level=_log_level)
        except ValueError:
            self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        self._cfg: JsonObject = {}
        self._started = False
        self._service = PvZAgentService(
            logger=self.logger,
            notifier=self._on_service_notify,
        )
        self._neko = PvZNekoInterface(self._service)

    # ------------------------------------------------------------------ #
    #  生命周期
    # ------------------------------------------------------------------ #
    @lifecycle(id="startup")
    async def startup(self, **_: Any):
        self._cfg = self._read_own_plugin_config()
        # 若直接读不到（如插件目录无 plugin.toml），回退宿主 SDK 配置
        if not self._cfg:
            cfg = _as_mapping(await self.config.dump(timeout=5.0))
            self._cfg = _as_mapping(cfg.get("pvz_agent", {}))
        self._service.configure(self._cfg)
        preflight = self._service.probe()
        self.logger.info("[pvz_agent] 自检: %s", preflight)
        # 观察线程：周期把最新截图推给主模型（feed 纯截图 read + nudge 截图+触发 respond）
        self._service.start_observer(self._on_observation)
        self._started = True

        status: JsonObject = {
            "status": "ready",
            "preflight": preflight,
            "result": self._service.get_status(),
        }
        if bool(self._cfg.get("auto_start", False)):
            status["autostart"] = self._service.start()
        return Ok(status)

    def _read_own_plugin_config(self) -> dict:
        """直接读插件自带 plugin.toml 的 [pvz_agent] 段。

        宿主的 ``config.dump()`` 返回的是首次运行后复制到宿主 state 目录的 runtime 配置，
        改源码里的 plugin.toml 不会生效。这里**直接读插件目录的 plugin.toml**，
        让编辑配置即时生效（重启后）；读取失败回退空 dict（由调用方走 SDK 配置）。
        """
        try:
            path = Path(__file__).resolve().parent / "plugin.toml"
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            section = data.get("pvz_agent", {})
            return dict(section) if isinstance(section, dict) else {}
        except Exception as exc:
            self.logger.warning("[pvz_agent] 读取自带 plugin.toml 失败: %s", exc)
            return {}

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: Any):
        self._service.shutdown()
        self._started = False
        return Ok({"status": "shutdown"})

    @ui.context(id="quickstart", title="PVZ Agent 状态")
    def quickstart_ui_context(self, **_):
        """插件面板 quickstart surface 的只读上下文 provider。

        host 的 get_ui_context 需要它（surface 没声明 context 时取 surface id），
        缺了会报 "UI context not found" 连带 action 列表拿不到。返回轻量快照即可。
        """
        try:
            return {"status": self._service.get_status()}
        except Exception:
            return {"status": {}}

    # ------------------------------------------------------------------ #
    #  内部辅助
    # ------------------------------------------------------------------ #
    def _on_service_notify(self, *, text: str, kind: str = "") -> None:
        """后台循环线程回调：把游玩事件/故障转达给主模型。

        按 kind 分流：
        - terminate / window_lost / answer：ai_behavior="respond"（立即起回合）；
        - no_action / action_error / planner_error：ai_behavior="read"（进上下文不打断）
          + visibility=["hud"]（用户也能看到），让"解析失败/空动作"绝不静默。
        """
        behavior = "respond" if kind in ("terminate", "window_lost", "answer") else "read"
        visibility = ["hud"] if kind in ("no_action", "action_error", "planner_error") else []
        priority = 6 if kind in ("terminate", "window_lost") else (5 if behavior == "respond" else 4)
        try:
            self.push_message(
                source="pvz_agent",
                visibility=visibility,
                ai_behavior=behavior,
                parts=[{"type": "text", "text": str(text)}],
                priority=priority,
                metadata={"kind": kind or "pvz_event", "source": "pvz_agent"},
            )
        except Exception as exc:
            self.logger.warning("[pvz_agent] 推送转达失败: %s", exc)

    async def _screenshot_payload(self) -> JsonObject:
        """立即截图并送入主模型视野，返回文字摘要（图片不进 JSON 结果）。"""
        payload = _as_mapping(await self._neko.get_screenshot())
        if payload.get("status") != "ok":
            return {"summary": str(payload.get("summary") or "截图失败。")}
        try:
            jpeg = self._service.encode_jpeg(payload["image"])
            self.push_message(
                source="pvz_agent",
                visibility=[],
                ai_behavior="read",
                parts=[{"type": "image", "data": jpeg, "mime": "image/jpeg"}],
                metadata={"kind": "screenshot", "source": "pvz_agent"},
            )
            summary = f"已把最新 PVZ 画面（{payload['width']}x{payload['height']}）送入视野。"
        except Exception as exc:
            self.logger.warning("[pvz_agent] 截图推送失败: %s", exc)
            summary = f"截图成功但推送失败：{exc}"
        return {"summary": summary, "width": payload["width"], "height": payload["height"]}

    async def _run_entry(self, action):
        """plugin_entry 统一执行包装：Ok / Err + 日志。"""
        try:
            payload = _as_mapping(await action())
            return Ok(payload)
        except SdkError as error:
            self.logger.warning("[pvz_agent] entry 失败: %s", error)
            return Err(str(error))
        except Exception as error:
            self.logger.exception("[pvz_agent] entry 异常")
            return Err(f"PVZ Agent 插件内部错误: {error}")

    # ------------------------------------------------------------------ #
    #  观察通道（主模型观察）：观察线程 → 纯截图推送
    # ------------------------------------------------------------------ #
    def _on_observation(self, jpeg: bytes, nudge: bool) -> None:
        """service 观察线程回调：把最新游戏截图推给主模型。

        - ``nudge=False`` → 纯截图（``ai_behavior="read"``，进上下文不打断）；
        - ``nudge=True`` → 截图 + 最小触发文本（``ai_behavior="respond"``，
          唤起主模型看画面并行动；触发文本是让模型起回合的技术必需，不是游戏信息）。
        """
        try:
            if nudge:
                parts: list[dict] = [
                    {"type": "image", "data": jpeg, "mime": "image/jpeg"},
                ]
                nudge_text = str(self._cfg.get("screenshot_nudge_text", "") or "").strip()
                if nudge_text:
                    parts.append({"type": "text", "text": nudge_text})
                self.push_message(
                    source="pvz_agent",
                    visibility=[],
                    ai_behavior="respond",
                    parts=parts,
                    priority=5,
                    coalesce_key="pvz_nudge",
                    metadata={"kind": "pvz_nudge", "source": "pvz_agent"},
                )
            else:
                self.push_message(
                    source="pvz_agent",
                    visibility=[],
                    ai_behavior="read",
                    parts=[{"type": "image", "data": jpeg, "mime": "image/jpeg"}],
                    metadata={"kind": "pvz_screenshot_feed", "source": "pvz_agent"},
                )
        except Exception as exc:
            self.logger.warning("[pvz_agent] 观察推送失败: %s", exc)

    # ------------------------------------------------------------------ #
    #  @llm_tool —— 主聊天模型（猫娘）调用
    # ------------------------------------------------------------------ #
    @llm_tool(
        name="pvz_status",
        description=(
            "只读获取《植物大战僵尸》游玩的运行状态：是否在运行/暂停、"
            "当前目标、窗口是否找到、已执行动作数、最近一次扫描与动作反馈。"
            "适合了解现状、或排查'为什么没动'时先看一眼。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=15.0,
    )
    async def llm_pvz_status(self, **_: Any) -> JsonObject:
        return await self._neko.get_status()

    @llm_tool(
        name="pvz_screenshot",
        description=(
            "立即截取《植物大战僵尸》游戏窗口当前画面，并把图片送入你的视野，同时返回文字摘要。"
            "用于确认战局、判断是否需要调整打法。若周期性截图推送已提供最新画面，"
            "此工具用于按需确认。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=30.0,
    )
    async def llm_pvz_screenshot(self, **_: Any) -> JsonObject:
        return await self._screenshot_payload()

    @llm_tool(
        name="pvz_scan",
        description=(
            "对《植物大战僵尸》当前画面做一次网格 + 卡片扫描，返回植物坐标、"
            "僵尸所在行、空地、可用/不可用卡片等文本信息（供你调整打法前参考）。"
            "无需额外配置，纯图像。"
        ),
        parameters={"type": "object", "properties": {}},
        timeout=20.0,
    )
    async def llm_pvz_scan(self, **_: Any) -> JsonObject:
        return await self._neko.get_scan()

    @llm_tool(
        name="pvz_start",
        description=(
            "开始玩《植物大战僵尸》：能自己看画面、给策略并操作游戏。"
            "可选传 goal 设定目标；restart=true 会中断当前对局后重新开始。"
            "已暂停时调用会自动恢复；已在游玩时只更新目标。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "游玩目标，如'自动玩完当前这一关并尽可能取得胜利'。"},
                "restart": {"type": "boolean", "description": "是否中断当前循环重启，默认 false。"},
            },
        },
        timeout=30.0,
    )
    async def llm_pvz_start(self, *, goal: Any = None, restart: Any = None, **_: Any) -> JsonObject:
        goal_text = goal if isinstance(goal, str) and goal.strip() else None
        return await self._neko.start(goal=goal_text, restart=restart is True)

    @llm_tool(
        name="pvz_pause",
        description="暂停游玩。暂停期间仍会持续推送最新游戏画面。",
        parameters={"type": "object", "properties": {}},
        timeout=15.0,
    )
    async def llm_pvz_pause(self, **_: Any) -> JsonObject:
        return await self._neko.pause()

    @llm_tool(
        name="pvz_resume",
        description="恢复被暂停的游玩，猫娘从暂停处继续操作。",
        parameters={"type": "object", "properties": {}},
        timeout=15.0,
    )
    async def llm_pvz_resume(self, **_: Any) -> JsonObject:
        return await self._neko.resume()

    @llm_tool(
        name="pvz_stop",
        description="停止游玩（停止操作循环与阳光收集）。",
        parameters={"type": "object", "properties": {}},
        timeout=20.0,
    )
    async def llm_pvz_stop(self, **_: Any) -> JsonObject:
        return await self._neko.stop()

    @llm_tool(
        name="pvz_goal",
        description=(
            "设定/修改猫娘玩《植物大战僵尸》的当前目标（自然语言）。"
            "例如'自动玩完当前这一关并尽可能取得胜利'。"
            "目标会进入她每轮决策的依据。"
        ),
        parameters={
            "type": "object",
            "properties": {"goal": {"type": "string", "description": "新的游玩目标。"}},
            "required": ["goal"],
        },
        timeout=15.0,
    )
    async def llm_pvz_goal(self, *, goal: Any = None, **_: Any) -> JsonObject:
        if not isinstance(goal, str) or not goal.strip():
            return {"summary": "需要提供 goal 参数（游玩目标）。"}
        return await self._neko.set_goal(goal.strip())

    @llm_tool(
        name="pvz_instruction",
        description=(
            "给玩《植物大战僵尸》的自己下发一条自然语言打法引导，下一轮操作会遵循它。"
            "用于调整具体打法——先分析当前局势再给方向性指令，不提供精确坐标/步骤；"
            "阳光已由程序自动收集，不用管它；避免重复说过的指令。"
            "例如'先种豌豆射手'、'先种向日葵'、'这波僵尸多，多种几棵'。"
        ),
        parameters={
            "type": "object",
            "properties": {"instruction": {"type": "string", "description": "战略引导指令。"}},
            "required": ["instruction"],
        },
        timeout=15.0,
    )
    async def llm_pvz_instruction(self, *, instruction: Any = None, **_: Any) -> JsonObject:
        if not isinstance(instruction, str) or not instruction.strip():
            return {"summary": "需要提供 instruction 参数（战略引导）。"}
        return await self._neko.give_instruction(instruction.strip())

    # ------------------------------------------------------------------ #
    #  @plugin_entry —— Agent 分析器 / HTTP / 未来 UI
    # ------------------------------------------------------------------ #
    @ui.action(id="pvz_get_status", label="刷新状态")
    @plugin_entry(
        id="pvz_get_status",
        name="查看 PVZ 游玩状态",
        description="查看《植物大战僵尸》游玩的运行状态、目标、已执行动作数、最近扫描。",
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    async def pvz_get_status(self, **_: Any):
        return await self._run_entry(lambda: self._neko.get_status())

    @ui.action(id="pvz_start", label="开始游玩", tone="primary")
    @plugin_entry(
        id="pvz_start",
        name="开始游玩",
        description="让猫娘开始玩《植物大战僵尸》：她自己看画面、给策略并操作游戏。可选传 goal。",
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "游玩目标"},
                "restart": {"type": "boolean", "description": "是否中断重启，默认 false"},
            },
        },
        metadata={"agent_auto": False},
    )
    async def pvz_start(self, goal: str = "", restart: bool = False, **_: Any):
        return await self._run_entry(lambda: self._neko.start(goal or None, restart=restart))

    @ui.action(id="pvz_pause", label="暂停")
    @plugin_entry(
        id="pvz_pause",
        name="暂停游玩",
        description="暂停游玩。暂停期间仍会持续推送最新游戏画面。",
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    async def pvz_pause(self, **_: Any):
        return await self._run_entry(lambda: self._neko.pause())

    @ui.action(id="pvz_resume", label="恢复")
    @plugin_entry(
        id="pvz_resume",
        name="恢复游玩",
        description="恢复被暂停的游玩，猫娘从暂停处继续操作。",
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    async def pvz_resume(self, **_: Any):
        return await self._run_entry(lambda: self._neko.resume())

    @ui.action(id="pvz_stop", label="停止", tone="warning")
    @plugin_entry(
        id="pvz_stop",
        name="停止游玩",
        description="停止游玩（停止操作循环与阳光收集）。",
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    async def pvz_stop(self, **_: Any):
        return await self._run_entry(lambda: self._neko.stop())

    @plugin_entry(
        id="pvz_set_goal",
        name="设定游玩目标",
        description="设定/修改猫娘玩《植物大战僵尸》的当前目标（自然语言）。",
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"goal": {"type": "string"}},
            "required": ["goal"],
        },
        metadata={"agent_auto": False},
    )
    async def pvz_set_goal(self, goal: str, **_: Any):
        return await self._run_entry(lambda: self._neko.set_goal(goal))

    @plugin_entry(
        id="pvz_give_instruction",
        name="下发打法引导",
        description="给猫娘玩《植物大战僵尸》下发一条自然语言打法引导。",
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {"instruction": {"type": "string"}},
            "required": ["instruction"],
        },
        metadata={"agent_auto": False},
    )
    async def pvz_give_instruction(self, instruction: str, **_: Any):
        return await self._run_entry(lambda: self._neko.give_instruction(instruction))

    @plugin_entry(
        id="pvz_screenshot",
        name="截图并送入视野",
        description="截取《植物大战僵尸》当前画面，把图片送入你的视野，返回文字摘要。",
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    async def pvz_screenshot(self, **_: Any):
        return await self._run_entry(self._screenshot_payload)

    @plugin_entry(
        id="pvz_scan",
        name="扫描当前战局",
        description="对《植物大战僵尸》当前画面做战局扫描，返回植物/僵尸/可用卡片等文本。",
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    async def pvz_scan(self, **_: Any):
        return await self._run_entry(lambda: self._neko.get_scan())
