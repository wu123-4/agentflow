// 消息列表：用户/助手气泡、Markdown 渲染、工具卡片、状态条、自动滚底
import { useEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { useApp } from '../store'
import ToolCallCard from './ToolCallCard'
import type { ChatMessage } from '../types'

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-4`}>
      <div
        className={`max-w-[75%] rounded-2xl px-4 py-2.5 text-sm ${
          isUser
            ? 'bg-blue-600 text-white rounded-br-sm'
            : 'bg-white border border-gray-200 text-gray-800 rounded-bl-sm'
        }`}
      >
        {isUser ? (
          <div className="whitespace-pre-wrap break-words">{msg.content}</div>
        ) : (
          <div className="md-body">
            {msg.content ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
                {msg.content}
              </ReactMarkdown>
            ) : (
              <span className="text-gray-400 italic">思考中…</span>
            )}
          </div>
        )}
        {msg.tool_calls && msg.tool_calls.length > 0 && (
          <div className="mt-2 space-y-1">
            {msg.tool_calls.map((tc) => (
              <ToolCallCard key={tc.call_id} call={tc} />
            ))}
          </div>
        )}
        {msg.usage && (
          <div className="mt-2 text-[10px] text-gray-400">
            Token：输入 {msg.usage.input_tokens} · 输出 {msg.usage.output_tokens}
          </div>
        )}
      </div>
    </div>
  )
}

export default function MessageList() {
  const messages = useApp((s) => s.messages)
  const statusText = useApp((s) => s.statusText)
  const endRef = useRef<HTMLDivElement>(null)

  // 自动滚底
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, statusText])

  return (
    <div className="flex-1 overflow-y-auto px-6 py-4">
      {messages.length === 0 && (
        <div className="text-center text-gray-400 text-sm mt-20">
          发送消息开始对话
        </div>
      )}
      {messages.map((m) => (
        <MessageBubble key={m.id} msg={m} />
      ))}
      {statusText && (
        <div className="flex justify-start mb-4">
          <div className="bg-gray-100 border border-gray-200 rounded-lg px-3 py-1.5 text-xs text-gray-500 animate-pulse">
            {statusText}
          </div>
        </div>
      )}
      <div ref={endRef} />
    </div>
  )
}
