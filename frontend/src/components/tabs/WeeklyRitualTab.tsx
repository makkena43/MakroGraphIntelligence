import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, fetchInvestmentFinalShortlist } from '../../api'

const fetchChanges = (country: string) =>
  api.get('/changes-since-last-run', { params: { country } }).then(r => r.data)
const fetchJournal = (country: string) =>
  api.get('/paper-trades', { params: { country } }).then(r => r.data)
const postDecision = (payload: Record<string, unknown>) =>
  api.post('/paper-trade', payload).then(r => r.data)

function DecisionButtons({ s, country, onDone }: { s: Record<string, any>; country: string; onDone: () => void }) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const act = async (action: string) => {
    setBusy(true)
    try {
      await postDecision({ ticker: s.ticker, company: s.company, country, action, note })
      onDone()
    } finally { setBusy(false) }
  }
  return (
    <div className="flex items-center gap-1.5">
      <input value={note} onChange={e => setNote(e.target.value)} placeholder="why? (journal note)"
        className="px-2 py-1 rounded text-[10px] bg-slate-800 border border-slate-700 text-slate-200 w-44" />
      <button disabled={busy} onClick={() => act('buy')}
        className="px-2 py-1 rounded text-[10px] font-bold bg-green-900/60 border border-green-700/60 text-green-300 hover:bg-green-800/60">📝 BUY (paper)</button>
      <button disabled={busy} onClick={() => act('pass')}
        className="px-2 py-1 rounded text-[10px] font-bold bg-slate-800 border border-slate-700 text-slate-400 hover:text-slate-200">PASS</button>
    </div>
  )
}

