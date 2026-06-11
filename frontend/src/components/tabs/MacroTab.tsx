import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchMacroSeries, fetchCommodity, fetchMacroEvents, fetchPolicyEvents, runMacroFetch } from '../../api'
import { CountryBanner, Spinner } from '../ui'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine,
} from 'recharts'

interface Props { country: string; countryFlag: string; countryLabel: string }

const today = new Date().toISOString().slice(0, 10)

const SERIES_MENU: Record<string, string> = {
  GDP: 'US Real GDP',
  CPIAUCSL: 'CPI (All Urban)',
  CPILFESL: 'Core CPI (ex Food & Energy)',
  UNRATE: 'Unemployment Rate',
  DGS10: '10Y Treasury Yield',
  DGS2: '2Y Treasury Yield',
  T10Y2Y: '10Y-2Y Yield Spread',
  FEDFUNDS: 'Fed Funds Rate',
  INDPRO: 'Industrial Production',
  M2SL: 'M2 Money Supply',
  BAMLH0A0HYM2: 'HY Credit Spread',
  DCOILWTICO: 'WTI Crude Oil (FRED)',
  DHHNGSP: 'Henry Hub Gas (FRED)',
}

const COMMODITY_MENU: Record<string, string> = {
  WTI_CRUDE: 'WTI Crude Oil (USD/bbl)',
  BRENT_CRUDE: 'Brent Crude Oil (USD/bbl)',
  HENRY_HUB: 'Henry Hub Gas (USD/MMBtu)',
  US_CRUDE_INVENTORY: 'US Crude Inventory (Mn Bbls)',
  REFINERY_UTIL: 'US Refinery Utilization (%)',
  ELEC_RETAIL_US: 'US Electricity Retail Price (¢/kWh)',
  COPPER: 'Copper (USD/MT)',
  LITHIUM: 'Lithium Carbonate (USD/MT)',
  BALTIC_DRY: 'Baltic Dry Index',
}

const SERIES_THRESHOLDS: Record<string, number> = {
  DGS10: 4.5, T10Y2Y: 0.0, CPIAUCSL: 5.0, FEDFUNDS: 5.0, UNRATE: 6.0,
}
const COMM_THRESHOLDS: Record<string, number> = {
  WTI_CRUDE: 100, BRENT_CRUDE: 100, HENRY_HUB: 5,
  COPPER: 10000, LITHIUM: 50000, BALTIC_DRY: 3000,
}

const IN_SERIES_MENU: Record<string, string> = {
  'WB_NY.GDP.MKTP.KD.ZG': 'GDP Growth Rate (annual %)',
  'WB_FP.CPI.TOTL.ZG':    'CPI Inflation (annual %)',
  'WB_NY.GDP.PCAP.CD':    'GDP per Capita (current USD)',
  'WB_NE.EXP.GNFS.ZS':   'Exports of Goods & Services (% GDP)',
  'WB_NE.IMP.GNFS.ZS':   'Imports of Goods & Services (% GDP)',
  'WB_BN.CAB.XOKA.CD':   'Current Account Balance (USD)',
  'WB_GC.DOD.TOTL.GD.ZS':'Government Debt (% GDP)',
  'WB_FI.RES.TOTL.CD':   'Foreign Reserves (USD)',
  'WB_EG.FEC.RNEW.ZS':   'Renewable Energy Share (% total)',
  'WB_SP.POP.TOTL':       'Population (total)',
}

const IN_SERIES_THRESHOLDS: Record<string, number> = {
  'WB_FP.CPI.TOTL.ZG':    6.0,
  'WB_GC.DOD.TOTL.GD.ZS': 85.0,
  'WB_NY.GDP.MKTP.KD.ZG': 6.0,
}

const SECTORS = ['Energy', 'Technology', 'Healthcare', 'Industrials', 'Financials', 'Agriculture', 'Materials', 'Utilities', 'Defense']

