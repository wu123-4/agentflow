// 输入框：支持多行、Ctrl/Cmd+Enter 发送、流式期间禁用并显示停止按钮；上方挂载任务契约面板
import { useState, useRef, type KeyboardEvent } from 'react'
import { useApp } from '../store'
import type { Channel } from '../types'
import ContractPanel from './ContractPanel'

const CHANNELS: Array<{ value: Channel | null; label: string; title: string }> = [
  { value: null, label: '自动', title: '由 router 判定通道（可升级）' },
  { value: 'fast', label: 'fast', title: '2 步 / 15s / 不挂工具（锁定）' },
  { value: 'calc', label: 'calc', title: '8 步 / 60s / 取数类工具（锁定）' },
  { value: 'deep', label: 'deep', title: '50 步 / 600s / 全部工具（锁定）' },
]

export default function InputBox() {
  const [text, setText] = useState('')
  const streaming = useApp((s) => s.streaming)
  const send = useApp((s) => s.send)
  const stop = useApp((s) => s.stop)
  const channelDraft = useApp((s) => s.channelDraft)
  const setChannelDraft = useApp((s) => s.setChannelDraft)
  const taRef = useRef<HTMLTextAreaElement>(null)

  const submit = () => {
    if (streaming || !text.trim()) return
    void send(text)
    setText('')
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Ctrl/Cmd + Enter 发送；Shift+Enter 换行
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="border-t border-gray-200 bg-white">
      <ContractPanel />
      <div className="px-6 py-3">
        <div className="flex items-center gap-1 mb-2 text-[11px] text-gray-500">
          <span className="mr-1">通道</span>
          {CHANNELS.map((c) => {
            const active = channelDraft === c.value
            return (
              <button
                key={c.label}
                type="button"
                title={c.title}
                disabled={streaming}
                onClick={() => setChannelDraft(c.value)}
                className={`px-2 py-0.5 rounded border ${
                  active
                    ? 'bg-blue-600 border-blue-600 text-white'
                    : 'bg-white border-gray-300 text-gray-600 hover:bg-gray-50'
                } disabled:opacity-40`}
              >
                {c.label}
              </button>
            )
          })}
          {channelDraft !== null && (
            <span className="ml-1 text-amber-600">已锁定，预算内不会升级</span>
          )}
        </div>
        <div className="flex items-end gap-2">
        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={streaming ? '正在生成中…' : '输入消息，Ctrl/Cmd+Enter 发送'}
          disabled={streaming}
          rows={2}
          className="flex-1 resize-none rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:border-blue-500 disabled:bg-gray-50 disabled:text-gray-400"
        />
        {streaming ? (
          <button
            onClick={stop}
            className="px-4 py-2 rounded-lg bg-red-500 text-white text-sm hover:bg-red-600"
          >
            停止
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={!text.trim()}
            className="px-4 py-2 rounded-lg bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-40"
          >
            发送
          </button>
        )}
      </div>
      <div className="text-[11px] text-gray-400 mt-1">
        Shift+Enter 换行 · Ctrl/Cmd+Enter 发送
      </div>
      </div>
    </div>
  )
}
