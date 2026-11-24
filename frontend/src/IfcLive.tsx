import { useState } from 'react'
import type { Chunk, Element } from './IfcPilot'

export type LiveDiscipline = 'ventilation' | 'electrical'
export type LiveRun = {
  discipline: LiveDiscipline; resultCount: number; queryExecutionMs: number; loadMs: number
  runtimeInitializationMs: number; wallMs: number; structureReused: boolean; inputReused: boolean
  datasetReads: number; datasetUploads: number; indexBuilds: number; addedGpuBytes: number
  phaseTimingsMs: Record<string, number>
  pairs: [number, number][]
  scene: { elements: Element[]; chunks: Chunk[] } | null
}

export function IfcLive({ bundleId, onResult }: { bundleId: string; onResult: (result?: LiveRun) => void }) {
  const [history, setHistory] = useState<Omit<LiveRun, 'scene' | 'pairs'>[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const last = history.at(-1)

  async function run(discipline?: LiveDiscipline) {
    setBusy(true)
    setError('')
    const start = performance.now()
    try {
      const path = discipline ? '/api/ifc/' + bundleId + '/live/' + discipline : '/api/ifc/live/reset'
      const response = await fetch(path, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Live request failed.')
      if (discipline) {
        const result: LiveRun = { ...payload, discipline, wallMs: performance.now() - start }
        const { scene, pairs, ...metrics } = result
        setHistory(previous => [...previous, metrics])
        onResult(result)
      } else {
        setHistory([])
        onResult()
      }
    } catch (error) {
      setError((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return <section className="ifc-live" aria-label="Live GPU joins">
    <h2>Live GPU joins</h2>
    <div className="ifc-controls">
      <button disabled={busy} onClick={() => void run('ventilation')}>
        {last?.discipline === 'ventilation' ? 'Repeat structure × ventilation' : 'Run structure × ventilation'}
      </button>
      <button disabled={busy} onClick={() => void run('electrical')}>Run structure × electrical</button>
      <button disabled={busy} onClick={() => void run()}>Unload Sixty5</button>
    </div>
    {busy && <p role="status">Waiting for Pierce…</p>}
    {error && <p role="alert">{error}</p>}
    {last && <p role="status">
      Structure {last.structureReused ? 'BVH reused from GPU' : 'read, uploaded and indexed'}.
      {' '}{last.discipline === 'ventilation' ? 'Ventilation' : 'Electrical'} {last.inputReused ? 'BVH reused from GPU' : 'read, uploaded and indexed'}.
      {' '}{last.datasetReads} reads · {last.datasetUploads} uploads · {last.indexBuilds} BVH builds · {(last.addedGpuBytes / 2 ** 20).toFixed(2)} MiB added to GPU.
    </p>}
    {history.length > 0 && <div className="ifc-live-history"><table>
      <thead><tr><th>Join</th><th>Loaded</th><th>Crossings</th><th>Setup</th><th>Native query</th><th>Request</th></tr></thead>
      <tbody>{history.map((row, index) => <tr key={index}>
        <td>Structure × {row.discipline}</td>
        <td>{row.inputReused ? '—' : row.structureReused ? row.discipline : 'structure + ' + row.discipline}</td>
        <td>{row.resultCount}</td>
        <td>{(row.loadMs + row.runtimeInitializationMs).toFixed(2)} ms</td>
        <td title={Object.entries(row.phaseTimingsMs).map(([key, ms]) => key + ' ' + ms.toFixed(2) + ' ms').join(' · ')}>{row.queryExecutionMs.toFixed(2)} ms</td>
        <td>{row.wallMs.toFixed(2)} ms</td>
      </tr>)}</tbody>
    </table></div>}
  </section>
}
