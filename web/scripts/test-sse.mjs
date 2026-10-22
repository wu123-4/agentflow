// SSE 解析器单元测试：验证分块、CRLF、心跳注释、多行 data 等场景。
// 运行：npm run test:sse（Node >= 23.6 原生支持 .ts 类型剥离）
import { createSseParser } from '../src/lib/sse.ts'

let failed = 0
function assert(cond, name) {
  if (cond) console.log(`  ✓ ${name}`)
  else {
    failed++
    console.error(`  ✗ ${name}`)
  }
}

// 用例 1：标准事件流（LF）
{
  const events = []
  const p = createSseParser((e) => events.push(e))
  p.feed(
    'event: run_started\ndata: {"run_id":"r1","session_id":"s1"}\n\n' +
      'event: token\ndata: {"text":"你好"}\n\n' +
      ': heartbeat\n\n' +
      'event: run_finished\ndata: {"run_id":"r1"}\n\n',
  )
  p.flush()
  assert(events.length === 3, '标准流解析出 3 个事件（心跳被忽略）')
  assert(events[0].event === 'run_started', '第一个事件为 run_started')
  assert(JSON.parse(events[1].data).text === '你好', 'token 事件 data 可解析')
}

// 用例 2：CRLF 行尾 + 跨 chunk 切断（含 \r\n 被劈开）
{
  const events = []
  const p = createSseParser((e) => events.push(e))
  const raw = 'event: state\r\ndata: {"node":"agent","status":"running"}\r\n\r\n'
  for (let i = 0; i < raw.length; i += 3) p.feed(raw.slice(i, i + 3)) // 每 3 字符喂一次
  p.flush()
  assert(events.length === 1, 'CRLF + 3 字符分块仍解析出 1 个事件')
  assert(events[0].event === 'state', 'CRLF 场景事件名正确')
}

// 用例 3：多行 data 合并
{
  const events = []
  const p = createSseParser((e) => events.push(e))
  p.feed('data: line1\ndata: line2\n\n')
  p.flush()
  assert(events.length === 1 && events[0].data === 'line1\nline2', '多行 data 以换行合并')
  assert(events[0].event === 'message', '缺省事件名为 message')
}

// 用例 4：data 中的 JSON 含空行转义（服务端不会发出裸空行，但 data 值可含任意字符）
{
  const events = []
  const p = createSseParser((e) => events.push(e))
  p.feed('event: token\ndata: {"text":"a\\nb"}\n\nevent: tool_start\ndata: {"call_id":"tc1"}\n\n')
  p.flush()
  assert(events.length === 2, '连续事件正确切分')
  assert(JSON.parse(events[0].data).text === 'a\nb', 'JSON 内转义换行保留')
}

// 用例 5：flush 冲刷无结尾空行的残留
{
  const events = []
  const p = createSseParser((e) => events.push(e))
  p.feed('event: error\ndata: {"code":"x","message":"boom"}')
  p.flush()
  assert(events.length === 1 && events[0].event === 'error', 'flush 冲刷残留事件')
}

if (failed > 0) {
  console.error(`\n${failed} 个用例失败`)
  process.exit(1)
}
console.log('\n全部 SSE 解析用例通过')
