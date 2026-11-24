import { useEffect, useMemo, useState } from 'react'
import { IfcScene } from './IfcScene'
import { IfcLive } from './IfcLive'
import type { LiveRun } from './IfcLive'
import './ifc.css'

export type Discipline = 'structure' | 'ventilation' | 'electrical'
type Point = [number, number, number]
export type Element = { id: number; globalId: string; class: string; name: string; bounds: [Point, Point] }
export type Chunk = { file: string; discipline: Discipline; vertices: number; triangles: number; ranges: { id: number; first: number; count: number }[] }
export type Selection = { elements: [Discipline, Element][]; rejected: boolean; fit: boolean }
type Pair = [number, number]
type Bundle = {
  id: string; source: string; attribution: string; predicate: string; region: [Point, Point] | null
  inputs: { file: string; storey: { name: string; elevation: number } }[]
  elements: Record<'structure' | 'ventilation', Element[]>
  floorCounts?: Record<'structure' | 'ventilation', number>
  exclusions: Record<'structure' | 'ventilation', { globalId: string; name: string; reason: string }[]>
  chunks: Chunk[]; pairCount: number; rejected: Pair[]; verified: boolean; demoReady: boolean
}
const disciplines = ['structure', 'ventilation'] as const
const labels = { structure: 'Structure', ventilation: 'Ventilation', electrical: 'Electrical' }
const allVisible = { structure: true, ventilation: true, electrical: true }
const pageSize = 50

async function get<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal })
  const body = await response.json()
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'IFC pilot data is unavailable.')
  return body as T
}

function name(element: Element) {
  return element.name || element.class
}

