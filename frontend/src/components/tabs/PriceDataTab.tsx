import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchPriceDataStatus } from '../../api'
import { SectionHeader, Spinner } from '../ui'

const today = new Date().toISOString().slice(0, 10)

function logLineClass(line: string): string {
  if (line.startsWith('[DONE]'))      return 'text-emerald-400 font-bold'
  if (line.startsWith('[DONE-NSE]'))  return 'text-emerald-400 font-bold'
  if (line.startsWith('[DONE-BSE]'))  return 'text-emerald-400 font-bold'
  if (line.startsWith('[ERROR]'))     return 'text-red-400'
  if (/\bERROR\b/.test(line))         return 'text-red-400'
  if (/\bWARNING\b/.test(line))       return 'text-yellow-400'
  if (/\bINFO\b/.test(line))          return 'text-sky-300'
  return 'text-slate-400'
}

type Mode = 'daily' | 'historical' | 'copy-from-algo' | 'bulk-deals' | 'block-deals' | 'insider-trades' | 'fundamentals' | 'sector-master'

function StatusCard({ label, exists, minDate, maxDate, rowCount, lastUpdated }: {
  label: string; exists: boolean; minDate?: string | null; maxDate?: string | null
  rowCount: number; lastUpdated?: string | null
}) {
  return (
    <div className={`rounded-xl border px-4 py-3 space-y-1 ${
      exists ? 'border-emerald-800/60 bg-emerald-950/20' : 'border-slate-700 bg-slate-800/40'
    }`}>
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-slate-200">{label}</span>
        <span className={`text-[10px] font-bold px-2 py-0.5 rounded ${
          exists ? 'bg-emerald-900/50 text-emerald-300' : 'bg-slate-700 text-slate-400'
        }`}>{exists ? 'READY' : 'EMPTY'}</span>
      </div>
      <div className="text-xs text-slate-400">
        Rows: <strong className="text-slate-200">{rowCount.toLocaleString()}</strong>
      </div>
      {minDate && maxDate && (
        <div className="text-xs text-slate-500">
          {minDate} → {maxDate}
        </div>
      )}
      {lastUpdated && (
        <div className="text-xs text-slate-500">Last updated: {lastUpdated}</div>
      )}
    </div>
  )
}

