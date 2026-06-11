import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchFilings, fetchDocSignals, fetchDocThemes } from '../../api'
import { CountryBanner, ConvictionBadge, DIR_COLOR, EmptyState, Spinner } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const today = new Date().toISOString().slice(0, 10)

export default function FilingsTab({ country, countryFlag, countryLabel }: Props) {
  const [fromDate, setFromDate] = useState('2022-01-01')
  const [toDate, setToDate] = useState(today)
  const [ticker, setTicker] = useState('')
  const [filingType, setFilingType] = useState('All')
  const [limit, setLimit] = useState(100)
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null)
  const [detailTab, setDetailTab] = useState<'signals' | 'themes'>('signals')
  const [refresh, setRefresh] = useState(0)

  const { data: docs = [], isLoading } = useQuery({
    queryKey: ['filings', country, fromDate, toDate, ticker, filingType, limit, refresh],
    queryFn: () => fetchFilings({ country, from_date: fromDate, to_date: toDate, ticker: ticker || undefined, filing_type: filingType, limit }),
    enabled: country === 'US',
  })

  const selectedDoc = selectedIdx !== null ? (docs as Record<string, unknown>[])[selectedIdx] : null
  const docId = selectedDoc ? Number(selectedDoc.id) : null

  const { data: signals = [], isLoading: sigsLoading } = useQuery({
    queryKey: ['doc-signals', docId],
    queryFn: () => fetchDocSignals(docId!),
    enabled: docId !== null && detailTab === 'signals',
  })

  const { data: docThemes = [], isLoading: themesLoading } = useQuery({
    queryKey: ['doc-themes', docId],
    queryFn: () => fetchDocThemes(docId!),
    enabled: docId !== null && detailTab === 'themes',
  })

  const totalSigs = (docs as Record<string,unknown>[]).reduce((s, d) => s + Number(d.signal_count ?? 0), 0)
  const totalWords = (docs as Record<string,unknown>[]).reduce((s, d) => s + Number(d.word_count ?? 0), 0)
  const companies = new Set((docs as Record<string,unknown>[]).map(d => d.ticker).filter(Boolean)).size

  if (country === 'IN') {
    return (
      <div className="space-y-4">
        <CountryBanner flag={countryFlag} label={countryLabel} />
        <div className="bg-blue-950/40 border border-blue-800/40 rounded-xl p-6 text-blue-300 text-sm">
          🇮🇳 <strong>India pipeline coming soon.</strong><br />
          NSE/BSE concall transcripts + quarterly results ingestion will be added in the next phase.
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <CountryBanner flag={countryFlag} label={countryLabel}>
        <strong>{countryLabel}</strong> filings — change market in the sidebar ←
      </CountryBanner>

      {/* Filters */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div>
          <label className="text-xs text-slate-400 mb-1 block">From date</label>
          <input type="date" value={fromDate} max={today} onChange={e => setFromDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">To date</label>
          <input type="date" value={toDate} max={today} onChange={e => setToDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Ticker / Company</label>
          <input value={ticker} onChange={e => setTicker(e.target.value)}
            placeholder="e.g. NVDA" className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Filing type</label>
          <select value={filingType} onChange={e => setFilingType(e.target.value)} className="select w-full">
            {['All', '10-K', '10-Q', '8-K', 'DEF 14A', 'S-1'].map(t => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Rows</label>
          <select value={limit} onChange={e => setLimit(+e.target.value)} className="select w-full">
            {[50, 100, 200, 500].map(n => <option key={n}>{n}</option>)}
          </select>
        </div>
      </div>

      <button onClick={() => setRefresh(r => r + 1)} className="btn-secondary">
        🔄 Refresh Filings
      </button>

      {isLoading && <div className="flex justify-center py-8"><Spinner /></div>}

      {!isLoading && (docs as unknown[]).length > 0 && (
        <>
          <p className="text-xs text-slate-500">
            <strong className="text-slate-300">{(docs as unknown[]).length}</strong> filings · {fromDate} → {toDate}
          </p>

          <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
            {/* Filing table */}
            <div className="lg:col-span-3 overflow-x-auto rounded-lg border border-slate-700">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-slate-800 border-b border-slate-700">
                    {['Date', 'Ticker', 'Company', 'Type', 'Period', 'Words', 'Signals', 'Status'].map(h => (
                      <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide whitespace-nowrap">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(docs as Record<string, unknown>[]).map((d, i) => (
                    <tr
                      key={i}
                      onClick={() => { setSelectedIdx(i); setDetailTab('signals') }}
                      className={`border-b border-slate-800 cursor-pointer transition-colors ${
                        selectedIdx === i ? 'bg-indigo-950 border-l-2 border-l-indigo-500' : 'hover:bg-slate-800/60'
                      }`}
                    >
                      <td className="px-3 py-2 text-slate-400 whitespace-nowrap">{String(d.filed_at ?? '').slice(0, 10)}</td>
                      <td className="px-3 py-2 text-indigo-300 font-bold whitespace-nowrap">{String(d.ticker ?? '—')}</td>
                      <td className="px-3 py-2 text-slate-300 max-w-[160px] truncate">{String(d.company ?? '')}</td>
                      <td className="px-3 py-2 text-slate-400 whitespace-nowrap">{String(d.filing_type ?? '—')}</td>
                      <td className="px-3 py-2 text-slate-500 whitespace-nowrap">{String(d.fiscal_period ?? '—')}</td>
                      <td className="px-3 py-2 text-slate-400 text-right">{Number(d.word_count ?? 0).toLocaleString()}</td>
                      <td className="px-3 py-2 text-right">
                        <span className="text-indigo-400 font-bold">{String(d.signal_count ?? 0)}</span>
                      </td>
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

            {/* Detail panel */}
            <div className="lg:col-span-2">
              {selectedDoc ? (
                <div className="bg-slate-800 border border-slate-700 rounded-xl overflow-hidden">
                  <div className="p-4 border-b border-slate-700">
                    <div className="font-bold text-white text-sm">
                      {String(selectedDoc.ticker ?? '?')} — {String(selectedDoc.company ?? '').slice(0, 50)}
                    </div>
                    <div className="flex flex-wrap gap-2 mt-2 text-xs text-slate-400">
                      <span>📅 {String(selectedDoc.filed_at ?? '').slice(0, 10)}</span>
                      <span>📋 {String(selectedDoc.filing_type ?? '?')}</span>
                      <span>📝 {Number(selectedDoc.word_count ?? 0).toLocaleString()} words</span>
                    </div>
                  </div>

                  <div className="flex border-b border-slate-700">
                    {(['signals', 'themes'] as const).map(t => (
                      <button key={t} onClick={() => setDetailTab(t)}
                        className={`flex-1 py-2 text-xs font-medium transition-colors ${
                          detailTab === t ? 'text-indigo-400 border-b-2 border-indigo-500' : 'text-slate-500 hover:text-slate-300'
                        }`}>
                        {t === 'signals' ? '⚡ Signals' : '🗺️ Theme Links'}
                      </button>
                    ))}
                  </div>

                  <div className="p-3 max-h-96 overflow-y-auto space-y-2">
                    {detailTab === 'signals' && (
                      sigsLoading ? <div className="flex justify-center py-4"><Spinner size="sm" /></div> :
                      (signals as Record<string, unknown>[]).length === 0
                        ? <p className="text-xs text-slate-600">No signals extracted yet — run NLP stage.</p>
                        : (signals as Record<string, unknown>[]).slice(0, 20).map((sg, i) => {
                          const dc = DIR_COLOR[String(sg.direction ?? 'neutral')] ?? '#94a3b8'
                          return (
                            <div key={i} className="rounded-lg p-2 bg-slate-900 text-xs"
                              style={{ borderLeft: `3px solid ${dc}` }}>
                              <div className="flex justify-between">
                                <span className="text-slate-300 font-semibold">
                                  {String(sg.signal_type ?? '?')}
                                  {sg.entity_name ? `  ·  ${sg.entity_name}` : ''}
                                </span>
                                <span style={{ color: dc }}>
                                  {String(sg.direction ?? '?')} · conf: {Number(sg.confidence ?? 0).toFixed(2)}
                                </span>
                              </div>
                              <div className="text-slate-500 italic mt-1 line-clamp-2">
                                "{String(sg.context_text ?? '').slice(0, 220)}"
                              </div>
                            </div>
                          )
                        })
                    )}
                    {detailTab === 'themes' && (
                      themesLoading ? <div className="flex justify-center py-4"><Spinner size="sm" /></div> :
                      (docThemes as Record<string, unknown>[]).length === 0
                        ? <p className="text-xs text-slate-600">No linked themes yet — run NLP + Themes stages.</p>
                        : (docThemes as Record<string, unknown>[]).map((dt, i) => (
                          <div key={i} className="bg-slate-900 rounded-lg p-2 flex justify-between items-center">
                            <div>
                              <div className="font-bold text-white text-xs">{String(dt.theme_name ?? '')}</div>
                              <div className="text-slate-600 text-xs">{String(dt.theme_slug ?? '')}</div>
                            </div>
                            <div className="text-right">
                              <div className="text-indigo-400 font-bold text-xs">{String(dt.signal_count ?? 0)} signals</div>
                              <ConvictionBadge conviction={String(dt.conviction ?? 'emerging')} />
                            </div>
                          </div>
                        ))
                    )}
                  </div>
                </div>
              ) : (
                <EmptyState>← Click a row to see signals & theme links</EmptyState>
              )}
            </div>
          </div>

          {/* Summary strip */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3 pt-2 border-t border-slate-800">
            {[
              ['Filings', (docs as unknown[]).length.toLocaleString()],
              ['Companies', companies.toLocaleString()],
              ['Total Signals', totalSigs.toLocaleString()],
              ['Total Words', totalWords.toLocaleString()],
              ['Filing Types', [...new Set((docs as Record<string,unknown>[]).map(d => d.filing_type).filter(Boolean))].slice(0, 4).join(', ')],
            ].map(([label, val]) => (
              <div key={String(label)} className="kpi-card">
                <div className="text-lg font-black text-indigo-400 leading-none">{String(val)}</div>
                <div className="text-xs text-slate-500 mt-1">{String(label)}</div>
              </div>
            ))}
          </div>
        </>
      )}

      {!isLoading && (docs as unknown[]).length === 0 && (
        <EmptyState>
          No documents found. Run the Pipeline to ingest SEC filings first.
        </EmptyState>
      )}
    </div>
  )
}
