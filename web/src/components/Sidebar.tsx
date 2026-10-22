// 左侧会话侧栏：列表、新建、删除、重命名
import { useState } from 'react'
import { useApp } from '../store'

export default function Sidebar() {
  const sessions = useApp((s) => s.sessions)
  const currentId = useApp((s) => s.currentSessionId)
  const streaming = useApp((s) => s.streaming)
  const select = useApp((s) => s.selectSession)
  const create = useApp((s) => s.newSession)
  const remove = useApp((s) => s.remove)
  const rename = useApp((s) => s.rename)

  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  const commitRename = () => {
    if (editingId) void rename(editingId, draft)
    setEditingId(null)
  }

  return (
    <aside className="w-64 shrink-0 h-full flex flex-col border-r border-gray-200 bg-gray-50">
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
        <h1 className="text-base font-bold text-gray-800">AgentFlow</h1>
        <button
          onClick={() => void create()}
          disabled={streaming}
          className="text-xs px-2.5 py-1.5 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
          title="新建会话"
        >
          ＋ 新建
        </button>
      </div>

      <ul className="flex-1 overflow-y-auto p-2 space-y-1">
        {sessions.length === 0 && (
          <li className="text-xs text-gray-400 px-2 py-4 text-center">暂无会话，点击「新建」开始</li>
        )}
        {sessions.map((s) => (
          <li
            key={s.id}
            className={`group rounded-md px-2 py-1.5 text-sm cursor-pointer flex items-center gap-1 ${
              s.id === currentId ? 'bg-blue-100 text-blue-900' : 'hover:bg-gray-200 text-gray-700'
            }`}
            onClick={() => void select(s.id)}
            onDoubleClick={() => {
              setEditingId(s.id)
              setDraft(s.title)
            }}
          >
            {editingId === s.id ? (
              <input
                autoFocus
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commitRename}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitRename()
                  if (e.key === 'Escape') setEditingId(null)
                }}
                onClick={(e) => e.stopPropagation()}
                className="flex-1 min-w-0 px-1 py-0.5 rounded border border-blue-400 text-sm bg-white"
              />
            ) : (
              <>
                <span className="flex-1 min-w-0 truncate">{s.title || '未命名会话'}</span>
                <button
                  title="重命名"
                  onClick={(e) => {
                    e.stopPropagation()
                    setEditingId(s.id)
                    setDraft(s.title)
                  }}
                  className="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-gray-700 text-xs px-1"
                >
                  ✎
                </button>
                <button
                  title="删除"
                  disabled={streaming}
                  onClick={(e) => {
                    e.stopPropagation()
                    if (confirm(`删除会话「${s.title}」？`)) void remove(s.id)
                  }}
                  className="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-600 text-xs px-1 disabled:opacity-0"
                >
                  ✕
                </button>
              </>
            )}
          </li>
        ))}
      </ul>

      <div className="px-4 py-2 text-[11px] text-gray-400 border-t border-gray-200">
        M2 · 双击会话可重命名
      </div>
    </aside>
  )
}
