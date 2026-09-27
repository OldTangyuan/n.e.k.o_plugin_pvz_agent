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

import json
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


def _nonempty_str(v: Any) -> bool:
    """合并时的"有效值"判定：非 None、非空串、非纯空白串。"""
    if v is None or v == "":
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True


# ---------------------------------------------------------------------- #
#  配置面板：配置项清单与 TOML 序列化（pvz_config_get / pvz_config_set）
# ---------------------------------------------------------------------- #
_CONFIG_ENUM_CHOICES: dict[str, tuple[str, ...]] = {
    "mode": ("text", "vision"),
    "planting_mode": ("mouseclick", "putplant"),
    "tool_call_mode": ("regex", "fc"),
    "card_position_mode": ("opencv", "fixed"),
}
_CONFIG_BOOL_KEYS = (
    "auto_start", "agent_controls_seed_selection", "notify_on_terminate",
    "notify_window_lost", "sun_auto_collect", "scan_grid_enabled",
    "scan_cards_enabled", "screenshot_feed_enabled", "screenshot_nudge_enabled",
)
_CONFIG_FLOAT_RANGE: dict[str, tuple[float, float]] = {
    "screenshot_feed_interval": (2.0, 300.0),
    "screenshot_nudge_interval": (2.0, 300.0),
}
_CONFIG_INT_RANGE: dict[str, tuple[int, int]] = {
    "screenshot_max_edge_px": (0, 4096),
    "screenshot_jpeg_quality": (1, 95),
}
_CONFIG_STR_KEYS = (
    "api_base_url", "api_model", "text_api_base_url", "text_api_model",
    "thinking", "text_thinking", "screenshot_nudge_text",
)
_CONFIG_SECRET_KEYS = ("api_key", "text_api_key")


