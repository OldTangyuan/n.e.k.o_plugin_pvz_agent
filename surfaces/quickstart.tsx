import {
  ActionButton,
  Alert,
  Button,
  ButtonGroup,
  Card,
  Field,
  Grid,
  Input,
  KeyValue,
  Page,
  PasswordInput,
  Select,
  Stack,
  StatCard,
  Step,
  Steps,
  Text,
  Warning,
  useEffect,
  useRef,
  useState,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps, Tone } from "@neko/plugin-ui"

// PVZ 游玩助手插件面板「快速开始」教程 + AI 服务配置表单。zh-CN 单语言。
// 通过 props.api.call 调插件 entry（需在 __init__.py 用 @ui.action 暴露）。

const STATUS_REFRESH_INTERVAL_MS = 5000

// phase 中文映射，避免把 idle/running 等英文直接丢给用户。
const PHASE_LABEL: Record<string, string> = {
  idle: "空闲",
  running: "游玩中",
  paused: "已暂停",
  stopping: "停止中",
  error: "出错",
}

// 思考参数预设（与 pvz/pvz_agent/vlm.py 的 THINKING_EXTRA_BODY_PRESETS 一一对应）。
// 各家思维链参数不统一，按模型选预设；选错 provider 也不会报"参数错误"
// （插件端对未知值自动忽略）。
const THINKING_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "不发送思考参数（服务端默认）" },
  { value: "disabled", label: "关闭思考 — DeepSeek / Kimi" },
  { value: "enabled", label: "开启思考 — DeepSeek / Kimi" },
  { value: "openai", label: "关闭思考 — qwen / silicon（旧版兼容）" },
  { value: "openai_thinking", label: "开启思考 — qwen / silicon（旧版兼容）" },
  { value: "openai_native", label: "关闭思考 — OpenAI 原生（reasoning_effort=none）" },
  { value: "openai_native_thinking", label: "低档思考 — OpenAI 原生（reasoning_effort=low）" },
  { value: "openai_native_minimal", label: "最小思考 — OpenAI 原生（reasoning_effort=minimal）" },
  { value: "openai_native_minimal_thinking", label: "低档思考 — OpenAI 原生（minimal→low）" },
  { value: "claude", label: "关闭思考 — Claude（Anthropic 标准）" },
  { value: "claude_thinking", label: "开启思考 — Claude（Anthropic 标准）" },
  { value: "gemini", label: "关闭思考 — Gemini 2.5（budget=0）" },
  { value: "gemini_thinking", label: "低预算思考 — Gemini 2.5（budget=800）" },
  { value: "gemini_3", label: "关闭思考 — Gemini 3（level=low，不透出过程）" },
  { value: "gemini_3_thinking", label: "低档思考并透出过程 — Gemini 3（level=low）" },
  { value: "openrouter", label: "关闭思考 — OpenRouter（effort=none）" },
  { value: "openrouter_thinking", label: "低档思考 — OpenRouter（effort=low）" },
]

type StatusState = {
  loading: boolean
  phase: string
  ready: boolean
  windowFound: boolean
  windowTitle: string
  windowHwnd: number | null
  windows: { hwnd: number; title: string }[]
  selectingHwnd: number | null
  steps: number
  goal: string
  error: string
  notRunning: boolean
}

// 配置表单状态（api_key 输入框永远不回显已保存的明文，只显示打码摘要）。
type ConfigState = {
  loading: boolean
  saving: boolean
  apiBaseurl: string
  apiModel: string
  apiKeyInput: string
  textApiModel: string
  thinking: string
  textThinking: string
  keySet: boolean
  keyMasked: string
  message: string
  error: string
}

const EMPTY_CONFIG: ConfigState = {
  loading: false,
  saving: false,
  apiBaseurl: "",
  apiModel: "",
  apiKeyInput: "",
  textApiModel: "",
  thinking: "",
  textThinking: "",
  keySet: false,
  keyMasked: "",
  message: "",
  error: "",
}

