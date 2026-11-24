import { useState } from 'react'
import workloads from './memory-experiment.json'

const mib = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' MiB'
function bytes(value: number): string {
  if (value < 0) return '−' + bytes(-value)
  if (value >= 2 ** 20) return mib(value / 2 ** 20)
  if (value >= 1024) return (value / 1024).toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' KiB'
  return value.toLocaleString() + ' B'
}

type Component = {
  name: string
  location: string
  size?: number
  method: string
}

function Components({ title, rows }: { title: string; rows: Component[] }) {
  return <article className="memory-components">
    <h2>{title}</h2>
    <div className="memory-table-scroll">
      <table>
        <thead><tr><th scope="col">Component</th><th scope="col">Memory</th><th scope="col">Size</th><th scope="col">How accounted for</th></tr></thead>
        <tbody>{rows.map(row => <tr key={row.name}>
          <th scope="row">{row.name}</th><td>{row.location}</td>
          <td className="memory-number">{row.size === undefined ? 'Unmeasured' : bytes(row.size)}</td>
          <td>{row.method}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </article>
}

export function MemoryExperiment() {
  const [selection, setSelection] = useState(0)
  const [live, setLive] = useState<Partial<Record<string, (typeof workloads)[number]>>>({})
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const selected = workloads[selection]
  const workload = live[selected.key] ?? selected

  async function run() {
    setPending(true)
    setError('')
    try {
      const response = await fetch('/api/experiments/memory', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workload: selected.key }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail?.message ?? data.detail ?? 'Memory experiment failed.')
      setLive(previous => ({ ...previous, [selected.key]: data }))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setPending(false)
    }
  }
  const a: Record<string, number | undefined> & typeof workload.allocations = workload.allocations
  const mesh = a.positions + a.indices
  return <main>
    <header><a className="brand" href="/">pierce<span> / explorer</span></a>
      <nav className="experiment-links" aria-label="Experiments"><a href="/">Cube workloads</a><a href="/experiments/shared-bvh">Shared BVH experiment</a></nav>
    </header>
    <section className="workload" aria-labelledby="memory-experiment-title">
      <h1 id="memory-experiment-title">Memory experiment</h1>
      <p>Compare peak memory for Pierce and TDBase under WSL. Each run launches five fresh processes per engine and verifies every returned pair.</p>
      <div className="actions"><div><label htmlFor="memory-workload">Workload </label>
      <select id="memory-workload" disabled={pending} value={selection} onChange={event => setSelection(Number(event.target.value))}>
        {workloads.map((item, index) => <option value={index} key={item.key}>{item.title}</option>)}
      </select></div>
      <button disabled={pending} onClick={() => void run()}>Run experiment</button></div>
      <p role="status">{pending ? 'Running and verifying both engines in WSL…' : live[selected.key] ? 'Latest run · pair verification passed.' : 'Showing recorded results.'}</p>
      {error && <p role="alert">{error}</p>}
      <div className="facts"><span><strong>{workload.triangles.toLocaleString()}</strong> triangles</span>
        <span><strong>{workload.pairs.toLocaleString()}</strong> verified pairs</span></div>
    </section>
    <section className="memory-panel" aria-labelledby="memory-peaks">
      <h2 id="memory-peaks">Whole-run peaks</h2>
      <p>Median of five runs. Host uses Linux peak RSS. GPU uses the sampled device-wide increase above idle.</p>
      <div className="memory-table-scroll">
        <table>
          <thead><tr><th scope="col">Reference or engine</th><th scope="col">Host RAM</th><th scope="col">GPU VRAM</th></tr></thead>
          <tbody>
            <tr><th scope="row">Packed mesh reference, one copy</th><td colSpan={2}>{bytes(mesh)}</td></tr>
            <tr><th scope="row">Pierce</th><td>{mib(workload.pierce.cpu_peak_mib)}</td><td>{mib(workload.pierce.gpu_sampled_peak_above_idle_mib)}</td></tr>
            <tr><th scope="row">TDBase</th><td>{mib(workload.tdbase.cpu_peak_mib)}</td><td>{mib(workload.tdbase.gpu_sampled_peak_above_idle_mib)}</td></tr>
          </tbody>
        </table>
      </div>
    </section>
    <Components title="Pierce components" rows={[
      { name: 'Vertex positions', location: 'Host + GPU', size: a.positions, method: 'Packed payload per copy · 12 bytes per vertex' },
      { name: 'Triangle indices', location: 'Host + GPU', size: a.indices, method: 'Packed payload per copy · 12 bytes per triangle' },
      { name: 'Triangle object IDs', location: 'Host + GPU', size: a.triangleIds, method: 'Packed payload per copy · 4 bytes per triangle' },
      { name: 'Edge endpoints', location: 'Host + GPU', size: a.edgeEndpoints, method: 'Packed payload per copy · 24 bytes per edge' },
      { name: 'Edge object IDs', location: 'Host + GPU', size: a.edgeIds, method: 'Packed payload per copy · 4 bytes per edge' },
      { name: 'Selectivity grid entries', location: 'Host', size: a.grid, method: 'Sparse-cell payload from the prepared inputs' },
      { name: 'Pair hash table', location: 'GPU', size: a.hashTable, method: 'Allocated bytes in the query log' },
      { name: 'Compacted result buffer', location: 'GPU', size: a.resultBuffer, method: 'Capacity in the query log' },
      { name: 'Downloaded pairs', location: 'Host', size: a.hostResults, method: 'Pair payload in the query log, before container and CSV overhead' },
      { name: 'BVH output · A', location: 'GPU', size: a.bvhA, method: 'OptiX output allocation' },
      { name: 'BVH output · B', location: 'GPU', size: a.bvhB, method: 'OptiX output allocation' },
      { name: 'Build scratch · A', location: 'GPU', size: a.buildScratchA, method: 'Temporary allocation, freed before building B' },
      { name: 'Build scratch · B', location: 'GPU', size: a.buildScratchB, method: 'Temporary allocation, freed after the build' },
      { name: 'Selectivity estimation scratch', location: 'GPU', size: a.estimatorGpu, method: 'Two matched cell buffers and one result buffer' },
      { name: 'Matched-cell vectors', location: 'Host', size: a.estimatorHost, method: 'Allocated vector capacity during estimation' },
      { name: 'Launcher buffers', location: 'GPU', size: a.launcherGpu, method: 'Shader binding table and launch parameters' },
      { name: 'OptiX context initialization', location: 'GPU', size: a.contextGpuDelta, method: 'Device-memory delta after CUDA is initialized' },
      { name: 'Pipeline and launcher initialization', location: 'GPU', size: a.launcherGpuDelta, method: 'Device-memory delta, includes launcher buffers above' },
      { name: 'Host lookup table and runtime internals', location: 'Host + GPU', method: 'Included where captured in whole-run peaks' },
    ]} />
    <Components title="TDBase components" rows={[
      { name: 'Reserved refinement workspace', location: 'GPU', size: a.workspace, method: 'Allocation recorded at GPU initialization' },
      { name: 'Compressed objects and levels of detail', location: 'Host', method: 'Resident bytes were not recorded individually' },
      { name: 'Decoded geometry and voxel structures', location: 'Host', method: 'Resident bytes were not recorded individually' },
      { name: 'Spatial index, candidates, and refinement staging', location: 'Host', method: 'Resident bytes were not recorded individually' },
      { name: 'Result collection and runtime', location: 'Host + GPU', method: 'Included where captured in whole-run peaks' },
    ]} />
    <footer><span>Component sizes are allocations or packed payloads. They cannot be added to reconstruct a peak because buffers have different lifetimes. “Host + GPU” gives the size per copy.</span></footer>
  </main>
}
