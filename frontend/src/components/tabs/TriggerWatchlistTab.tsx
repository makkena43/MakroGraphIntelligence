import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchSelectorWatchlist, runTriggerCheck } from '../../api'
import { SectionHeader, Spinner } from '../ui'

const CAT_STYLE: Record<string, string> = {
  CORE_BUY:   'bg-emerald-900/60 text-emerald-300 border-emerald-700',
  TIMING_BUY: 'bg-indigo-900/60 text-indigo-300 border-indigo-700',
  WATCH:      'bg-slate-700/60 text-slate-300 border-slate-600',
}
const CAT_LABEL: Record<string, string> = {
  CORE_BUY: 'CORE BUY', TIMING_BUY: 'TIMING BUY', WATCH: 'WATCH',
}

type Match = { d: string; title: string }
type Entry = {
  id: number; ticker: string; category: string; theme: string
  trigger_desc: string; action: string; as_of_basis: string
  last_checked: string | null; last_fired: boolean | null
  last_decision: string | null; last_matches: Match[] | null
}

function decisionClass(e: Entry): string {
  if (e.category === 'CORE_BUY') return 'text-emerald-300'
  if (e.last_fired) return 'text-amber-300 font-semibold'
  return 'text-slate-400'
}

export default function TriggerWatchlistTab({ country }: { country: 'US' | 'IN'; countryFlag?: string; countryLabel?: string }) {
  const qc = useQueryClient()
  const [sinceDays, setSinceDays] = useState(30)
  const { data, isLoading } = useQuery({
    queryKey: ['selector-watchlist', country],
    queryFn: () => fetchSelectorWatchlist(country),
  })
  const check = useMutation({
    mutationFn: () => {
      const since = new Date(Date.now() - sinceDays * 86400_000).toISOString().slice(0, 10)
      return runTriggerCheck(country, since)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['selector-watchlist', country] }),
  })

  if (isLoading) return <Spinner />
  const entries: Entry[] = data?.entries ?? []
  const fired = entries.filter(e => e.last_fired && e.category !== 'CORE_BUY')

  return (
    <div className="space-y-4">
      <SectionHeader
        title="🔔 Trigger Watchlist"
        subtitle="Selector-report stocks (Core / Timing / Watch) with machine-checkable triggers. Run the check to get each stock's decision as of today — decisions come from newly ingested filings matching each trigger pattern."
      />

      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={() => check.mutate()}
          disabled={check.isPending}
          className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-sm font-semibold text-white"
        >
          {check.isPending ? 'Checking filings…' : '▶ Run Trigger Check Now'}
        </button>
        <label className="text-xs text-slate-400 flex items-center gap-2">
          Look back
          <select value={sinceDays} onChange={e => setSinceDays(Number(e.target.value))}
                  className="bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-200">
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
            <option value={365}>1 year</option>
          </select>
        </label>
        <span className="text-xs text-slate-500">
          Last run: {data?.last_run ? data.last_run.slice(0, 16) : 'never'} · Filings ingested through: {data?.data_through?.slice(0, 10) ?? '—'}
        </span>
      </div>

      {check.isSuccess && (
        <div className="rounded-lg border border-indigo-800 bg-indigo-950/40 px-4 py-2 text-xs text-indigo-200">
          Check complete for {check.data.run_date} (filings after {check.data.since}) —{' '}
          {check.data.results.filter((r: { fired: boolean }) => r.fired).length} trigger(s) fired.
        </div>
      )}

      {fired.length > 0 && (
        <div className="rounded-xl border border-amber-700 bg-amber-950/30 px-4 py-3">
          <div className="text-sm font-bold text-amber-300 mb-1">🔔 Fired — act after confirming the action line</div>
          {fired.map(e => (
            <div key={e.id} className="text-xs text-amber-200/90">
              <strong>{e.ticker}</strong> — {e.last_decision}
            </div>
          ))}
        </div>
      )}

      <div className="overflow-x-auto rounded-xl border border-slate-800">
        <table className="w-full text-xs">
          <thead className="bg-slate-800/80 text-slate-300">
            <tr>
              <th className="px-3 py-2 text-left">Stock</th>
              <th className="px-3 py-2 text-left">Bucket</th>
              <th className="px-3 py-2 text-left">Theme</th>
              <th className="px-3 py-2 text-left">Trigger</th>
              <th className="px-3 py-2 text-left">Decision (as of last run)</th>
              <th className="px-3 py-2 text-left">Matched filings</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(e => (
              <tr key={e.id} className="border-t border-slate-800/70 align-top hover:bg-slate-800/30">
                <td className="px-3 py-2 font-bold text-slate-100">{e.ticker}</td>
                <td className="px-3 py-2">
                  <span className={`px-2 py-0.5 rounded-full border text-[10px] font-bold ${CAT_STYLE[e.category] ?? ''}`}>
                    {CAT_LABEL[e.category] ?? e.category}
                  </span>
                </td>
                <td className="px-3 py-2 text-slate-400">{e.theme}</td>
                <td className="px-3 py-2 text-slate-300">{e.trigger_desc}</td>
                <td className={`px-3 py-2 max-w-md ${decisionClass(e)}`}>
                  {e.last_decision ?? '— not checked yet —'}
                  {e.last_checked && (
                    <div className="text-[10px] text-slate-500 mt-0.5">checked {e.last_checked.slice(0, 16)}</div>
                  )}
                </td>
                <td className="px-3 py-2 text-slate-400">
                  {(e.last_matches ?? []).map((m, i) => (
                    <div key={i} className="text-[10px]">{m.d} — {m.title}</div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-[11px] text-slate-500">
        Decisions: CORE BUY = standing buy per position guidance · TIMING BUY = buy only when its trigger fires ·
        WATCH = upgrade to trade-sized buy when the catalyst fires. Alert freshness is bounded by filings ingestion
        (run the Concall &amp; Filings pipeline first for the latest documents). Price action never changes a decision.
      </p>
    </div>
  )
}