export default function WeeklyRitualTab({ country }: { country: 'US' | 'IN'; countryFlag?: string; countryLabel?: string }) {
  const c = 'IN'   // ritual runs on India (price + journal support)
  const qc = useQueryClient()
  const { data: changes } = useQuery({ queryKey: ['ritual-changes', c], queryFn: () => fetchChanges(c) })
  const { data: shortlist } = useQuery({ queryKey: ['ritual-shortlist', c], queryFn: () => fetchInvestmentFinalShortlist(c, 2024) })
  const { data: journal } = useQuery({ queryKey: ['ritual-journal', c], queryFn: () => fetchJournal(c) })

  const refreshJournal = () => qc.invalidateQueries({ queryKey: ['ritual-journal', c] })
  const conviction = ((shortlist?.final_shortlist as Record<string, any>[]) ?? []).filter(s => s.list_tier === 'conviction')
  const journalTickers = new Set(((journal?.trades as Record<string, any>[]) ?? []).map(t => t.ticker))
  const trades = (journal?.trades as Record<string, any>[]) ?? []
  const opens = trades.filter(t => t.status === 'open')
  const others = trades.filter(t => t.status !== 'open')

  const Chip = ({ label, items, color }: { label: string; items: Record<string, any>[]; color: string }) => (
    items.length ? (
      <div>
        <div className={`text-[10px] font-bold ${color}`}>{label} ({items.length})</div>
        <div className="flex flex-wrap gap-1 mt-1">
          {items.map((x, i) => (
            <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-300">
              {x.ticker}{x.from_stage != null ? ` S${x.from_stage}→S${x.to_stage}` : ''}{x.from_tier ? ` ${x.from_tier}→${x.to_tier}` : ''}
            </span>
          ))}
        </div>
      </div>
    ) : null
  )

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-base font-bold text-slate-100">📅 Weekly Ritual — 🇮🇳 Monday Review</h2>
        <p className="text-xs text-slate-500 mt-0.5">
          1) What changed · 2) Conviction review → paper decisions · 3) Open journal. Verify evidence before any BUY — the card quotes are your reading list.
        </p>
      </div>

      {/* Step 1: changes since last run */}
      <div className="rounded-xl border border-indigo-900/50 bg-slate-900/50 px-4 py-3 space-y-2">
        <div className="text-sm font-black text-indigo-300">1️⃣ WHAT CHANGED SINCE LAST RUN</div>
        {changes?.runs_available >= 2 ? (
          <>
            <div className="text-[10px] text-slate-500">{String(changes.previous_run)} → {String(changes.current_run)}</div>
            <div className="grid md:grid-cols-2 gap-3">
              <Chip label="🆕 NEW ENTRANTS" items={changes.new_entrants ?? []} color="text-green-400" />
              <Chip label="⬆ STAGE MOVES" items={changes.stage_moves ?? []} color="text-amber-400" />
              <Chip label="🎯 TIER MOVES" items={changes.tier_moves ?? []} color="text-red-400" />
              <Chip label="👁 NEW EASING FLAGS" items={changes.new_easing_flags ?? []} color="text-orange-400" />
              <Chip label="🚪 DROPPED" items={changes.dropped ?? []} color="text-slate-400" />
            </div>
            {!((changes.new_entrants ?? []).length || (changes.stage_moves ?? []).length || (changes.tier_moves ?? []).length) &&
              <div className="text-xs text-slate-500">No changes — a quiet week is information too.</div>}
          </>
        ) : (
          <div className="text-xs text-slate-500">
            Need two live runs to diff (runs so far: {String(changes?.runs_available ?? 0)}). Open Today's Opportunities on "Latest" once a week to record runs — diffs appear from week two.
          </div>
        )}
      </div>

      {/* Step 2: conviction review with decisions */}
      <div className="rounded-xl border border-red-900/50 bg-slate-900/50 px-4 py-3 space-y-2">
        <div className="text-sm font-black text-red-300">2️⃣ CONVICTION LIST — DECIDE &amp; JOURNAL ({conviction.length})</div>
        <div className="text-[10px] text-slate-500">Entry price auto-captured from latest NSE close. Check the 🚀 Breakout Scanner for timing before buying.</div>
        <div className="space-y-1.5">
          {conviction.map((s, i) => (
            <div key={i} className="flex items-center gap-3 px-3 py-2 rounded-lg bg-slate-900/60 border border-slate-800 flex-wrap">
              <div className="w-52 min-w-0">
                <span className="text-sm font-black text-slate-100">{s.ticker}</span>
                <span className="text-[10px] text-slate-400 ml-2">{String(s.company ?? '').slice(0, 22)}</span>
                <div className="text-[10px] text-slate-500">S{s.constraint_stage} · {s.trajectory} · score {s.rank_score} · {s.peer_corroboration} peers</div>
              </div>
              {journalTickers.has(s.ticker)
                ? <span className="text-[10px] text-slate-500 italic">already journaled ✓</span>
                : <DecisionButtons s={s} country={c} onDone={refreshJournal} />}
            </div>
          ))}
        </div>
      </div>

      {/* Step 3: the journal */}
      <div className="rounded-xl border border-green-900/50 bg-slate-900/50 px-4 py-3 space-y-2">
        <div className="text-sm font-black text-green-300">
          3️⃣ PAPER JOURNAL — {journal?.open_count ?? 0} open
          {journal?.open_avg_pnl_pct != null && (
            <span className={`ml-2 ${journal.open_avg_pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              avg {journal.open_avg_pnl_pct >= 0 ? '+' : ''}{journal.open_avg_pnl_pct}%
            </span>
          )}
        </div>
        {opens.map((t, i) => (
          <div key={i} className="flex items-center gap-3 px-3 py-2 rounded-lg bg-slate-900/60 border border-slate-800 flex-wrap">
            <span className="text-sm font-black text-slate-100 w-24">{t.ticker}</span>
            <span className="text-[10px] text-slate-500">bought {t.decided_at} @ ₹{t.entry_price}</span>
            {t.pnl_pct != null && (
              <span className={`text-xs font-bold ${t.pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct}% <span className="text-slate-600 font-normal">(₹{t.current_price} as of {t.price_as_of})</span>
              </span>
            )}
            <span className="text-[10px] text-slate-500 flex-1 truncate">{t.note}</span>
            <button onClick={async () => { await postDecision({ ticker: t.ticker, country: c, action: 'exit', note: 'manual exit' }); refreshJournal() }}
              className="px-2 py-1 rounded text-[10px] font-bold bg-red-950/60 border border-red-800/60 text-red-300">EXIT</button>
          </div>
        ))}
        {others.length > 0 && (
          <details className="text-[10px] text-slate-500">
            <summary className="cursor-pointer">history ({others.length})</summary>
            {others.map((t, i) => (
              <div key={i} className="flex gap-3 px-3 py-1">
                <span className="w-24 font-bold text-slate-400">{t.ticker}</span>
                <span>{t.action} · {t.decided_at} · {t.status}</span>
                {t.pnl_pct != null && <span className={t.pnl_pct >= 0 ? 'text-green-500' : 'text-red-500'}>{t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct}%</span>}
                <span className="flex-1 truncate italic">{t.note}</span>
              </div>
            ))}
          </details>
        )}
        {!trades.length && <div className="text-xs text-slate-500">No decisions recorded yet. Your out-of-sample record starts with the first one.</div>}
      </div>
    </div>
  )
}
