// HITL 确认卡片（M3）：批准 / 改参 / 拒绝，对应 docs/04 §2 interrupt 流程
import { useState } from 'react'
import { useApp } from '../store'

export default function ApprovalDialog() {
  const it = useApp((s) => s.pendingInterrupt)
  const resume = useApp((s) => s.resume)
  const streaming = useApp((s) => s.streaming)
  const [editing, setEditing] = useState(false)
  const [argsText, setArgsText] = useState('')
  const [reason, setReason] = useState('')
  const [argsError, setArgsError] = useState<string | null>(null)

  if (!it) return null

  const startEdit = () => {
    setArgsText(JSON.stringify(it.args, null, 2))
    setArgsError(null)
    setEditing(true)
  }

  const submitEdit = async () => {
    try {
      const parsed = JSON.parse(argsText) as Record<string, unknown>
      setEditing(false)
      await resume('edit', parsed)
    } catch {
      setArgsError('参数不是合法 JSON，请检查')
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-lg overflow-hidden">
        <div className="flex items-center gap-2 px-5 py-3 bg-red-50 border-b border-red-100">
          <span className="text-red-600 text-sm font-bold">⚠ 高风险操作确认</span>
          <span className="ml-auto text-[11px] px-2 py-0.5 rounded-full bg-red-100 text-red-600 font-medium">
            {it.risk}
          </span>
        </div>
        <div className="px-5 py-4 space-y-3">
          <p className="text-sm text-gray-700">{it.message ?? '智能体请求执行以下操作：'}</p>
          <div className="text-xs">
            <div className="text-gray-400 mb-1">工具</div>
            <code className="px-2 py-1 rounded bg-gray-100 text-gray-800">{it.tool}</code>
          </div>
          <div className="text-xs">
            <div className="text-gray-400 mb-1">参数</div>
            {editing ? (
              <>
                <textarea
                  value={argsText}
                  onChange={(e) => setArgsText(e.target.value)}
                  rows={6}
                  spellCheck={false}
                  className="w-full font-mono text-[11px] px-2 py-1.5 rounded border border-blue-300 focus:outline-none focus:ring-1 focus:ring-blue-400"
                />
                {argsError && <div className="text-red-500 mt-1">⚠ {argsError}</div>}
              </>
            ) : (
              <pre className="px-2 py-1.5 rounded bg-gray-100 text-gray-800 overflow-x-auto whitespace-pre-wrap break-all">
                {JSON.stringify(it.args, null, 2)}
              </pre>
            )}
          </div>

          {editing ? (
            <div className="flex gap-2 justify-end pt-1">
              <button
                onClick={() => setEditing(false)}
                className="text-xs px-3 py-1.5 rounded-md border border-gray-300 hover:bg-gray-50"
              >
                取消改参
              </button>
              <button
                onClick={() => void submitEdit()}
                disabled={streaming}
                className="text-xs px-3 py-1.5 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
              >
                按新参数批准
              </button>
            </div>
          ) : (
            <>
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="拒绝理由（可选，将回喂给智能体）"
                className="w-full text-xs px-2.5 py-1.5 rounded-md border border-gray-300"
              />
              <div className="flex flex-wrap gap-2 pt-1">
                <button
                  onClick={() => void resume('approve')}
                  disabled={streaming}
                  className="text-xs px-3 py-1.5 rounded-md bg-green-600 text-white hover:bg-green-700 disabled:opacity-40"
                >
                  ✓ 批准执行
                </button>
                <button
                  onClick={startEdit}
                  disabled={streaming}
                  className="text-xs px-3 py-1.5 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
                >
                  ✎ 改参后批准
                </button>
                <button
                  onClick={() => void resume('reject', undefined, reason || '用户拒绝')}
                  disabled={streaming}
                  className="text-xs px-3 py-1.5 rounded-md border border-red-300 text-red-600 hover:bg-red-50 disabled:opacity-40"
                >
                  ✕ 拒绝
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
