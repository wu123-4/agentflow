// AgentFlow M1 开发用 Mock Server（纯 Node，无依赖）
// 实现 docs/05-API设计.md 中前端所需端点的内存 mock，监听 8000，CORS 全开。
// 启动：npm run mock
import { createServer } from 'node:http'

const PORT = 8000

// ---------- 内存数据 ----------
let idSeq = 0
const nid = (p) => `${p}_${Date.now().toString(36)}_${++idSeq}`

/** sessions: id -> {id,title,updated_at,model,message_count} */
const sessions = new Map()
/** messages: sessionId -> [{id,role,content,tool_calls,created_at}] */
const messages = new Map()

// 预置一个历史会话，便于验证历史回放（含工具调用与 token 用量）
{
  const s = {
    id: nid('sess'),
    title: '示例：读取 readme.txt',
    updated_at: new Date().toISOString(),
    model: 'gpt-4o-mini',
    message_count: 2,
  }
  const t0 = new Date(Date.now() - 60_000).toISOString()
  sessions.set(s.id, s)
  messages.set(s.id, [
    {
      id: nid('msg'),
      role: 'user',
      content: '帮我读取 readme.txt 并总结一下',
      created_at: t0,
    },
    {
      id: nid('msg'),
      role: 'assistant',
      content:
        '我读取了 `readme.txt`，内容如下：\n\n> AgentFlow 是一个智能体对话平台，基于 LangGraph + MCP。\n\n**总结**：项目目标是构建支持工具调用与流式输出的 Agent 对话平台。\n\n```ts\nconst hello = (name: string) => `hi ${name}`\n```',
      tool_calls: [
        {
          call_id: 'tc_demo_1',
          tool: 'filesystem__read_file',
          args: { path: 'readme.txt' },
          result: 'AgentFlow 是一个智能体对话平台，基于 LangGraph + MCP。',
          status: 'ok',
          duration_ms: 12,
        },
      ],
      created_at: t0,
      usage: { input_tokens: 1500, output_tokens: 86 },
    },
  ])
}

// ---------- 工具函数 ----------
function json(res, code, data) {
  const body = JSON.stringify(data)
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,PATCH,DELETE,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
  })
  res.end(body)
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let raw = ''
    req.on('data', (c) => {
      raw += c
      if (raw.length > 1e6) reject(new Error('body too large'))
    })
    req.on('end', () => {
      try {
        resolve(raw ? JSON.parse(raw) : {})
      } catch {
        resolve({})
      }
    })
    req.on('error', reject)
  })
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** SSE 写出一个事件 */
function sseSend(res, event, data) {
  res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
}

