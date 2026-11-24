import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'
import { Experiments } from './Experiments'

type WorkloadId = 'small' | 'large'
type Operation = 'load' | 'run' | 'unload'
type LoadInfo = { datasetLoadMs: number; runtimeInitializationMs: number }
type QueryPhase = 'estimation' | 'trace' | 'compaction' | 'download'
type Metrics = {
  workloadId: WorkloadId; queryExecutionMs: number; resultCount: number; completedQueries: number
  actualSelectivity: number; rawHitCount: number; estimatedPairCount: number
  hashTableBytes: number; resultBufferBytes: number
  phaseTimingsMs: Record<QueryPhase, number>
}
type Workload = {
  workloadId: WorkloadId; name: string; countA: number; countB: number
  selectivityKind: 'exact' | 'target'; selectivity: number; expectedResultCount: number | null
  prepared: boolean; issues: string[]; loaded: boolean; loadInfo: LoadInfo | null
  completedQueries: number; datasetGpuBytes: number; lastResult: Metrics | null; lastError: string | null
}
type Memory = { device: string; totalBytes: number; freeBytes: number; reservedBytes: number }
type Health = { ready: boolean; running: boolean; issues: string[]; workerAvailable: boolean; operation: string | null; operationWorkload: WorkloadId | null; memory: Memory | null; lastError: string | null }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try { response = await fetch(path, init) }
  catch { throw new Error('Cannot reach the backend. Start FastAPI on port 8000, then retry.') }
  const data = await response.json().catch(() => null)
  if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : data?.detail?.message ?? 'The request could not be completed.')
  if (!data) throw new Error('The backend returned an invalid response.')
  return data as T
}
const mib = (bytes: number) => `${(bytes / 2 ** 20).toFixed(1)} MiB`
const gib = (bytes: number) => `${(bytes / 2 ** 30).toFixed(2)} GiB`
const queryPhases: { key: QueryPhase; label: string }[] = [
  { key: 'estimation', label: 'Selectivity estimation' },
  { key: 'trace', label: 'Trace and insert' },
  { key: 'compaction', label: 'Hash compaction' },
  { key: 'download', label: 'Download' },
]

