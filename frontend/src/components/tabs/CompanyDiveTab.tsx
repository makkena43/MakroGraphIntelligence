/**
 * Company Deep Dive Tab
 *
 * Enter any company name/ticker → fetch last 2-3 years of concalls +
 * announcements → Claude investment analysis → cached in DB.
 * Shows: investment thesis, constraint/demand evidence, recommendation,
 * industry peers, recent filings.
 */

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { searchCompanies, fetchSavedCompanyDive, runCompanyDive, fetchAllAnalysedCompanies } from '../../api'
import { Spinner, EmptyState } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const YEARS = [2026, 2025, 2024, 2023, 2022, 2021, 2020]

const ACTION_STYLE: Record<string, { color: string; bg: string; border: string }> = {
  strong_buy: { color: 'text-emerald-300', bg: 'bg-emerald-950/60', border: 'border-emerald-700/60' },
  buy:        { color: 'text-blue-300',    bg: 'bg-blue-950/50',    border: 'border-blue-700/50' },
  hold:       { color: 'text-amber-300',   bg: 'bg-amber-950/40',   border: 'border-amber-700/50' },
  sell:       { color: 'text-red-300',     bg: 'bg-red-950/50',     border: 'border-red-700/50' },
}
const SEV_COLOR: Record<string, string> = {
  critical: 'text-red-400', high: 'text-orange-400', moderate: 'text-amber-400', low: 'text-slate-400',
}
const CONV_COLOR: Record<string, string> = {
  high: 'text-emerald-400', medium: 'text-amber-400', low: 'text-slate-500',
}

