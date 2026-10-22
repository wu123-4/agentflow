// 运行状态条（Harness Phase 1-3）：最近一次 run 的门控结果、预算用量、降级/拦截计数。
import { useApp } from '../store'

const STOP_LABEL: Record<string, string> = {
  end_turn: '正常完成',
  gate_blocked: '门控拦截',
  budget_exceeded: '预算耗尽',
  loop_detected: '循环终止',
  waiting_approval: '等待确认',
  interrupted_stop: '已停止',
  error: '运行出错',
}

function Bar({ used, cap }: { used: number; cap: number }) {
  const pct = cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 100
  const color = pct >= 90 ? 'bg-red-400' : pct >= 70 ? 'bg-amber-400' : 'bg-green-400'
  return (
    <span className="inline-flex items-center gap-1">
      <span className="w-16 h-1.5 rounded bg-gray-200 overflow-hidden">
        <span className={`block h-full ${color}`} style={{ width: `${pct}%` }} />
      </span>
      <span className="text-gray-500">{used}/{cap}</span>
    </span>
  )
}

export default function RunStatusBar() {
  const meta = useApp((s) => s.lastRunMeta)
  const notice = useApp((s) => s.notice)
  const activeChannel = useApp((s) => s.activeChannel)
  const dismissNotice = useApp((s) => s.dismissNotice)
  if (!meta && !notice && !activeChannel) return null

  const b = meta?.budget
  return (
    <div className="px-6 py-1.5 bg-white border-t border-gray-100 text-[11px] flex items-center gap-3 flex-wrap">
      {(meta?.channel ?? activeChannel) && (
        <span className="px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 font-medium">
          通道 {meta?.channel ?? activeChannel}
        </span>
      )}
      {meta && (
        <>
          <span
            className={`px-1.5 py-0.5 rounded font-medium ${
              meta.stop_reason === 'end_turn'
                ? 'bg-green-100 text-green-700'
                : meta.stop_reason === 'gate_blocked'
                  ? 'bg-red-100 text-red-700'
                  : 'bg-amber-100 text-amber-700'
            }`}
          >
            {STOP_LABEL[meta.stop_reason ?? ''] ?? meta.stop_reason}
          </span>
          {meta.gate_result && (
            <span className="text-gray-500" title={meta.gate_result.checked.map((c) => `${c.name}:${c.passed ? '✓' : '✗'}`).join('  ')}>
              门控 {meta.gate_result.passed ? '通过' : `拦截（${meta.gate_result.blocked_by}）`}
            </span>
          )}
          {b?.max_tokens != null && (
            <span>Token <Bar used={b.tokens_used ?? 0} cap={b.max_tokens} /></span>
          )}
          {b?.max_tool_calls != null && (
            <span>工具 <Bar used={b.tool_calls_used ?? 0} cap={b.max_tool_calls} /></span>
          )}
          {meta.denied_count ? <span className="text-purple-600">拦截 {meta.denied_count} 次</span> : null}
          {meta.escalations?.length ? (
            <span
              className="text-amber-600"
              title={meta.escalations.map((e) => `${e.from}→${e.to}: ${e.reason}`).join('\n')}
            >
              升级 {meta.escalations.map((e) => `${e.from}→${e.to}`).join('、')}
            </span>
          ) : null}
          {meta.wrap_up ? <span className="text-amber-600">已收尾</span> : null}
          {meta.fallback_count ? <span className="text-blue-600">降级 {meta.fallback_count} 次</span> : null}
          {meta.model_used && <span className="text-gray-400">模型 {meta.model_used}</span>}
        </>
      )}
      {notice && (
        <span className="ml-auto flex items-center gap-1 text-blue-700 bg-blue-50 border border-blue-200 rounded px-2 py-0.5">
          ℹ {notice}
          <button onClick={dismissNotice} className="text-blue-400 hover:text-blue-600">✕</button>
        </span>
      )}
    </div>
  )
}
