// 端到端验证：调用 mock 的 POST /api/v1/chat，用 sse.ts 解析器消费流，
// 断言事件序列与工具卡片状态流转符合契约。运行：node scripts/verify-chat.mjs
import { createSseParser } from '../src/lib/sse.ts'

const BASE = 'http://localhost:8000/api/v1'

let failed = 0
const assert = (cond, name) => {
  if (cond) console.log(`  ✓ ${name}`)
  else {
    failed++
    console.error(`  ✗ ${name}`)
  }
}

// 1. 新建会话
const created = await (
  await fetch(`${BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: '验证会话' }),
  })
).json()
assert(created.id?.startsWith('sess_'), `创建会话成功（${created.id}）`)

// 2. 发送消息并消费 SSE
const events = []
const res = await fetch(`${BASE}/chat`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ session_id: created.id, content: '帮我读取 readme.txt 并总结' }),
})
assert(res.headers.get('content-type')?.includes('text/event-stream'), '响应为 text/event-stream')

const reader = res.body.getReader()
const decoder = new TextDecoder()
const parser = createSseParser((e) => events.push(e))
for (;;) {
  const { done, value } = await reader.read()
  if (done) break
  parser.feed(decoder.decode(value, { stream: true }))
}
parser.flush()

const names = events.map((e) => e.event)
console.log(`  事件序列: ${names.join(' -> ')}`)
assert(names[0] === 'run_started', '首事件为 run_started')
assert(names.includes('state') && names.includes('token'), '包含 state/token')
assert(names.indexOf('tool_start') < names.indexOf('tool_end'), 'tool_start 先于 tool_end')
assert(names.at(-1) === 'run_finished', '末事件为 run_finished')

// 3. 校验关键负载
const toolStart = JSON.parse(events.find((e) => e.event === 'tool_start').data)
assert(toolStart.tool === 'filesystem__read_file' && toolStart.args.path === 'readme.txt', 'tool_start 负载正确')
const toolEnd = JSON.parse(events.find((e) => e.event === 'tool_end').data)
assert(toolEnd.call_id === toolStart.call_id && toolEnd.status === 'ok' && toolEnd.duration_ms > 0, 'tool_end 关联 call_id 且含耗时')
const finished = JSON.parse(events.find((e) => e.event === 'run_finished').data)
assert(finished.usage?.input_tokens > 0 && finished.usage?.output_tokens > 0, 'run_finished 含 token 用量')

// 4. 历史回放：新会话应含 user + assistant（带 tool_calls）
const hist = await (await fetch(`${BASE}/sessions/${created.id}/messages?limit=50`)).json()
assert(hist.items.length === 2, '历史回放返回 2 条消息')
const assistant = hist.items.find((m) => m.role === 'assistant')
assert(assistant?.tool_calls?.[0]?.status === 'ok', '历史 assistant 消息含已完成的 tool_calls')
assert(assistant?.usage?.output_tokens > 0, '历史 assistant 消息含 usage')

// 5. 会话标题自动更新
const sessions = (await (await fetch(`${BASE}/sessions`)).json()).items
assert(sessions.find((s) => s.id === created.id)?.title !== '验证会话', '首条消息后标题自动更新')

if (failed) {
  console.error(`\n${failed} 项失败`)
  process.exit(1)
}
console.log('\nSSE 端到端契约验证全部通过')