export default function CompanyDiveTab({ country }: Props) {
  const [localCountry, setLocalCountry] = useState<'IN' | 'US'>(country === 'IN' ? 'IN' : 'US')
  const [selectedYear, setSelectedYear] = useState<number>(new Date().getFullYear())
  const [query, setQuery]             = useState('')
  const [selected, setSelected]       = useState<{ name: string; ticker: string } | null>(null)
  const [loading, setLoading]         = useState(false)
  const [result, setResult]           = useState<Record<string, unknown> | null>(null)
  const [error, setError]             = useState<string | null>(null)
  const [section, setSection]         = useState<'analysis' | 'concalls' | 'policy' | 'filings' | 'peers'>('analysis')

  const queryClient = useQueryClient()

  // Search suggestions
  const { data: suggestions = [] } = useQuery({
    queryKey: ['company-search', query, localCountry],
    queryFn: () => searchCompanies(query, localCountry),
    enabled: query.length >= 2,
    staleTime: 30_000,
  })

  // Previously analysed companies
  const { data: history = [], refetch: refetchHistory } = useQuery({
    queryKey: ['all-analysed', localCountry],
    queryFn: () => fetchAllAnalysedCompanies(localCountry),
    staleTime: 60_000,
  })

  // Auto-load saved analysis when company + year are selected
  const { data: savedDive, isLoading: savedLoading } = useQuery({
    queryKey: ['company-dive-saved', selected?.name, localCountry, selectedYear],
    queryFn: () => fetchSavedCompanyDive(selected!.ticker || selected!.name, localCountry, selectedYear),
    enabled: !!selected,
    staleTime: 5 * 60_000,
  })

  const displayResult = result ?? (
    savedDive && Object.keys(savedDive).length > 0 ? savedDive as Record<string, unknown> : null
  )

  const run = async (forceRefresh = false) => {
    if (!selected) return
    setLoading(true); setResult(null); setError(null)
    try {
      const res = await runCompanyDive({
        company: selected.ticker || selected.name,
        country: localCountry,
        year: selectedYear,
        force_refresh: forceRefresh,
      })
      setResult(res)
      queryClient.invalidateQueries({ queryKey: ['company-dive-saved', selected.name, localCountry, selectedYear] })
      refetchHistory()
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } }; message?: string })
        ?.response?.data?.detail ?? String(e)
      setError(msg)
    } finally { setLoading(false) }
  }

  const brief = displayResult?.brief as Record<string, unknown> | undefined
  const rec   = brief?.recommendation as Record<string, unknown> | undefined
  const recStyle = ACTION_STYLE[String(rec?.action ?? 'hold')] ?? ACTION_STYLE.hold

  return (
    <div className="space-y-4">

      {/* Header */}
      <div>
        <h2 className="text-base font-bold text-slate-100">🔍 Company Deep Dive</h2>
        <p className="text-xs text-slate-500 mt-0.5">
          Search any company → fetch 3 years of concalls → Claude investment analysis · results cached
        </p>
      </div>

      {/* Controls */}
      <div className="flex items-start gap-3 flex-wrap">
        {/* Country */}
        <div className="flex rounded-lg border border-slate-700 overflow-hidden">
          {(['IN', 'US'] as const).map(c => (
            <button key={c} onClick={() => { setLocalCountry(c); setSelected(null); setResult(null); setQuery('') }}
              className={`px-3 py-1 text-xs font-semibold transition-colors ${
                localCountry === c ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'
              }`}>{c === 'IN' ? '🇮🇳 India' : '🇺🇸 US'}</button>
          ))}
        </div>

        {/* Year */}
        <div className="flex gap-1.5 flex-wrap">
          {YEARS.map(y => (
            <button key={y} onClick={() => { setSelectedYear(y); setResult(null) }}
              className={`px-3 py-1 rounded-full text-xs font-semibold border transition-colors ${
                selectedYear === y ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-slate-800 border-slate-700 text-slate-300 hover:border-indigo-500'
              }`}>{String(y)}{y === new Date().getFullYear() ? ' YTD' : ''}</button>
          ))}
        </div>

        {/* Company search */}
        <div className="flex-1 min-w-64 relative">
          <input
            type="text"
            value={selected ? String(selected.name) + " (" + String(selected.ticker) + ")" : query}
            onChange={e => { setQuery(e.target.value); setSelected(null); setResult(null) }}
            onFocus={() => { if (selected) setQuery('') }}
            placeholder="Search company name or ticker…"
            className="input w-full pr-8"
          />
          {query.length >= 2 && !selected && Boolean((suggestions as Record<string,unknown>[]).length) && (
            <div className="absolute top-full left-0 right-0 mt-1 bg-slate-800 border border-slate-700 rounded-lg shadow-xl z-20 max-h-48 overflow-y-auto">
              {(suggestions as Record<string,unknown>[]).slice(0, 10).map((s, i) => (
                <button key={i} onClick={() => {
                  setSelected({ name: String(s.company ?? s.name ?? ''), ticker: String(s.ticker ?? '') })
                  setQuery('')
                }}
                  className="w-full text-left px-3 py-2 text-xs hover:bg-slate-700 transition-colors flex items-center gap-2">
                  <span className="font-mono font-bold text-indigo-300 w-16 flex-shrink-0">{String(s.ticker ?? '—')}</span>
                  <span className="text-slate-200 truncate">{String(s.company ?? s.name ?? '')}</span>
                </button>
              ))}
            </div>
          )}
          {!!selected && (
            <button onClick={() => { setSelected(null); setQuery(''); setResult(null) }}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-red-400 text-xs">✕</button>
          )}
        </div>

        {/* Run button */}
        {!!selected && (
          <div className="flex items-center gap-2">
            <button onClick={() => run(false)} disabled={loading || savedLoading}
              className="flex items-center gap-2 px-4 py-2 rounded-xl bg-gradient-to-r from-indigo-700 to-purple-700 hover:from-indigo-600 hover:to-purple-600 disabled:opacity-50 text-white text-sm font-bold transition-all">
              {loading ? <><Spinner /> Analysing…</> : displayResult ? '✨ Re-analyse' : '✨ Analyse'}
            </button>
            {!!displayResult && !loading && (
              <button onClick={() => run(true)} disabled={loading}
                className="px-3 py-2 rounded-xl bg-slate-800 border border-slate-700 hover:border-amber-600 text-slate-400 hover:text-amber-300 text-xs transition-colors">
                🔄 Force refresh
              </button>
            )}
          </div>
        )}
      </div>

      {/* Loading */}
      {loading && (
        <div className="bg-indigo-950/30 border border-indigo-800/30 rounded-xl p-6 text-center space-y-2">
          <Spinner />
          <p className="text-sm text-indigo-300 font-medium">
            Fetching {String(selectedYear-2)}–{String(selectedYear)} data for {selected?.name ?? ""}…
          </p>
          <p className="text-xs text-slate-500">
            Searching concalls · extracting signals · calling Claude Sonnet
            {localCountry === 'IN' ? ' · may trigger fresh India fetch if data sparse' : ''}
          </p>
        </div>
      )}

      {/* Cache notice */}
      {!!displayResult && !!(displayResult as Record<string,unknown>).from_cache && !loading && !result && (
        <div className="flex items-center gap-2 text-xs text-slate-500 bg-slate-900/40 border border-slate-800 rounded-lg px-3 py-1.5">
          <span className="text-emerald-500">✓ Cached analysis</span>
          <span>— {new Date(String((displayResult as Record<string,unknown>).generated_at)).toLocaleDateString()}</span>
          <span>·</span>
          <span>Click Re-analyse for latest data</span>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="bg-red-950/40 border border-red-700/50 rounded-xl px-4 py-3">
          <div className="text-sm font-bold text-red-300 mb-1">❌ Analysis Failed</div>
          <p className="text-xs text-red-400">{error}</p>
        </div>
      )}

      {/* Results */}
      {!!displayResult && !!brief && !loading && (
        <>
          {/* Company header + recommendation */}
          <div className={`rounded-xl border px-5 py-4 ${recStyle.bg} ${recStyle.border}`}>
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-lg font-black text-slate-100">
                    {String((displayResult as Record<string,unknown>).ticker ?? '')}
                  </span>
                  <span className="text-sm text-slate-300">
                    {String((displayResult as Record<string,unknown>).company ?? '')}
                  </span>
                  <span className="text-xs text-slate-500">
                    · {String((displayResult as Record<string,unknown>).year ?? '')}
                    · {String((displayResult as Record<string,unknown>).country ?? '')}
                  </span>
                </div>
                <p className="text-xs text-slate-400">
                  {String(brief.company_overview ?? '')}
                </p>
              </div>
              {!!rec && (
                <div className={`text-center px-4 py-3 rounded-xl border ${recStyle.bg} ${recStyle.border}`}>
                  <div className={`text-xl font-black uppercase ${recStyle.color}`}>
                    {String(rec.action ?? '').replace('_',' ')}
                  </div>
                  <div className={`text-xs font-bold ${CONV_COLOR[String(rec.conviction ?? 'low')]}`}>
                    {String(rec.conviction ?? '')} conviction
                  </div>
                  <div className="text-[10px] text-slate-500 mt-0.5">
                    {String(rec.time_horizon ?? '')}
                  </div>
                </div>
              )}
            </div>

            {/* Best quote */}
            {!!brief.best_quote && (
              <div className="mt-3 border-t border-slate-700/50 pt-3">
                <p className="text-[11px] text-slate-300 italic leading-relaxed">
                  📋 "{String(brief.best_quote).slice(0, 300)}"
                </p>
              </div>
            )}
          </div>

          {/* Data summary strip */}
          {!!(displayResult as Record<string,unknown>).data_summary && (
            <div className="flex gap-4 text-xs text-slate-500 bg-slate-900/40 border border-slate-800 rounded-lg px-4 py-2">
              <span>📄 {Number(((displayResult as Record<string,unknown>).data_summary as Record<string,unknown>)?.filings_found ?? 0)} filings ({String(((displayResult as Record<string,unknown>).data_summary as Record<string,unknown>)?.period ?? '')})</span>
              <span>📊 {Number(((displayResult as Record<string,unknown>).data_summary as Record<string,unknown>)?.themes_count ?? 0)} themes</span>
              <span>👥 {Number(((displayResult as Record<string,unknown>).data_summary as Record<string,unknown>)?.peers_count ?? 0)} peers</span>
              <span className="text-slate-600">Signal types: {(((displayResult as Record<string,unknown>).data_summary as Record<string,unknown>)?.signal_types as string[] ?? []).join(', ')}</span>
            </div>
          )}

          {/* Section tabs */}
          <div className="flex gap-1 border-b border-slate-800">
            {([
              { key: 'analysis',  label: '📈 Investment Analysis' },
              { key: 'concalls',  label: `📞 Concalls (${((displayResult as Record<string,unknown>).concall_timeline as unknown[] ?? []).length})` },
              { key: 'policy',    label: `🏛️ Policy & PLI (${((displayResult as Record<string,unknown>).pli_matches as unknown[] ?? []).length})` },
              { key: 'filings',   label: `📄 Filings (${((displayResult as Record<string,unknown>).recent_filings as unknown[] ?? []).length})` },
              { key: 'peers',     label: `👥 Peers (${((displayResult as Record<string,unknown>).peers as unknown[] ?? []).length})` },
            ] as const).map(s => (
              <button key={s.key} onClick={() => setSection(s.key)}
                className={`px-4 py-2 text-xs font-medium rounded-t transition-colors ${
                  section === s.key ? 'bg-slate-800 text-indigo-300 border-b-2 border-indigo-500' : 'text-slate-500 hover:text-slate-300'
                }`}>{s.label}</button>
            ))}
          </div>

          {/* ANALYSIS */}
          {section === 'analysis' && (
            <div className="space-y-3">
              {/* Investment thesis */}
              <div className="bg-indigo-950/30 border border-indigo-800/30 rounded-xl p-4">
                <div className="text-xs font-bold text-indigo-400 uppercase tracking-wide mb-2">💡 Investment Thesis</div>
                <p className="text-sm text-slate-200 leading-relaxed">{String(brief.investment_thesis ?? '')}</p>
              </div>

              {/* Constraint + Demand + Capex grid */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                {/* Constraint */}
                {!!brief.constraint_analysis && (
                  <div className="bg-red-950/20 border border-red-900/30 rounded-xl p-3">
                    <div className="text-[10px] font-bold text-red-400 uppercase tracking-wide mb-2">⚠️ Constraint</div>
                    <div className={`text-xs font-bold mb-1 ${SEV_COLOR[String((brief.constraint_analysis as Record<string,unknown>)?.severity ?? 'low')]}`}>
                      {String((brief.constraint_analysis as Record<string,unknown>)?.severity ?? '').toUpperCase()} severity
                    </div>
                    <p className="text-xs text-slate-300">{String((brief.constraint_analysis as Record<string,unknown>)?.constraint_type ?? '')}</p>
                    {!!(brief.constraint_analysis as Record<string,unknown>)?.evidence && (
                      <p className="text-[10px] text-slate-500 italic mt-1">"{String((brief.constraint_analysis as Record<string,unknown>)?.evidence ?? '').slice(0,150)}"</p>
                    )}
                  </div>
                )}

                {/* Demand */}
                {!!brief.demand_analysis && (
                  <div className="bg-blue-950/20 border border-blue-900/30 rounded-xl p-3">
                    <div className="text-[10px] font-bold text-blue-400 uppercase tracking-wide mb-2">📈 Demand</div>
                    <div className={`text-xs font-bold mb-1 ${
                      String((brief.demand_analysis as Record<string,unknown>)?.demand_trend ?? '') === 'growing' ? 'text-emerald-400' :
                      String((brief.demand_analysis as Record<string,unknown>)?.demand_trend ?? '') === 'declining' ? 'text-red-400' : 'text-slate-400'
                    }`}>
                      {String((brief.demand_analysis as Record<string,unknown>)?.demand_trend ?? '').toUpperCase()}
                    </div>
                    <div className="flex flex-wrap gap-1 mt-1">
                      {((brief.demand_analysis as Record<string,unknown>)?.key_demand_drivers as string[] ?? []).map((d, i) => (
                        <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-blue-900/30 text-blue-300">{d}</span>
                      ))}
                    </div>
                  </div>
                )}

                {/* Capex */}
                {!!brief.capex_signal && (
                  <div className={`border rounded-xl p-3 ${
                    (brief.capex_signal as Record<string,unknown>)?.investing_to_grow
                      ? 'bg-amber-950/20 border-amber-900/30'
                      : 'bg-slate-900/40 border-slate-800'
                  }`}>
                    <div className="text-[10px] font-bold text-amber-400 uppercase tracking-wide mb-2">🔨 Capex</div>
                    <div className={`text-xs font-bold mb-1 ${(brief.capex_signal as Record<string,unknown>)?.investing_to_grow ? 'text-amber-400' : 'text-slate-500'}`}>
                      {(brief.capex_signal as Record<string,unknown>)?.investing_to_grow ? 'INVESTING' : 'NOT INVESTING'}
                    </div>
                    <p className="text-[10px] text-slate-400">{String((brief.capex_signal as Record<string,unknown>)?.implication ?? '')}</p>
                  </div>
                )}
              </div>

              {/* Financial signals */}
              {(brief.financial_signals as Record<string,unknown>[] ?? []).length > 0 && (
                <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
                  <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wide mb-2">💰 Financial Signals</div>
                  <div className="space-y-2">
                    {(brief.financial_signals as Record<string,unknown>[]).map((fs, i) => (
                      <div key={i} className="flex items-start gap-2 text-xs">
                        <span className="text-slate-500 mt-0.5 flex-shrink-0">•</span>
                        <div>
                          <span className="text-slate-300">{String(fs.signal ?? '')}</span>
                          <span className="text-emerald-500 ml-2 italic">{String(fs.implication ?? '')}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Recommendation detail */}
              {!!rec && (
                <div className={`rounded-xl border p-4 ${recStyle.bg} ${recStyle.border}`}>
                  <div className="text-[10px] font-bold uppercase tracking-wide mb-2" style={{ color: recStyle.color.replace('text-','') }}>
                    🎯 Recommendation Detail
                  </div>
                  <div className="grid grid-cols-2 gap-3 text-xs">
                    <div>
                      <span className="text-slate-500">Entry trigger: </span>
                      <span className="text-slate-200">{String(rec.price_trigger ?? '')}</span>
                    </div>
                    <div>
                      <span className="text-slate-500">Exit trigger: </span>
                      <span className="text-red-300">{String(rec.stop_loss_trigger ?? '')}</span>
                    </div>
                  </div>
                </div>
              )}

              {/* Peer comparison */}
              {!!brief.peer_comparison && (
                <div className="text-xs text-slate-400 bg-slate-900/40 border border-slate-800 rounded-lg px-4 py-2.5">
                  <span className="text-slate-500 font-bold">vs Peers: </span>
                  {String(brief.peer_comparison)}
                </div>
              )}

              {/* Risks */}
              {(brief.key_risks as string[] ?? []).length > 0 && (
                <div className="bg-red-950/10 border border-red-900/20 rounded-xl p-3">
                  <div className="text-[10px] font-bold text-red-400 uppercase tracking-wide mb-2">⚠️ Key Risks</div>
                  {(brief.key_risks as string[]).map((r, i) => (
                    <div key={i} className="text-xs text-slate-400 flex items-start gap-1.5 mt-1">
                      <span className="text-red-600 flex-shrink-0">{i+1}.</span>
                      <span>{r}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}


          {/* CONCALLS */}
          {section === 'concalls' && (
            <div className="space-y-3">
              {!!brief?.sentiment_trend && (
                <div className="bg-slate-900/60 border border-slate-800 rounded-xl px-4 py-2.5 text-xs text-slate-300">
                  📊 <strong className="text-slate-100">Sentiment Trend: </strong>{String(brief.sentiment_trend)}
                </div>
              )}
              {((displayResult as Record<string,unknown>).concall_timeline as Record<string,unknown>[] ?? []).map((c, i) => {
                const score = Number(c.sentiment_score ?? 5)
                const barColor = score >= 7 ? '#22c55e' : score >= 5 ? '#f59e0b' : '#ef4444'
                return (
                  <div key={i} className="bg-slate-900/70 border border-slate-800 rounded-xl px-4 py-3">
                    <div className="flex items-center gap-3 mb-2">
                      <span className="text-xs text-slate-400 font-mono w-24">{String(c.date ?? '')}</span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700/50 text-slate-400">{String(c.filing_type ?? '')}</span>
                      <div className="flex items-center gap-2 ml-auto">
                        <div className="w-20 h-2 bg-slate-700 rounded-full overflow-hidden">
                          <div className="h-full rounded-full" style={{ width: `${score*10}%`, background: barColor }} />
                        </div>
                        <span className="text-sm font-black" style={{ color: barColor }}>{score}<span className="text-[9px] text-slate-500">/10</span></span>
                      </div>
                      <span className="text-[10px]">
                        {Number(c.positive_signals ?? 0) > 0 && <span className="text-emerald-500">+{Number(c.positive_signals)}</span>}
                        {Number(c.negative_signals ?? 0) > 0 && <span className="text-red-400"> -{Number(c.negative_signals)}</span>}
                      </span>
                    </div>
                    {!!c.constraint_quote && <p className="text-[11px] text-red-300/80 italic border-l-2 border-red-800/50 pl-2 mb-1">⚠️ "{String(c.constraint_quote).slice(0,160)}"</p>}
                    {!!c.demand_quote    && <p className="text-[11px] text-blue-300/80 italic border-l-2 border-blue-800/50 pl-2">📈 "{String(c.demand_quote).slice(0,160)}"</p>}
                  </div>
                )
              })}
              {(brief?.concall_highlights as Record<string,unknown>[] ?? []).length > 0 && (
                <div>
                  <div className="text-xs font-bold text-slate-400 uppercase tracking-wide mb-2 mt-3">🤖 Claude Highlights</div>
                  {(brief?.concall_highlights as Record<string,unknown>[]).map((h, i) => {
                    const score = Number(h.sentiment_score ?? 5)
                    const label = String(h.sentiment_label ?? '')
                    const lc = label.includes('Bullish') ? 'text-emerald-400 bg-emerald-950/40' : label.includes('Cautious') || label.includes('Bearish') ? 'text-red-400 bg-red-950/40' : 'text-amber-400 bg-amber-950/30'
                    return (
                      <div key={i} className="bg-slate-900/60 border border-slate-800 rounded-xl p-3 mb-2">
                        <div className="flex items-center gap-2 mb-2">
                          <span className="text-xs text-slate-400 font-mono">{String(h.date ?? '')}</span>
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${lc}`}>{label}</span>
                          <span className="text-xs font-black ml-auto" style={{ color: score>=7?'#22c55e':score>=5?'#f59e0b':'#ef4444' }}>{score}/10</span>
                        </div>
                        {(h.key_highlights as string[] ?? []).map((hl, hi) => (
                          <div key={hi} className="text-[11px] text-slate-300 flex gap-1.5 mt-1"><span className="text-slate-600">•</span><span>{hl}</span></div>
                        ))}
                        {!!h.standout_quote && <p className="text-[10px] text-slate-400 italic border-l-2 border-slate-700 pl-2 mt-2">"{String(h.standout_quote).slice(0,200)}"</p>}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {/* POLICY & PLI */}
          {section === 'policy' && (
            <div className="space-y-3">
              {!!brief?.policy_support && (
                <div className="bg-indigo-950/30 border border-indigo-800/30 rounded-xl p-4 space-y-2">
                  <div className="text-xs font-bold text-indigo-400 uppercase tracking-wide mb-2">🏛️ Policy Analysis (Claude)</div>
                  {!!(brief.policy_support as Record<string,unknown>)?.has_pli_benefit && <div className="text-[10px] text-emerald-400 font-bold">✅ PLI Scheme Beneficiary</div>}
                  {((brief.policy_support as Record<string,unknown>)?.schemes as Record<string,unknown>[] ?? []).map((s, i) => (
                    <div key={i} className="bg-emerald-950/20 border border-emerald-900/30 rounded-lg px-3 py-2">
                      <div className="text-xs font-semibold text-emerald-300">{String(s.name ?? '')}</div>
                      <div className="text-[10px] text-slate-300 mt-0.5">{String(s.benefit ?? '')}</div>
                      <div className="text-[10px] text-amber-400">{String(s.incentive ?? '')}</div>
                    </div>
                  ))}
                  {((brief.policy_support as Record<string,unknown>)?.budget_tailwinds as string[] ?? []).map((t, i) => (
                    <div key={i} className="text-[11px] text-slate-300 flex gap-1.5"><span className="text-emerald-600">•</span>{t}</div>
                  ))}
                  {!!(brief.policy_support as Record<string,unknown>)?.policy_risk && (
                    <div className="text-[10px] text-red-400 border-l-2 border-red-800 pl-2">⚠️ {String((brief.policy_support as Record<string,unknown>).policy_risk)}</div>
                  )}
                </div>
              )}
              {((displayResult as Record<string,unknown>).pli_matches as Record<string,unknown>[] ?? []).map((m, i) => (
                <div key={i} className="bg-slate-900/60 border border-slate-700 rounded-xl p-3">
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="text-xs font-semibold text-slate-100">{String(m.scheme ?? '')}</span>
                    {!!m.budget_crore && <span className="text-[10px] font-bold text-emerald-400">₹{Number(m.budget_crore).toLocaleString('en-IN')} Cr</span>}
                  </div>
                  <div className="text-[10px] text-amber-400 mb-1">{String(m.incentive ?? '')}</div>
                  <div className="text-[10px] text-slate-500 italic">{String(m.match_reason ?? '')}</div>
                  {!!m.layman_impact && <p className="text-[11px] text-slate-300 mt-1.5 leading-relaxed">{String(m.layman_impact)}</p>}
                </div>
              ))}
              {((displayResult as Record<string,unknown>).budget_support as string[] ?? []).length > 0 && (
                <div className="bg-amber-950/20 border border-amber-900/30 rounded-xl p-3">
                  <div className="text-[10px] font-bold text-amber-400 uppercase tracking-wide mb-2">📋 Budget Support</div>
                  {((displayResult as Record<string,unknown>).budget_support as string[]).map((b, i) => (
                    <div key={i} className="text-[11px] text-slate-300 flex gap-1.5 mt-1"><span className="text-amber-600">•</span>{b}</div>
                  ))}
                </div>
              )}
              {(displayResult as Record<string,unknown>).country !== 'IN' && (
                <p className="text-xs text-slate-500 italic text-center py-4">PLI & Policy analysis is specific to India stocks 🇮🇳</p>
              )}
            </div>
          )}

          {/* FILINGS */}
          {section === 'filings' && (
            <div className="space-y-1.5">
              {((displayResult as Record<string,unknown>).recent_filings as Record<string,unknown>[] ?? []).length === 0
                ? <EmptyState>No filings found in database for this period.</EmptyState>
                : ((displayResult as Record<string,unknown>).recent_filings as Record<string,unknown>[]).map((f, i) => (
                  <div key={i} className="flex items-center gap-3 bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-2">
                    <span className="text-[10px] text-slate-500 w-20 flex-shrink-0">{String(f.date ?? '')}</span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold flex-shrink-0 ${
                      String(f.type ?? '').includes('concall') || String(f.type ?? '').includes('earnings')
                        ? 'bg-indigo-900/40 text-indigo-300'
                        : 'bg-slate-700/40 text-slate-400'
                    }`}>{String(f.type ?? '').slice(0,20)}</span>
                    <span className="text-xs text-slate-200 truncate">{String(f.title ?? '')}</span>
                  </div>
                ))
              }
            </div>
          )}

          {/* PEERS */}
          {section === 'peers' && (
            <div className="space-y-1.5">
              {((displayResult as Record<string,unknown>).peers as Record<string,unknown>[] ?? []).length === 0
                ? <EmptyState>No industry peers found.</EmptyState>
                : ((displayResult as Record<string,unknown>).peers as Record<string,unknown>[]).map((p, i) => (
                  <div key={i} className="flex items-center gap-3 bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-2">
                    <span className="font-mono font-bold text-indigo-300 text-xs w-16 flex-shrink-0">{String(p.ticker ?? '—')}</span>
                    <span className="text-xs text-slate-200 flex-1 truncate">{String(p.company_name ?? '')}</span>
                    <span className="text-[10px] text-slate-500 capitalize">{String(p.company_role ?? '').replace(/_/g,' ')}</span>
                    <span className="text-[10px] text-slate-600 truncate max-w-48">{String(p.theme_name ?? '')}</span>
                    <span className="text-[10px] text-amber-400">{Number(p.relevance_score ?? 0).toFixed(0)}</span>
                  </div>
                ))
              }
            </div>
          )}
        </>
      )}

      {/* Empty state */}
      {!selected && !loading && (
        <div className="border border-dashed border-slate-700 rounded-xl p-8 text-center space-y-2">
          <div className="text-3xl">🔍</div>
          <p className="text-sm text-slate-400 font-medium">Search a company above to begin</p>
          <p className="text-xs text-slate-600 max-w-md mx-auto leading-relaxed">
            Type a company name or ticker → select from suggestions →
            Claude fetches 3 years of concalls, extracts supply/demand signals,
            and produces an investment-grade brief with recommendation.
            {' '}Results are cached so re-opens are instant.
          </p>
        </div>
      )}

      {selected && !displayResult && !loading && !error && (
        <div className="border border-dashed border-slate-700 rounded-xl p-6 text-center">
          <p className="text-sm text-slate-400">
            Click <strong className="text-slate-200">✨ Analyse</strong> to fetch concall data and run Claude analysis for <strong className="text-indigo-300">{selected.name}</strong>
          </p>
        </div>
      )}
      {/* ── Previously Analysed Companies ───────────────────────────────── */}
      {(history as Record<string,unknown>[]).length > 0 && (
        <div className="border-t border-slate-800 pt-4 mt-2">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-sm font-semibold text-slate-200">📋 Previously Analysed</span>
            <span className="text-xs text-slate-500">{(history as unknown[]).length} companies · {localCountry === 'IN' ? '🇮🇳 India' : '🇺🇸 US'}</span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2">
            {(history as Record<string,unknown>[]).map((h, i) => {
              const action     = String(h.action ?? 'hold')
              const conviction = String(h.conviction ?? '')
              const sentiment  = h.avg_sentiment != null ? Number(h.avg_sentiment) : null
              const sentColor  = sentiment != null
                ? sentiment >= 7 ? '#22c55e' : sentiment >= 5 ? '#f59e0b' : '#ef4444'
                : '#475569'
              const actionStyle: Record<string,string> = {
                strong_buy: 'text-emerald-300 bg-emerald-950/50 border-emerald-700/50',
                buy:        'text-blue-300 bg-blue-950/40 border-blue-700/40',
                hold:       'text-amber-300 bg-amber-950/30 border-amber-700/40',
                sell:       'text-red-300 bg-red-950/40 border-red-700/40',
              }
              const aStyle = actionStyle[action] ?? actionStyle.hold
              const actionLabel: Record<string,string> = {
                strong_buy: '🟢 Strong Buy', buy: '🔵 Buy', hold: '🟡 Hold', sell: '🔴 Sell'
              }
              return (
                <button
                  key={i}
                  onClick={() => {
                    const tk = String(h.ticker ?? '')
                    const nm = String(h.company ?? h.ticker ?? '')
                    setSelected({ name: tk || nm, ticker: tk })  // use ticker as name for cache key matching
                    setSelectedYear(Number(h.year ?? selectedYear))
                    setQuery('')
                    setResult(null)
                    setSection('analysis')
                  }}
                  className={`w-full text-left rounded-xl border px-3 py-2.5 transition-all hover:brightness-110 ${aStyle}`}
                >
                  <div className="flex items-start justify-between gap-2 mb-1.5">
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5 mb-0.5">
                        <span className="text-xs font-black text-slate-100">{String(h.ticker ?? '')}</span>
                        <span className="text-[10px] text-slate-400">{String(h.year ?? '')}</span>
                        {Number(h.pli_matches ?? 0) > 0 && (
                          <span className="text-[9px] text-emerald-500 font-bold">🏛️PLI</span>
                        )}
                      </div>
                      <p className="text-[10px] text-slate-400 truncate">{String(h.company ?? '').slice(0, 30)}</p>
                    </div>
                    <div className="text-right flex-shrink-0">
                      <div className={`text-[10px] font-bold px-1.5 py-0.5 rounded border ${aStyle}`}>
                        {actionLabel[action] ?? action}
                      </div>
                      {sentiment != null && (
                        <div className="text-xs font-black mt-1" style={{ color: sentColor }}>
                          {sentiment.toFixed(1)}<span className="text-[9px] text-slate-600">/10</span>
                        </div>
                      )}
                    </div>
                  </div>
                  {!!h.investment_thesis && (
                    <p className="text-[10px] text-slate-400 leading-snug line-clamp-2">{String(h.investment_thesis).slice(0,120)}</p>
                  )}
                  <div className="text-[9px] text-slate-600 mt-1.5 flex items-center gap-2">
                    <span className="capitalize">{conviction} conviction</span>
                    <span>·</span>
                    <span>{new Date(String(h.generated_at)).toLocaleDateString()}</span>
                  </div>
                </button>
              )
            })}
          </div>
        </div>
      )}


    </div>
  )
}
