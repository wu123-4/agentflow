// 全局状态：会话列表、当前会话、消息、流式状态、工具调用映射、HITL 待确认、任务契约、运行元信息。
import { create } from 'zustand'
import type {
  BudgetExhaustedEvent,
  Channel,
  ChannelSelectedEvent,
  ChatMessage,
  ContextCompressedEvent,
  EscalationRecord,
  FallbackEvent,
  InterruptEvent,
  ResumeDecision,
  RunFinishedEvent,
  RunMeta,
  Session,
  TaskContract,
  ToolCall,
  WrapUpEvent,
} from './types'
import * as api from './lib/api'
import type { SseEvent } from './lib/sse'

interface AppState {
  sessions: Session[]
  currentSessionId: string | null
  messages: ChatMessage[]
  streaming: boolean
  /** 状态条文案（由 state 事件驱动），null 表示不显示 */
  statusText: string | null
  /** 全局错误提示（红色条） */
  error: string | null
  /** 轻量提示条（降级/上下文压缩等 Harness 事件，蓝色条，可关闭） */
  notice: string | null
  /** call_id -> 所属 assistant 消息 id（工具卡片状态流转的映射） */
  toolCallOwner: Record<string, string>
  /** HITL 待确认请求（interrupt 事件），非空时弹出确认卡片 */
  pendingInterrupt: InterruptEvent | null
  /** 下一次发送携带的任务契约（null=默认契约） */
  contractDraft: TaskContract | null
  /** 下一次发送指定的通道（null=由 router 自动判定；指定即锁定不升级） */
  channelDraft: Channel | null
  /** 当前运行使用的通道（channel_selected / escalated 驱动） */
  activeChannel: Channel | null
  /** 最近一次 run 的终态元信息（门控/预算/降级），供 RunStatusBar 渲染 */
  lastRunMeta: RunMeta | null

  loadSessions: () => Promise<void>
  selectSession: (id: string) => Promise<void>
  newSession: () => Promise<void>
  rename: (id: string, title: string) => Promise<void>
  remove: (id: string) => Promise<void>
  send: (content: string) => Promise<void>
  resume: (decision: ResumeDecision, editedArgs?: Record<string, unknown>, reason?: string) => Promise<void>
  stop: () => void
  setContractDraft: (c: TaskContract | null) => void
  setChannelDraft: (c: Channel | null) => void
  dismissNotice: () => void
}

let abort: AbortController | null = null
let seq = 0
const tmpId = () => `local_${Date.now()}_${++seq}`

/** 把 SSE 的 data 字段安全解析为 JSON */
function parseData<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