export function IfcPilot() {
  const [bundle, setBundle] = useState<Bundle>()
  const [error, setError] = useState('')
  const [pairError, setPairError] = useState('')
  const [page, setPage] = useState(0)
  const [pairs, setPairs] = useState<Pair[]>([])
  const [rejected, setRejected] = useState(false)
  const [active, setActive] = useState('')
  const [selection, setSelection] = useState<Selection>({ elements: [], rejected: false, fit: false })
  const [visible, setVisible] = useState(allVisible)
  const [reset, setReset] = useState(0)
  const [live, setLive] = useState<LiveRun>()
  const [electrical, setElectrical] = useState<LiveRun['scene']>(null)
  const [layerError, setLayerError] = useState('')
  const target = live?.discipline ?? 'ventilation'
  const targetElements = live?.scene?.elements ?? bundle?.elements.ventilation ?? []
  const chunks = useMemo(() => [...(bundle?.chunks ?? []), ...(electrical?.chunks ?? [])], [bundle, electrical])
  const total = live?.resultCount ?? bundle?.pairCount ?? 0
  useEffect(() => {
    const abort = new AbortController()
    get<Bundle>('/api/ifc/current', abort.signal).then(setBundle).catch(error => {
      if (!abort.signal.aborted) setError(error.message)
    })
    return () => abort.abort()
  }, [])
  useEffect(() => {
    if (!bundle) return
    const abort = new AbortController()
    get<LiveRun['scene']>('/api/ifc/' + bundle.id + '/electrical', abort.signal).then(scene => {
      if (!abort.signal.aborted) setElectrical(current => current ?? scene)
    }).catch(error => {
      if (!abort.signal.aborted) setLayerError(error.message)
    })
    return () => abort.abort()
  }, [bundle])
  useEffect(() => {
    if (!bundle || live) return
    const abort = new AbortController()
    setPairs([])
    setPairError('')
    get<{ pairs: Pair[] }>('/api/ifc/' + bundle.id + '/pairs?offset=' + page * pageSize + '&limit=' + pageSize,
      abort.signal).then(result => setPairs(result.pairs)).catch(error => {
      if (!abort.signal.aborted) setPairError(error.message)
    })
    return () => abort.abort()
  }, [bundle, page, live])

  function showLive(result?: LiveRun) {
    setLive(result)
    if (result?.scene) {
      const scene = result.scene
      setElectrical(previous => previous?.chunks[0]?.file === scene.chunks[0]?.file ? previous : scene)
      setLayerError('')
    }
    setPage(0)
    setPairError('')
    showRejected(false)
  }

  function selectPair(pair: Pair) {
    if (!bundle) return
    setActive(pair.join(':'))
    setVisible(previous => ({ ...previous, structure: true, [target]: true }))
    setSelection({
      elements: [['structure', bundle.elements.structure[pair[0]]], [target, targetElements[pair[1]]]],
      rejected, fit: true,
    })
  }

  function showRejected(next: boolean) {
    setRejected(next)
    setActive('')
    setSelection({ elements: [], rejected: false, fit: false })
  }

  return <main className="ifc-pilot">
    <header><a className="brand" href="/">pierce<span> / explorer</span></a><a href="/experiments/shared-bvh">Shared BVH experiment</a></header>
    <h1>Sixty5 IFC pilot</h1>
    {!bundle ? <p role="status">{error || 'Loading recorded experiment…'}</p> : <>
      <p>{bundle.inputs.map(input => input.storey.name + ' (' + input.storey.elevation.toFixed(2) + ' m)').join(' · ')} · {bundle.region ? 'Selected floor region' : 'Selected storeys'}</p>
      <p><strong>{bundle.pairCount.toLocaleString()} surface crossings</strong> · {bundle.verified ? 'Object-pair verification passed' : 'Verification pending'} · Recorded GPU run</p>
      <IfcLive bundleId={bundle.id} onResult={showLive} />
      <h2>{live ? 'Live' : 'Recorded'} structure × {labels[target].toLowerCase()} · {total} crossings</h2>
      {!bundle.demoReady && <p>This bundle lacks a crossing or rejected candidate for side-by-side inspection.</p>}
      <div className="ifc-layout">
        <section className="ifc-view" aria-label="Scene">
          <div className="ifc-controls">
            {(electrical ? (['structure', 'ventilation', 'electrical'] as const) : disciplines).map(key => <label key={key}><input type="checkbox" checked={visible[key]}
              onChange={event => setVisible({ ...visible, [key]: event.target.checked })} /> {labels[key]}</label>)}
            <button onClick={() => { setActive(''); setSelection({ elements: [], fit: false, rejected: false }); setReset(reset + 1) }}>Reset view</button>
          </div>
          {layerError && <p role="alert">Electrical layer: {layerError}</p>}
          <IfcScene bundleId={bundle.id} chunks={chunks} selection={selection} visible={visible} reset={reset}
            floorMin={bundle.elements.structure.reduce((low, element) => Math.min(low, element.bounds[0][2]), Infinity) - 0.1}
            floorMax={bundle.elements.structure.reduce((high, element) => Math.max(high, element.bounds[1][2]), -Infinity) + 0.1}
            onPick={(discipline, id) => {
              const element = discipline === 'electrical' ? electrical?.elements[id] : bundle.elements[discipline][id]
              if (!element) return
              setActive('')
              setSelection({ elements: [[discipline, element]], fit: false, rejected: false })
            }} />
          <small>View clipped to the structural floor height. The join uses whole elements.</small>
          <div aria-live="polite" className="ifc-selection">
            {selection.elements.map(([discipline, element]) => <div key={discipline}>
              <strong>{labels[discipline]} · {name(element)}</strong>
              <small>{element.class} · {element.globalId}</small>
            </div>)}
          </div>
        </section>
        <section className="ifc-results" aria-label="Pairs">
          <div className="ifc-controls">
            <button aria-pressed={!rejected} onClick={() => showRejected(false)}>Surface crossings</button>
            {target === 'ventilation' && <button aria-pressed={rejected} onClick={() => showRejected(true)}>Recorded rejected candidates</button>}
          </div>
          {pairError && <p role="alert">{pairError}</p>}
          {live && total === 0 && <p>No surface crossings in this run.</p>}
          <ul>
            {(rejected ? bundle.rejected : live ? live.pairs.slice(page * pageSize, (page + 1) * pageSize) : pairs).map(pair => <li key={pair.join(':')}>
              <button aria-pressed={active === pair.join(':')} onClick={() => selectPair(pair)}>
                {name(bundle.elements.structure[pair[0]])}<br />{name(targetElements[pair[1]])}
              </button>
            </li>)}
          </ul>
          {!rejected && total > pageSize && <div className="ifc-controls">
            <button disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
            <span>{page + 1} / {Math.ceil(total / pageSize)}</span>
            <button disabled={(page + 1) * pageSize >= total} onClick={() => setPage(page + 1)}>Next</button>
          </div>}
        </section>
      </div>
      <section className="ifc-audit" aria-label="Geometry audit">
        <h2>Geometry audit</h2>
        {disciplines.map(key => <details key={key}>
          <summary>{labels[key]} · {bundle.elements[key].length.toLocaleString()} elements · {bundle.chunks.filter(chunk => chunk.discipline === key).reduce((total, chunk) => total + chunk.triangles, 0).toLocaleString()} triangles · {bundle.exclusions[key].length} excluded</summary>
          {bundle.region && bundle.floorCounts && <p>{bundle.floorCounts[key] - bundle.elements[key].length} valid elements outside the selected region</p>}
          <ul>{bundle.exclusions[key].map(item => <li key={item.globalId}>{item.name || item.globalId} · {item.reason}</li>)}</ul>
        </details>)}
        <a href={bundle.source}>{bundle.attribution}</a>
      </section>
    </>}
  </main>
}
