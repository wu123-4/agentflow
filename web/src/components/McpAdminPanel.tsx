// MCP Server 管理页（M2）：列表 / 注册 / 启停 / 删除 / 工具预览
import { useCallback, useEffect, useState } from 'react'
import * as mcpApi from '../lib/mcpApi'
import type { McpServer, McpToolMeta } from '../lib/mcpApi'

const STATUS_STYLE: Record<McpServer['status'], string> = {
  connected: 'bg-green-100 text-green-700',
  degraded: 'bg-yellow-100 text-yellow-700',
  disconnected: 'bg-gray-200 text-gray-500',
}

const RISK_STYLE: Record<McpToolMeta['risk'], string> = {
  low: 'bg-green-50 text-green-600 border-green-200',
  medium: 'bg-yellow-50 text-yellow-700 border-yellow-300',
  high: 'bg-red-50 text-red-600 border-red-200',
}

export default function McpAdminPanel({ onClose }: { onClose: () => void }) {
  const [servers, setServers] = useState<McpServer[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [liveTools, setLiveTools] = useState<Record<string, McpToolMeta[]>>({})
  const [showForm, setShowForm] = useState(false)

  const load = useCallback(async () => {
    try {
      setServers(await mcpApi.listServers())
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const toggle = async (s: McpServer) => {
    setBusy(true)
    try {
      await mcpApi.patchServer(s.id, { enabled: !s.enabled })
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
    setBusy(false)
  }

  const remove = async (s: McpServer) => {
    if (!confirm(`注销 Server「${s.name}」？其工具将立即从对话中移除。`)) return
    setBusy(true)
    try {
      await mcpApi.deleteServer(s.id)
      await load()
    } catch (e) {
      setError((e as Error).message)
    }
    setBusy(false)
  }

  const toggleExpand = async (s: McpServer) => {
    if (expanded === s.id) {
      setExpanded(null)
      return
    }
    setExpanded(s.id)
    try {
      const d = await mcpApi.fetchServerTools(s.id)
      setLiveTools((m) => ({ ...m, [s.id]: d.tools }))
    } catch {
      /* 保留缓存视图 */
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-2xl w-full max-w-3xl max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200">
          <div>
            <h2 className="text-base font-bold text-gray-800">MCP Server 管理</h2>
            <p className="text-xs text-gray-400 mt-0.5">注册/启停/注销即时生效（热插拔），无需重启服务</p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => void load()}
              className="text-xs px-3 py-1.5 rounded-md border border-gray-300 hover:bg-gray-50"
            >
              刷新
            </button>
            <button onClick={onClose} className="text-gray-400 hover:text-gray-600 px-2">✕</button>
          </div>
        </div>

        {error && (
          <div className="mx-5 mt-3 rounded-md bg-red-50 border border-red-200 text-red-700 text-xs px-3 py-2">
            ⚠ {error}
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-5 space-y-2">
          {servers.map((s) => (
            <div key={s.id} className="rounded-lg border border-gray-200">
              <div className="flex items-center gap-3 px-4 py-3">
                <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${STATUS_STYLE[s.status]}`}>
                  {s.status}
                </span>
                <button className="font-semibold text-sm text-gray-800 hover:underline" onClick={() => void toggleExpand(s)}>
                  {s.name}
                </button>
                <span className="text-xs text-gray-400">{s.transport}</span>
                <span className="text-xs text-gray-500 ml-auto">{s.tools_count} 工具</span>
                {s.restart_count > 0 && (
                  <span className="text-[11px] text-orange-500" title="stdio 崩溃自动重启次数">重启×{s.restart_count}</span>
                )}
                <button
                  disabled={busy}
                  onClick={() => void toggle(s)}
                  className={`text-xs px-2.5 py-1 rounded-md border ${
                    s.enabled
                      ? 'border-gray-300 text-gray-600 hover:bg-gray-50'
                      : 'border-green-300 text-green-700 hover:bg-green-50'
                  } disabled:opacity-40`}
                >
                  {s.enabled ? '停用' : '启用'}
                </button>
                <button
                  disabled={busy}
                  onClick={() => void remove(s)}
                  className="text-xs px-2.5 py-1 rounded-md border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-40"
                >
                  注销
                </button>
              </div>
              {s.last_error && s.status !== 'connected' && (
                <div className="px-4 pb-2 text-[11px] text-red-400 break-all">{s.last_error}</div>
              )}
              {expanded === s.id && (
                <div className="border-t border-gray-100 px-4 py-2 bg-gray-50/60">
                  <div className="text-[11px] text-gray-400 mb-1">
                    命令：{s.command} {(s.args ?? []).join(' ') || ''}
                    {s.env_keys.length > 0 && ` ｜ env: ${s.env_keys.join(', ')}`}
                  </div>
                  {(liveTools[s.id] ?? s.tools).map((t) => (
                    <div key={t.name} className="flex items-center gap-2 py-1 text-xs">
                      <code className="text-gray-700">{s.name}__{t.name}</code>
                      <span className={`text-[10px] px-1.5 py-0.5 rounded border ${RISK_STYLE[t.risk]}`}>{t.risk}</span>
                      <span className="text-gray-400 truncate">{t.description}</span>
                    </div>
                  ))}
                  {(liveTools[s.id] ?? s.tools).length === 0 && (
                    <div className="text-xs text-gray-400 py-1">（无工具，Server 可能未连接）</div>
                  )}
                </div>
              )}
            </div>
          ))}
          {servers.length === 0 && !showForm && (
            <div className="text-center text-sm text-gray-400 py-8">暂无注册的 Server</div>
          )}
          {!showForm && (
            <button
              onClick={() => setShowForm(true)}
              className="w-full text-xs py-2 rounded-lg border border-dashed border-blue-300 text-blue-600 hover:bg-blue-50"
            >
              ＋ 注册新 Server
            </button>
          )}
          {showForm && <RegisterForm onDone={() => { setShowForm(false); void load() }} onCancel={() => setShowForm(false)} />}
        </div>
      </div>
    </div>
  )
}

function RegisterForm({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [name, setName] = useState('')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const submit = async () => {
    setSubmitting(true)
    setErr(null)
    try {
      await mcpApi.createServer({
        name: name.trim(),
        transport: 'stdio',
        command: command.trim(),
        args: args.trim() ? args.trim().split(/\s+/) : [],
        enabled: true,
      })
      onDone()
    } catch (e) {
      setErr((e as Error).message)
    }
    setSubmitting(false)
  }

  return (
    <div className="rounded-lg border border-blue-200 bg-blue-50/40 p-4 space-y-2">
      <div className="text-sm font-semibold text-gray-700">注册 stdio Server</div>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="name（小写字母/数字/-/_，用作工具前缀）"
        className="w-full text-xs px-2.5 py-1.5 rounded-md border border-gray-300" />
      <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="command（如 python / npx）"
        className="w-full text-xs px-2.5 py-1.5 rounded-md border border-gray-300" />
      <input value={args} onChange={(e) => setArgs(e.target.value)} placeholder="args（空格分隔，如 -y @modelcontextprotocol/server-filesystem D:\dir）"
        className="w-full text-xs px-2.5 py-1.5 rounded-md border border-gray-300" />
      {err && <div className="text-xs text-red-600">⚠ {err}</div>}
      <div className="flex gap-2 justify-end">
        <button onClick={onCancel} className="text-xs px-3 py-1.5 rounded-md border border-gray-300 hover:bg-gray-50">取消</button>
        <button
          onClick={() => void submit()}
          disabled={submitting || !name.trim() || !command.trim()}
          className="text-xs px-3 py-1.5 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
        >
          {submitting ? '注册中…' : '注册并连接'}
        </button>
      </div>
    </div>
  )
}
