// 任务契约面板（Harness Phase 1）：配置下一次发送携带的 TaskContract。
// 留空/默认 = 不收紧（与后端 TaskContract.default() 行为一致）。
import { useState } from 'react'
import { useApp } from '../store'
import type { TaskContract } from '../types'

function num(v: string): number | undefined {
  const n = parseInt(v, 10)
  return Number.isFinite(n) && n >= 0 ? n : undefined
}

export default function ContractPanel() {
  const draft = useApp((s) => s.contractDraft)
  const setDraft = useApp((s) => s.setContractDraft)
  const [open, setOpen] = useState(false)

  const c = draft ?? {}
  const budgets = c.budgets ?? {}

  const update = (patch: Partial<TaskContract>) => {
    const base: TaskContract = draft ?? {}
    const next: TaskContract = { ...base, ...patch }
    // budgets 深合并
    if (patch.budgets) {
      next.budgets = { ...(base.budgets ?? {}), ...patch.budgets }
      for (const k of ['max_tokens', 'max_tool_calls', 'max_iterations'] as const) {
        if (next.budgets[k] == null) delete next.budgets[k]
      }
      if (Object.keys(next.budgets).length === 0) delete next.budgets
    }
    for (const k of ['max_risk', 'allowed_tools', 'denied_tools', 'invariants'] as const) {
      const v = next[k]
      if (v == null || (Array.isArray(v) && v.length === 0)) delete next[k]
    }
    if (next.gates_enabled === true) delete next.gates_enabled
    setDraft(Object.keys(next).length ? next : null)
  }

  const listField = (v: string): string[] | undefined => {
    const arr = v.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
    return arr.length ? arr : undefined
  }

  const active = draft !== null
  const chips = active
    ? [
        c.max_risk ? `风险≤${c.max_risk}` : null,
        budgets.max_tokens != null ? `Token≤${budgets.max_tokens}` : null,
        budgets.max_tool_calls != null ? `工具≤${budgets.max_tool_calls}` : null,
        budgets.max_iterations != null ? `迭代≤${budgets.max_iterations}` : null,
        c.allowed_tools ? `白名单${c.allowed_tools.length}` : null,
        c.denied_tools ? `黑名单${c.denied_tools.length}` : null,
        c.invariants?.length ? `不变量${c.invariants.length}` : null,
        c.gates_enabled === false ? '门控关' : null,
      ].filter(Boolean)
    : []

  return (
    <div className="border-t border-gray-200 bg-gray-50 text-xs">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-4 py-1.5 text-left text-gray-600 hover:bg-gray-100"
      >
        <span>📋 任务契约</span>
        {active ? (
          <span className="flex gap-1 flex-wrap">
            {chips.map((t) => (
              <span key={t} className="px-1.5 py-0.5 rounded bg-blue-100 text-blue-700">{t}</span>
            ))}
          </span>
        ) : (
          <span className="text-gray-400">默认（不收紧）</span>
        )}
        <span className="ml-auto text-gray-400">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="px-4 pb-3 grid grid-cols-2 md:grid-cols-4 gap-3">
          <label className="flex flex-col gap-1 text-gray-500">
            最高风险
            <select
              value={c.max_risk ?? 'high'}
              onChange={(e) => update({ max_risk: e.target.value === 'high' ? undefined : (e.target.value as 'low' | 'medium' | 'high') })}
              className="rounded border border-gray-300 bg-white px-2 py-1"
            >
              <option value="high">high（默认）</option>
              <option value="medium">medium</option>
              <option value="low">low</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-gray-500">
            Token 预算
            <input
              type="number" min={0} defaultValue={budgets.max_tokens ?? ''}
              placeholder="不限"
              onBlur={(e) => update({ budgets: { max_tokens: num(e.target.value) } })}
              className="rounded border border-gray-300 px-2 py-1"
            />
          </label>
          <label className="flex flex-col gap-1 text-gray-500">
            工具调用上限
            <input
              type="number" min={0} defaultValue={budgets.max_tool_calls ?? ''}
              placeholder="不限"
              onBlur={(e) => update({ budgets: { max_tool_calls: num(e.target.value) } })}
              className="rounded border border-gray-300 px-2 py-1"
            />
          </label>
          <label className="flex flex-col gap-1 text-gray-500">
            迭代上限
            <input
              type="number" min={1} defaultValue={budgets.max_iterations ?? ''}
              placeholder="不限"
              onBlur={(e) => update({ budgets: { max_iterations: num(e.target.value) } })}
              className="rounded border border-gray-300 px-2 py-1"
            />
          </label>
          <label className="flex flex-col gap-1 text-gray-500 col-span-2">
            工具白名单（逗号分隔限定名，留空=不限）
            <input
              type="text" defaultValue={(c.allowed_tools ?? []).join(', ')}
              placeholder="filesystem__read_text_file"
              onBlur={(e) => update({ allowed_tools: listField(e.target.value) })}
              className="rounded border border-gray-300 px-2 py-1 font-mono"
            />
          </label>
          <label className="flex flex-col gap-1 text-gray-500 col-span-2">
            工具黑名单（逗号分隔限定名）
            <input
              type="text" defaultValue={(c.denied_tools ?? []).join(', ')}
              placeholder="filesystem__write_file"
              onBlur={(e) => update({ denied_tools: listField(e.target.value) })}
              className="rounded border border-gray-300 px-2 py-1 font-mono"
            />
          </label>
          <label className="flex flex-col gap-1 text-gray-500 col-span-2">
            不变量（逗号分隔：no_denied / denied&lt;=N / requires_tool:name / tool_success_rate&gt;=X）
            <input
              type="text" defaultValue={(c.invariants ?? []).join(', ')}
              placeholder="no_denied"
              onBlur={(e) => update({ invariants: listField(e.target.value) })}
              className="rounded border border-gray-300 px-2 py-1 font-mono"
            />
          </label>
          <label className="flex items-center gap-2 text-gray-500 self-end pb-1">
            <input
              type="checkbox"
              checked={c.gates_enabled !== false}
              onChange={(e) => update({ gates_enabled: e.target.checked ? undefined : false })}
            />
            完成门控
          </label>
          <div className="self-end pb-1 flex gap-2">
            <button
              onClick={() => setDraft(null)}
              className="px-2 py-1 rounded border border-gray-300 bg-white text-gray-600 hover:bg-gray-100"
            >
              清空契约
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
