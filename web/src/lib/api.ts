// REST + SSE 客户端。开发期走 Vite proxy（/api -> http://localhost:8000）。
import type { Channel, ChatMessage, ModelInfo, RunSummary, Session, TaskContract, TraceSpan } from '../types'
import { createSseParser, type SseEvent } from './sse'

const BASE = '/api/v1'

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = ''
    try {
      detail = (await res.json())?.detail ?? ''
    } catch {
      /* 忽略非 JSON 错误体 */
    }
    throw new Error(`HTTP ${res.status}${detail ? `: ${detail}` : ''}`)
  }
  return (await res.json()) as T
}

/** 会话列表 */
export async function listSessions(): Promise<Session[]> {
  const data = await j<{ items: Session[] }>(await fetch(`${BASE}/sessions`))
  return data.items
}

/** 创建会话 */
export async function createSession(title?: string): Promise<Session> {
  const res = await fetch(`${BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: title ?? '新会话' }),
  })
  return j<Session>(res)
}

/** 重命名会话 */
export async function renameSession(id: string, title: string): Promise<Session> {
  const res = await fetch(`${BASE}/sessions/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
  return j<Session>(res)
}

/** 删除会话 */
export async function deleteSession(id: string): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${id}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 404) throw new Error(`删除会话失败 HTTP ${res.status}`)
}

/** 历史消息回放 */
export async function listMessages(sessionId: string, limit = 50): Promise<ChatMessage[]> {
  const data = await j<{ items: ChatMessage[] }>(
    await fetch(`${BASE}/sessions/${sessionId}/messages?limit=${limit}`),
  )
  return data.items
}

/** SSE 事件回调集合 */
export interface ChatStreamHandlers {
  onEvent: (event: SseEvent) => void
  onDone: () => void
  onError: (err: Error) => void
}

/**
 * POST /chat 并以 ReadableStream 手动解析 SSE（EventSource 不支持 POST）。
 * signal 用于前端本地中断读取。
 */
export async function streamChat(
  body: {
    session_id: string
    content: string
    model?: string
    task_contract?: TaskContract
    channel?: Channel
  },
  handlers: ChatStreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  await streamSse(`${BASE}/chat`, body, handlers, signal)
}

/** POST /chat/resume：提交 HITL 决定并继续接收 SSE 流 */
export async function streamResume(
  body: {
    session_id: string
    run_id: string
    interrupt_id?: string
    decision: string
    edited_args?: Record<string, unknown> | null
    reason?: string | null
  },
  handlers: ChatStreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  await streamSse(`${BASE}/chat/resume`, body, handlers, signal)
}

/** 通用 SSE POST 流解析 */
async function streamSse(
  url: string,
  body: unknown,
  handlers: ChatStreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    })
    if (!res.ok || !res.body) {
      throw new Error(`请求失败 HTTP ${res.status}`)
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    const parser = createSseParser(handlers.onEvent)
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      parser.feed(decoder.decode(value, { stream: true }))
    }
    parser.flush()
    handlers.onDone()
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      // 用户主动停止：视为正常结束
      handlers.onDone()
      return
    }
    handlers.onError(err instanceof Error ? err : new Error(String(err)))
  }
}

/** 模型注册表（Harness Phase 2） */
export async function listModels(): Promise<{ items: ModelInfo[]; default: string }> {
  return j<{ items: ModelInfo[]; default: string }>(await fetch(`${BASE}/models`))
}

/** 会话运行列表（Harness Phase 2 轨迹入口） */
export async function listRuns(sessionId: string, limit = 50): Promise<RunSummary[]> {
  const data = await j<{ items: RunSummary[] }>(
    await fetch(`${BASE}/sessions/${sessionId}/runs?limit=${limit}`),
  )
  return data.items
}

/** 某 run 的 span waterfall（观测调试） */
export async function getTrace(runId: string): Promise<TraceSpan[]> {
  const data = await j<{ run_id: string; items: TraceSpan[] }>(
    await fetch(`${BASE}/traces/${runId}`),
  )
  return data.items
}
