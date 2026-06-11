import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

export default api

// ─── KPIs / Config ───────────────────────────────────────────────────────────
export const fetchKPIs = (country: string) =>
  api.get('/kpis', { params: { country } }).then(r => r.data)

export const fetchConfigInfo = () =>
  api.get('/config/info').then(r => r.data)

// ─── Themes ──────────────────────────────────────────────────────────────────
export const fetchThemes = (country: string, params?: Record<string, unknown>) =>
  api.get('/themes', { params: { country, ...params } }).then(r => r.data)

export const fetchRanking = (country: string) =>
  api.get('/themes/ranking', { params: { country } }).then(r => r.data)

export const fetchShortlisted = (country: string, min_quarters: number) =>
  api.get('/themes/shortlisted', { params: { country, min_quarters } }).then(r => r.data)

export const fetchBeneficiaries = (themeId: number, as_of?: string) =>
  api.get(`/themes/${themeId}/beneficiaries`, { params: { as_of } }).then(r => r.data)

export const fetchSnapshots = (themeId: number, from_date?: string, to_date?: string) =>
  api.get(`/themes/${themeId}/snapshots`, { params: { from_date, to_date } }).then(r => r.data)

export const fetchQuarterly = (themeId: number, as_of?: string) =>
  api.get(`/themes/${themeId}/quarterly`, { params: { as_of } }).then(r => r.data)

export const fetchSourceCompanies = (slug: string, as_of?: string, from_date?: string) =>
  api.get(`/themes/${slug}/source-companies`, { params: { as_of, from_date } }).then(r => r.data)

export const fetchEvidence = (slug: string, as_of?: string, from_date?: string) =>
  api.get(`/themes/${slug}/evidence`, { params: { as_of, from_date } }).then(r => r.data)

export const fetchMacroContext = (slug: string, as_of?: string) =>
  api.get(`/themes/${slug}/macro-context`, { params: { as_of } }).then(r => r.data)

// ─── Canonical Reviews ────────────────────────────────────────────────────────
export const fetchPendingCanonical = () =>
  api.get('/canonical/pending').then(r => r.data)

export const approveCanonical = (approvals: Record<string, string>) =>
  api.post('/canonical/approve', { approvals }).then(r => r.data)

export const dismissCanonical = (clusterId: string) =>
  api.post(`/canonical/dismiss/${clusterId}`).then(r => r.data)

export const canonicalAIResolve = (prompt: string) =>
  api.post('/canonical/ai-resolve', { prompt }).then(r => r.data)

// ─── Causal Chains / Contradictions ──────────────────────────────────────────
export const fetchCausalChains = (country: string, params?: { as_of?: string; from_date?: string }) =>
  api.get('/causal-chains', { params: { country, ...params } }).then(r => r.data)

export const fetchIndiaChainBeneficiaries = (as_of?: string, min_conviction?: number) =>
  api.get('/india/chain-beneficiaries', { params: { as_of, min_conviction } }).then(r => r.data)

export const fetchContradictions = (country: string) =>
  api.get('/contradictions', { params: { country } }).then(r => r.data)

// ─── Replay History ───────────────────────────────────────────────────────────
export const fetchReplayHistory = () =>
  api.get('/replay-history').then(r => r.data)

// ─── Filings ─────────────────────────────────────────────────────────────────
export const fetchFilings = (params: Record<string, unknown>) =>
  api.get('/filings', { params }).then(r => r.data)

export const fetchDocSignals = (docId: number) =>
  api.get(`/filings/${docId}/signals`).then(r => r.data)

export const fetchDocThemes = (docId: number) =>
  api.get(`/filings/${docId}/themes`).then(r => r.data)

// ─── Company Explorer ─────────────────────────────────────────────────────────
export const searchCompanies = (q: string, country: string) =>
  api.get('/company/search', { params: { q, country } }).then(r => r.data)

export const fetchCompanyProfile = (ticker: string, country: string, as_of?: string) =>
  api.get(`/company/${ticker}/profile`, { params: { country, as_of } }).then(r => r.data)

export const fetchCompanyTimeline = (ticker: string, country: string, from_date?: string, to_date?: string) =>
  api.get(`/company/${ticker}/timeline`, { params: { country, from_date, to_date } }).then(r => r.data)

export const fetchCompanyThemes = (ticker: string, country: string, as_of?: string) =>
  api.get(`/company/${ticker}/themes`, { params: { country, as_of } }).then(r => r.data)

// ─── Macro ────────────────────────────────────────────────────────────────────
export const fetchMacroSeries = (series_id: string, from_date?: string, to_date?: string, country?: string) =>
  api.get('/macro/series', { params: { series_id, from_date, to_date, country } }).then(r => r.data)

export const fetchCommodity = (commodity_id: string, from_date?: string, to_date?: string) =>
  api.get('/macro/commodity', { params: { commodity_id, from_date, to_date } }).then(r => r.data)

export const fetchMacroEvents = (as_of?: string, since_days?: number) =>
  api.get('/macro/events', { params: { as_of, since_days } }).then(r => r.data)

export const fetchPolicyEvents = (params: Record<string, unknown>) =>
  api.get('/macro/policy-events', { params }).then(r => r.data)

export const runMacroFetch = (body: Record<string, unknown>) =>
  api.post('/macro/fetch', body).then(r => r.data)

// ─── Rankings ─────────────────────────────────────────────────────────────────
export const runRankings = (body: Record<string, unknown>) =>
  api.post('/rankings/run', body).then(r => r.data)

// ─── AI Analysis ──────────────────────────────────────────────────────────────
export const runAIAnalysis = (body: Record<string, unknown>) =>
  api.post('/ai/analyze', body).then(r => r.data)

export const fetchAICache = (country: string) =>
  api.get('/ai/cache', { params: { country } }).then(r => r.data)