function App() {
  const [selected, setSelected] = useState<WorkloadId>('small')
  const [workloads, setWorkloads] = useState<Workload[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [pending, setPending] = useState<{ kind: Operation; workloadId: WorkloadId } | null>(null)
  const [checking, setChecking] = useState(true)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    if (pending) return
    let cancelled = false
    let inFlight = false
    async function check() {
      if (inFlight) return
      inFlight = true
      try {
        const [nextHealth, nextWorkloads] = await Promise.all([request<Health>('/api/health'), request<Workload[]>('/api/workloads')])
        if (!cancelled) { setHealth(nextHealth); setWorkloads(nextWorkloads) }
      } catch (error) {
        if (!cancelled) { setHealth(null); setWorkloads([]); setError((error as Error).message) }
      } finally { inFlight = false; if (!cancelled) setChecking(false) }
    }
    void check()
    const interval = setInterval(() => void check(), 2000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [pending, refresh])

  async function operate(kind: Operation) {
    const workloadId = selected
    setPending({ kind, workloadId })
    setError('')
    try {
      await request(`/api/${kind}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workloadId }) })
    } catch (error) { setError((error as Error).message) }
    finally {
      try {
        const [nextHealth, nextWorkloads] = await Promise.all([request<Health>('/api/health'), request<Workload[]>('/api/workloads')])
        setHealth(nextHealth); setWorkloads(nextWorkloads)
      } catch { setHealth(null); setWorkloads([]) }
      setPending(null)
    }
  }

  const workload = workloads.find(item => item.workloadId === selected)
  const busy = !!pending || !!health?.running
  const activeWorkload = pending?.workloadId ?? health?.operationWorkload
  const operation = pending?.kind ?? health?.operation
  const failure = error || workload?.lastError || health?.lastError
  const status = busy ? `${activeWorkload === 'large' ? 'Large' : 'Small'}: ${operation === 'load' ? 'loading' : operation === 'unload' ? 'unloading' : 'query running'}`
    : checking ? 'Checking connection' : failure ? 'Operation failed'
    : !health?.ready ? 'Setup required' : workload?.loaded ? 'Loaded on GPU' : workload?.prepared ? 'Unloaded' : 'Preparation required'
  const metrics = workload?.lastResult
  const loadInfo = workload?.loadInfo
  const memory = health?.memory
  return (
    <main>
      <header><a className="brand" href="/">pierce<span> / explorer</span></a><a href="/experiments/shared-bvh">Shared BVH experiment</a></header>
      <nav className="workload-selector" aria-label="Cube workload">
        {(['small', 'large'] as WorkloadId[]).map(id => {
          const item = workloads.find(w => w.workloadId === id)
          return <button key={id} aria-pressed={selected === id} onClick={() => setSelected(id)}>
            {id === 'small' ? 'Small · 1k x 1k' : 'Large · 200k x 200k'}<small>{item?.loaded ? 'Loaded on GPU' : item?.prepared ? 'Unloaded' : 'Not prepared'}</small>
          </button>
        })}
      </nav>
      <section className="workload" aria-labelledby="workload-title">
        <div className="workload-heading"><h2 id="workload-title">{selected === 'small' ? 'Fixed cube overlap' : 'Random cube overlap'}</h2><span className={`status ${busy ? 'active' : ''}`} role="status">{status}</span></div>
        <p>Load either workload independently. Switching between loaded workloads reuses their GPU data and indexes.</p>
        <div className="facts"><span><strong>{workload?.countA.toLocaleString() ?? '—'}</strong> objects in A</span><span><strong>{workload?.countB.toLocaleString() ?? '—'}</strong> objects in B</span><span><strong>{workload?.selectivity.toFixed(selected === 'small' ? 3 : 6) ?? '—'}</strong> {selected === 'small' ? 'exact' : 'target'} selectivity</span></div>
        <p>{selected === 'small' ? 'Deterministic test case: exactly 5,000 unique matching pairs.' : 'Random cubes with sizes 1–2 and seed 42, generated by Pierce. Actual selectivity is measured by the query.'}</p>
        <div className="actions">
          <button onClick={() => void operate('load')} disabled={busy || checking || !health?.ready || !workload?.prepared || workload.loaded}>Load workload</button>
          <button onClick={() => void operate('run')} disabled={busy || checking || !workload?.loaded}>Run query<span aria-hidden="true">↗</span></button>
          <button className="secondary" onClick={() => void operate('unload')} disabled={busy || checking || !workload?.loaded}>Unload workload</button>
        </div>
        <p aria-live="polite">Completed queries since loading: <strong>{workload?.completedQueries ?? 0}</strong>{metrics && <> · Actual selectivity: <strong>{metrics.actualSelectivity.toFixed(8)}</strong></>}</p>
        {(failure || !health?.ready || !workload?.prepared) && !checking && <div className="diagnostics">
          {failure && <p role="alert">{failure}</p>}
          {health?.issues.map(issue => <p key={issue}>{issue}</p>)}
          {workload?.issues.map(issue => <p key={issue}>{issue}</p>)}
          <button className="secondary" onClick={() => { setError(''); setChecking(true); setRefresh(value => value + 1) }} disabled={busy}>Check connection again</button>
        </div>}
      </section>
      <section className="results" aria-label="Query results" aria-live="polite">
        <div className="metric-grid">
          <article><h3>Dataset load time</h3><p className="value">{loadInfo ? loadInfo.datasetLoadMs.toFixed(3) : '—'}<span>ms</span></p><p>Reading, uploading, and building both indexes. Shared runtime initialization: {loadInfo ? `${loadInfo.runtimeInitializationMs.toFixed(3)} ms` : '—'} (once per worker).</p></article>
          <article><h3>Query execution time</h3><p className="value">{metrics ? metrics.queryExecutionMs.toFixed(3) : '—'}<span>ms</span></p><p>Complete native query, including selectivity estimation and hash-based deduplication.</p></article>
          <article><h3>Unique pairs</h3><p className="value">{metrics ? metrics.resultCount.toLocaleString() : '—'}<span>pairs</span></p><p><strong>{metrics?.estimatedPairCount.toLocaleString() ?? '—'}</strong> estimated pairs · <strong>{metrics?.rawHitCount.toLocaleString() ?? '—'}</strong> raw hits</p></article>
        </div>
        <article className="phase-panel" aria-labelledby="query-phases-title">
          <h3 id="query-phases-title">Query phase breakdown</h3>
          <dl>
            {queryPhases.map(({ key, label }) => {
              const duration = metrics?.phaseTimingsMs?.[key]
              const share = duration !== undefined && metrics && metrics.queryExecutionMs > 0
                ? Math.min(100, 100 * duration / metrics.queryExecutionMs) : 0
              return <div className="phase-row" key={key}>
                <dt>{label}</dt>
                <dd className="phase-track" aria-hidden="true"><span style={{ width: share + '%' }} /></dd>
                <dd className="phase-time">{duration !== undefined ? duration.toFixed(3) + ' ms' : '—'}</dd>
              </div>
            })}
          </dl>
          <p>Bars show each phase’s share of total query time. Deduplication happens during insertion, then compaction collects the unique pairs.</p>
        </article>
      </section>
      <section className="memory-panel" aria-label="GPU memory">
        <h3>GPU memory · latest operation snapshot</h3>
        <p>{memory ? `${memory.device} · ${gib(memory.freeBytes)} free / ${gib(memory.totalBytes)} total · ${mib(memory.reservedBytes)} reserved as headroom` : 'Load a workload to observe GPU memory.'}</p>
        <div className="memory-facts">{workloads.map(item => <span key={item.workloadId}>{item.workloadId === 'small' ? 'Small' : 'Large'} resident buffers: <strong>{mib(item.datasetGpuBytes)}</strong></span>)}</div>
        {metrics && <p>Hash table: {mib(metrics.hashTableBytes)} · Result buffer: {mib(metrics.resultBufferBytes)}</p>}
        <p>Owned buffer sizes exclude driver and internal runtime allocations. Free memory is measured after each operation.</p>
      </section>
      <footer><span>Executed locally. Measured by Pierce.</span><span>GPU data stays loaded until explicitly unloaded. Native timings exclude preprocessing, process startup, and web communication. No hidden warmups.</span></footer>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(<StrictMode>{window.location.pathname === "/experiments/shared-bvh" ? <Experiments /> : <App />}</StrictMode>)