def _toml_value_repr(value: Any) -> str:
    """标量/字符串列表 → TOML 行内值。basic string 转义与 JSON 兼容
    （引号/反斜杠/控制字符；JSON 的 \\uXXXX 转义在 TOML 里同样合法）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value_repr(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _mask_secret(value: Any) -> str:
    """密钥打码摘要：明文不出插件。"""
    v = str(value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:4]}****{v[-4:]}"


def merge_config_sources(
    file_cfg: JsonObject, host_cfg: JsonObject, mtimes: tuple[float, float]
) -> JsonObject:
    """合并"插件自带 plugin.toml"与"宿主运行时配置"两个配置来源。

    用户改配置有两条通道：直接编辑 plugin.toml，或在宿主 GUI 配置界面里改
    （写进 profiles/default.toml，反映到 ``config.dump()``）。以**修改时间较新
    的一方为基准**（用户最后编辑的通道整体生效），另一方里的非空值作补充——
    两边都没填的键走内置默认，任何一边填了都能被读到，不再互相掩盖。
    """
    file_mtime, profile_mtime = mtimes
    if host_cfg and profile_mtime > file_mtime:
        base, overlay = dict(file_cfg), host_cfg
    else:
        base, overlay = dict(host_cfg), file_cfg
    merged = dict(base)
    for key, value in overlay.items():
        if _nonempty_str(value):
            merged[key] = value
    return merged


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
        file_cfg = self._read_own_plugin_config()
        # 宿主运行时配置（GUI 配置界面编辑会写进 profiles/default.toml，
        # config.dump() 是"包默认值 + 用户覆盖"的合并视图）
        host_cfg: JsonObject = {}
        try:
            dumped = _as_mapping(await self.config.dump(timeout=5.0))
            host_cfg = _as_mapping(dumped.get("pvz_agent", {}))
        except Exception as exc:
            self.logger.warning("[pvz_agent] 读取宿主运行时配置失败（忽略）: %s", exc)
        self._cfg = merge_config_sources(file_cfg, host_cfg, self._source_mtimes())
        self.logger.info(
            "[pvz_agent] 配置来源: 自带 plugin.toml %d 键 + 宿主运行时 %d 键 → 合并 %d 键",
            len(file_cfg), len(host_cfg), len(self._cfg),
        )
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

    def _source_mtimes(self) -> tuple[float, float]:
        """（自带 plugin.toml mtime, profiles/default.toml mtime），不存在记 0。"""
        root = Path(__file__).resolve().parent
        file_toml = root / "plugin.toml"
        profile_toml = root / "profiles" / "default.toml"
        try:
            file_mtime = file_toml.stat().st_mtime if file_toml.exists() else 0.0
        except OSError:
            file_mtime = 0.0
        try:
            profile_mtime = profile_toml.stat().st_mtime if profile_toml.exists() else 0.0
        except OSError:
            profile_mtime = 0.0
        return file_mtime, profile_mtime

    def _read_own_plugin_config(self) -> dict:
        """直接读插件自带 plugin.toml 的 [pvz_agent] 段。

        与宿主运行时配置（``config.dump()``，含 GUI 配置界面的用户覆盖）在
        startup 里按修改时间新者胜合并（``merge_config_sources``）；
        读取失败回退空 dict（由合并逻辑兜底）。
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

    @ui.context(id="config_panel", title="PVZ Agent 配置")
    def config_panel_ui_context(self, **_):
        """配置面板 surface 的上下文 provider（轻量快照；缺了同样必败）。"""
        try:
            return {"status": self._service.get_status(), "plugin_started": self._started}
        except Exception:
            return {"status": {}, "plugin_started": False}

    def _read_env_fallback(self) -> dict[str, str]:
        """读 pvz/.env（旧配置通道），只用于"密钥是否已设置"的探测。"""
        try:
            path = Path(__file__).resolve().parent / "pvz" / ".env"
            if not path.exists():
                return {}
            out: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
            return out
        except Exception:
            return {}

    def _write_profile_section(self, section: str, updates: JsonObject) -> Path:
        """把 updates 合并进 profiles/default.toml 的 [section] 段（其余段原样保留）。

        合并语义：先解析现有段的键值，updates 覆盖同名字段、保留未提及字段
        （否则配置面板保存会抹掉教程面板先前保存的密钥）。写 profile 覆盖文件
        而不是直接改 plugin.toml：仓库模板保持纯净（注释不被机器改写），且与
        宿主 GUI 配置界面写的是同一个文件，两条通道互通。
        """
        root = Path(__file__).resolve().parent
        path = root / "profiles" / "default.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        existing: JsonObject = {}
        if path.exists():
            try:
                parsed = tomllib.loads(path.read_text(encoding="utf-8"))
                sec = parsed.get(section)
                if isinstance(sec, dict):
                    existing = dict(sec)
            except Exception:
                existing = {}  # 解析失败按空段处理（updates 仍会写入）
        merged: JsonObject = {**existing, **updates}
        block = [f"[{section}]"] + [f"{k} = {_toml_value_repr(v)}" for k, v in merged.items()]
        out: list[str] = []
        i = 0
        replaced = False
        while i < len(lines):
            line = lines[i]
            if line.strip() == f"[{section}]":
                out.extend(block)
                replaced = True
                i += 1
                while i < len(lines) and not lines[i].lstrip().startswith("["):
                    i += 1
                continue
            out.append(line)
            i += 1
        if not replaced:
            if out and out[-1].strip():
                out.append("")
            out.extend(block)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return path

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

    @ui.action(id="pvz_select_window", label="选择游戏窗口")
    @plugin_entry(
        id="pvz_select_window",
        name="选择游戏窗口",
        description=(
            "多个匹配的 PvZ 窗口时切换使用哪一个（hwnd 取状态里 windows 列表的句柄）。"
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer", "description": "要使用的窗口句柄（windows 列表里的 hwnd）。"},
            },
            "required": ["hwnd"],
        },
        metadata={"agent_auto": False},
    )
    async def pvz_select_window(self, hwnd: int, **_: Any):
        return await self._run_entry(lambda: self._neko.select_window(hwnd))

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

    # ------------------------------------------------------------------ #
    #  配置面板（hosted surface）：读取 / 保存 [pvz_agent] 配置
    # ------------------------------------------------------------------ #
    @ui.action(id="pvz_config_get", label="读取配置")
    @plugin_entry(
        id="pvz_config_get",
        name="读取 PVZ 插件配置",
        description="读取 [pvz_agent] 当前合并配置。密钥只回打码摘要，明文不出插件。",
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
        metadata={"agent_auto": False},
    )
    async def pvz_config_get(self, **_: Any):
        async def _run():
            cfg = dict(self._cfg or {})
            data: JsonObject = {k: v for k, v in cfg.items() if k not in _CONFIG_SECRET_KEYS}
            # 密钥：明文不出插件，回打码摘要 + 是否已设置（含 pvz/.env 回退探测）
            env_vals = self._read_env_fallback()
            merged_key = (
                str(cfg.get("api_key", "") or "").strip()
                or str(env_vals.get("VLM_API_KEY", "") or "").strip()
            )
            merged_text_key = (
                str(cfg.get("text_api_key", "") or "").strip()
                or str(env_vals.get("TEXT_VLM_API_KEY", "") or "").strip()
            )
            data["api_key_masked"] = _mask_secret(merged_key)
            data["api_key_set"] = bool(merged_key)
            data["text_api_key_masked"] = _mask_secret(merged_text_key)
            data["text_api_key_set"] = bool(merged_text_key)
            return {"config": data}

        return await self._run_entry(_run)

    @ui.action(id="pvz_config_set", label="保存配置")
    @plugin_entry(
        id="pvz_config_set",
        name="保存 PVZ 插件配置",
        description=(
            "保存 [pvz_agent] 配置到 profiles/default.toml（与宿主 GUI 配置界面同一文件，"
            "其余段原样保留）。密钥留空 = 不修改已保存值。保存后需重启插件生效。"
        ),
        llm_result_fields=["summary"],
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["text", "vision"]},
                "planting_mode": {"type": "string", "enum": ["mouseclick", "putplant"]},
                "tool_call_mode": {"type": "string", "enum": ["regex", "fc"]},
                "card_position_mode": {"type": "string", "enum": ["opencv", "fixed"]},
                "auto_start": {"type": "boolean"},
                "agent_controls_seed_selection": {"type": "boolean"},
                "notify_on_terminate": {"type": "boolean"},
                "notify_window_lost": {"type": "boolean"},
                "sun_auto_collect": {"type": "boolean"},
                "scan_grid_enabled": {"type": "boolean"},
                "scan_cards_enabled": {"type": "boolean"},
                "screenshot_feed_enabled": {"type": "boolean"},
                "screenshot_feed_interval": {"type": "number"},
                "screenshot_nudge_enabled": {"type": "boolean"},
                "screenshot_nudge_interval": {"type": "number"},
                "screenshot_nudge_text": {"type": "string"},
                "screenshot_max_edge_px": {"type": "integer"},
                "screenshot_jpeg_quality": {"type": "integer"},
                "window_titles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "窗口标题关键词列表",
                },
                "api_base_url": {"type": "string"},
                "api_model": {"type": "string"},
                "api_key": {"type": "string", "description": "留空 = 不修改"},
                "text_api_base_url": {"type": "string"},
                "text_api_model": {"type": "string"},
                "text_api_key": {"type": "string", "description": "留空 = 不修改"},
                "thinking": {"type": "string"},
                "text_thinking": {"type": "string"},
            },
        },
        metadata={"agent_auto": False},
    )
    async def pvz_config_set(self, **values: Any):
        async def _run():
            updates: JsonObject = {}
            skipped: list[str] = []
            for key, val in values.items():
                if key in _CONFIG_ENUM_CHOICES:
                    s = str(val or "").strip().lower()
                    if s in _CONFIG_ENUM_CHOICES[key]:
                        updates[key] = s
                    else:
                        skipped.append(key)
                elif key in _CONFIG_BOOL_KEYS:
                    updates[key] = bool(val)
                elif key in _CONFIG_FLOAT_RANGE:
                    lo, hi = _CONFIG_FLOAT_RANGE[key]
                    try:
                        updates[key] = max(lo, min(hi, float(val)))
                    except (TypeError, ValueError):
                        skipped.append(key)
                elif key in _CONFIG_INT_RANGE:
                    lo, hi = _CONFIG_INT_RANGE[key]
                    try:
                        updates[key] = max(lo, min(hi, int(float(val))))
                    except (TypeError, ValueError):
                        skipped.append(key)
                elif key == "window_titles":
                    if isinstance(val, (list, tuple)):
                        titles = [str(t).strip() for t in val if str(t).strip()]
                    else:
                        titles = [
                            t.strip()
                            for t in str(val or "").replace("，", ",").split(",")
                            if t.strip()
                        ]
                    if titles:
                        updates["window_titles"] = titles
                    else:
                        skipped.append(key)
                elif key in _CONFIG_SECRET_KEYS:
                    s = str(val or "").strip()
                    if s:  # 留空 = 不修改已保存的密钥
                        updates[key] = s
                elif key in _CONFIG_STR_KEYS:
                    updates[key] = str(val or "")
                # 未知键静默忽略
            if not updates:
                return {
                    "summary": "没有可保存的变更" + (f"（跳过: {', '.join(skipped)}）" if skipped else ""),
                    "needs_restart": False,
                    "skipped": skipped,
                }
            self._write_profile_section("pvz_agent", updates)
            # 同步内存中的合并视图（pvz_config_get 立即反映；service 层重启后生效）
            self._cfg = {**(self._cfg or {}), **updates}
            secret_keys = [k for k in updates if k in _CONFIG_SECRET_KEYS]
            summary = f"已保存 {len(updates)} 项到 profiles/default.toml"
            if secret_keys:
                summary += "（含密钥更新）"
            if skipped:
                summary += f"；跳过无效项: {', '.join(skipped)}"
            return {"summary": summary, "needs_restart": True, "saved": sorted(updates)}

        return await self._run_entry(_run)