// 解包 hosted-surface action 返回的 envelope（{plugin_id, action_id, result}）。
function unwrapActionResult(envelope: any): Record<string, any> {
  if (envelope && typeof envelope === "object") {
    if (envelope.result && typeof envelope.result === "object") return envelope.result
    return envelope
  }
  return {}
}

export default function PvZAgentQuickstart(props: PluginSurfaceProps) {
  const [state, setState] = useState<StatusState>({
    loading: false,
    phase: "",
    ready: false,
    windowFound: false,
    windowTitle: "",
    windowHwnd: null,
    windows: [],
    selectingHwnd: null,
    steps: 0,
    goal: "",
    error: "",
    notRunning: false,
  })
  const [cfg, setCfg] = useState<ConfigState>(EMPTY_CONFIG)
  const refreshingRef = useRef(false)
  const selectingRef = useRef(false)
  const unmountedRef = useRef(false)

  const refresh = async () => {
    if (refreshingRef.current || unmountedRef.current) return
    refreshingRef.current = true
    setState((prev) => ({ ...prev, loading: true, error: "" }))
    try {
      // Hosted surface 在 sandbox iframe 里，不能直接 fetch；用 props.api.call
      // 桥接到宿主调插件 entry（需要 permissions=["action:call"] + @ui.action）。
      const envelope = await props.api.call("pvz_get_status")
      if (unmountedRef.current) return
      const data = unwrapActionResult(envelope)
      const win = data.window && typeof data.window === "object" ? data.window : {}
      const windows = (Array.isArray(data.windows) ? data.windows : [])
        .filter((w: any) => w && typeof w === "object")
        .map((w: any) => ({ hwnd: Number(w.hwnd), title: String(w.title || "") }))
      setState({
        loading: false,
        phase: String(data.phase || ""),
        ready: Boolean(data.ready),
        windowFound: Boolean(win.found),
        windowTitle: String(win.title || ""),
        windowHwnd: win.hwnd == null ? null : Number(win.hwnd),
        windows,
        selectingHwnd: null,
        steps: Number(data.steps || 0),
        goal: String(data.goal || ""),
        error: "",
        notRunning: false,
      })
    } catch (exc: any) {
      if (unmountedRef.current) return
      const raw = String(exc?.message || exc)
      const notRunning = /PLUGIN_NOT_RUNNING|not running|not started/i.test(raw)
      setState((prev) => ({
        ...prev,
        loading: false,
        error: notRunning ? "" : raw,
        notRunning,
      }))
    } finally {
      refreshingRef.current = false
    }
  }

  // 读取当前 AI 配置（密钥只拿打码摘要，明文永不出插件）。
  const loadConfig = async () => {
    setCfg((prev) => ({ ...prev, loading: true, error: "", message: "" }))
    try {
      const envelope = await props.api.call("pvz_config_get")
      if (unmountedRef.current) return
      const data = unwrapActionResult(envelope)
      setCfg((prev) => ({
        ...prev,
        loading: false,
        apiBaseurl: String(data.api_base_url || ""),
        apiModel: String(data.api_model || ""),
        textApiModel: String(data.text_api_model || ""),
        thinking: String(data.thinking || ""),
        textThinking: String(data.text_thinking || ""),
        keySet: Boolean(data.key_set),
        keyMasked: String(data.key_masked || ""),
        apiKeyInput: "",
      }))
    } catch (exc: any) {
      if (unmountedRef.current) return
      setCfg((prev) => ({ ...prev, loading: false, error: String(exc?.message || exc) }))
    }
  }

  // 保存：api_key 输入框留空 = 不修改已保存的密钥。
  const saveConfig = async () => {
    setCfg((prev) => ({ ...prev, saving: true, error: "", message: "" }))
    try {
      const envelope = await props.api.call("pvz_config_set", {
        api_base_url: cfg.apiBaseurl,
        api_model: cfg.apiModel,
        api_key: cfg.apiKeyInput,
        text_api_model: cfg.textApiModel,
        thinking: cfg.thinking,
        text_thinking: cfg.textThinking,
      })
      if (unmountedRef.current) return
      const data = unwrapActionResult(envelope)
      setCfg((prev) => ({
        ...prev,
        saving: false,
        apiKeyInput: "",
        keyMasked: String(data.key_masked || prev.keyMasked),
        keySet: Boolean(data.key_masked) || prev.keySet,
        message:
          String(data.summary || "已保存") +
          (data.needs_restart ? "（重启插件后生效）" : ""),
      }))
    } catch (exc: any) {
      if (unmountedRef.current) return
      setCfg((prev) => ({ ...prev, saving: false, error: String(exc?.message || exc) }))
    }
  }

  useEffect(() => {
    refresh()
    loadConfig()
    const timer = window.setInterval(refresh, STATUS_REFRESH_INTERVAL_MS)
    return () => {
      unmountedRef.current = true
      window.clearInterval(timer)
    }
  }, [])

  // 手动切换游玩目标窗口（pvz_select_window：hwnd 取状态 windows 列表里的句柄）。
  const selectWindow = async (hwnd: number) => {
    if (selectingRef.current || unmountedRef.current) return
    selectingRef.current = true
    setState((prev) => ({ ...prev, selectingHwnd: hwnd }))
    try {
      await props.api.call("pvz_select_window", { hwnd })
    } catch {
      // 失败不打断：随后的 refresh 会带回最新状态/错误。
    } finally {
      selectingRef.current = false
      if (!unmountedRef.current) await refresh()
    }
  }

  const phaseTone: Tone =
    state.phase === "running" ? "success" : state.phase === "paused" ? "warning" : "default"

  return (
    <Page title="PVZ 游玩助手" subtitle="让猫娘自己玩《植物大战僵尸》。">
      {state.notRunning ? (
        <Alert tone="warning">
          插件当前未运行。请先在插件列表里启动「PVZ Agent」，再回来刷新状态。
        </Alert>
      ) : null}

      <Card title="AI 服务配置（保存到 profile 覆盖文件，密钥不进仓库）">
        <Stack>
          <Field
            label="AI 服务地址"
            help="OpenAI 兼容接口地址；视觉 / 纯文本两种模式共用（示例：https://api.deepseek.com）"
          >
            <Input
              value={cfg.apiBaseurl}
              placeholder="https://api.example.com/v1"
              onChange={(v: string) => setCfg((prev) => ({ ...prev, apiBaseurl: v }))}
            />
          </Field>
          <Field label="模型名" help="vision / text 共用；纯文本想用别的模型时填下面那行">
            <Input
              value={cfg.apiModel}
              placeholder="如 deepseek-chat / gpt-4o-mini"
              onChange={(v: string) => setCfg((prev) => ({ ...prev, apiModel: v }))}
            />
          </Field>
          <Field
            label="AI 密钥"
            help={
              cfg.keySet
                ? `已保存（${cfg.keyMasked}）。输入框留空 = 不修改；输入新值 = 覆盖。`
                : "尚未设置。密钥保存在插件目录 profiles/default.toml（用户本地文件，已排除在仓库外）"
            }
          >
            <PasswordInput
              value={cfg.apiKeyInput}
              placeholder={cfg.keySet ? "留空 = 不修改已保存的密钥" : "粘贴服务密钥（sk-...）"}
              onChange={(v: string) => setCfg((prev) => ({ ...prev, apiKeyInput: v }))}
            />
          </Field>
          <Field label="纯文本模式模型（可选）" help="留空 = 与上面模型名共用">
            <Input
              value={cfg.textApiModel}
              placeholder="留空 = 共用上面的模型名"
              onChange={(v: string) => setCfg((prev) => ({ ...prev, textApiModel: v }))}
            />
          </Field>
          <Field label="思考参数（视觉模式）" help="按你用的模型选；选错 provider 也不会报参数错误">
            <Select
              value={cfg.thinking}
              options={THINKING_OPTIONS}
              onChange={(v: string) => setCfg((prev) => ({ ...prev, thinking: v }))}
            />
          </Field>
          <Field label="思考参数（纯文本模式）" help="与视觉模式独立选择">
            <Select
              value={cfg.textThinking}
              options={THINKING_OPTIONS}
              onChange={(v: string) => setCfg((prev) => ({ ...prev, textThinking: v }))}
            />
          </Field>
          {cfg.message ? <Alert tone="success">{cfg.message}</Alert> : null}
          {cfg.error ? <Alert tone="danger">{cfg.error}</Alert> : null}
          <ButtonGroup>
            <Button onClick={loadConfig}>{cfg.loading ? "读取中…" : "重新读取"}</Button>
            <Button onClick={saveConfig} tone={cfg.saving ? "default" : "primary"}>
              {cfg.saving ? "保存中…" : "保存配置"}
            </Button>
          </ButtonGroup>
          <Text>保存后需重启插件生效（插件列表里停止再启动）。</Text>
        </Stack>
      </Card>

      <Card title="游玩状态">
        <Stack>
          <Grid cols={3}>
            <StatCard label="游玩状态" value={PHASE_LABEL[state.phase] || state.phase || "未知"} />
            <StatCard label="AI 决策" value={state.ready ? "就绪" : "未就绪"} />
            <StatCard label="已执行动作" value={state.steps} />
          </Grid>
          <Text>游戏窗口：{state.windowFound ? state.windowTitle : "未找到"}</Text>
          <Text>目标：{state.goal || "未设置"}</Text>
          {state.error ? <Alert tone="danger">{state.error}</Alert> : null}
          <ButtonGroup>
            <Button onClick={refresh}>{state.loading ? "刷新中…" : "刷新"}</Button>
            <ActionButton
              actionId="pvz_start"
              label="开始游玩"
              tone="primary"
              args={{ goal: "自动玩完当前这一关并尽可能取得胜利" }}
            />
            <ActionButton actionId="pvz_pause" label="暂停" tone="default" />
            <ActionButton actionId="pvz_stop" label="停止" tone="warning" />
          </ButtonGroup>
        </Stack>
      </Card>

      <Card title="游戏窗口（多个匹配时默认用第一个，可切换）">
        {state.windows.length === 0 ? (
          <Text>
            未找到匹配的游戏窗口。启动游戏后点「刷新」，这里会自动列出所有匹配窗口。
          </Text>
        ) : (
          <Stack>
            {state.windows.map((w) => {
              const active = state.windowHwnd === w.hwnd
              return (
                <Stack key={`win-${w.hwnd}`}>
                  <Text>
                    {active ? "✔ 当前使用：" : ""}
                    {w.title}（句柄 {w.hwnd}）
                  </Text>
                  {active ? null : (
                    <Button onClick={() => selectWindow(w.hwnd)}>
                      {state.selectingHwnd === w.hwnd ? "切换中…" : "使用此窗口"}
                    </Button>
                  )}
                </Stack>
              )
            })}
          </Stack>
        )}
      </Card>

      <Card title="怎么开始">
        <Steps>
          <Step index="1" title="打开游戏">
            启动《植物大战僵尸》。插件会按 window_titles 关键词自动识别游戏窗口
            （关键词模糊匹配 + PopCap 引擎窗口类兜底）；多个匹配时**默认用第一个**，
            也可在上方「游戏窗口」卡片里手动切换。标题特殊时把关键词加进
            pvz/config.json 的 window_titles（保存即生效）。
          </Step>
          <Step index="2" title="配置 AI 决策">
            在本页顶部「AI 服务配置」卡片填写**服务地址 / 模型 / 密钥**并保存，
            然后重启插件。vision / text 两种模式共用地址与密钥；思考参数按你用的
            模型在下拉里选（DeepSeek / OpenAI / Claude / Gemini / OpenRouter 都有对应项）。
          </Step>
          <Step index="3" title="手动选卡后开始">
            默认**选卡由你手动操作**（agent_controls_seed_selection=false，选卡不触发 LLM）：
            在游戏里选好卡进入战斗后，对猫娘说“去玩植物大战僵尸吧”，或点上面的「开始游玩」。
            （想让猫娘自动选卡，把该配置改为 true 并重启。）
          </Step>
        </Steps>
      </Card>

      <Card title="她会怎么做">
        <Text>
          开始游玩后，插件每隔几秒截一张游戏画面推给猫娘。猫娘会直接说出她现在的打法
          （比如“我在第 2 行种一棵豌豆射手”），并自己操作游戏，不会反复问你该怎么做。
        </Text>
        <Text>
          你随时可以在对话里让她调整，比如“这波先攒阳光”“寒冰射手守第二行”，或者
          说“暂停一下”“停吧”。
        </Text>
      </Card>

      <Card title="排障">
        <Steps>
          <Step index="1" title="状态一直显示“未找到窗口”">
            确认游戏已打开；匹配到的窗口会列在上方「游戏窗口」卡片，可手动切换使用哪个。
            若列表为空，把窗口标题里的关键词加进插件配置或 pvz/config.json 的
            window_titles（保存即生效）。
          </Step>
          <Step index="2" title="AI 决策未就绪">
            说明还没配置 AI 决策：在本页顶部「AI 服务配置」卡片填好并保存，重启插件。
          </Step>
          <Step index="3" title="纯文本模式提示“内存连接失败”">
            确认游戏已启动且为受支持的版本；刚启动游戏的话稍等片刻重试，或重启插件。
          </Step>
          <Step index="4" title="点「开始游玩」没反应">
            先确认插件已在运行（列表页启动），再确认游戏窗口与 AI 决策都已就绪。
          </Step>
        </Steps>
      </Card>

      <Card title="其它配置（plugin.toml [pvz_agent]）">
        <KeyValue
          items={[
            { key: "mode", label: "运行模式", value: '"text"=纯文本内存(默认) / "vision"=视觉' },
            { key: "agent_controls_seed_selection", label: "AgentB 操控选卡", value: "false(默认，手动选卡)" },
            { key: "tool_call_mode", label: "工具调用", value: '"fc"=原生函数调用 / "regex"=简化正则' },
            { key: "screenshot_feed_enabled", label: "被动推画面", value: "true（8 秒，画面变了才推）" },
            { key: "screenshot_nudge_enabled", label: "主动催猫娘行动", value: "true（5 秒）" },
            { key: "sun_auto_collect", label: "自动收阳光", value: "true" },
            { key: "window_titles", label: "窗口标题", value: "植物大战僵尸 / Plants vs. Zombies 等关键词" },
          ]}
        />
      </Card>

      <Warning>
        猫娘的 AI 决策需要能联网调用 AI 服务（在本页「AI 服务配置」卡片配置）。没配置时
        面板会显示“AI 决策未就绪”，点「开始游玩」会提示错误。
      </Warning>

      <Alert tone="info">
        **纯文本模式已经可以用了**（mode="text"，当前默认）：不用视觉模型 / OpenCV，一切
        状态与触发靠读游戏内存（pvz/vendor/pvz_memory）——精确拿到阳光/卡片/植物/僵尸血量/
        波次，用**纯文本 LLM** 决策，动作走代码注入执行；
        但**仍照常把游戏截图推给猫娘**供她看画面指挥。
        特点：LLM 思考期间不冻结游戏、窗口失焦也不暂停、非战斗界面不喂 LLM 只轮询等待。
        注意：使用受支持的游戏版本体验最佳。
      </Alert>
    </Page>
  )
}