export default function MacroTab({ country, countryFlag, countryLabel }: Props) {
  const [fromDate, setFromDate] = useState('2018-01-01')
  const [toDate, setToDate] = useState(today)
  const [useAlfred, setUseAlfred] = useState(false)
  const [runConstraint, setRunConstraint] = useState(true)
  const [fetchLoading, setFetchLoading] = useState(false)
  const [fetchResult, setFetchResult] = useState<Record<string, unknown> | null>(null)
  const [selectedSeries, setSelectedSeries] = useState(() => country === 'IN' ? 'WB_NY.GDP.MKTP.KD.ZG' : 'GDP')
  const [selectedComm, setSelectedComm] = useState('WTI_CRUDE')
  const [chartYears, setChartYears] = useState(5)
  const [evWindow, setEvWindow] = useState(365)
  const [polSectors, setPolSectors] = useState<string[]>([])
  const [polDirection, setPolDirection] = useState('All')

  const activeSeriesMenu = country === 'IN' ? IN_SERIES_MENU : SERIES_MENU
  const activeThresholds = country === 'IN' ? IN_SERIES_THRESHOLDS : SERIES_THRESHOLDS

  const seriesStart = new Date(new Date().getFullYear() - chartYears, 0, 1).toISOString().slice(0, 10)

  const { data: seriesData = [] } = useQuery({
    queryKey: ['macro-series', selectedSeries, seriesStart, toDate, country],
    queryFn: () => fetchMacroSeries(selectedSeries, seriesStart, toDate, country),
  })

  const { data: commData = [] } = useQuery({
    queryKey: ['commodity', selectedComm, seriesStart, toDate],
    queryFn: () => fetchCommodity(selectedComm, seriesStart, toDate),
  })

  const { data: macroEvents = [] } = useQuery({
    queryKey: ['macro-events', country, toDate, evWindow],
    queryFn: () => fetchMacroEvents(toDate, evWindow),
    enabled: country === 'US',
  })

  const { data: policyEvents = [] } = useQuery({
    queryKey: ['policy-events', country, toDate, polSectors.join(','), polDirection],
    queryFn: () => fetchPolicyEvents({
      as_of: toDate,
      country,
      sectors: polSectors.length ? polSectors.join(',') : undefined,
      impact_direction: polDirection !== 'All' ? polDirection : undefined,
    }),
  })

  const fetchMacro = async () => {
    setFetchLoading(true)
    setFetchResult(null)
    try {
      const res = await runMacroFetch({
        from_date: fromDate, to_date: toDate,
        use_alfred: useAlfred, run_constraint_engine: runConstraint,
        country,
      })
      setFetchResult(res)
    } catch (e) {
      setFetchResult({ error: String(e) })
    } finally {
      setFetchLoading(false)
    }
  }

  const seriesChart = (seriesData as Record<string, unknown>[]).map(r => ({
    date: String(r.observation_date ?? '').slice(0, 10),
    value: Number(r.value ?? 0),
  }))
  const commChart = (commData as Record<string, unknown>[]).map(r => ({
    date: String(r.observation_date ?? '').slice(0, 10),
    value: Number(r.value ?? 0),
  }))

  const severityColor = (sev: number) => {
    if (sev >= 80) return '#ef4444'
    if (sev >= 65) return '#f97316'
    if (sev >= 40) return '#f59e0b'
    return '#64748b'
  }

  const dirColor: Record<string, string> = {
    positive: '#22c55e', negative: '#ef4444', neutral: '#94a3b8', mixed: '#f59e0b',
  }
  const polTypeIcon: Record<string, string> = {
    bill: '📜', rule: '⚖️', executive_order: '🖊️', notice: '📢', resolution: '🗳️',
  }

  const toggleSector = (s: string) =>
    setPolSectors(p => p.includes(s) ? p.filter(x => x !== s) : [...p, s])

  return (
    <div className="space-y-6">
      <CountryBanner flag={countryFlag} label={countryLabel}>
        Macro & Policy data for <strong>{countryLabel}</strong>
      </CountryBanner>

      <h3 className="text-base font-bold text-slate-100">🌐 Macro & Policy Intelligence</h3>

      {/* Country context */}
      {country === 'IN' ? (
        <div className="bg-blue-950/30 border border-blue-800/30 rounded-xl p-4 text-sm text-blue-300">
          📅 <strong>India Macro Sources:</strong> RBI (monetary policy, repo rate, inflation) · SEBI (circulars) ·
          PIB (govt press releases: PLI, semiconductor, defence, EV) · InvestIndia (sector reports) · Commerce/DGFT (trade policy)
        </div>
      ) : (
        <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-4 text-sm text-blue-200">
          📅 <strong>Historical data fully supported.</strong> FRED from 1940s · EIA/World Bank from 1980s–90s ·
          Congress from 2001 · Federal Register from 1994. Enable <strong>ALFRED vintage mode</strong> for exact publication-date values.
        </div>
      )}

      {/* Fetch controls */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 items-end">
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Start date</label>
          <input type="date" value={fromDate} max={today} onChange={e => setFromDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">End date</label>
          <input type="date" value={toDate} max={today} onChange={e => setToDate(e.target.value)} className="input" />
        </div>
        <div className="flex flex-col gap-2 pt-4">
          {country !== 'IN' && (
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={useAlfred} onChange={e => setUseAlfred(e.target.checked)} className="accent-indigo-500" />
              <span className="text-xs text-slate-300">ALFRED vintage mode (no look-ahead bias)</span>
            </label>
          )}
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={runConstraint} onChange={e => setRunConstraint(e.target.checked)} className="accent-indigo-500" />
            <span className="text-xs text-slate-300">Run Constraint Engine after fetch</span>
          </label>
        </div>
        <button onClick={fetchMacro} disabled={fetchLoading} className="btn-primary flex items-center gap-2">
          {fetchLoading ? <Spinner size="sm" /> : '⬇️'}
          {country === 'IN' ? 'Fetch India Macro' : 'Fetch Macro Data'}
        </button>
      </div>

      {fetchResult && (
        <div className={`rounded-xl p-4 text-sm ${fetchResult.error ? 'bg-red-950/30 text-red-300 border border-red-800/30' : 'bg-emerald-950/30 text-emerald-300 border border-emerald-800/30'}`}>
          {fetchResult.error ? `❌ Error: ${String(fetchResult.error)}` : (
            country === 'IN'
              ? `✅ India macro complete — Policy events: ${String(fetchResult.india_macro_events ?? 0)} · Themes scored: ${String(fetchResult.themes_constraint_scored ?? 0)}`
              : `✅ Macro complete — FRED: ${fetchResult.fred_rows ?? 0} · EIA: ${fetchResult.eia_rows ?? 0} · World Bank: ${fetchResult.world_bank_rows ?? 0} · Congress: ${fetchResult.congress_events ?? 0} · Fed Register: ${fetchResult.federal_register_events ?? 0} · Themes scored: ${fetchResult.themes_constraint_scored ?? 0}`
          )}
        </div>
      )}

      <hr className="border-slate-800" />

      {/* Economic Series */}
      <div>
        <h4 className="text-sm font-bold text-slate-200 mb-3">📈 Economic Series</h4>
        <div className="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label className="text-xs text-slate-400 mb-1 block">Select series</label>
            <select value={selectedSeries} onChange={e => setSelectedSeries(e.target.value)} className="select w-full">
              {Object.entries(activeSeriesMenu).map(([k, v]) => (
                <option key={k} value={k}>{k} — {v}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="text-xs text-slate-400 mb-1 block">Years of history: {chartYears}</label>
            <input type="range" min={1} max={10} value={chartYears} onChange={e => setChartYears(+e.target.value)}
              className="w-full accent-indigo-500 mt-2" />
          </div>
        </div>
        {seriesChart.length > 0 ? (
          <div className="bg-slate-900 rounded-xl p-4 border border-slate-800">
            <div className="text-xs text-slate-400 mb-2 font-medium">
              {selectedSeries} — {activeSeriesMenu[selectedSeries]}
            </div>
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={seriesChart} margin={{ top: 4, right: 8, left: -10, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} />
                <YAxis tick={{ fill: '#64748b', fontSize: 10 }} />
                <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }}
                  labelStyle={{ color: '#94a3b8' }} />
                <Line type="monotone" dataKey="value" stroke="#818cf8" strokeWidth={2} dot={false} name={selectedSeries} />
                {activeThresholds[selectedSeries] != null && (
                  <ReferenceLine y={activeThresholds[selectedSeries]} stroke="#ef4444" strokeDasharray="4 4"
                    label={{ value: `Threshold ${activeThresholds[selectedSeries]}`, fill: '#ef4444', fontSize: 10, position: 'insideTopRight' }} />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="text-sm text-slate-500 bg-slate-800/40 rounded-xl p-4">
            {country === 'IN'
              ? `No data for ${selectedSeries}. Fetch India Macro to load World Bank data (free, no key required).`
              : `No data for ${selectedSeries}. Fetch macro data first (FRED_API_KEY required).`}
          </div>
        )}
      </div>

      <hr className="border-slate-800" />

      {/* Commodity Prices */}
      <div>
        <h4 className="text-sm font-bold text-slate-200 mb-3">🛢️ Commodity Prices</h4>
        <div className="mb-3">
          <label className="text-xs text-slate-400 mb-1 block">Select commodity</label>
          <select value={selectedComm} onChange={e => setSelectedComm(e.target.value)} className="select w-full md:w-72">
            {Object.entries(COMMODITY_MENU).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
        </div>
        {commChart.length > 0 ? (
          <div className="bg-slate-900 rounded-xl p-4 border border-slate-800">
            <div className="text-xs text-slate-400 mb-2 font-medium">{COMMODITY_MENU[selectedComm]}</div>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={commChart} margin={{ top: 4, right: 8, left: -10, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} />
                <YAxis tick={{ fill: '#64748b', fontSize: 10 }} />
                <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }}
                  labelStyle={{ color: '#94a3b8' }} />
                <Line type="monotone" dataKey="value" stroke="#f59e0b" strokeWidth={2} dot={false} name={selectedComm} />
                {COMM_THRESHOLDS[selectedComm] != null && (
                  <ReferenceLine y={COMM_THRESHOLDS[selectedComm]} stroke="#ef4444" strokeDasharray="4 4"
                    label={{ value: 'Constraint threshold', fill: '#ef4444', fontSize: 10, position: 'insideTopRight' }} />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="text-sm text-slate-500 bg-slate-800/40 rounded-xl p-4">
            No commodity data yet. Fetch macro data first (EIA_API_KEY required).
          </div>
        )}
      </div>

      <hr className="border-slate-800" />

      {/* Macro Threshold Events — US only (FRED-derived) */}
      {country === 'US' && <div>
        <h4 className="text-sm font-bold text-slate-200 mb-2">⚠️ Macro Threshold Events</h4>
        <p className="text-xs text-slate-500 mb-3">Automatically triggered when key economic series cross critical levels.</p>
        <div className="flex items-center gap-3 mb-4">
          <label className="text-xs text-slate-400">Look-back (days): <strong className="text-slate-200">{evWindow}</strong></label>
          <input type="range" min={90} max={1825} step={90} value={evWindow} onChange={e => setEvWindow(+e.target.value)}
            className="w-48 accent-indigo-500" />
        </div>
        {(macroEvents as unknown[]).length === 0 ? (
          <div className="text-sm text-slate-500 bg-slate-800/40 rounded-xl p-4">
            No macro threshold events yet. Fetch macro data first, then the Constraint Engine will detect threshold crossings.
          </div>
        ) : (
          <div className="space-y-2">
            {(macroEvents as Record<string, unknown>[]).slice(0, 20).map((ev, i) => {
              const sev = Number(ev.severity ?? 0)
              const col = severityColor(sev)
              return (
                <div key={i} className="bg-slate-800 border border-slate-700 rounded-xl p-3"
                  style={{ borderLeft: `3px solid ${col}` }}>
                  <div className="flex justify-between flex-wrap gap-2">
                    <span className="font-bold text-white text-sm">
                      {String(ev.event_type ?? '').replace(/_/g, ' ').toUpperCase()}
                    </span>
                    <span className="text-xs text-slate-400">
                      {String(ev.event_date ?? '')} | severity: <span style={{ color: col }}>{sev.toFixed(0)}/100</span>
                    </span>
                  </div>
                  <div className="text-slate-300 text-xs mt-1.5">{String(ev.description ?? '')}</div>
                  <div className="text-xs mt-1.5 flex gap-4">
                    <span>⚠️ At-risk: <span className="text-red-300">{(ev.sectors_at_risk as string[] ?? []).join(', ') || '—'}</span></span>
                    <span>✅ Benefiting: <span className="text-emerald-300">{(ev.sectors_benefit as string[] ?? []).join(', ') || '—'}</span></span>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>}

      <hr className="border-slate-800" />

      {/* Policy Events */}
      <div>
        <h4 className="text-sm font-bold text-slate-200 mb-2">
          🏛️ Policy Events ({country === 'IN' ? 'PIB · SEBI · RBI · InvestIndia · Commerce/DGFT' : 'Congress + Federal Register'})
        </h4>
        <p className="text-xs text-slate-500 mb-3">
          {country === 'IN'
            ? 'Government circulars, RBI/SEBI notices, PLI updates, and trade policy events.'
            : 'Bills, regulations, and executive orders affecting sectors and technologies.'}
        </p>

        <div className="flex flex-wrap gap-3 mb-4 items-end">
          <div>
            <label className="text-xs text-slate-400 mb-1 block">Sector filter</label>
            <div className="flex flex-wrap gap-1.5">
              {SECTORS.map(s => (
                <button key={s} onClick={() => toggleSector(s)}
                  className={`text-xs px-2 py-0.5 rounded-full border transition-colors ${
                    polSectors.includes(s) ? 'bg-indigo-600/30 border-indigo-500 text-indigo-300' : 'bg-slate-800 border-slate-700 text-slate-400 hover:border-slate-500'
                  }`}>
                  {s}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className="text-xs text-slate-400 mb-1 block">Impact</label>
            <select value={polDirection} onChange={e => setPolDirection(e.target.value)} className="select">
              {['All', 'positive', 'negative', 'neutral', 'mixed'].map(d => <option key={d}>{d}</option>)}
            </select>
          </div>
        </div>

        {(policyEvents as unknown[]).length === 0 ? (
          <div className="text-sm text-slate-500 bg-slate-800/40 rounded-xl p-4">
            No policy events yet. Fetch macro data first (CONGRESS_API_KEY for Congress data; Federal Register is free).
          </div>
        ) : (
          <div className="space-y-2">
            {(policyEvents as Record<string, unknown>[]).slice(0, 30).map((pe, i) => {
              const dir = String(pe.impact_direction ?? 'neutral')
              const col = dirColor[dir] ?? '#94a3b8'
              const icon = polTypeIcon[String(pe.policy_type ?? 'notice')] ?? '📄'
              const mag = Number(pe.impact_magnitude ?? 0)
              return (
                <div key={i} className="bg-slate-800 border border-slate-700 rounded-xl p-3"
                  style={{ borderLeft: `3px solid ${col}` }}>
                  <div className="flex justify-between flex-wrap gap-2">
                    <span className="font-bold text-white text-sm">{icon} {String(pe.title ?? '').slice(0, 140)}</span>
                    <span className="text-xs text-slate-400">
                      {String(pe.source ?? '').replace(/_/g, ' ')} | {String(pe.enacted_date ?? pe.introduced_date ?? '?')}
                    </span>
                  </div>
                  <div className="text-xs text-slate-400 mt-1.5">
                    Impact: <span style={{ color: col }}>{dir}</span> ({mag.toFixed(0)}/100) | Status: {String(pe.status ?? '')}
                  </div>
                  <div className="text-xs mt-1">
                    Sectors: <span className="text-indigo-300">{(pe.sectors_affected as string[] ?? []).join(', ') || '—'}</span>
                    {' | '}Technologies: <span className="text-emerald-300">{(pe.technologies_affected as string[] ?? []).join(', ') || '—'}</span>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      <hr className="border-slate-800" />

      {/* API key status */}
      <div>
        <h4 className="text-sm font-bold text-slate-200 mb-3">🔑 API Key Status</h4>
        <div className="space-y-1">
          {[
            ['FRED_API_KEY', 'FRED (economic series)', 'https://fred.stlouisfed.org/docs/api/fred/'],
            ['EIA_API_KEY', 'EIA (energy data)', 'https://www.eia.gov/opendata/'],
            ['CONGRESS_API_KEY', 'Congress.gov (legislation)', 'https://api.congress.gov/sign-up/'],
          ].map(([env, label, url]) => (
            <div key={env} className="text-xs text-slate-400">
              <span className="text-emerald-400 mr-2">ℹ️</span>
              <strong>{label}</strong>: Check {env} env var or config/settings.yaml.{' '}
              <a href={url} target="_blank" rel="noreferrer" className="text-indigo-400 hover:underline">Get free key</a>
            </div>
          ))}
          <div className="text-xs text-emerald-400">✅ <strong>Federal Register</strong>: free, no API key required</div>
          <div className="text-xs text-emerald-400">✅ <strong>World Bank</strong>: free, no API key required</div>
        </div>
      </div>
    </div>
  )
}
