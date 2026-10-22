// 工具调用卡片：工具名、状态徽标、耗时、参数 JSON（折叠）、结果预览（折叠）
import { useState } from 'react'
import type { ToolCall } from '../types'

function pretty(v: unknown): string {
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

export default function ToolCallCard({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(false)
  const badge =
    call.status === 'running' ? (
      <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 animate-pulse">
        运行中
      </span>
    ) : call.status === 'ok' ? (
      <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-green-100 text-green-700">成功</span>
    ) : call.status === 'denied' ? (
      <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-purple-100 text-purple-700">
        已拦截
      </span>
    ) : (
      <span className="text-[11px] px-1.5 py-0.5 rounded-full bg-red-100 text-red-700">失败</span>
    )

  return (
    <div className="my-1.5 rounded-lg border border-gray-200 bg-gray-50 text-xs">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left"
      >
        <span className="font-mono text-gray-800">🔧 {call.tool}</span>
        {badge}
        {call.duration_ms != null && (
          <span className="text-gray-400 ml-auto">{call.duration_ms} ms</span>
        )}
        <span className="text-gray-400">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="px-3 pb-2 space-y-2 border-t border-gray-200 pt-2">
          <div>
            <div className="text-gray-500 mb-0.5">参数</div>
            <pre className="bg-white border border-gray-200 rounded p-2 overflow-x-auto font-mono text-[11px] whitespace-pre-wrap">
              {pretty(call.args)}
            </pre>
          </div>
          {call.result != null && (
            <div>
              <div className="text-gray-500 mb-0.5">结果预览</div>
              <pre className="bg-white border border-gray-200 rounded p-2 overflow-x-auto font-mono text-[11px] whitespace-pre-wrap max-h-40 overflow-y-auto">
                {pretty(call.result)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
