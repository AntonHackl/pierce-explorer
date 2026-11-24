import { useState } from 'react'

type Timings = { median: Record<string, number>; filtered: Record<string, number> | null; retained: number; trials: Record<string, number>[] }
type Experiment = {
  directions: { triangles: number; pairs: number; estimatedPairs: number; setup: Record<string, number>; timings: { aToB: Timings; bToA: Timings } }[]
  refinement: { tiles: number; pairs: number; candidates: number; traceCalls: number; sharedSetup: number; parallelSetup: number; treeSetup: number; shared: Timings; parallel: Timings }
}
type Series = { label: string; value: number; color: string }
type ChartRow = { label: string; series: Series[] }
const blue = '#315db5'
const orange = '#b45a26'
const trace = '#633e93'
const ms = (value: number) => value.toFixed(3) + ' ms'

function download(name: string, contents: string, type: string) {
  const url = URL.createObjectURL(new Blob([contents], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function Chart({ id, title, caption, rows }: { id: string; title: string; caption: string; rows: ChartRow[] }) {
  const max = Math.max(0.001, ...rows.map(row => row.series.reduce((sum, item) => sum + item.value, 0)))
  const legend = [...new Map(rows.flatMap(row => row.series.map(item => [item.label, item] as const))).values()]
  const height = 142 + rows.length * 47 + Math.ceil(legend.length / 3) * 26
  return <figure className="experiment-figure">
    <svg id={id} xmlns="http://www.w3.org/2000/svg" viewBox={'0 0 780 ' + height} role="img" aria-label={title} fontFamily="system-ui, sans-serif" fill="#192943">
      <rect width="780" height={height} fill="#fff" />
      <text x="24" y="36" fontSize="22" fontWeight="700">{title}</text>
      <text x="24" y="64" fontSize="14">{caption}</text>
      {rows.map((row, index) => {
        const y = 92 + index * 47
        const total = row.series.reduce((sum, item) => sum + item.value, 0)
        let x = 225
        return <g key={row.label}>
          <text x="24" y={y + 16} fontSize="14">{row.label}</text>
          {row.series.map(item => {
            const width = item.value / max * 427
            const start = x
            x += width
            return <rect key={item.label} x={start} y={y} width={width} height="22" fill={item.color}><title>{item.label} · {ms(item.value)}</title></rect>
          })}
          <text x="666" y={y + 16} fontSize="14">{ms(total)}</text>
        </g>
      })}
      {legend.map((item, index) => {
        const x = 24 + index % 3 * 252
        const y = 105 + rows.length * 47 + Math.floor(index / 3) * 26
        return <g key={item.label}><rect x={x} y={y-11} width="12" height="12" fill={item.color} /><text x={x+19} y={y} fontSize="13">{item.label}</text></g>
      })}
    </svg>
    <button className="secondary" onClick={() => {
      const svg = document.getElementById(id)
      if (svg) download(id + '.svg', new XMLSerializer().serializeToString(svg), 'image/svg+xml')
    }}>Download SVG</button>
  </figure>
}

export function Experiments() {
  const [preset, setPreset] = useState('live')
  const [result, setResult] = useState<Experiment>()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  async function run() {
    setPending(true)
    setError('')
    setResult(undefined)
    try {
      const response = await fetch('/api/experiments/shared-bvh', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preset }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail?.message ?? data.detail ?? 'Experiment failed.')
      setResult(data)
    } catch (e) { setError((e as Error).message) }
    finally { setPending(false) }
  }
  const r = result?.refinement
  const phases = (summary: Timings, fields: [string, string, string][]) => {
    const values = fields.map(([key, label, color]) => ({ label, color, value: summary.median[key] }))
    const remainder = summary.median.total - values.reduce((sum, item) => sum + item.value, 0)
    return [...values, { label: 'Other', color: '#92a2b6', value: Math.max(0, remainder) }]
  }
  return <main>
    <header><a className="brand" href="/">pierce<span> / explorer</span></a><nav className="experiment-links" aria-label="Experiments"><a href="/">Cube workloads</a><a href="/experiments/memory">Memory experiment</a></nav></header>
    <section className="workload">
      <h1>Shared BVH experiment</h1>
      <p>Measure how trace time changes as B gets finer, then compare Pierce with R-tree filtering and per-object OptiX checks on tiled nuclei and vessels.</p>
      <div className="actions">
        <label>Nuclei and vessels <select value={preset} disabled={pending} onChange={e => setPreset(e.target.value)}>
          <option value="live">Live · 64 tiles</option><option value="large">Large · 192 tiles, about 4 GiB</option>
        </select></label>
        <button onClick={() => void run()} disabled={pending}>Run experiment</button>
      </div>
      <p role="status">{pending ? 'Building indexes, warming up, and measuring both experiments…' : result ? 'Pair verification passed.' : ''}</p>
      {error && <p role="alert">{error}</p>}
    </section>
    {result && r && <section aria-label="Experiment results">
      <Chart id="directional-trace" title="Trace time as the target mesh gets finer"
        caption="Median warm trace time · A fixed · 5,000 matching pairs at every level"
        rows={result.directions.flatMap(level => [
          { label: level.triangles.toLocaleString() + ' tris · A → B', series: [{ label: 'A → B', color: blue, value: level.timings.aToB.median.trace }] },
          { label: level.triangles.toLocaleString() + ' tris · B → A', series: [{ label: 'B → A', color: orange, value: level.timings.bToA.median.trace }] },
        ])} />
      <Chart id="refinement-breakdown" title="Refinement on nuclei and vessels"
        caption={r.tiles + ' tiles · ' + r.candidates.toLocaleString() + ' bounding-box candidates · ' + r.pairs.toLocaleString() + ' verified pairs'}
        rows={[
          { label: 'Pierce shared BVH', series: phases(r.shared, [['estimate','Estimate',blue],['trace','OptiX trace and wait',trace],['compact','Compact','#23836b'],['download','Download','#44788f']]) },
          { label: 'Object pairs · 16 workers', series: phases(r.parallel, [['filter','Filter and stage',orange],['upload','Upload','#aec4e4'],['trace','OptiX trace and wait',trace],['download','Download','#44788f'],['host','Host collection','#9a5468']]) },
        ]} />
      <p>Measured totals were {ms(r.shared.median.total)} for Pierce and {ms(r.parallel.median.total)} for the object-pair path. Phase medians are computed independently. The object-pair path issued {r.traceCalls.toLocaleString()} trace calls.</p>
      <p>Setup took {ms(r.sharedSetup)} for Pierce load and indexing and {ms(r.parallelSetup)} for object-pair preparation from loaded geometry, including {ms(r.treeSetup)} for the R-tree.</p>
      <details><summary>Setup, raw trials, and filtered medians</summary>
        <p>The charts use all 12 warm measured trials. Filtered medians exclude high-side total or trace samples beyond the median plus three scaled median absolute deviations, with a 1 ms minimum scale.</p>
        <button className="secondary" onClick={() => download('shared-bvh-trials.json', JSON.stringify(result, null, 2), 'application/json')}>Download trial data</button>
        <pre className="experiment-data">{JSON.stringify(result, null, 2)}</pre>
      </details>
    </section>}
  </main>
}
