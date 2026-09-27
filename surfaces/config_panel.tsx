import {
  Alert,
  Button,
  ButtonGroup,
  Card,
  Field,
  Input,
  KeyValue,
  Page,
  PasswordInput,
  Select,
  Stack,
  Text,
  Warning,
  useEffect,
  useRef,
  useState,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps, Tone } from "@neko/plugin-ui"

// PVZ 游玩助手插件面板「配置面板」。zh-CN 单语言。
// 覆盖 plugin.toml [pvz_agent] 的全部配置项；经 props.api.call 调
// pvz_config_get / pvz_config_set（__init__.py @ui.action 暴露）。
// 保存写入 profiles/default.toml（与宿主 GUI 配置界面同一文件），重启插件生效。

// 思考参数预设（与 pvz/pvz_agent/vlm.py 的 THINKING_EXTRA_BODY_PRESETS 一一对应）。
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
  { value: "openrouter_thinking", label: "关闭思考 — OpenRouter（effort=low）" },
]

const YESNO: { value: string; label: string }[] = [
  { value: "true", label: "开启" },
  { value: "false", label: "关闭" },
]

const MODE_OPTIONS: { value: string; label: string }[] = [
  { value: "text", label: "纯文本内存方案（推荐）：读内存 + 纯文本 LLM 决策" },
  { value: "vision", label: "OpenCV 视觉方案：截图识别 + 视觉模型决策" },
]

const PLANTING_OPTIONS: { value: string; label: string }[] = [
  { value: "mouseclick", label: "模拟鼠标点击（推荐，0.3.0 路线，游戏自管阳光/冷却）" },
  { value: "putplant", label: "PutPlant 直接注入（绕过 UI，副作用多，非原版自动退化到此）" },
]

const TOOLCALL_OPTIONS: { value: string; label: string }[] = [
  { value: "fc", label: "原生函数调用（function calling）" },
  { value: "regex", label: "简化正则提取 <tool_call>" },
]

const CARDPOS_OPTIONS: { value: string; label: string }[] = [
  { value: "opencv", label: "OpenCV 实时扫描（传送带/变布局适配，失败回退固定坐标）" },
  { value: "fixed", label: "固定坐标（按配置的卡片位置直接点击）" },
]

type FormState = {
  loading: boolean
  saving: boolean
  message: string
  error: string
  apiKeySet: boolean
  apiKeyMasked: string
  textApiKeySet: boolean
  textApiKeyMasked: string
  // 表单值（布尔/数字都以字符串承载，保存时转换）
  mode: string
  plantingMode: string
  toolCallMode: string
  autoStart: string
  agentSelects: string
  apiBaseurl: string
  apiModel: string
  apiKeyInput: string
  textApiBaseurl: string
  textApiModel: string
  textApiKeyInput: string
  thinking: string
  textThinking: string
  feedEnabled: string
  feedInterval: string
  nudgeEnabled: string
  nudgeInterval: string
  nudgeText: string
  maxEdgePx: string
  jpegQuality: string
  windowTitles: string
  sunAutoCollect: string
  scanGrid: string
  scanCards: string
  cardPositionMode: string
  notifyTerminate: string
  notifyWindowLost: string
}

const EMPTY_FORM: FormState = {
  loading: false,
  saving: false,
  message: "",
  error: "",
  apiKeySet: false,
  apiKeyMasked: "",
  textApiKeySet: false,
  textApiKeyMasked: "",
  mode: "text",
  plantingMode: "mouseclick",
  toolCallMode: "fc",
  autoStart: "false",
  agentSelects: "false",
  apiBaseurl: "",
  apiModel: "",
  apiKeyInput: "",
  textApiBaseurl: "",
  textApiModel: "",
  textApiKeyInput: "",
  thinking: "",
  textThinking: "",
  feedEnabled: "true",
  feedInterval: "8",
  nudgeEnabled: "true",
  nudgeInterval: "5",
  nudgeText: "",
  maxEdgePx: "0",
  jpegQuality: "95",
  windowTitles: "",
  sunAutoCollect: "true",
  scanGrid: "true",
  scanCards: "true",
  cardPositionMode: "opencv",
  notifyTerminate: "true",
  notifyWindowLost: "true",
}

function unwrapActionResult(envelope: any): Record<string, any> {
  if (envelope && typeof envelope === "object") {
    if (envelope.result && typeof envelope.result === "object") return envelope.result
    return envelope
  }
  return {}
}

