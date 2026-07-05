import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchHighVolumeStocks, fetchStockVolumeHistory } from '../../api'
import { SectionHeader, Spinner, EmptyState } from '../ui'
import {
  ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'

const today = new Date().toISOString().slice(0, 10)

interface Row {
  exchange: string
  symbol: string
  series: string | null
  trade_date: string
  volume: number
  value: number | null
  close: number | null
  delivery_pct: number | null
}

interface StockRow {
  exchange: string
  symbol: string
  series: string | null
  trade_date: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  value: number | null
  delivery_pct: number | null
}

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

type ViewMode = 'all-stocks' | 'single-stock'

function StockVolumeView() {
  const [symbol, setSymbol] = useState('')
  const [startDate, setStartDate] = useState(
    new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10)
  )
  const [endDate, setEndDate] = useState(today)
  const [exchange, setExchange] = useState<'nse' | 'bse' | 'both'>('both')
  const [searchTrigger, setSearchTrigger] = useState(0)

  const symbolTrimmed = symbol.trim().toUpperCase()

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['stock-volume-history', symbolTrimmed, startDate, endDate, exchange, searchTrigger],
    queryFn: () => fetchStockVolumeHistory(symbolTrimmed, startDate, endDate, exchange),
    enabled: searchTrigger > 0 && Boolean(symbolTrimmed),
  })

  const results = (data?.results ?? []) as StockRow[]
  const chartData = results.map(r => ({
    date: r.trade_date,
    volume: r.volume ?? 0,
    close: r.close ?? null,
  }))

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        Shows the daily traded volume (and closing price) for a single stock across the selected
        date range — useful for spotting volume spikes/trends over time for one symbol.
      </p>

      {/* Filters */}
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Symbol</label>
          <input type="text" value={symbol}
            onChange={e => setSymbol(e.target.value)}
            placeholder="RELIANCE"
            className="input w-40 uppercase" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Start date</label>
          <input type="date" value={startDate} max={today}
            onChange={e => setStartDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">End date</label>
          <input type="date" value={endDate} max={today}
            onChange={e => setEndDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Exchange</label>
          <select value={exchange} onChange={e => setExchange(e.target.value as 'nse' | 'bse' | 'both')} className="select">
            <option value="both">Both</option>
            <option value="nse">NSE</option>
            <option value="bse">BSE</option>
          </select>
        </div>
        <button
          onClick={() => setSearchTrigger(t => t + 1)}
          disabled={isFetching || !symbolTrimmed}
          className="btn-primary flex items-center gap-2"
        >
          {isFetching ? <Spinner size="sm" /> : '🔍'}
          {isFetching ? 'Loading…' : 'Search'}
        </button>
      </div>

      {error && (
        <div className="text-sm text-red-400">Error: {String((error as Error).message)}</div>
      )}

      {searchTrigger === 0 && !isLoading && (
        <EmptyState>Enter a symbol and date range, then click Search.</EmptyState>
      )}

      {searchTrigger > 0 && !isLoading && results.length === 0 && !error && (
        <EmptyState>No trading data found for {symbolTrimmed || 'this symbol'} in this range.</EmptyState>
      )}

      {results.length > 0 && (
        <>
          <div className="grid grid-cols-3 gap-3 max-w-lg">
            <div className="rounded-lg border border-slate-700 bg-slate-800/40 px-3 py-2">
              <div className="text-[10px] text-slate-500 uppercase">Avg Volume</div>
              <div className="text-sm font-semibold text-slate-200">{fmtNum(data?.avg_volume)}</div>
            </div>
            <div className="rounded-lg border border-slate-700 bg-slate-800/40 px-3 py-2">
              <div className="text-[10px] text-slate-500 uppercase">Max Volume</div>
              <div className="text-sm font-semibold text-emerald-400">{fmtNum(data?.max_volume)}</div>
            </div>
            <div className="rounded-lg border border-slate-700 bg-slate-800/40 px-3 py-2">
              <div className="text-[10px] text-slate-500 uppercase">Min Volume</div>
              <div className="text-sm font-semibold text-slate-200">{fmtNum(data?.min_volume)}</div>
            </div>
          </div>

          <div className="rounded-xl border border-slate-700 bg-slate-900/40 p-2" style={{ height: 280 }}>
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartData} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis dataKey="date" tick={{ fontSize: 10, fill: '#94a3b8' }} />
                <YAxis yAxisId="vol" tick={{ fontSize: 10, fill: '#94a3b8' }}
                  tickFormatter={(v) => fmtNum(v)} />
                <YAxis yAxisId="price" orientation="right" tick={{ fontSize: 10, fill: '#94a3b8' }} />
                <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid #334155', fontSize: 12 }}
                  formatter={(v: number, name: string) => [name === 'volume' ? fmtNum(v) : v, name]} />
                <Bar yAxisId="vol" dataKey="volume" fill="#6366f1" radius={[2, 2, 0, 0]} />
                <Line yAxisId="price" type="monotone" dataKey="close" stroke="#22c55e" strokeWidth={2} dot={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          <div className="overflow-x-auto rounded-lg border border-slate-700">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-slate-800 border-b border-slate-700">
                  {['Date', 'Exchange', 'Series', 'Open', 'High', 'Low', 'Close', 'Volume', 'Value (₹)', 'Delivery %'].map(h => (
                    <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[...results].reverse().map((r) => (
                  <tr key={`${r.exchange}-${r.trade_date}`} className="border-b border-slate-800 hover:bg-slate-800/50">
                    <td className="px-3 py-2 text-slate-300">{r.trade_date}</td>
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                        r.exchange === 'NSE' ? 'bg-indigo-900/50 text-indigo-300' : 'bg-amber-900/50 text-amber-300'
                      }`}>{r.exchange}</span>
                    </td>
                    <td className="px-3 py-2 text-slate-400">{r.series ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.open ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.high ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.low ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.close ?? '—'}</td>
                    <td className="px-3 py-2 text-emerald-400 font-medium">{fmtNum(r.volume)}</td>
                    <td className="px-3 py-2 text-slate-300">{fmtNum(r.value)}</td>
                    <td className="px-3 py-2 text-slate-300">{r.delivery_pct !== null ? `${r.delivery_pct}%` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

function AllStocksView() {
  const [startDate, setStartDate] = useState(
    new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10)
  )
  const [endDate, setEndDate] = useState(today)
  const [exchange, setExchange] = useState<'nse' | 'bse' | 'both'>('both')
  const [minVolume, setMinVolume] = useState(1_000_000)
  const [limit, setLimit] = useState(100)
  const [searchTrigger, setSearchTrigger] = useState(0)

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['high-volume-stocks', startDate, endDate, exchange, minVolume, limit, searchTrigger],
    queryFn: () => fetchHighVolumeStocks(startDate, endDate, exchange, minVolume, limit),
    enabled: searchTrigger > 0,
  })

  const results = (data?.results ?? []) as Row[]

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        For each symbol, finds its all-time (lifetime) highest-volume trading day across the entire
        history table, then keeps only symbols whose lifetime-peak day falls within the selected date
        range and exceeds the minimum volume — i.e. stocks that set a new all-time volume record during
        this window.
      </p>

      {/* Filters */}
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Start date</label>
          <input type="date" value={startDate} max={today}
            onChange={e => setStartDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">End date</label>
          <input type="date" value={endDate} max={today}
            onChange={e => setEndDate(e.target.value)} className="input" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Exchange</label>
          <select value={exchange} onChange={e => setExchange(e.target.value as 'nse' | 'bse' | 'both')} className="select">
            <option value="both">Both</option>
            <option value="nse">NSE</option>
            <option value="bse">BSE</option>
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Min volume</label>
          <input type="number" min={0} step={100_000} value={minVolume}
            onChange={e => setMinVolume(+e.target.value)} className="input w-32" />
        </div>
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Limit</label>
          <input type="number" min={1} max={500} value={limit}
            onChange={e => setLimit(+e.target.value)} className="input w-20" />
        </div>
        <button
          onClick={() => setSearchTrigger(t => t + 1)}
          disabled={isFetching}
          className="btn-primary flex items-center gap-2"
        >
          {isFetching ? <Spinner size="sm" /> : '🔍'}
          {isFetching ? 'Searching…' : 'Search'}
        </button>
      </div>

      {error && (
        <div className="text-sm text-red-400">Error: {String((error as Error).message)}</div>
      )}

      {searchTrigger === 0 && !isLoading && (
        <EmptyState>Set a date range and minimum volume, then click Search.</EmptyState>
      )}

      {searchTrigger > 0 && !isLoading && results.length === 0 && !error && (
        <EmptyState>No symbols found with peak-day volume ≥ {fmtNum(minVolume)} in this range.</EmptyState>
      )}

      {results.length > 0 && (
        <>
          <div className="text-xs text-slate-500">
            Showing {data.count} of {data.total_matches} matches
          </div>
          <div className="overflow-x-auto rounded-lg border border-slate-700">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-slate-800 border-b border-slate-700">
                  {['#', 'Exchange', 'Symbol', 'Series', 'Peak Volume Date', 'Volume', 'Value (₹)', 'Close', 'Delivery %'].map(h => (
                    <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {results.map((r, i) => (
                  <tr key={`${r.exchange}-${r.symbol}`} className="border-b border-slate-800 hover:bg-slate-800/50">
                    <td className="px-3 py-2 text-slate-500">{i + 1}</td>
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                        r.exchange === 'NSE' ? 'bg-indigo-900/50 text-indigo-300' : 'bg-amber-900/50 text-amber-300'
                      }`}>{r.exchange}</span>
                    </td>
                    <td className="px-3 py-2 text-slate-200 font-semibold">{r.symbol}</td>
                    <td className="px-3 py-2 text-slate-400">{r.series ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.trade_date}</td>
                    <td className="px-3 py-2 text-emerald-400 font-medium">{fmtNum(r.volume)}</td>
                    <td className="px-3 py-2 text-slate-300">{fmtNum(r.value)}</td>
                    <td className="px-3 py-2 text-slate-300">{r.close ?? '—'}</td>
                    <td className="px-3 py-2 text-slate-300">{r.delivery_pct !== null ? `${r.delivery_pct}%` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

export default function HighVolumeTab() {
  const [mode, setMode] = useState<ViewMode>('all-stocks')

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <SectionHeader>🔥 High Volume Days</SectionHeader>
        <div className="flex gap-2">
          {([
            { id: 'all-stocks', label: 'All Stocks (by date)' },
            { id: 'single-stock', label: 'Single Stock (by range)' },
          ] as const).map(m => (
            <button
              key={m.id}
              onClick={() => setMode(m.id)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                mode === m.id
                  ? 'bg-indigo-600 text-white'
                  : 'bg-slate-800 border border-slate-700 text-slate-400 hover:text-slate-200'
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {mode === 'all-stocks' ? <AllStocksView /> : <StockVolumeView />}
    </div>
  )
}
