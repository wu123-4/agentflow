// MCP Server 管理 API（docs/05 §2.4）
export interface McpToolMeta {
  name: string
  description?: string
  risk: 'low' | 'medium' | 'high'
}

export interface McpServer {
  id: string
  name: string
  transport: 'stdio' | 'streamable_http'
  command: string | null
  args: string[] | null
  url: string | null
  env_keys: string[]
  enabled: boolean
  risk_overrides: Record<string, string> | null
  status: 'connected' | 'degraded' | 'disconnected'
  last_error: string | null
  tools: McpToolMeta[]
  tools_count: number
  restart_count: number
  created_at: string
  updated_at: string
}

const BASE = '/api/v1/mcp/servers'

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = await res.json()
      msg = body?.detail?.message ?? body?.detail ?? msg
    } catch {
      /* 忽略 */
    }
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return (await res.json()) as T
}

export async function listServers(): Promise<McpServer[]> {
  const d = await j<{ items: McpServer[] }>(await fetch(BASE))
  return d.items
}

export interface ServerCreateBody {
  name: string
  transport?: string
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  enabled?: boolean
  risk_overrides?: Record<string, string>
}

export async function createServer(body: ServerCreateBody): Promise<McpServer> {
  return j<McpServer>(
    await fetch(BASE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  )
}

export async function patchServer(id: string, body: Partial<ServerCreateBody>): Promise<McpServer> {
  return j<McpServer>(
    await fetch(`${BASE}/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  )
}

export async function deleteServer(id: string): Promise<void> {
  await j<{ ok: boolean }>(await fetch(`${BASE}/${id}`, { method: 'DELETE' }))
}

export async function fetchServerTools(id: string): Promise<{ status: string; tools: McpToolMeta[]; error?: string }> {
  return j(await fetch(`${BASE}/${id}/tools`))
}
