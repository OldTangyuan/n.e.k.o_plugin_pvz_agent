import {
  ActionButton,
  Alert,
  Button,
  ButtonGroup,
  Card,
  Grid,
  KeyValue,
  Page,
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

// PVZ 游玩助手插件面板「快速开始」教程。zh-CN 单语言（与插件现有中文文案一致）。
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

type StatusState = {
  loading: boolean
  phase: string
  ready: boolean
  windowFound: boolean
  windowTitle: string
  steps: number
  goal: string
  error: string
  notRunning: boolean
  envPath: string
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
    steps: 0,
    goal: "",
    error: "",
    notRunning: false,
    envPath: "",
  })
  const refreshingRef = useRef(false)
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
      const cfgPaths = data.config_paths && typeof data.config_paths === "object" ? data.config_paths : {}
      setState({
        loading: false,
        phase: String(data.phase || ""),
        ready: Boolean(data.ready),
        windowFound: Boolean(win.found),
        windowTitle: String(win.title || ""),
        steps: Number(data.steps || 0),
        goal: String(data.goal || ""),
        error: "",
        notRunning: false,
        envPath: String(cfgPaths.env || ""),
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

  useEffect(() => {
    refresh()
    const timer = window.setInterval(refresh, STATUS_REFRESH_INTERVAL_MS)
    return () => {
      unmountedRef.current = true
      window.clearInterval(timer)
    }
  }, [])

  const phaseTone: Tone =
    state.phase === "running" ? "success" : state.phase === "paused" ? "warning" : "default"

  return (
    <Page title="PVZ 游玩助手" subtitle="让猫娘自己玩《植物大战僵尸》。">
      {state.notRunning ? (
        <Alert tone="warning">
          插件当前未运行。请先在插件列表里启动「PVZ Agent」，再回来刷新状态。
        </Alert>
      ) : null}

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

      <Card title="怎么开始">
        <Steps>
          <Step index="1" title="打开游戏">
            启动《植物大战僵尸》**原版 1.0.0.1051**（**最稳定**；其它受支持版本可玩但读内存
            可能不稳，**杂交版正在适配中**）。插件会按 window_titles 里的精确标题轮询查找游戏
            窗口；标题不符时把窗口精确标题写进 pvz/config.json 的 window_titles。
          </Step>
          <Step index="2" title="配置 AI 决策">
            把插件目录 pvz/.env.example 复制为 **pvz/.env**（路径见上方「配置文件位置」卡片），
            填入 AI 服务地址与密钥。**纯文本模式填 TEXT_VLM_MODEL**（可只填这一个，URL/密钥
            复用 VLM_*）；视觉模式填 VLM_MODEL。不配置则无法自主决策。
          </Step>
          <Step index="3" title="（纯文本模式）管理员运行">
            读内存 + 代码注入需要**以管理员身份**运行宿主，否则会提示“内存连接失败”。
          </Step>
          <Step index="4" title="手动选卡后开始">
            默认**选卡由你手动操作**（agent_controls_seed_selection=false，选卡不触发 LLM）：
            在游戏里选好卡进入战斗后，对猫娘说“去玩植物大战僵尸吧”，或点上面的「开始游玩」。
            （想让猫娘自动选卡，把该配置改为 true 并重启。）
          </Step>
        </Steps>
      </Card>

      {state.envPath ? (
        <Card title="配置文件位置（.env 在这里）">
          <Text>把 `pvz/.env.example` 复制为下面的文件并填写 AI 密钥/模型：</Text>
          <Text>{state.envPath}</Text>
          <Text>
            其它配置：`plugin.toml` 的 [pvz_agent] 段（插件开关）、`pvz/config.json`
            （核心行为/布局）。改完保存后**重启插件**生效。
          </Text>
        </Card>
      ) : null}

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
            确认游戏已打开、没最小化；窗口标题需与 window_titles 里的**精确标题**一致
            （可在插件配置或 pvz/config.json 的 window_titles 里补充，保存即生效）。
          </Step>
          <Step index="2" title="AI 决策未就绪">
            说明还没配置 AI 决策：纯文本模式在 pvz/.env 填 TEXT_VLM_MODEL，视觉模式填
            VLM_MODEL，填好并重启插件。
          </Step>
          <Step index="3" title="纯文本模式提示“内存连接失败”">
            ① 需以**管理员身份**运行宿主；② 确认游戏是**受支持版本**（建议原版，
            杂交版正在适配中）。
          </Step>
          <Step index="4" title="点「开始游玩」没反应">
            先确认插件已在运行（列表页启动），再确认游戏窗口与 AI 决策都已就绪。
          </Step>
        </Steps>
      </Card>

      <Card title="配置（plugin.toml [pvz_agent]）">
        <KeyValue
          items={[
            { key: "mode", label: "运行模式", value: '"text"=纯文本内存(默认) / "vision"=视觉' },
            { key: "agent_controls_seed_selection", label: "AgentB 操控选卡", value: "false(默认，手动选卡)" },
            { key: "tool_call_mode", label: "工具调用", value: '"fc"=原生函数调用 / "regex"=简化正则' },
            { key: "screenshot_feed_enabled", label: "被动推画面", value: "true（8 秒，画面变了才推）" },
            { key: "screenshot_nudge_enabled", label: "主动催猫娘行动", value: "true（5 秒）" },
            { key: "sun_auto_collect", label: "自动收阳光", value: "true" },
            { key: "window_titles", label: "窗口标题", value: "植物大战僵尸 / pvz / 杂交版" },
          ]}
        />
      </Card>

      <Warning>
        猫娘的 AI 决策需要能联网调用 AI 服务（在 pvz/.env 配置）。没配置时面板会显示
        “AI 决策未就绪”，点「开始游玩」会提示错误。
      </Warning>

      <Alert tone="info">
        **纯文本模式已经可以用了**（mode="text"，当前默认）：不用视觉模型 / OpenCV，一切
        状态与触发靠读游戏内存（pvz/vendor/pvz_memory）——精确拿到阳光/卡片/植物/僵尸血量/
        波次，用**纯文本 LLM** 决策（默认开启思考模式、更多上下文），动作走代码注入执行；
        但**仍照常把游戏截图推给猫娘**供她看画面指挥。
        特点：LLM 思考期间不冻结游戏、窗口失焦也不暂停、非战斗界面不喂 LLM 只轮询等待。
        注意：需以**管理员身份**运行宿主 + **受支持版本**（建议原版；**杂交版正在适配中**）。
      </Alert>
    </Page>
  )
}
