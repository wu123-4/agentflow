// 应用主布局：左侧会话侧栏 + 右侧对话主区（消息列表 + 运行状态条 + 输入框）+ 全局错误条 + MCP 管理页 + 运行轨迹
import { useEffect, useState } from 'react'
import { useApp } from './store'
import Sidebar from './components/Sidebar'
import MessageList from './components/MessageList'
import InputBox from './components/InputBox'
import McpAdminPanel from './components/McpAdminPanel'
import ApprovalDialog from './components/ApprovalDialog'
import RunStatusBar from './components/RunStatusBar'
import TraceViewer from './components/TraceViewer'

export default function App() {
  const error = useApp((s) => s.error)
  const loadSessions = useApp((s) => s.loadSessions)
  const [showAdmin, setShowAdmin] = useState(false)
  const [showTrace, setShowTrace] = useState(false)

  useEffect(() => {
    void loadSessions()
  }, [loadSessions])

  return (
    <div className="h-screen flex bg-gray-100 text-gray-900">
      <Sidebar />
      <main className="flex-1 flex flex-col min-w-0">
        <div className="flex justify-end gap-2 px-4 pt-2">
          <button
            onClick={() => setShowTrace(true)}
            className="text-xs px-3 py-1.5 rounded-md border border-gray-300 bg-white text-gray-600 hover:bg-gray-50 shadow-sm"
          >
            🧭 运行轨迹
          </button>
          <button
            onClick={() => setShowAdmin(true)}
            className="text-xs px-3 py-1.5 rounded-md border border-gray-300 bg-white text-gray-600 hover:bg-gray-50 shadow-sm"
          >
            ⚙ MCP Server 管理
          </button>
        </div>
        {error && (
          <div className="mx-6 mt-1 flex items-center justify-between rounded-lg bg-red-50 border border-red-200 text-red-700 text-xs px-3 py-2">
            <span>⚠ {error}</span>
            <button
              onClick={() => useApp.setState({ error: null })}
              className="text-red-400 hover:text-red-600 ml-3"
            >
              关闭
            </button>
          </div>
        )}
        <MessageList />
        <RunStatusBar />
        <InputBox />
      </main>
      {showAdmin && <McpAdminPanel onClose={() => setShowAdmin(false)} />}
      {showTrace && <TraceViewer onClose={() => setShowTrace(false)} />}
      <ApprovalDialog />
    </div>
  )
}
