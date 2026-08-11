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
            启动《植物大战僵尸》（原版 / 杂交版均可），游戏窗口标题需含
            “植物大战僵尸 / pvz / 杂交版”（可在插件配置里改 window_title_keywords）。
          </Step>
          <Step index="2" title="配置 AI 决策">
            在插件目录 pvz/.env 填入你的 AI 服务地址与密钥（复制 pvz/.env.example
            为 pvz/.env 后填写）。不配置的话猫娘无法自主决策，游玩无法开始。
          </Step>
          <Step index="3" title="对猫娘说一句">
            在对话里说“去玩植物大战僵尸吧”，或直接点上面的「开始游玩」。
            之后猫娘会周期性看游戏画面，主动告诉你她接下来的操作，并自己打下去。
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
            确认游戏已打开、没最小化；窗口标题需匹配关键词，可在插件配置
            window_title_keywords 里补充。
          </Step>
          <Step index="2" title="AI 决策未就绪">
            说明还没配置 AI 决策：在 pvz/.env 填好并重启插件。
          </Step>
          <Step index="3" title="点「开始游玩」没反应">
            先确认插件已在运行（列表页启动），再确认游戏窗口与 AI 决策都已就绪。
          </Step>
        </Steps>
      </Card>

      <Card title="配置（plugin.toml [pvz_agent]）">
        <KeyValue
          items={[
            { key: "screenshot_feed_enabled", label: "被动推画面", value: "true（8 秒，画面变了才推）" },
            { key: "screenshot_nudge_enabled", label: "主动催猫娘行动", value: "true（5 秒）" },
            { key: "sun_auto_collect", label: "自动收阳光", value: "true" },
            { key: "window_title_keywords", label: "窗口关键词", value: "植物大战僵尸 / pvz / 杂交版" },
          ]}
        />
      </Card>

      <Warning>
        猫娘的 AI 决策需要能联网调用 AI 服务（在 pvz/.env 配置）。没配置时面板会显示
        “AI 决策未就绪”，点「开始游玩」会提示错误。
      </Warning>
    </Page>
  )
}