export const useApp = create<AppState>((set, get) => {
  /** 向当前流式 assistant 消息追加文本 */
  const appendToken = (text: string) => {
    set((s) => {
      const msgs = [...s.messages]
      const last = msgs[msgs.length - 1]
      if (last && last.role === 'assistant') {
        msgs[msgs.length - 1] = { ...last, content: last.content + text }
      }
      return { messages: msgs }
    })
  }

  /** 更新当前流式 assistant 消息上的工具调用 */
  const patchToolCall = (callId: string, patch: Partial<ToolCall>) => {
    const s = get()
    const owner = s.toolCallOwner[callId]
    if (!owner) return
    set({
      messages: s.messages.map((m) =>
        m.id === owner
          ? {
              ...m,
              tool_calls: (m.tool_calls ?? []).map((tc) =>
                tc.call_id === callId ? { ...tc, ...patch } : tc,
              ),
            }
          : m,
      ),
    })
  }

  /** SSE 事件分发 */
  const handleEvent = (ev: SseEvent) => {
    switch (ev.event) {
      case 'run_started':
        break
      case 'state': {
        const d = parseData<{ node: string; status: string }>(ev.data)
        if (!d) break
        if (d.status === 'running') {
          set({ statusText: d.node === 'tools' ? '正在调用工具…' : '正在思考…' })
        } else if (d.status === 'finished') {
          set({ statusText: null })
        }
        break
      }
      case 'token': {
        const d = parseData<{ text: string }>(ev.data)
        if (d?.text) appendToken(d.text)
        break
      }
      case 'tool_start': {
        const d = parseData<{ call_id: string; tool: string; args: unknown }>(ev.data)
        if (!d) break
        const s = get()
        const msgs = [...s.messages]
        const last = msgs[msgs.length - 1]
        const call: ToolCall = {
          call_id: d.call_id,
          tool: d.tool,
          args: d.args,
          status: 'running',
        }
        if (last && last.role === 'assistant') {
          msgs[msgs.length - 1] = { ...last, tool_calls: [...(last.tool_calls ?? []), call] }
          set({
            messages: msgs,
            toolCallOwner: { ...s.toolCallOwner, [d.call_id]: last.id },
          })
        }
        break
      }
      case 'tool_end': {
        const d = parseData<{
          call_id: string
          status: ToolCall['status']
          tool?: string
          duration_ms?: number
          result_preview?: string
        }>(ev.data)
        if (!d) break
        if (!get().toolCallOwner[d.call_id] && d.tool) {
          // 被 Harness 拦截的调用没有 tool_start，这里直接合成一张终态卡片
          const s = get()
          const msgs = [...s.messages]
          const last = msgs[msgs.length - 1]
          if (last && last.role === 'assistant') {
            const call: ToolCall = {
              call_id: d.call_id,
              tool: d.tool,
              args: null,
              status: d.status,
              duration_ms: d.duration_ms,
              result: d.result_preview,
            }
            msgs[msgs.length - 1] = { ...last, tool_calls: [...(last.tool_calls ?? []), call] }
            set({
              messages: msgs,
              toolCallOwner: { ...s.toolCallOwner, [d.call_id]: last.id },
            })
          }
          break
        }
        patchToolCall(d.call_id, {
          status: d.status,
          duration_ms: d.duration_ms,
          result: d.result_preview,
        })
        break
      }
      case 'channel_selected': {
        const d = parseData<ChannelSelectedEvent>(ev.data)
        if (!d) break
        // 显式指定即锁定（不会升级）；自动判定则如实展示 router 结果
        set({ activeChannel: d.channel })
        break
      }
      case 'escalated': {
        const d = parseData<EscalationRecord>(ev.data)
        if (!d) break
        set({
          activeChannel: d.to,
          notice: `通道升级：${d.from} → ${d.to}（${d.reason}）`,
        })
        break
      }
      case 'budget_exhausted': {
        const d = parseData<BudgetExhaustedEvent>(ev.data)
        if (d) set({ notice: `通道预算耗尽（${d.reason}）：${d.channel ?? ''}` })
        break
      }
      case 'wrap_up': {
        const d = parseData<WrapUpEvent>(ev.data)
        if (d) {
          set({ notice: `已进入收尾（${d.reason ?? '预算用尽'}）：基于已有信息作答，不再调用工具` })
        }
        break
      }
      case 'run_finished': {
        const d = parseData<RunFinishedEvent>(ev.data)
        if (!d) break
        const meta: RunMeta = {
          stop_reason: d.stop_reason,
          gate_result: d.gate_result,
          budget: d.budget,
          model_used: d.model_used,
          fallback_count: d.fallback_count,
          denied_count: d.denied_count,
          channel: d.channel,
          escalations: d.escalations,
          wrap_up: d.wrap_up,
        }
        const s = get()
        const msgs = [...s.messages]
        const last = msgs[msgs.length - 1]
        if (last && last.role === 'assistant') {
          msgs[msgs.length - 1] = { ...last, usage: d.usage, run_meta: meta }
          set({ messages: msgs })
        }
        // gate_blocked / budget_exceeded 用轻量提示条说明（终态仍由消息气泡渲染）
        if (d.stop_reason === 'gate_blocked') {
          set({ notice: `完成门控未通过：${d.gate_result?.blocked_by ?? '未知原因'}` })
        } else if (d.stop_reason === 'loop_detected') {
          set({ notice: '检测到连续重复的工具调用，已强制终止（循环保护）' })
        } else if (d.stop_reason === 'budget_exceeded') {
          set({ notice: '预算已耗尽，运行被干净终止（检查点已保存，可继续追问）' })
        }
        set({ lastRunMeta: meta, activeChannel: d.channel ?? get().activeChannel })
        // waiting_approval 时保留 pendingInterrupt，其它情况清空
        if (d.stop_reason !== 'waiting_approval') set({ pendingInterrupt: null })
        set({ statusText: null })
        break
      }
      case 'fallback': {
        const d = parseData<FallbackEvent>(ev.data)
        if (d) set({ notice: `模型已降级：${d.from} → ${d.to}` })
        break
      }
      case 'context_compressed': {
        const d = parseData<ContextCompressedEvent>(ev.data)
        if (d) {
          set((s) => ({
            notice: `上下文已治理（${d.strategy}）：${d.before_msgs} → ${d.after_msgs} 条消息`,
            messages: s.messages,
          }))
        }
        break
      }
      case 'interrupt': {
        const d = parseData<InterruptEvent>(ev.data)
        if (d) {
          set({ pendingInterrupt: d, statusText: '等待你的确认…' })
        }
        break
      }
      case 'error': {
        const d = parseData<{ code: string; message: string; retryable?: boolean }>(ev.data)
        if (d?.code === 'budget_exceeded') {
          // 预算耗尽是受控终止（run_finished 会带终态），用提示条而非错误条
          set({ notice: d.message, statusText: null })
        } else {
          set({
            error: d ? `${d.message}${d.retryable ? '（可重试）' : ''}` : '服务端返回未知错误',
            statusText: null,
          })
        }
        break
      }
      default:
        // 未知事件忽略（追加式契约）
        break
    }
  }

  return {
    sessions: [],
    currentSessionId: null,
    messages: [],
    streaming: false,
    statusText: null,
    error: null,
    notice: null,
    toolCallOwner: {},
    pendingInterrupt: null,
    contractDraft: null,
    channelDraft: null,
    activeChannel: null,
    lastRunMeta: null,

    setContractDraft: (c) => set({ contractDraft: c }),
    setChannelDraft: (c) => set({ channelDraft: c }),
    dismissNotice: () => set({ notice: null }),

    loadSessions: async () => {
      try {
        const sessions = await api.listSessions()
        set({ sessions })
        // 无选中会话时默认打开第一个
        if (!get().currentSessionId && sessions.length > 0) {
          await get().selectSession(sessions[0].id)
        }
      } catch (e) {
        set({ error: `加载会话失败：${(e as Error).message}` })
      }
    },

    selectSession: async (id) => {
      if (get().streaming) return
      set({ currentSessionId: id, error: null, toolCallOwner: {} })
      try {
        const messages = await api.listMessages(id)
        set({ messages })
      } catch (e) {
        set({ messages: [], error: `加载消息失败：${(e as Error).message}` })
      }
    },

    newSession: async () => {
      if (get().streaming) return
      try {
        const s = await api.createSession()
        set((st) => ({
          sessions: [s, ...st.sessions],
          currentSessionId: s.id,
          messages: [],
          error: null,
        }))
      } catch (e) {
        set({ error: `创建会话失败：${(e as Error).message}` })
      }
    },

    rename: async (id, title) => {
      const t = title.trim()
      if (!t) return
      try {
        const updated = await api.renameSession(id, t)
        set((s) => ({
          sessions: s.sessions.map((x) => (x.id === id ? { ...x, ...updated } : x)),
        }))
      } catch (e) {
        set({ error: `重命名失败：${(e as Error).message}` })
      }
    },

    remove: async (id) => {
      if (get().streaming) return
      try {
        await api.deleteSession(id)
        const rest = get().sessions.filter((x) => x.id !== id)
        set((s) => ({
          sessions: rest,
          currentSessionId: s.currentSessionId === id ? null : s.currentSessionId,
          messages: s.currentSessionId === id ? [] : s.messages,
        }))
        if (rest.length > 0 && get().currentSessionId === null) {
          await get().selectSession(rest[0].id)
        }
      } catch (e) {
        set({ error: `删除会话失败：${(e as Error).message}` })
      }
    },

    send: async (content) => {
      const text = content.trim()
      if (!text || get().streaming) return
      // 无选中会话则先创建一个
      let sessionId = get().currentSessionId
      if (!sessionId) {
        try {
          const s = await api.createSession(text.slice(0, 20))
          sessionId = s.id
          set((st) => ({ sessions: [s, ...st.sessions], currentSessionId: s.id }))
        } catch (e) {
          set({ error: `创建会话失败：${(e as Error).message}` })
          return
        }
      }

      const userMsg: ChatMessage = {
        id: tmpId(),
        role: 'user',
        content: text,
        created_at: new Date().toISOString(),
      }
      const assistantMsg: ChatMessage = {
        id: tmpId(),
        role: 'assistant',
        content: '',
        tool_calls: [],
        created_at: new Date().toISOString(),
      }
      set((s) => ({
        messages: [...s.messages, userMsg, assistantMsg],
        streaming: true,
        statusText: '正在思考…',
        error: null,
        notice: null,
        lastRunMeta: null,
        activeChannel: s.channelDraft,
      }))

      abort = new AbortController()
      const contract = get().contractDraft
      const channel = get().channelDraft
      await api.streamChat(
        {
          session_id: sessionId,
          content: text,
          ...(contract ? { task_contract: contract } : {}),
          ...(channel ? { channel } : {}),
        },
        {
          onEvent: handleEvent,
          onDone: () => {
            abort = null
            set({ streaming: false, statusText: null })
            // 刷新会话列表（标题/更新时间可能变化）
            void get().loadSessions()
          },
          onError: (err) => {
            abort = null
            set({ streaming: false, statusText: null, error: `请求失败：${err.message}` })
          },
        },
        abort.signal,
      )
    },

    resume: async (decision, editedArgs, reason) => {
      const it = get().pendingInterrupt
      if (!it || get().streaming) return
      const assistantMsg: ChatMessage = {
        id: tmpId(),
        role: 'assistant',
        content: '',
        tool_calls: [],
        created_at: new Date().toISOString(),
      }
      set((s) => ({
        messages: [...s.messages, assistantMsg],
        streaming: true,
        statusText: decision === 'reject' ? '已拒绝，正在处理…' : '已批准，正在执行…',
        error: null,
        pendingInterrupt: null,
      }))
      abort = new AbortController()
      await api.streamResume(
        {
          session_id: it.session_id,
          run_id: it.run_id,
          interrupt_id: it.interrupt_id,
          decision,
          edited_args: decision === 'edit' ? editedArgs ?? null : null,
          reason: decision === 'reject' ? reason ?? null : null,
        },
        {
          onEvent: handleEvent,
          onDone: () => {
            abort = null
            set({ streaming: false, statusText: null })
            void get().loadSessions()
          },
          onError: (err) => {
            abort = null
            set({ streaming: false, statusText: null, error: `恢复失败：${err.message}` })
          },
        },
        abort.signal,
      )
    },

    stop: () => {
      abort?.abort()
      abort = null
    },
  }
})
