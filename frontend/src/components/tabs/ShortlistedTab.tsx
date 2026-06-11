import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchShortlisted, runAIAnalysis, fetchIndiaChainBeneficiaries } from '../../api'
import { CountryBanner, ConvictionBadge, EmptyState, Spinner } from '../ui'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'

interface Props { country: string; countryFlag: string; countryLabel: string }

export default function ShortlistedTab({ country, countryFlag, countryLabel }: Props) {
  const [minQ, setMinQ] = useState(3)
  const [trendFilter, setTrendFilter] = useState<'all' | 'growing' | 'declining'>('all')
  const [geminiLoading, setGeminiLoading] = useState(false)
  const [geminiResult, setGeminiResult] = useState<string | null>(null)
  const qc = useQueryClient()

  const { data: themes = [], isLoading } = useQuery({
    queryKey: ['shortlisted', country, minQ],
    queryFn: () => fetchShortlisted(country, minQ),
  })

  const filtered = (themes as Record<string, unknown>[]).filter(t => {
    const trend = Number(t.strength_trend ?? 0)
    if (trendFilter === 'growing') return trend >= 0
    if (trendFilter === 'declining') return trend < 0
    return true
  })

  const refresh = () => qc.invalidateQueries({ queryKey: ['shortlisted', country, minQ] })

  const runGemini = async () => {
    setGeminiLoading(true)
    try {
      const market = country === 'IN' ? 'India (NSE/BSE)' : 'USA (NYSE/NASDAQ)'
      const themeLines = filtered.slice(0, 15).map((t, i) =>
        `${i + 1}. ${t.theme_name} [${String(t.conviction ?? '').toUpperCase()}] | Score: ${Number(t.strength_score ?? 0).toFixed(0)} | ${String(t.confirmed_quarters ?? 0)} quarters | ${String(t.company_count ?? 0)} companies`
      ).join('\n')
      const prompt = `You are an expert macro investment analyst. The following investment themes were auto-detected from ${market} market company filings and earnings data.\n\nSHORTLISTED THEMES (${filtered.length} themes):\n${themeLines}\n\nProvide a concise investment analysis covering:\n1. Top 3 themes with the strongest multi-year investment case (2-3 sentences each)\n2. Cross-theme connections and amplifying factors\n3. Key macro risks to monitor\n4. Sector rotation implications\n\nBe specific, data-driven, and actionable. Write for a professional equity investor.`
      const res = await runAIAnalysis({ prompt, mode: 'theme', market, themes_count: filtered.length, sl_count: filtered.length })
      setGeminiResult(res.result)
    } catch (e) {
      setGeminiResult(`Error: ${e}`)
    } finally {
      setGeminiLoading(false)
    }
  }

  return (
    <div className="space-y-4">
      <CountryBanner flag={countryFlag} label={countryLabel}>
        <strong>{countryLabel}</strong> shortlisted themes
      </CountryBanner>

      <div className="bg-indigo-950/30 border border-indigo-800/30 rounded-xl px-4 py-2 text-sm text-indigo-200">
        ⭐ <strong>Shortlisted Themes</strong> — auto-discovered themes that have persisted across multiple
        quarters with sustained or growing strength. 100% signal-driven.
      </div>

      {/* Controls */}
      <div className="flex flex-wrap gap-4 items-end">
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Min quarters present</label>
          <select value={minQ} onChange={e => setMinQ(+e.target.value)} className="select">
            {[2, 3, 4].map(n => <option key={n}>{n}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Trend filter</label>
          <select value={trendFilter} onChange={e => setTrendFilter(e.target.value as typeof trendFilter)} className="select">
            <option value="all">All (any direction)</option>
            <option value="growing">Growing only (↑)</option>
            <option value="declining">Declining only (↓)</option>
          </select>
        </div>
        <button onClick={refresh} className="btn-secondary">🔄 Refresh</button>
      </div>

      {isLoading && (
        <div className="flex justify-center py-12">
          <div className="w-6 h-6 border-2 border-slate-700 border-t-indigo-500 rounded-full animate-spin" />
        </div>
      )}

      {!isLoading && filtered.length === 0 && (
        <EmptyState>
          No themes found with {minQ}+ confirmed quarters yet.<br />
          Run the pipeline across multiple months/quarters to build up snapshots.
        </EmptyState>
      )}

      {!isLoading && filtered.length > 0 && (
        <>
          <p className="text-xs text-slate-500">
            <strong className="text-slate-300">{filtered.length}</strong> themes shortlisted (≥{minQ} quarters)
          </p>

          <div className="space-y-4">
            {filtered.map((theme: Record<string, unknown>, rank) => (
              <ThemeCard key={rank} theme={theme} rank={rank + 1} />
            ))}
          </div>

          {/* Gemini AI Analysis */}
          <div className="border-t border-slate-800 pt-4">
            <div className="bg-purple-950/30 border-l-4 border-purple-600 rounded-r-lg px-4 py-2 text-sm text-purple-200 mb-3">
              🤖 <strong>Gemini AI Analysis</strong> · Google Gemini Flash · {countryLabel} · {filtered.length} shortlisted themes
            </div>
            <div className="flex gap-3">
              <button onClick={runGemini} disabled={geminiLoading} className="btn-primary flex items-center gap-2">
                {geminiLoading ? <Spinner size="sm" /> : '✨'} Run Gemini Analysis
              </button>
              {geminiResult && <button onClick={() => setGeminiResult(null)} className="btn-secondary">🗑 Clear</button>}
            </div>
            {geminiResult && (
              <div className="mt-4 bg-purple-950/20 border border-purple-900/40 rounded-xl p-5">
                <div className="text-xs text-purple-400 font-bold uppercase tracking-wider mb-3">🤖 Gemini Flash — Investment Analysis</div>
                <div className="text-sm text-slate-200 leading-relaxed whitespace-pre-wrap">{geminiResult}</div>
              </div>
            )}
          </div>

          {/* India Chain Beneficiaries — only shown for India market */}
          {country === 'IN' && <IndiaChainPlays />}

          {/* Summary table */}
          <details className="group">
            <summary className="text-sm text-slate-400 cursor-pointer hover:text-slate-200 transition-colors py-2 border-t border-slate-800">
              📋 Shortlist summary table
            </summary>
            <div className="mt-2 overflow-x-auto rounded-lg border border-slate-700">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-slate-800 border-b border-slate-700">
                    {['Theme', 'Quarters', 'Avg', 'Current', 'Peak', 'Trend', 'Momentum', 'Companies', 'Conviction'].map(h => (
                      <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide whitespace-nowrap">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((t: Record<string, unknown>, i) => (
                    <tr key={i} className="border-b border-slate-800 hover:bg-slate-800/50">
                      <td className="px-3 py-2 text-slate-200 font-medium max-w-[200px] truncate">{String(t.theme_name ?? '')}</td>
                      <td className="px-3 py-2 text-indigo-400 font-bold text-center">{String(t.confirmed_quarters ?? 0)}Q</td>
                      <td className="px-3 py-2 text-slate-300 text-right">{Number(t.avg_strength ?? 0).toFixed(1)}</td>
                      <td className="px-3 py-2 text-indigo-400 font-bold text-right">{Number(t.strength_score ?? 0).toFixed(1)}</td>
                      <td className="px-3 py-2 text-amber-400 font-bold text-right">{Number(t.peak_strength ?? 0).toFixed(1)}</td>
                      <td className="px-3 py-2 text-right">
                        <TrendPill value={Number(t.strength_trend ?? 0)} />
                      </td>
                      <td className="px-3 py-2 text-right">
                        <span className={Number(t.momentum_score ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                          {Number(t.momentum_score ?? 0) > 0 ? '+' : ''}{Number(t.momentum_score ?? 0).toFixed(1)}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-slate-300 text-center">{String(t.company_count ?? 0)}</td>
                      <td className="px-3 py-2"><ConvictionBadge conviction={String(t.conviction ?? 'emerging')} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </div>
  )
}

// ─── India Chain Plays ────────────────────────────────────────────────────────
function IndiaChainPlays() {
  const [groupBy, setGroupBy] = useState<'theme' | 'type'>('theme')
  const [minConv, setMinConv] = useState(0.75)

  const { data: bens = [], isLoading } = useQuery({
    queryKey: ['india-chain-bens-sl', minConv],
    queryFn: () => fetchIndiaChainBeneficiaries(undefined, minConv),
  })

  const rows = bens as Record<string, unknown>[]
  if (isLoading) return <div className="py-4 text-center text-slate-500 text-sm">Loading India chain plays…</div>
  if (rows.length === 0) return null

  // Group by theme or by beneficiary type
  const groups: Record<string, Record<string, unknown>[]> = {}
  for (const b of rows) {
    const key = groupBy === 'theme'
      ? String(b.theme_name ?? 'Unknown')
      : String(b.beneficiary_type ?? 'other')
    if (!groups[key]) groups[key] = []
    groups[key].push(b)
  }

  const typeLabel: Record<string, string> = {
    direct: '🔧 Direct Suppliers',
    indirect: '🔗 Indirect / Upstream',
    localization_play: '🏭 Localisation Plays',
  }

  return (
    <div className="border-t border-slate-800 pt-5 mt-2">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-3">
        <div className="flex items-center gap-2">
          <span className="text-base">🇮🇳</span>
          <span className="text-sm font-semibold text-indigo-300">India Supply Chain Plays</span>
          <span className="text-xs text-slate-500">from capacity gaps + causal chains</span>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1 text-xs text-slate-400">
            Min conviction:
            <select value={minConv} onChange={e => setMinConv(+e.target.value)} className="select text-xs ml-1">
              {[0.90, 0.85, 0.80, 0.75, 0.70].map(v => (
                <option key={v} value={v}>{(v * 100).toFixed(0)}%</option>
              ))}
            </select>
          </div>
          <div className="flex rounded-lg overflow-hidden border border-slate-700">
            {(['theme', 'type'] as const).map(g => (
              <button key={g} onClick={() => setGroupBy(g)}
                className={`px-3 py-1 text-xs transition-colors ${groupBy === g ? 'bg-indigo-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-slate-200'}`}>
                By {g}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="space-y-3">
        {Object.entries(groups).map(([groupKey, items]) => (
          <details key={groupKey} open={Object.keys(groups).length <= 4}>
            <summary className="cursor-pointer text-xs font-semibold text-slate-300 py-1 hover:text-white transition-colors flex items-center gap-2">
              <span>{groupBy === 'type' ? (typeLabel[groupKey] ?? groupKey) : groupKey}</span>
              <span className="text-slate-500 font-normal">({items.length} companies)</span>
            </summary>
            <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 pl-2">
              {items.map((b, bi) => {
                const conv = Number(b.conviction_score ?? 0)
                const btype = String(b.beneficiary_type ?? '')
                const borderColor = btype === 'direct' ? 'border-emerald-800/40'
                  : btype === 'localization_play' ? 'border-amber-800/40' : 'border-sky-800/40'
                const tagColor = btype === 'direct' ? 'text-emerald-400'
                  : btype === 'localization_play' ? 'text-amber-400' : 'text-sky-400'
                return (
                  <div key={bi} className={`bg-slate-900 rounded-lg border ${borderColor} px-3 py-2`}>
                    <div className="flex items-start justify-between gap-1">
                      <div>
                        <div className="text-sm font-semibold text-white leading-tight">{String(b.company ?? '')}</div>
                        {!!b.ticker && <div className="text-xs text-slate-400 font-mono">{String(b.ticker)}</div>}
                      </div>
                      <div className="text-right shrink-0">
                        <div className="text-sm font-black text-emerald-400">{(conv * 100).toFixed(0)}%</div>
                        <div className="text-[10px] text-slate-600">conv</div>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      <span className={`text-[10px] font-medium ${tagColor}`}>
                        {btype === 'direct' ? '🔧' : btype === 'localization_play' ? '🏭' : '🔗'}{' '}
                        {btype.replace('_', ' ')}
                      </span>
                      {!!b.has_order_book_signals && (
                        <span className="text-[10px] text-amber-400 bg-amber-950/30 rounded px-1">📋 OB signals</span>
                      )}
                      {!!b.import_substitution_play && (
                        <span className="text-[10px] text-sky-400 bg-sky-950/30 rounded px-1">🔄 Import sub</span>
                      )}
                    </div>
                    <div className="text-[10px] text-slate-500 mt-1 truncate">{String(b.constrained_product ?? '')}</div>
                    {Number(b.signal_count ?? 0) > 0 && (
                      <div className="text-[10px] text-emerald-600 mt-0.5">
                        📶 {String(b.signal_count)} confirmatory signals
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </details>
        ))}
      </div>
    </div>
  )
}

// ─── Theme Card ───────────────────────────────────────────────────────────────
function ThemeCard({ theme, rank }: { theme: Record<string, unknown>; rank: number }) {
  let qSeries: Record<string, unknown>[] = []
  try {
    qSeries = typeof theme.quarter_series === 'string'
      ? JSON.parse(theme.quarter_series)
      : (theme.quarter_series as Record<string, unknown>[]) ?? []
  } catch { qSeries = [] }

  const confirmedQ = Number(theme.confirmed_quarters ?? 0)
  const avgS = Number(theme.avg_strength ?? 0)
  const peak = Number(theme.peak_strength ?? 0)
  const trend = Number(theme.strength_trend ?? 0)
  const cur = Number(theme.strength_score ?? 0)
  const mom = Number(theme.momentum_score ?? 0)
  const conv = String(theme.conviction ?? 'emerging')

  const chartData = qSeries.map(q => ({
    label: `Q${q.quarter}-${q.year}`,
    strength: Number(q.strength ?? 0),
    momentum: Number(q.momentum ?? 0),
  }))

  return (
    <div className="bg-slate-800 border border-slate-700 rounded-xl p-4">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <span className="text-slate-500 text-xs font-bold mr-2">#{rank}</span>
          <span className="font-bold text-white text-sm">{String(theme.theme_name ?? '')}</span>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <ConvictionBadge conviction={conv} />
          <TrendPill value={trend} />
        </div>
      </div>

      {/* Quarter badges */}
      {qSeries.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-3">
          {qSeries.map((q, i) => (
            <span key={i} className={`text-xs px-2 py-0.5 rounded border font-bold ${
              i < confirmedQ - 1
                ? 'bg-emerald-950/40 border-emerald-600/40 text-emerald-300'
                : 'bg-blue-950/40 border-blue-700/40 text-blue-300'
            }`}>
              Q{String(q.quarter)}-{String(q.year)}{' '}
              <span className="opacity-60">{Number(q.strength ?? 0).toFixed(0)}</span>
            </span>
          ))}
        </div>
      )}

      {/* Metrics row */}
      <div className="grid grid-cols-3 md:grid-cols-6 gap-2 mb-3">
        {[
          ['CURRENT', cur.toFixed(0), '#818cf8'],
          ['AVG', avgS.toFixed(0), '#e2e8f0'],
          ['PEAK', peak.toFixed(0), '#f59e0b'],
          ['MOMENTUM', (mom >= 0 ? '+' : '') + mom.toFixed(1), mom >= 0 ? '#22c55e' : '#ef4444'],
          ['QUARTERS', `${confirmedQ}Q`, '#c4b5fd'],
          ['COMPANIES', String(theme.company_count ?? 0), '#e2e8f0'],
        ].map(([label, val, color]) => (
          <div key={String(label)} className="bg-slate-900 rounded-lg p-2 text-center">
            <div className="text-sm font-black leading-none" style={{ color: String(color) }}>{String(val)}</div>
            <div className="text-xs text-slate-600 mt-0.5">{String(label)}</div>
          </div>
        ))}
      </div>

      {/* Sparkline */}
      {chartData.length >= 2 && (
        <ResponsiveContainer width="100%" height={110}>
          <LineChart data={chartData} margin={{ top: 2, right: 4, left: -28, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="label" tick={{ fill: '#64748b', fontSize: 9 }} tickLine={false} />
            <YAxis tick={{ fill: '#64748b', fontSize: 9 }} />
            <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }} />
            <Line type="monotone" dataKey="strength" stroke="#818cf8" strokeWidth={2.5} dot={{ r: 3 }} name="Strength" fill="rgba(129,140,248,0.1)" />
            {chartData.some(d => d.momentum !== 0) && (
              <Line type="monotone" dataKey="momentum" stroke="#f59e0b" strokeWidth={1.5} dot={false} strokeDasharray="4 4" name="Momentum" />
            )}
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

// ─── Trend Pill ───────────────────────────────────────────────────────────────
function TrendPill({ value }: { value: number }) {
  let icon = '→', col = '#94a3b8'
  if (value > 5) { icon = '▲'; col = '#22c55e' }
  else if (value > 0) { icon = '↗'; col = '#86efac' }
  else if (value < 0) { icon = '▼'; col = '#ef4444' }
  return (
    <span className="text-xs px-2 py-0.5 rounded-full font-bold border"
      style={{ color: col, background: `${col}22`, borderColor: `${col}44` }}>
      {icon} {value > 0 ? '+' : ''}{value.toFixed(1)}
    </span>
  )
}
