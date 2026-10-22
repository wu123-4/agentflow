// 运行轨迹查看器（Harness Phase 2/4）：会话 run 列表 + span waterfall。
import { useCallback, useEffect, useState } from 'react'
import * as api from '../lib/api'
import { useApp } from '../store'
import type { RunSummary, TraceSpan } from '../types'

const STOP_STYLE: Record<string, string> = {
  end_turn: 'bg-green-100 text-green-700',
  gate_blocked: 'bg-red-100 text-red-700',
  budget_exceeded: 'bg-amber-100 text-amber-700',
  loop_detected: 'bg-purple-100 text-purple-700',
  waiting_approval: 'bg-blue-100 text-blue-700',
}

function Waterfall({ spans }: { spans: TraceSpan[] }) {
  if (!spans.length) return <div className="text-gray-400 text-xs p-3">该 run 无 span（可能已被缓冲淘汰）</div>
  const t0 = Math.min(...spans.map((s) => s.start_ms))
  const total = Math.max(1, ...spans.map((s) => s.start_ms + s.duration_ms - t0))
  const depth = (s: TraceSpan, byId: Map<string, TraceSpan>): number => {
    let d = 0
    let cur = s
    const seen = new Set<string>()
    while (cur.parent_id && !seen.has(cur.parent_id)) {
      seen.add(cur.parent_id)
      const p = byId.get(cur.parent_id)
      if (!p) break
      d += 1
      cur = p
    }
    return d
  }
  const byId = new Map(spans.map((s) => [s.span_id ?? '', s]))
  return (
    <div className="space-y-1">
      {spans.map((s, i) => {
        const left = ((s.start_ms - t0) / total) * 100
        const width = Math.max(0.5, (s.duration_ms / total) * 100)
        const d = depth(s, byId)
        const err = s.status === 'ERROR'
        return (
          <div key={i} className="flex items-center gap-2 text-[11px]">
            <span
              className="w-44 shrink-0 truncate font-mono text-gray-700"
              style={{ paddingLeft: d * 12 }}
              title={s.name}
            >
              {s.name}
            </span>
            <span className="relative flex-1 h-4 bg-gray-50 rounded overflow-hidden">
              <span
                className={`absolute top-0 h-full rounded ${err ? 'bg-red-400' : 'bg-blue-400'}`}
                style={{ left: `${left}%`, width: `${width}%` }}
              />
            </span>
            <span className="w-16 shrink-0 text-right text-gray-500">{s.duration_ms} ms</span>
          </div>
        )
      })}
    </div>
  )
}

export default function TraceViewer({ onClose }: { onClose: () => void }) {
  const sessionId = useApp((s) => s.currentSessionId)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [spans, setSpans] = useState<TraceSpan[]>([])
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!sessionId) return
    try {
      const items = await api.listRuns(sessionId)
      setRuns(items)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [sessionId])

  useEffect(() => {
    void load()
  }, [load])

  const pick = async (runId: string) => {
    setSelected(runId)
    try {
      setSpans(await api.getTrace(runId))
    } catch (e) {
      setError((e as Error).message)
      setSpans([])
    }
  }

  const sel = runs.find((r) => r.id === selected)

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-2xl w-full max-w-4xl max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200">
          <h2 className="text-sm font-semibold">运行轨迹（Traces）</h2>
          <div className="flex gap-2">
            <button onClick={() => void load()} className="text-xs px-2 py-1 rounded border border-gray-300 hover:bg-gray-50">
              刷新
            </button>
            <button onClick={onClose} className="text-xs px-2 py-1 rounded border border-gray-300 hover:bg-gray-50">
              关闭
            </button>
          </div>
        </div>
        {error && <div className="mx-5 mt-2 text-xs text-red-600 bg-red-50 rounded px-2 py-1">⚠ {error}</div>}
        {!sessionId && <div className="p-5 text-sm text-gray-400">请先选择一个会话</div>}
        {sessionId && (
          <div className="flex-1 overflow-y-auto p-5 space-y-4 min-h-0">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 min-h-0">
              <div className="space-y-1.5 overflow-y-auto max-h-[30vh]">
                {runs.length === 0 && <div className="text-xs text-gray-400">该会话暂无运行记录</div>}
                {runs.map((r) => (
                  <button
                    key={r.id}
                    onClick={() => void pick(r.id)}
                    className={`w-full text-left rounded-lg border px-3 py-2 text-xs space-y-1 ${
                      selected === r.id ? 'border-blue-400 bg-blue-50' : 'border-gray-200 hover:bg-gray-50'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className={`px-1.5 py-0.5 rounded ${STOP_STYLE[r.stop_reason ?? ''] ?? 'bg-gray-100 text-gray-600'}`}>
                        {r.stop_reason ?? r.status}
                      </span>
                      <span className="font-mono text-gray-500 truncate">{r.id.slice(0, 18)}…</span>
                    </div>
                    <div className="text-gray-400">
                      {new Date(r.started_at).toLocaleTimeString()} · 工具 {r.tool_calls} 次 ·
                      Token {(r.usage?.input_tokens ?? 0) + (r.usage?.output_tokens ?? 0)}
                    </div>
                  </button>
                ))}
              </div>
              <div className="border border-gray-200 rounded-lg p-3 overflow-y-auto max-h-[30vh]">
                {sel ? (
                  <>
                    <div className="text-xs text-gray-500 mb-2 font-mono">{sel.id}</div>
                    <Waterfall spans={spans} />
                  </>
                ) : (
                  <div className="text-xs text-gray-400">点选左侧 run 查看 span waterfall</div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