export default function PvZAgentConfigPanel(props: PluginSurfaceProps) {
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const loadingRef = useRef(false)
  const unmountedRef = useRef(false)

  const set = (patch: Partial<FormState>) => setForm((prev) => ({ ...prev, ...patch }))

  const loadConfig = async () => {
    if (loadingRef.current || unmountedRef.current) return
    loadingRef.current = true
    set({ loading: true, error: "", message: "" })
    try {
      const envelope = await props.api.call("pvz_config_get")
      if (unmountedRef.current) return
      const data = unwrapActionResult(envelope)
      const boolStr = (v: any, dflt: boolean) =>
        v === undefined || v === null ? String(dflt) : String(Boolean(v))
      set({
        loading: false,
        error: "",
        mode: String(data.mode || "text"),
        plantingMode: String(data.planting_mode || "mouseclick"),
        toolCallMode: String(data.tool_call_mode || "fc"),
        autoStart: boolStr(data.auto_start, false),
        agentSelects: boolStr(data.agent_controls_seed_selection, false),
        apiBaseurl: String(data.api_base_url || ""),
        apiModel: String(data.api_model || ""),
        apiKeyInput: "",
        textApiBaseurl: String(data.text_api_base_url || ""),
        textApiModel: String(data.text_api_model || ""),
        textApiKeyInput: "",
        thinking: String(data.thinking || ""),
        textThinking: String(data.text_thinking || ""),
        feedEnabled: boolStr(data.screenshot_feed_enabled, true),
        feedInterval: String(data.screenshot_feed_interval ?? 8),
        nudgeEnabled: boolStr(data.screenshot_nudge_enabled, true),
        nudgeInterval: String(data.screenshot_nudge_interval ?? 5),
        nudgeText: String(data.screenshot_nudge_text || ""),
        maxEdgePx: String(data.screenshot_max_edge_px ?? 0),
        jpegQuality: String(data.screenshot_jpeg_quality ?? 95),
        windowTitles: Array.isArray(data.window_titles)
          ? (data.window_titles as string[]).join("，")
          : String(data.window_titles || ""),
        sunAutoCollect: boolStr(data.sun_auto_collect, true),
        scanGrid: boolStr(data.scan_grid_enabled, true),
        scanCards: boolStr(data.scan_cards_enabled, true),
        cardPositionMode: String(data.card_position_mode || "opencv"),
        notifyTerminate: boolStr(data.notify_on_terminate, true),
        notifyWindowLost: boolStr(data.notify_window_lost, true),
        apiKeySet: Boolean(data.api_key_set),
        apiKeyMasked: String(data.api_key_masked || ""),
        textApiKeySet: Boolean(data.text_api_key_set),
        textApiKeyMasked: String(data.text_api_key_masked || ""),
      })
    } catch (exc: any) {
      if (unmountedRef.current) return
      set({ loading: false, error: String(exc?.message || exc) })
    } finally {
      loadingRef.current = false
    }
  }

  const saveConfig = async () => {
    set({ saving: true, error: "", message: "" })
    try {
      const numOrUndef = (v: string) => {
        const n = Number(v)
        return Number.isFinite(n) ? n : undefined
      }
      const payload: Record<string, any> = {
        mode: form.mode,
        planting_mode: form.plantingMode,
        tool_call_mode: form.toolCallMode,
        auto_start: form.autoStart === "true",
        agent_controls_seed_selection: form.agentSelects === "true",
        api_base_url: form.apiBaseurl,
        api_model: form.apiModel,
        api_key: form.apiKeyInput,
        text_api_base_url: form.textApiBaseurl,
        text_api_model: form.textApiModel,
        text_api_key: form.textApiKeyInput,
        thinking: form.thinking,
        text_thinking: form.textThinking,
        notify_on_terminate: form.notifyTerminate === "true",
        notify_window_lost: form.notifyWindowLost === "true",
        sun_auto_collect: form.sunAutoCollect === "true",
        scan_grid_enabled: form.scanGrid === "true",
        scan_cards_enabled: form.scanCards === "true",
        screenshot_feed_enabled: form.feedEnabled === "true",
        screenshot_feed_interval: numOrUndef(form.feedInterval),
        screenshot_nudge_enabled: form.nudgeEnabled === "true",
        screenshot_nudge_interval: numOrUndef(form.nudgeInterval),
        screenshot_nudge_text: form.nudgeText,
        screenshot_max_edge_px: numOrUndef(form.maxEdgePx),
        screenshot_jpeg_quality: numOrUndef(form.jpegQuality),
        window_titles: form.windowTitles
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean),
      }
      Object.keys(payload).forEach((k) => payload[k] === undefined && delete payload[k])
      const envelope = await props.api.call("pvz_config_set", payload)
      if (unmountedRef.current) return
      const data = unwrapActionResult(envelope)
      set({
        saving: false,
        apiKeyInput: "",
        textApiKeyInput: "",
        apiKeySet: Boolean(data.saved && (data.saved as string[]).includes("api_key"))
          || form.apiKeySet,
        message:
          String(data.summary || "已保存") +
          (data.needs_restart ? "——重启插件后生效（插件列表里停止再启动）" : ""),
      })
    } catch (exc: any) {
      if (unmountedRef.current) return
      set({ saving: false, error: String(exc?.message || exc) })
    }
  }

  useEffect(() => {
    loadConfig()
    return () => {
      unmountedRef.current = true
    }
  }, [])

  const phaseTone: Tone = "default"

  return (
    <Page title="PVZ 配置面板" subtitle="覆盖 plugin.toml [pvz_agent] 的全部配置项；保存写入 profiles/default.toml。">
      {form.error ? <Alert tone="danger">{form.error}</Alert> : null}
      {form.message ? <Alert tone="success">{form.message}</Alert> : null}

      <Card title="运行方式">
        <Stack>
          <Field label="运行模式 (mode)" help="text=读内存+纯文本 LLM（推荐，需管理员权限）；vision=OpenCV 截图+视觉模型">
            <Select value={form.mode} options={MODE_OPTIONS} onChange={(v: string) => set({ mode: v })} />
          </Field>
          <Field label="种植模式 (planting_mode)" help="模拟点击=游戏自管阳光/冷却（0.3.0 手感）；直注=插件手动修补副作用">
            <Select value={form.plantingMode} options={PLANTING_OPTIONS} onChange={(v: string) => set({ plantingMode: v })} />
          </Field>
          <Field label="工具调用模式 (tool_call_mode)" help="模型输出动作的解析方式">
            <Select value={form.toolCallMode} options={TOOLCALL_OPTIONS} onChange={(v: string) => set({ toolCallMode: v })} />
          </Field>
          <Field label="插件启动时自动开始游玩 (auto_start)">
            <Select value={form.autoStart} options={YESNO} onChange={(v: string) => set({ autoStart: v })} />
          </Field>
          <Field label="让猫娘自动选卡 (agent_controls_seed_selection)" help="关闭=选卡界面不触发 LLM，你手动选卡（推荐）">
            <Select value={form.agentSelects} options={YESNO} onChange={(v: string) => set({ agentSelects: v })} />
          </Field>
        </Stack>
      </Card>

      <Card title="AI 决策服务（vision / text 共用；text 可单独覆盖）">
        <Stack>
          <Field label="服务地址 (api_base_url)" help="OpenAI 兼容接口地址，如 https://api.deepseek.com">
            <Input value={form.apiBaseurl} placeholder="https://api.example.com/v1" onChange={(v: string) => set({ apiBaseurl: v })} />
          </Field>
          <Field label="模型名 (api_model)" help="如 deepseek-chat / gpt-4o-mini">
            <Input value={form.apiModel} placeholder="模型名" onChange={(v: string) => set({ apiModel: v })} />
          </Field>
          <Field
            label="API 密钥 (api_key)"
            help={
              form.apiKeySet
                ? `已保存（${form.apiKeyMasked}）。留空 = 不修改；输入新值 = 覆盖。`
                : "尚未设置（若 pvz/.env 里配了 VLM_API_KEY 也可用）。密钥保存到 profiles/default.toml"
            }
          >
            <PasswordInput
              value={form.apiKeyInput}
              placeholder={form.apiKeySet ? "留空 = 不修改已保存的密钥" : "粘贴服务密钥（sk-...）"}
              onChange={(v: string) => set({ apiKeyInput: v })}
            />
          </Field>
          <Field label="纯文本模式地址 (text_api_base_url)" help="留空 = 共用上面的服务地址">
            <Input value={form.textApiBaseurl} placeholder="留空 = 共用" onChange={(v: string) => set({ textApiBaseurl: v })} />
          </Field>
          <Field label="纯文本模式模型 (text_api_model)" help="留空 = 共用上面的模型名">
            <Input value={form.textApiModel} placeholder="留空 = 共用" onChange={(v: string) => set({ textApiModel: v })} />
          </Field>
          <Field
            label="纯文本模式密钥 (text_api_key)"
            help={
              form.textApiKeySet
                ? `已保存（${form.textApiKeyMasked}）。留空 = 不修改。`
                : "留空 = 共用上面的密钥"
            }
          >
            <PasswordInput
              value={form.textApiKeyInput}
              placeholder={form.textApiKeySet ? "留空 = 不修改" : "留空 = 共用上面的密钥"}
              onChange={(v: string) => set({ textApiKeyInput: v })}
            />
          </Field>
          <Field label="思考参数 — 视觉模式 (thinking)" help="按模型选；选错 provider 也不会报参数错误">
            <Select value={form.thinking} options={THINKING_OPTIONS} onChange={(v: string) => set({ thinking: v })} />
          </Field>
          <Field label="思考参数 — 纯文本模式 (text_thinking)" help="与视觉模式独立选择">
            <Select value={form.textThinking} options={THINKING_OPTIONS} onChange={(v: string) => set({ textThinking: v })} />
          </Field>
        </Stack>
      </Card>

      <Card title="观察通道（推给猫娘的画面）">
        <Stack>
          <Field label="被动推送画面 (screenshot_feed_enabled)" help="周期把最新游戏截图推进猫娘视野（不打断）">
            <Select value={form.feedEnabled} options={YESNO} onChange={(v: string) => set({ feedEnabled: v })} />
          </Field>
          <Field label="被动推送间隔秒 (screenshot_feed_interval)" help="画面没变化时不重复推">
            <Input value={form.feedInterval} onChange={(v: string) => set({ feedInterval: v })} />
          </Field>
          <Field label="主动催猫娘行动 (screenshot_nudge_enabled)" help="周期推截图+短触发文本，唤起猫娘继续玩">
            <Select value={form.nudgeEnabled} options={YESNO} onChange={(v: string) => set({ nudgeEnabled: v })} />
          </Field>
          <Field label="催行动间隔秒 (screenshot_nudge_interval)">
            <Input value={form.nudgeInterval} onChange={(v: string) => set({ nudgeInterval: v })} />
          </Field>
          <Field label="催行动触发文本 (screenshot_nudge_text)" help="{MASTER_NAME} 会替换为对用户的称呼">
            <Input value={form.nudgeText} onChange={(v: string) => set({ nudgeText: v })} />
          </Field>
          <Field label="截图最大边长像素 (screenshot_max_edge_px)" help="0 = 原图不缩放">
            <Input value={form.maxEdgePx} onChange={(v: string) => set({ maxEdgePx: v })} />
          </Field>
          <Field label="推送 JPEG 质量 (screenshot_jpeg_quality)" help="1~95，越高越清晰、越大越占带宽">
            <Input value={form.jpegQuality} onChange={(v: string) => set({ jpegQuality: v })} />
          </Field>
        </Stack>
      </Card>

      <Card title="游戏识别与操作">
        <Stack>
          <Field label="窗口标题关键词 (window_titles)" help="逗号分隔；模糊匹配 + PopCap 引擎窗口类兜底">
            <Input value={form.windowTitles} placeholder="植物大战僵尸，pvz，Plants vs. Zombies" onChange={(v: string) => set({ windowTitles: v })} />
          </Field>
          <Field label="自动收集阳光 (sun_auto_collect)" help="关闭时由模型/你手动收">
            <Select value={form.sunAutoCollect} options={YESNO} onChange={(v: string) => set({ sunAutoCollect: v })} />
          </Field>
          <Field label="棋盘格扫描 (scan_grid_enabled)">
            <Select value={form.scanGrid} options={YESNO} onChange={(v: string) => set({ scanGrid: v })} />
          </Field>
          <Field label="卡片扫描 (scan_cards_enabled)">
            <Select value={form.scanCards} options={YESNO} onChange={(v: string) => set({ scanCards: v })} />
          </Field>
          <Field label="卡片定位方式 (card_position_mode)" help="vision 模式种植时用">
            <Select value={form.cardPositionMode} options={CARDPOS_OPTIONS} onChange={(v: string) => set({ cardPositionMode: v })} />
          </Field>
        </Stack>
      </Card>

      <Card title="通知">
        <Stack>
          <Field label="通关/失败通知 (notify_on_terminate)">
            <Select value={form.notifyTerminate} options={YESNO} onChange={(v: string) => set({ notifyTerminate: v })} />
          </Field>
          <Field label="游戏窗口丢失通知 (notify_window_lost)">
            <Select value={form.notifyWindowLost} options={YESNO} onChange={(v: string) => set({ notifyWindowLost: v })} />
          </Field>
        </Stack>
      </Card>

      <Card title="保存">
        <Stack>
          {form.message ? <Alert tone="success">{form.message}</Alert> : null}
          {form.error ? <Alert tone="danger">{form.error}</Alert> : null}
          <ButtonGroup>
            <Button onClick={loadConfig}>{form.loading ? "读取中…" : "重新读取"}</Button>
            <Button onClick={saveConfig} tone={form.saving ? "default" : "primary"}>
              {form.saving ? "保存中…" : "保存全部配置"}
            </Button>
          </ButtonGroup>
          <Text tone={phaseTone as any}>
            保存写入插件目录 profiles/default.toml（与宿主 GUI 配置界面同一文件，直接编辑
            plugin.toml 的改动也仍然有效）。保存后需重启插件生效。
          </Text>
        </Stack>
      </Card>

      <Warning>
        密钥只保存在插件本地的 profiles/default.toml / pvz/.env，不会进仓库；面板永远不回显明文。
      </Warning>
      <Alert tone="info">
        配置项与 plugin.toml [pvz_agent] 一一对应；直接编辑 plugin.toml 依然可用
        （两边都没填的键走内置默认，任何一边填了都会生效）。
      </Alert>
    </Page>
  )
}
