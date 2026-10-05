import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

type Health = { ready: boolean; running: boolean; issues: string[] }
type Metrics = { overallTimeMs: number; queryTimeMs: number; resultCount: number }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, init)
  } catch {
    throw new Error('Cannot reach the backend. Start FastAPI on port 8000, then retry.')
  }
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(data?.detail ?? 'Cannot reach the backend. Start FastAPI on port 8000, then retry.')
  }
  if (!data) throw new Error('The backend returned an invalid response. Check the backend terminal.')
  return data as T
}

function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [running, setRunning] = useState(false)
  const [checking, setChecking] = useState(true)
  const [metrics, setMetrics] = useState<Metrics | null>(null)
  const [error, setError] = useState('')

  async function checkHealth() {
    setChecking(true)
    setError('')
    try { setHealth(await request<Health>('/api/health')) }
    catch (error) { setHealth(null); setError((error as Error).message) }
    finally { setChecking(false) }
  }

  useEffect(() => { void checkHealth() }, [])

  async function run() {
    setRunning(true)
    setMetrics(null)
    setError('')
    try { setMetrics(await request<Metrics>('/api/run', { method: 'POST' })) }
    catch (error) {
      setError((error as Error).message)
      try { setHealth(await request<Health>('/api/health')) }
      catch { setHealth(null) }
    }
    finally { setRunning(false) }
  }

  const status = running ? 'Query running' : checking ? 'Checking connection' : health?.running ? 'Another query running' : health?.ready ? 'Ready to run' : 'Setup required'
  return (
    <main>
      <header><a className="brand" href="/">pierce<span> / explorer</span></a><span className="local">LOCAL DEMO · WINDOWS</span></header>
      <section className="workload" aria-labelledby="workload-title">
        <div className="workload-heading"><h2 id="workload-title">Synthetic cube overlap</h2><span className={`status ${running ? 'active' : ''}`} role="status">{status}</span></div>
        <p>1,000 × 1,000 objects with selectivity 0.005. Expected result: 5,000 unique matching object pairs.</p>
        <div className="facts"><span><strong>1,000</strong> objects in collection A</span><span><strong>1,000</strong> objects in collection B</span><span><strong>0.005</strong> selectivity</span></div>
        <div className="actions">
          <button onClick={() => void run()} disabled={running || checking || !health?.ready || health.running}>{running ? 'Running Pierce…' : 'Run Pierce'}<span aria-hidden="true">↗</span></button>
        </div>
        {!checking && (!health?.ready || error || health.running) && <div className="diagnostics">
          {error && <p role="alert">{error}</p>}
          {health?.issues.map(issue => <p key={issue}>{issue}</p>)}
          {health?.running && !running && <p>Another query is running. Wait for it to finish, then retry.</p>}
          <button className="secondary" onClick={() => void checkHealth()} disabled={running}>Check connection again</button>
        </div>}
      </section>
      <section className="results" aria-label="Query results" aria-live="polite">
        <div className="metric-grid">
          <article><h3>Overall time</h3><p className="value">{metrics ? metrics.overallTimeMs.toFixed(3) : '—'}<span>ms</span></p><p>Loading, GPU setup, query, and cleanup inside Pierce.</p></article>
          <article><h3>Query time</h3><p className="value">{metrics ? metrics.queryTimeMs.toFixed(3) : '—'}<span>ms</span></p><p>Ray tracing, GPU deduplication, and result download.</p></article>
          <article><h3>Result count</h3><p className="value">{metrics ? metrics.resultCount.toLocaleString() : '—'}<span>{metrics?.resultCount === 1 ? 'pair' : 'pairs'}</span></p><p>Unique object pairs returned by the overlap query.</p></article>
        </div>
      </section>
      <footer><span>Executed locally. Measured by Pierce.</span><span>Preprocessing and process startup are excluded from timings.</span></footer>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