export default function PriceDataTab() {
  const [mode, setMode] = useState<Mode>('copy-from-algo')
  const [exchange, setExchange] = useState<'nse' | 'bse' | 'both'>('both')
  const [startDate, setStartDate] = useState(
    new Date(Date.now() - 365 * 86400_000).toISOString().slice(0, 10)
  )
  const [endDate, setEndDate] = useState(today)
  const [useDateRange, setUseDateRange] = useState(true)
  const [daysBack, setDaysBack] = useState(5)
  const [dealSource, setDealSource] = useState<'algo' | 'live'>('algo')
  const [period, setPeriod] = useState<'1D' | '1W' | '1M' | '3M' | '6M' | '1Y'>('1W')
  const [symbolsInput, setSymbolsInput] = useState('')
  const [running, setRunning] = useState(false)
  const [logs, setLogs] = useState<string[]>([])
  const [done, setDone] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)
  const sseBuffer = useRef('')

  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ['price-data-status'],
    queryFn: fetchPriceDataStatus,
    staleTime: 15_000,
  })

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [logs])

  const copyLogs = useCallback(() => {
    navigator.clipboard.writeText(logs.join('\n'))
  }, [logs])

  const isDealMode = mode === 'bulk-deals' || mode === 'block-deals' || mode === 'insider-trades'

  const runFetch = async () => {
    setLogs([])
    setDone(false)
    setRunning(true)
    sseBuffer.current = ''
    try {
      const body: Record<string, unknown> = { mode, exchange }
      if (isDealMode) {
        body.source = dealSource
        if (dealSource === 'live') body.period = period
      }
      if (mode === 'historical' ||
        ((mode === 'copy-from-algo' || (isDealMode && dealSource === 'algo')) && useDateRange)) {
        body.start_date = startDate
        body.end_date = endDate
      }
      if (mode === 'daily') {
        body.days_back = daysBack
      }
      if (mode === 'fundamentals') {
        body.symbols = symbolsInput.split(',').map(s => s.trim()).filter(Boolean)
      }
      const res = await fetch('/api/price-data/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        setLogs(prev => [...prev, `[ERROR] HTTP ${res.status} ${res.statusText}`])
        return
      }
      const reader = res.body!.getReader()
      const decoder = new TextDecoder()
      while (true) {
        const { done: streamDone, value } = await reader.read()
        if (streamDone) break
        sseBuffer.current += decoder.decode(value, { stream: true })
        const lines = sseBuffer.current.split('\n')
        sseBuffer.current = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const msg = line.slice(6).trim()
          if (msg === '[DONE]') {
            setDone(true)
            setRunning(false)
            refetchStatus()
            return
          }
          if (msg) setLogs(prev => [...prev, msg])
        }
      }
    } catch (e) {
      setLogs(prev => [...prev, `[ERROR] ${e}`])
    } finally {
      setRunning(false)
      refetchStatus()
    }
  }

  const s = (status ?? {}) as Record<string, Record<string, unknown>>
  const nse = s.nse ?? {}
  const bse = s.bse ?? {}
  const fundamentals = s.fundamentals ?? {}
  const sectorMaster = s.sector_master ?? {}
  const nseBulk = s.nse_bulk ?? {}
  const bseBulk = s.bse_bulk ?? {}
  const nseBlock = s.nse_block ?? {}
  const bseBlock = s.bse_block ?? {}
  const nseInsider = s.nse_insider ?? {}
  const bseInsider = s.bse_insider ?? {}

  return (
    <div className="space-y-4">
      <SectionHeader>📈 NSE / BSE Price Data</SectionHeader>

      {/* Status */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatusCard label="NSE Bhavcopy" exists={Boolean(nse.exists)}
          minDate={nse.min_date as string} maxDate={nse.max_date as string}
          rowCount={Number(nse.row_count ?? 0)} />
        <StatusCard label="BSE Bhavcopy" exists={Boolean(bse.exists)}
          minDate={bse.min_date as string} maxDate={bse.max_date as string}
          rowCount={Number(bse.row_count ?? 0)} />
        <StatusCard label="Fundamentals" exists={Boolean(fundamentals.exists)}
          rowCount={Number(fundamentals.row_count ?? 0)}
          lastUpdated={fundamentals.last_updated as string} />
        <StatusCard label="Sector Master" exists={Boolean(sectorMaster.exists)}
          rowCount={Number(sectorMaster.row_count ?? 0)}
          lastUpdated={sectorMaster.last_updated as string} />
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        <StatusCard label="NSE Bulk Deals" exists={Boolean(nseBulk.exists)}
          minDate={nseBulk.min_date as string} maxDate={nseBulk.max_date as string}
          rowCount={Number(nseBulk.row_count ?? 0)} />
        <StatusCard label="NSE Block Deals" exists={Boolean(nseBlock.exists)}
          minDate={nseBlock.min_date as string} maxDate={nseBlock.max_date as string}
          rowCount={Number(nseBlock.row_count ?? 0)} />
        <StatusCard label="NSE Insider Trades" exists={Boolean(nseInsider.exists)}
          minDate={nseInsider.min_date as string} maxDate={nseInsider.max_date as string}
          rowCount={Number(nseInsider.row_count ?? 0)} />
        <StatusCard label="BSE Bulk Deals" exists={Boolean(bseBulk.exists)}
          minDate={bseBulk.min_date as string} maxDate={bseBulk.max_date as string}
          rowCount={Number(bseBulk.row_count ?? 0)} />
        <StatusCard label="BSE Block Deals" exists={Boolean(bseBlock.exists)}
          minDate={bseBlock.min_date as string} maxDate={bseBlock.max_date as string}
          rowCount={Number(bseBlock.row_count ?? 0)} />
        <StatusCard label="BSE Insider Trades" exists={Boolean(bseInsider.exists)}
          minDate={bseInsider.min_date as string} maxDate={bseInsider.max_date as string}
          rowCount={Number(bseInsider.row_count ?? 0)} />
      </div>

      <SectionHeader>Configure & Run</SectionHeader>

      {/* Mode selector */}
      <div>
        <label className="text-xs text-slate-400 mb-1 block">Mode</label>
        <select value={mode} onChange={e => setMode(e.target.value as Mode)} className="select w-full max-w-md">
          <option value="copy-from-algo">Copy from Algo_Test DB (bhavcopy + fundamentals + bulk/block/insider deals)</option>
          <option value="historical">Historical fetch from NSE/BSE APIs (slow)</option>
          <option value="daily">Daily incremental fetch (last N days)</option>
          <option value="bulk-deals">Bulk deals only (Algo_Test DB or live NSE/BSE)</option>
          <option value="block-deals">Block deals only (Algo_Test DB or live NSE/BSE)</option>
          <option value="insider-trades">Insider trades only (Algo_Test DB or live NSE/BSE)</option>
          <option value="fundamentals">Screener.in fundamentals (by symbol)</option>
          <option value="sector-master">Sector master (BSE downloads)</option>
        </select>
      </div>

      {/* Exchange selector */}
      {(mode === 'copy-from-algo' || mode === 'historical' || mode === 'daily' ||
        mode === 'bulk-deals' || mode === 'block-deals' || mode === 'insider-trades') && (
        <div className="flex gap-3">
          {(['both', 'nse', 'bse'] as const).map(ex => (
            <button
              key={ex}
              onClick={() => setExchange(ex)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors uppercase ${
                exchange === ex
                  ? 'bg-indigo-600 text-white'
                  : 'bg-slate-800 border border-slate-700 text-slate-400 hover:text-slate-200'
              }`}
            >
              {ex}
            </button>
          ))}
        </div>
      )}

      {/* Deal source selector — Algo_Test DB copy vs live NSE/BSE fetch */}
      {isDealMode && (
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Source</label>
          <div className="flex gap-3">
            {([
              { key: 'algo', label: 'Copy from Algo_Test DB' },
              { key: 'live', label: 'Fetch live from NSE/BSE' },
            ] as const).map(opt => (
              <button
                key={opt.key}
                onClick={() => setDealSource(opt.key)}
                className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                  dealSource === opt.key
                    ? 'bg-indigo-600 text-white'
                    : 'bg-slate-800 border border-slate-700 text-slate-400 hover:text-slate-200'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Period selector — live NSE/BSE deal fetch */}
      {isDealMode && dealSource === 'live' && (
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Period</label>
          <select value={period} onChange={e => setPeriod(e.target.value as typeof period)} className="select w-32">
            <option value="1D">1 Day</option>
            <option value="1W">1 Week</option>
            <option value="1M">1 Month</option>
            <option value="3M">3 Months</option>
            <option value="6M">6 Months</option>
            <option value="1Y">1 Year</option>
          </select>
          <p className="text-[11px] text-slate-500 mt-1">
            Live fetch drives a headless browser against NSE/BSE — this can take 1-3 minutes per exchange.
          </p>
        </div>
      )}

      {/* Date range — historical always, copy-from-algo optional */}
      {(mode === 'copy-from-algo' || (isDealMode && dealSource === 'algo')) && (
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={useDateRange} onChange={e => setUseDateRange(e.target.checked)}
            className="accent-indigo-500" />
          <span className="text-sm text-slate-300">Filter by trade date range (leave unchecked to copy all history)</span>
        </label>
      )}
      {(mode === 'historical' ||
        ((mode === 'copy-from-algo' || (isDealMode && dealSource === 'algo')) && useDateRange)) && (
        <div className="grid grid-cols-2 gap-3 max-w-md">
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
        </div>
      )}

      {/* Days back — daily mode */}
      {mode === 'daily' && (
        <div className="flex items-center gap-2">
          <span className="text-sm text-slate-400">Days back:</span>
          <input type="number" min={1} max={90} value={daysBack}
            onChange={e => setDaysBack(+e.target.value)} className="input w-24" />
        </div>
      )}

      {/* Symbols — fundamentals mode */}
      {mode === 'fundamentals' && (
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Symbols (comma-separated)</label>
          <input type="text" value={symbolsInput} onChange={e => setSymbolsInput(e.target.value)}
            placeholder="RELIANCE,TCS,INFY,HDFCBANK" className="input w-full max-w-md" />
        </div>
      )}

      <div className="border-t border-slate-800 pt-4 flex items-center gap-3">
        <button
          onClick={runFetch}
          disabled={running || (mode === 'fundamentals' && !symbolsInput.trim())}
          className="btn-primary flex items-center gap-2"
        >
          {running ? <Spinner size="sm" /> : '▶'}
          {running ? 'Running…' : 'Run Fetch'}
        </button>
        {(running || logs.length > 0) && (
          <>
            <button
              onClick={() => { setLogs([]); setDone(false) }}
              disabled={running}
              className="btn-secondary text-xs"
            >
              Clear
            </button>
            {logs.length > 0 && (
              <button onClick={copyLogs} className="btn-secondary text-xs">
                Copy logs
              </button>
            )}
          </>
        )}
      </div>

      {/* Log terminal */}
      {(running || logs.length > 0) && (
        <div className="rounded-xl border border-slate-700 overflow-hidden">
          <div className="flex items-center justify-between bg-slate-800 px-3 py-2 border-b border-slate-700">
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono font-semibold text-slate-300">Fetch Log</span>
              {running && (
                <span className="flex items-center gap-1.5 text-xs text-emerald-400">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                  Live
                </span>
              )}
              {done && !running && (
                <span className="text-xs text-emerald-400 font-semibold">✓ Complete</span>
              )}
              {!running && !done && logs.length > 0 && (
                <span className="text-xs text-red-400 font-semibold">✗ Stopped</span>
              )}
            </div>
            <span className="text-xs text-slate-500 font-mono">{logs.length} lines</span>
          </div>
          <div
            ref={logRef}
            className="bg-slate-950 font-mono text-xs p-3 overflow-y-auto"
            style={{ maxHeight: '520px', minHeight: running && logs.length === 0 ? '80px' : undefined }}
          >
            {running && logs.length === 0 && (
              <span className="text-slate-500 animate-pulse">Waiting for fetch output…</span>
            )}
            {logs.map((line, i) => (
              <div key={i} className={`leading-5 ${logLineClass(line)}`}>
                {line}
              </div>
            ))}
            {running && logs.length > 0 && (
              <span className="inline-block w-2 h-3.5 bg-sky-400 animate-pulse ml-0.5 align-middle" />
            )}
          </div>
        </div>
      )}
    </div>
  )
}
