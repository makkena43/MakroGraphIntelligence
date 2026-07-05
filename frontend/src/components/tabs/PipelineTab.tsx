import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchReplayHistory, fetchPipelineReadiness } from '../../api'
import { CountryBanner, SectionHeader, Spinner } from '../ui'

interface Props { country: string; countryFlag: string; countryLabel: string }

const today = new Date().toISOString().slice(0, 10)

function logLineClass(line: string): string {
  if (line.startsWith('[STAGE]'))      return 'text-amber-300 font-bold mt-2 border-t border-amber-900/30 pt-1'
  if (line.startsWith('[DONE]'))       return 'text-emerald-400 font-bold'
  if (line.startsWith('[ERROR]'))      return 'text-red-400'
  if (line.startsWith('[REPLAY]'))     return 'text-sky-400 font-semibold'
  if (line.startsWith('[INDIA-INTEL]'))return 'text-orange-400'
  if (/\bERROR\b/.test(line))         return 'text-red-400'
  if (/\bWARNING\b/.test(line))       return 'text-yellow-400'
  if (/\bINFO\b/.test(line))          return 'text-sky-300'
  return 'text-slate-400'
}

function ReadinessCheck({ country, startDate, endDate }: { country: string; startDate: string; endDate: string }) {
  const year = new Date(startDate).getFullYear()
  const yearValid = !isNaN(year) && year >= 1900 && year <= 2100
  const { data: readiness, isLoading } = useQuery({
    queryKey: ['pipeline-readiness', country, year],
    queryFn: () => fetchPipelineReadiness(country, year),
    enabled: yearValid,
    staleTime: 30_000,
  })

  if (isLoading) return null
  if (!readiness) return null

  const r = readiness as Record<string, unknown>
  const conclusion = String(r.conclusion ?? '')
  const diagnosis = (r.diagnosis as string[]) ?? []
  const actions = (r.action_needed as string[]) ?? []
  const totalDocs = Number(r.total_documents ?? 0)
  const readyNlp = Number(r.ready_for_nlp ?? 0)
  const signals = r.signals as Record<string,number> ?? {}
  const docsStatus = (r.docs_by_status as Record<string,unknown>[]) ?? []

  const conclusionColor =
    conclusion.startsWith('READY') ? 'border-emerald-700 bg-emerald-950/40' :
    conclusion.startsWith('NEED INGEST') ? 'border-red-700 bg-red-950/40' :
    'border-amber-700 bg-amber-950/40'

  return (
    <div className={`rounded-xl border px-4 py-3 space-y-2 mb-3 ${conclusionColor}`}>
      <div className="flex items-center justify-between">
        <div className="text-xs font-bold text-slate-200">
          🔎 Pre-flight Check — {country} {year}
        </div>
        <div className={`text-xs font-black px-2 py-0.5 rounded ${
          conclusion.startsWith('READY') ? 'text-emerald-300' :
          conclusion.startsWith('NEED INGEST') ? 'text-red-300' : 'text-amber-300'
        }`}>{conclusion}</div>
      </div>

      {/* Doc breakdown */}
      <div className="flex gap-4 text-[11px] text-slate-400 flex-wrap">
        <span>Total docs: <strong className="text-slate-200">{totalDocs}</strong></span>
        <span>Ready for NLP: <strong className={readyNlp>0?'text-emerald-400':'text-red-400'}>{readyNlp}</strong></span>
        <span>Existing signals: <strong className="text-slate-200">{Number(signals.signal_count??0)}</strong></span>
        <span>Seller signals: <strong className="text-amber-400">{Number(signals.seller_sigs??0)}</strong></span>
      </div>

      {/* Status breakdown */}
      {docsStatus.length > 0 && (
        <div className="flex gap-2 flex-wrap">
          {docsStatus.map((s,i) => (
            <span key={i} className="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-400">
              <strong className="text-slate-200">{String(s.processing_status)}</strong>: {Number(s.doc_count)} docs / {Number(s.companies)} cos
            </span>
          ))}
        </div>
      )}

      {/* Diagnosis */}
      <div className="space-y-0.5">
        {diagnosis.map((d,i) => <div key={i} className="text-[11px] text-slate-300">{d}</div>)}
      </div>

      {/* Actions needed */}
      {actions.length > 0 && (
        <div className="space-y-1 border-t border-slate-700/50 pt-2">
          <div className="text-[10px] font-bold text-amber-400">REQUIRED ACTIONS:</div>
          {actions.map((a,i) => (
            <div key={i} className="text-[11px] text-amber-300 flex items-start gap-1.5">
              <span>→</span><span>{a}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function PipelineTab({ country, countryFlag, countryLabel }: Props) {
  const [mode, setMode] = useState<'live' | 'replay'>('live')
  const [startDate, setStartDate] = useState(
    new Date(Date.now() - 90 * 86400_000).toISOString().slice(0, 10)
  )
  const [endDate, setEndDate] = useState(today)
  const [fetchMode, setFetchMode] = useState('all_us_complete')
  const [maxCo, setMaxCo] = useState(200)
  const [stages, setStages] = useState({
    ingest: true, nlp: true, graph: true, events: true,
    causal: true, themes: true, contradictions: true,
  })
  const [indiaIntelligence, setIndiaIntelligence] = useState(true)
  const [skipNeo4j, setSkipNeo4j] = useState(false)
  const [nlpBatch, setNlpBatch] = useState(500)
  const [pdfFetch, setPdfFetch] = useState(false)
  const [pdfWorkers, setPdfWorkers] = useState(6)
  const [resume, setResume] = useState(false)
  const [forceReprocess, setForceReprocess] = useState(false)
  const [running, setRunning] = useState(false)
  const [logs, setLogs] = useState<string[]>([])
  const [done, setDone] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)
  const sseBuffer = useRef('')

  const { data: replayHistory = [] } = useQuery({
    queryKey: ['replay-history'],
    queryFn: fetchReplayHistory,
  })

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [logs])

  const copyLogs = useCallback(() => {
    navigator.clipboard.writeText(logs.join('\n'))
  }, [logs])

  const runPipeline = async () => {
    setLogs([])
    setDone(false)
    setRunning(true)
    sseBuffer.current = ''
    try {
      const body = {
        country, start_date: startDate, end_date: endDate,
        is_replay: mode === 'replay',
        do_ingest: stages.ingest, do_nlp: stages.nlp, do_graph: stages.graph,
        do_events: stages.events, do_causal: stages.causal, do_themes: stages.themes,
        do_contradictions: stages.contradictions,
        do_india_intelligence: country === 'IN' && indiaIntelligence,
        do_pdf_fetch_india: pdfFetch, pdf_fetch_workers: pdfWorkers,
        skip_neo4j: skipNeo4j, nlp_batch_size: nlpBatch,
        fetch_mode: fetchMode, max_companies: maxCo, resume,
        force_reprocess_nlp: forceReprocess,
      }
      const res = await fetch('/api/pipeline/run', {
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
        // Append chunk to buffer; split on newlines; last incomplete line stays in buffer
        sseBuffer.current += decoder.decode(value, { stream: true })
        const lines = sseBuffer.current.split('\n')
        sseBuffer.current = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const msg = line.slice(6).trim()
          if (msg === '[DONE]') {
            setDone(true)
            setRunning(false)
            return
          }
          if (msg) setLogs(prev => [...prev, msg])
        }
      }
    } catch (e) {
      setLogs(prev => [...prev, `[ERROR] ${e}`])
    } finally {
      setRunning(false)
    }
  }

  const toggleStage = (k: keyof typeof stages) =>
    setStages(s => ({ ...s, [k]: !s[k] }))

  return (
    <div className="space-y-4">
      <CountryBanner flag={countryFlag} label={countryLabel}>
        Pipeline running for <strong>{countryLabel}</strong>
      </CountryBanner>

      <SectionHeader>Configure & Run</SectionHeader>

      {/* Mode */}
      <div className="flex gap-3">
        {(['live', 'replay'] as const).map(m => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              mode === m
                ? 'bg-indigo-600 text-white'
                : 'bg-slate-800 border border-slate-700 text-slate-400 hover:text-slate-200'
            }`}
          >
            {m === 'live' ? 'Live / Single-batch run' : 'Historical Replay (month-by-month)'}
          </button>
        ))}
      </div>

      {/* Date range */}
      <div className="grid grid-cols-2 gap-3">
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

      {/* Company universe (US) */}
      {country === 'US' && (
        <div>
          <label className="text-xs text-slate-400 mb-1 block">Company Universe</label>
          <select value={fetchMode} onChange={e => setFetchMode(e.target.value)} className="select w-full">
            <option value="selected">Selected companies (from config)</option>
            <option value="all_us_complete">All US companies — complete (~6000)</option>
          </select>
        </div>
      )}

      {/* Stages */}
      <div>
        <label className="text-xs text-slate-400 mb-2 block font-semibold">Stages to run</label>
        <div className="flex flex-wrap gap-2">
          {(Object.keys(stages) as Array<keyof typeof stages>).map(k => (
            <label key={k} className="flex items-center gap-1.5 cursor-pointer select-none">
              <input type="checkbox" checked={stages[k]} onChange={() => toggleStage(k)}
                className="accent-indigo-500" />
              <span className={`text-sm capitalize ${stages[k] ? 'text-slate-200' : 'text-slate-500'}`}>
                {k}
              </span>
            </label>
          ))}
        </div>
      </div>

      {/* India PDF fetch */}
      {country === 'IN' && (
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={pdfFetch} onChange={e => setPdfFetch(e.target.checked)}
            className="accent-indigo-500" />
          <span className="text-sm text-slate-300">📄 Fetch PDFs for high-value India filings</span>
        </label>
      )}

      {/* India Intelligence block — shown only for India */}
      {country === 'IN' && (
        <div className="rounded-xl border border-indigo-800/60 bg-indigo-950/30 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="text-base">🇮🇳</span>
              <span className="text-sm font-semibold text-indigo-300">India Intelligence Pipeline</span>
              <span className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-indigo-700/50 text-indigo-200 uppercase tracking-wide">10 Layers</span>
            </div>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={indiaIntelligence}
                onChange={e => setIndiaIntelligence(e.target.checked)}
                className="accent-indigo-500 w-4 h-4"
              />
              <span className="text-sm text-slate-300 font-medium">Enable</span>
            </label>
          </div>

          {indiaIntelligence && (
            <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs text-slate-400 pl-1">
              {[
                ['L1', 'Policy Intelligence', 'PLI, Budget, MNRE, RBI, Railways'],
                ['L2', 'Capacity Requirements', 'Policy targets → component needs'],
                ['L3', 'Capacity Gap Detector', 'Demand vs domestic capacity → investable gaps'],
                ['L4', 'Import Dependency', 'Sectors reliant on imports (wafers, ICs, CRGO)'],
                ['L5', 'Localization Opportunities', 'Import gap × PLI incentives'],
                ['L6', 'Supply Chain DB', 'Solar / Power / Electronics / EV / Railways / 5G'],
                ['L7', 'Beneficiary Discovery', 'Theme → Constraint → Supplier → Company'],
                ['L8', 'Tender Intelligence', 'GeM, SECI, NTPC, PowerGrid, Railways tenders'],
                ['L9', 'Order Book Pressure', 'Concall signals → supply bottleneck signals'],
                ['L10', 'Causal Chain Generator', 'Data Centers → Transformers → CG Power / ABB'],
              ].map(([layer, name, desc]) => (
                <div key={layer} className="flex gap-2">
                  <span className="text-indigo-500 font-mono font-bold w-6 shrink-0">{layer}</span>
                  <div>
                    <span className="text-slate-300 font-medium">{name}</span>
                    <span className="text-slate-500 ml-1 hidden xl:inline">— {desc}</span>
                  </div>
                </div>
              ))}
            </div>
          )}

          {!indiaIntelligence && (
            <p className="text-xs text-slate-500 pl-1">
              Disabled — only standard pipeline stages will run. Enable to inject policy targets,
              capacity gaps, import dependencies, order-book signals, and India causal chains
              before theme detection.
            </p>
          )}
        </div>
      )}

      {/* Options row */}
      <div className="flex flex-wrap gap-4">
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={skipNeo4j} onChange={e => setSkipNeo4j(e.target.checked)}
            className="accent-indigo-500" />
          <span className="text-sm text-slate-400">Skip Neo4j</span>
        </label>
        {stages.nlp && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-slate-400">NLP batch:</span>
            <input type="number" min={10} max={5000} step={100} value={nlpBatch}
              onChange={e => setNlpBatch(+e.target.value)}
              className="input w-24" />
          </div>
        )}
        {mode === 'replay' && (
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={resume} onChange={e => setResume(e.target.checked)}
              className="accent-indigo-500" />
            <span className="text-sm text-slate-400">Resume from last batch</span>
          </label>
        )}
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={forceReprocess} onChange={e => setForceReprocess(e.target.checked)}
            className="accent-amber-500" />
          <span className="text-sm text-amber-400 font-medium">
            🔄 Force re-process NLP
            <span className="text-slate-500 font-normal ml-1">(resets already-processed docs — use when signal types changed)</span>
          </span>
        </label>
      </div>

      {/* Pre-flight readiness check */}
      <ReadinessCheck country={country} startDate={startDate} endDate={endDate} />

      <div className="border-t border-slate-800 pt-4 flex items-center gap-3">
        <button
          onClick={runPipeline}
          disabled={running}
          className="btn-primary flex items-center gap-2"
        >
          {running ? <Spinner size="sm" /> : '▶'}
          {running ? 'Running…' : 'Run Pipeline'}
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
          {/* Terminal header */}
          <div className="flex items-center justify-between bg-slate-800 px-3 py-2 border-b border-slate-700">
            <div className="flex items-center gap-2">
              <span className="text-xs font-mono font-semibold text-slate-300">Pipeline Log</span>
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

          {/* Log lines */}
          <div
            ref={logRef}
            className="bg-slate-950 font-mono text-xs p-3 overflow-y-auto"
            style={{ maxHeight: '520px', minHeight: running && logs.length === 0 ? '80px' : undefined }}
          >
            {running && logs.length === 0 && (
              <span className="text-slate-500 animate-pulse">Waiting for pipeline output…</span>
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

      {/* Replay history */}
      {(replayHistory as unknown[]).length > 0 && (
        <div>
          <div className="border-t border-slate-800 pt-4 mb-3">
            <SectionHeader>Replay History</SectionHeader>
          </div>
          <div className="overflow-x-auto rounded-lg border border-slate-700">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-slate-800 border-b border-slate-700">
                  {['Batch', 'Docs', 'NLP', 'Themes', 'Causal', 'Duration (s)', 'Status'].map(h => (
                    <th key={h} className="px-3 py-2 text-left text-slate-400 font-semibold uppercase tracking-wide">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(replayHistory as Record<string, unknown>[]).map((r, i) => (
                  <tr key={i} className="border-b border-slate-800 hover:bg-slate-800/50">
                    <td className="px-3 py-2 text-slate-300">{String(r.replay_batch)}</td>
                    <td className="px-3 py-2 text-slate-300">{String(r.docs_ingested ?? 0)}</td>
                    <td className="px-3 py-2 text-slate-300">{String(r.docs_nlp ?? 0)}</td>
                    <td className="px-3 py-2 text-slate-300">{String(r.themes_detected ?? 0)}</td>
                    <td className="px-3 py-2 text-slate-300">{String(r.causal_score ?? 0)}</td>
                    <td className="px-3 py-2 text-slate-300">{String(r.duration_sec ?? 0)}</td>
                    <td className="px-3 py-2">
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                        r.status === 'success' ? 'bg-emerald-900/40 text-emerald-400' : 'bg-red-900/40 text-red-400'
                      }`}>{String(r.status)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
