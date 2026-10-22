// SSE（text/event-stream）增量解析器。
// 纯 TypeScript、无 DOM 依赖，可被 Node 脚本直接引用做单元测试。
// 规则（对齐 docs/05-API设计.md）：
// - 事件块以空行分隔；字段行形如 `event: xxx` / `data: xxx`
// - 以 `:` 开头的行是注释/心跳，忽略
// - `id:` / `retry:` 字段本阶段忽略

export interface SseEvent {
  event: string
  data: string
}

export interface SseParser {
  /** 喂入一段解码后的文本（可任意切分） */
  feed(chunk: string): void
  /** 流结束时调用，冲刷残留缓冲 */
  flush(): void
}

export function createSseParser(onEvent: (e: SseEvent) => void): SseParser {
  let buf = ''
  // 处理跨 chunk 被切断的 \r\n：暂存结尾的孤立 \r
  let pendingCr = ''

  function parseBlock(block: string): SseEvent | null {
    let event = 'message'
    const dataLines: string[] = []
    for (const line of block.split('\n')) {
      if (line === '') continue
      if (line.startsWith(':')) continue // 注释 / 心跳
      const colon = line.indexOf(':')
      const field = colon === -1 ? line : line.slice(0, colon)
      let value = colon === -1 ? '' : line.slice(colon + 1)
      if (value.startsWith(' ')) value = value.slice(1) // 去掉一个前导空格
      if (field === 'event') event = value
      else if (field === 'data') dataLines.push(value)
    }
    if (dataLines.length === 0) return null
    return { event, data: dataLines.join('\n') }
  }

  function drain() {
    let idx: number
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const block = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const ev = parseBlock(block)
      if (ev) onEvent(ev)
    }
  }

  return {
    feed(chunk: string) {
      let text = pendingCr + chunk
      pendingCr = ''
      if (text.endsWith('\r')) {
        pendingCr = '\r'
        text = text.slice(0, -1)
      }
      // 统一 CRLF / CR 为 LF
      buf += text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
      drain()
    },
    flush() {
      if (pendingCr) {
        buf += pendingCr
        pendingCr = ''
      }
      if (buf.trim() !== '') {
        const ev = parseBlock(buf)
        if (ev) onEvent(ev)
      }
      buf = ''
    },
  }
}