// ---------- /api/v1/chat 的 mock 脚本 ----------
// 模拟"读取 readme.txt 并总结"场景：
// run_started -> state(agent running) -> token* -> tool_start
// -> state(tools running) -> state(tools finished) -> tool_end
// -> state(agent running) -> token* -> run_finished
async function chatStream(res, body) {
  const sessionId = body.session_id
  const content = String(body.content ?? '')
  const runId = nid('run')

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'Access-Control-Allow-Origin': '*',
  })

  // 记录用户消息
  const list = messages.get(sessionId) ?? []
  const hadMessages = list.length > 0
  list.push({ id: nid('msg'), role: 'user', content, created_at: new Date().toISOString() })

  sseSend(res, 'run_started', { run_id: runId, session_id: sessionId, thread_id: sessionId })
  await sleep(150)
  sseSend(res, 'state', { node: 'agent', status: 'running' })
  await sleep(100)

  const toolCall = {
    call_id: 'tc_' + runId,
    tool: 'filesystem__read_file',
    args: { path: 'readme.txt' },
  }

  // 第一段流式文本
  for (const t of ['好的，我先', '读取 `readme.txt` 的内容', '，然后为你总结。\n\n']) {
    sseSend(res, 'token', { text: t })
    await sleep(120)
  }

  sseSend(res, 'tool_start', toolCall)
  await sleep(80)
  sseSend(res, 'state', { node: 'tools', status: 'running' })
  // 心跳注释行：验证前端会忽略
  res.write(': heartbeat\n\n')
  await sleep(600)
  sseSend(res, 'state', { node: 'tools', status: 'finished' })
  sseSend(res, 'tool_end', {
    call_id: toolCall.call_id,
    status: 'ok',
    duration_ms: 684,
    result_preview:
      'AgentFlow 是一个智能体对话平台，基于 LangGraph + MCP，支持流式输出与工具调用。',
  })
  await sleep(150)
  sseSend(res, 'state', { node: 'agent', status: 'running' })

  // 第二段流式文本（Markdown 内容）
  const para = [
    '已读取完成。',
    '\n\n## 文件概要\n\n',
    '该文件介绍了 **AgentFlow** 平台：\n\n',
    '1. 基于 LangGraph 编排 Agent 循环\n',
    '2. 通过 MCP 接入外部工具\n',
    '3. 支持 SSE 流式输出\n\n',
    '```python\nprint("hello agentflow")\n```\n\n',
    '如需进一步分析可以继续问我。',
  ]
  for (const t of para) {
    sseSend(res, 'token', { text: t })
    await sleep(100)
  }

  sseSend(res, 'run_finished', {
    run_id: runId,
    usage: { input_tokens: 1820, output_tokens: 96 },
    stop_reason: 'end_turn',
  })

  // 记录 assistant 消息（含工具调用）用于历史回放
  list.push({
    id: nid('msg'),
    role: 'assistant',
    content: para.join(''),
    tool_calls: [
      {
        ...toolCall,
        result: 'AgentFlow 是一个智能体对话平台，基于 LangGraph + MCP，支持流式输出与工具调用。',
        status: 'ok',
        duration_ms: 684,
      },
    ],
    created_at: new Date().toISOString(),
    usage: { input_tokens: 1820, output_tokens: 96 },
  })
  messages.set(sessionId, list)

  // 更新会话元信息
  const s = sessions.get(sessionId)
  if (s) {
    s.updated_at = new Date().toISOString()
    s.message_count = list.length
    // 会话首条消息时用消息内容作为标题
    if (!hadMessages) {
      s.title = content.slice(0, 20) || s.title
    }
  }
  res.end()
}

// ---------- 路由 ----------
const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://localhost:${PORT}`)
  const path = url.pathname

  if (req.method === 'OPTIONS') return json(res, 204, {})
  if (!path.startsWith('/api/v1/')) return json(res, 404, { detail: 'not found' })

  // GET /api/v1/sessions
  if (path === '/api/v1/sessions' && req.method === 'GET') {
    const items = [...sessions.values()].sort((a, b) =>
      b.updated_at.localeCompare(a.updated_at),
    )
    return json(res, 200, { items, next_cursor: null })
  }

  // POST /api/v1/sessions
  if (path === '/api/v1/sessions' && req.method === 'POST') {
    const body = await readBody(req)
    const s = {
      id: nid('sess'),
      title: body.title || '新会话',
      updated_at: new Date().toISOString(),
      model: body.model ?? null,
      message_count: 0,
    }
    sessions.set(s.id, s)
    messages.set(s.id, [])
    return json(res, 200, s)
  }

  // POST /api/v1/chat（SSE）
  if (path === '/api/v1/chat' && req.method === 'POST') {
    const body = await readBody(req)
    if (!body.session_id || !sessions.has(body.session_id)) {
      return json(res, 404, { detail: 'session_not_found' })
    }
    return chatStream(res, body)
  }

  // /api/v1/sessions/{id} 与 /api/v1/sessions/{id}/messages
  const m = path.match(/^\/api\/v1\/sessions\/([^/]+)(\/messages)?$/)
  if (m) {
    const id = m[1]
    const isMessages = !!m[2]
    if (!sessions.has(id)) return json(res, 404, { detail: 'session_not_found' })

    if (isMessages && req.method === 'GET') {
      const limit = Number(url.searchParams.get('limit') ?? 50)
      const items = (messages.get(id) ?? []).slice(-limit)
      return json(res, 200, { items, next_cursor: null })
    }
    if (!isMessages && req.method === 'PATCH') {
      const body = await readBody(req)
      const s = sessions.get(id)
      if (body.title != null) s.title = String(body.title)
      if (body.model != null) s.model = body.model
      s.updated_at = new Date().toISOString()
      return json(res, 200, s)
    }
    if (!isMessages && req.method === 'DELETE') {
      sessions.delete(id)
      messages.delete(id)
      return json(res, 200, { ok: true })
    }
  }

  json(res, 404, { detail: 'not found' })
})

server.listen(PORT, () => {
  console.log(`[mock] AgentFlow mock server 已启动: http://localhost:${PORT}（CORS 全开）`)
})
