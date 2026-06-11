import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  searchCompanies, fetchCompanyProfile, fetchCompanyTimeline,
  fetchCompanyThemes, fetchFilings,
} from '../../api'
import { CountryBanner, ConvictionBadge, EmptyState, Spinner } from '../ui'
import {
  ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Legend,
} from 'recharts'

interface Props { country: string; countryFlag: string; countryLabel: string }

const today = new Date().toISOString().slice(0, 10)

export default function CompanyTab({ country, countryFlag, countryLabel }: Props) {
  const [search, setSearch] = useState('')
  const [fromDate, setFromDate] = useState('2020-01-01')
  const [toDate, setToDate] = useState(today)
  const [activeTicker, setActiveTicker] = useState<string | null>(null)
  const [coTab, setCoTab] = useState<'timeline' | 'themes' | 'filings'>('timeline')

  const { data: results = [] } = useQuery({
    queryKey: ['co-search', search, country],
    queryFn: () => searchCompanies(search, country),
    enabled: search.trim().length >= 2,
  })

  const { data: profile = {} } = useQuery({
    queryKey: ['co-profile', activeTicker, country, toDate],
    queryFn: () => fetchCompanyProfile(activeTicker!, country, toDate),
    enabled: activeTicker !== null,
  })

  const { data: timeline = [] } = useQuery({
    queryKey: ['co-timeline', activeTicker, country, fromDate, toDate],
    queryFn: () => fetchCompanyTimeline(activeTicker!, country, fromDate, toDate),
    enabled: activeTicker !== null && coTab === 'timeline',
  })

  const { data: coThemes = [] } = useQuery({
    queryKey: ['co-themes', activeTicker, country, toDate],
    queryFn: () => fetchCompanyThemes(activeTicker!, country, toDate),
    enabled: activeTicker !== null && coTab === 'themes',
  })

  const { data: coFilings = [] } = useQuery({
    queryKey: ['co-filings', activeTicker, country, fromDate, toDate],
    queryFn: () => fetchFilings({ country, from_date: fromDate, to_date: toDate, ticker: activeTicker!, limit: 50 }),
    enabled: activeTicker !== null && coTab === 'filings',
  })

  const p = profile as Record<string, unknown>
  const tlData = (timeline as Record<string, unknown>[]).map(r => ({
    month: String(r.month ?? '').slice(0, 7),
    signals: Number(r.signals ?? 0),
    filings: Number(r.filings ?? 0),
    avgConf: Number(r.avg_confidence ?? 0),
  })).sort((a, b) => a.month.localeCompare(b.month))

  if (country === 'IN') {
    return (
      <div className="space-y-4">
        <CountryBanner flag={countryFlag} label={countryLabel} />
        <div className="bg-blue-950/40 border border-blue-800/40 rounded-xl p-6 text-blue-300 text-sm">
          🇮🇳 India company data will be available once the NSE/BSE pipeline is added.
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <CountryBanner flag={countryFlag} label={countryLabel}>
        Exploring <strong>{countryLabel}</strong> companies
      </CountryBanner>

      {/* Search controls */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
        <div className="md:col-span-2">
          <label className="text-xs text-slate-400 mb-1 block">Search ticker or company name</label>
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="e.g. NVDA, Microsoft, AMD"
            className="input"
          />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">From date</label>
          <input type="date" value={fromDate} max={today} onChange={e => setFromDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">To / As-of date</label>
          <input type="date" value={toDate} max={today} onChange={e => setToDate(e.target.value)} className="input" />
        </div>
      </div>

      {/* Search results */}
      {search.trim().length >= 2 && (results as unknown[]).length > 0 && (
        <div>
          <p className="text-xs text-slate-500 mb-2">
            {(results as unknown[]).length} match{(results as unknown[]).length !== 1 ? 'es' : ''} — select one:
          </p>
          <select
            className="select w-full"
            onChange={e => setActiveTicker(e.target.value)}
            value={activeTicker ?? ''}
          >
            <option value="">Select company…</option>
            {(results as Record<string, unknown>[]).map((r, i) => (
              <option key={i} value={String(r.ticker ?? '')}>
                {String(r.ticker ?? '')} — {String(r.company ?? '').slice(0, 60)} ({String(r.filing_count ?? 0)} filings)
              </option>
            ))}
          </select>
        </div>
      )}

      {search.trim().length >= 2 && (results as unknown[]).length === 0 && (
        <div className="text-sm text-amber-400 bg-amber-950/30 border border-amber-700/30 rounded-xl p-3">
          No companies found matching "{search}". Try a shorter ticker or partial name.
        </div>
      )}

      {!search.trim() && !activeTicker && (
        <EmptyState>
          🔍 Type a ticker (NVDA) or company name above to explore a company.
        </EmptyState>
      )}

      {/* Company detail */}
      {activeTicker && (
        <div className="space-y-4">
          {/* Header card */}
          <div className="bg-slate-800 border border-slate-700 rounded-xl p-4">
            <div className="text-xl font-black text-white mb-3">
              {countryFlag} {activeTicker} — {String(p.company ?? activeTicker)}
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {[
                ['Country', String(p.country ?? 'US')],
                ['Filings', String(p.filing_count ?? 0)],
                ['Total Signals', String(p.total_signals ?? 0)],
                ['Avg Confidence', Number(p.avg_confidence ?? 0).toFixed(3)],
                ['First Filing', String(p.first_filing ?? '—').slice(0, 10)],
                ['Last Filing', String(p.last_filing ?? '—').slice(0, 10)],
                ['Filing Types', (p.filing_types as string[] ?? []).join(', ') || '—'],
              ].map(([label, val]) => (
                <div key={String(label)} className="bg-slate-900 rounded-lg p-2.5">
                  <div className="text-xs text-slate-500 mb-0.5">{String(label)}</div>
                  <div className="text-sm font-bold text-slate-200">{String(val)}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Sub-tabs */}
          <div className="flex gap-1 border-b border-slate-700">
            {(['timeline', 'themes', 'filings'] as const).map(tab => (
              <button key={tab} onClick={() => setCoTab(tab)}
                className={`px-4 py-2 text-xs font-medium transition-colors border-b-2 -mb-px ${
                  coTab === tab ? 'border-indigo-500 text-indigo-400' : 'border-transparent text-slate-500 hover:text-slate-300'
                }`}>
                {tab === 'timeline' ? '📈 Signal Timeline' : tab === 'themes' ? '🗺️ Theme Contributions' : '📄 Recent Filings'}
              </button>
            ))}
          </div>

          {/* Signal timeline */}
          {coTab === 'timeline' && (
            <div className="space-y-4">
              {tlData.length > 0 ? (
                <>
                  <div className="bg-slate-900 rounded-xl p-4 border border-slate-800">
                    <ResponsiveContainer width="100%" height={260}>
                      <ComposedChart data={tlData} margin={{ top: 4, right: 8, left: -10, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                        <XAxis dataKey="month" tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} />
                        <YAxis yAxisId="left" tick={{ fill: '#94a3b8', fontSize: 10 }} />
                        <YAxis yAxisId="right" orientation="right" tick={{ fill: '#f59e0b', fontSize: 10 }} />
                        <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }} />
                        <Legend wrapperStyle={{ fontSize: 11, color: '#94a3b8' }} />
                        <Bar yAxisId="left" dataKey="signals" fill="#818cf8" name="Signals" />
                        <Line yAxisId="right" type="monotone" dataKey="filings" stroke="#f59e0b" strokeWidth={2} dot={{ r: 3 }} name="Filings" />
                      </ComposedChart>
                    </ResponsiveContainer>
                  </div>

                  <div className="overflow-x-auto rounded-lg border border-slate-700">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="bg-slate-800 border-b border-slate-700">
                          {['Month', 'Filings', 'Signals', 'Avg Conf'].map(h => (
                            <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {[...tlData].reverse().map((r, i) => (
                          <tr key={i} className="border-b border-slate-800 hover:bg-slate-800/50">
                            <td className="px-3 py-2 text-slate-300">{r.month}</td>
                            <td className="px-3 py-2 text-slate-300 text-right">{r.filings}</td>
                            <td className="px-3 py-2 text-indigo-400 font-bold text-right">{r.signals}</td>
                            <td className="px-3 py-2 text-slate-400 text-right">{r.avgConf.toFixed(3)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : (
                <EmptyState>No timeline data. Ingest and run NLP on filings for this company.</EmptyState>
              )}
            </div>
          )}

          {/* Theme contributions */}
          {coTab === 'themes' && (
            <div className="space-y-2">
              {(coThemes as Record<string, unknown>[]).length === 0 ? (
                <EmptyState>No themes linked yet. Run NLP + Themes stages first.</EmptyState>
              ) : (
                <>
                  <p className="text-xs text-slate-500 mb-1">
                    Themes that <strong className="text-slate-300">{activeTicker}</strong>'s filings contributed signals to (as of {toDate}).
                  </p>
                  {(coThemes as Record<string, unknown>[]).map((ct, i) => (
                    <div key={i} className="bg-slate-800 border border-slate-700 rounded-xl px-4 py-3 flex justify-between items-center gap-4 flex-wrap">
                      <div>
                        <div className="font-bold text-white text-sm">{String(ct.theme_name ?? '')}</div>
                        <div className="text-xs text-slate-600 mt-0.5">
                          {String(ct.theme_slug ?? '')} · first: {String(ct.first_detected ?? '').slice(0, 10)}
                        </div>
                      </div>
                      <div className="text-right">
                        <div className="text-indigo-400 font-bold">{String(ct.company_signal_count ?? 0)} signals</div>
                        <ConvictionBadge conviction={String(ct.conviction ?? 'emerging')} />
                        <div className="text-xs text-slate-500 mt-0.5">strength: {Number(ct.strength_score ?? 0).toFixed(1)}</div>
                      </div>
                    </div>
                  ))}
                  <p className="text-xs text-slate-500">
                    {(coThemes as unknown[]).length} themes sourced from {activeTicker} filings as of {toDate}.
                  </p>
                </>
              )}
            </div>
          )}

          {/* Recent filings */}
          {coTab === 'filings' && (
            <div>
              {(coFilings as unknown[]).length === 0 ? (
                <EmptyState>No filings found for {activeTicker} in the selected date range.</EmptyState>
              ) : (
                <div className="overflow-x-auto rounded-lg border border-slate-700">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="bg-slate-800 border-b border-slate-700">
                        {['Date', 'Type', 'Period', 'Words', 'Signals', 'Status'].map(h => (
                          <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide whitespace-nowrap">{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {(coFilings as Record<string, unknown>[]).map((d, i) => (
                        <tr key={i} className="border-b border-slate-800 hover:bg-slate-800/50">
                          <td className="px-3 py-2 text-slate-400">{String(d.filed_at ?? '').slice(0, 10)}</td>
                          <td className="px-3 py-2 text-slate-300">{String(d.filing_type ?? '—')}</td>
                          <td className="px-3 py-2 text-slate-500">{String(d.fiscal_period ?? '—')}</td>
                          <td className="px-3 py-2 text-slate-400 text-right">{Number(d.word_count ?? 0).toLocaleString()}</td>
                          <td className="px-3 py-2 text-indigo-400 font-bold text-right">{String(d.signal_count ?? 0)}</td>
                          <td className="px-3 py-2">
                            <span className={`px-1.5 py-0.5 rounded text-xs ${
                              d.processing_status === 'processed' ? 'bg-emerald-900/40 text-emerald-400' : 'bg-slate-800 text-slate-500'
                            }`}>{String(d.processing_status ?? '—')}</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
