import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import type { Chunk, Discipline, Element, Selection } from './IfcPilot'
import { findCrossingPoints } from './ifcCrossing'

type LoadedChunk = { descriptor: Chunk; mesh: THREE.Mesh<THREE.BufferGeometry, THREE.MeshLambertMaterial> }
type Props = {
  bundleId: string
  chunks: Chunk[]
  floorMin: number
  floorMax: number
  selection: Selection
  visible: Record<Discipline, boolean>
  reset: number
  onPick: (discipline: Discipline, id: number) => void
}

const palette = { structure: 0x889ab1, ventilation: 0x38b9ad, electrical: 0x8f93f5 }
const selectedPalette = { structure: 0xffac4b, ventilation: 0xfff07e, electrical: 0xdac5ff }

function crossingMarkers(points: THREE.Vector3[]) {
  const geometry = new THREE.BufferGeometry().setFromPoints(points)
  const markers = new THREE.Points(geometry, new THREE.PointsMaterial({
    color: 0x49f6bc, size: 7, sizeAttenuation: false, depthTest: false, depthWrite: false,
    transparent: true,
  }))
  markers.renderOrder = 3
  return markers
}

function box(element: Element) {
  return new THREE.Box3(new THREE.Vector3(...element.bounds[0]), new THREE.Vector3(...element.bounds[1]))
}

function elementAt(ranges: Chunk['ranges'], face: number) {
  let low = 0
  let high = ranges.length
  while (low < high) {
    const middle = (low + high) >>> 1
    const range = ranges[middle]
    if (face < range.first) high = middle
    else if (face >= range.first + range.count) low = middle + 1
    else return range.id
  }
}

async function loadChunk(bundleId: string, descriptor: Chunk, material: THREE.MeshLambertMaterial, signal: AbortSignal): Promise<LoadedChunk> {
  const response = await fetch('/api/ifc/' + bundleId + '/mesh/' + descriptor.file, { signal })
  if (!response.ok) throw new Error('Unable to load scene geometry.')
  const buffer = await response.arrayBuffer()
  const positionBytes = descriptor.vertices * 12
  if (buffer.byteLength !== positionBytes + descriptor.triangles * 12) throw new Error('Invalid scene buffer size.')
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(buffer, 0, descriptor.vertices * 3), 3))
  geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(buffer, positionBytes), 1))
  geometry.computeVertexNormals()
  geometry.computeBoundingBox()
  geometry.computeBoundingSphere()
  return { descriptor, mesh: new THREE.Mesh(geometry, material) }
}

function clearOverlay(overlay: THREE.Group) {
  for (const child of [...overlay.children]) {
    if (child instanceof THREE.Mesh || child instanceof THREE.LineSegments || child instanceof THREE.Points) {
      child.geometry.dispose()
      child.material.dispose()
    }
    child.removeFromParent()
  }
}

export function IfcScene(props: Props) {
  const { bundleId, chunks, visible, selection, reset, floorMin, floorMax } = props
  const target = useRef<HTMLDivElement>(null)
  const current = useRef(props)
  current.current = props
  const previousSelection = useRef(selection)
  const sceneTools = useRef<{ update: (fit: boolean) => void }>(null)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('Loading geometry…')
  const [crossingHint, setCrossingHint] = useState('')

  useEffect(() => {
    const host = target.current
    if (!host) return
    const abort = new AbortController()
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true })
    } catch {
      setError('WebGL 2 is unavailable. The pair list is still available.')
      return
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setClearColor(0x111d27)
    renderer.clippingPlanes = [
      new THREE.Plane(new THREE.Vector3(0, 0, 1), -floorMin),
      new THREE.Plane(new THREE.Vector3(0, 0, -1), floorMax),
    ]
    host.appendChild(renderer.domElement)
    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 5000)
    camera.up.set(0, 0, 1)
    const controls = new OrbitControls(camera, renderer.domElement)
    scene.add(new THREE.AmbientLight(0xffffff, 1.2))
    const light = new THREE.DirectionalLight(0xffffff, 2)
    light.position.set(0.5, -1, 2)
    scene.add(light)
    const overlay = new THREE.Group()
    scene.add(overlay)
    const loaded: LoadedChunk[] = []
    const materials = {
      structure: new THREE.MeshLambertMaterial({ color: palette.structure }),
      ventilation: new THREE.MeshLambertMaterial({ color: palette.ventilation }),
      electrical: new THREE.MeshLambertMaterial({ color: palette.electrical }),
    }
    const sceneBounds = new THREE.Box3()
    let frame = 0
    let disposed = false
    const invalidate = () => {
      if (disposed || frame) return
      frame = requestAnimationFrame(() => {
        frame = 0
        renderer.render(scene, camera)
        const info = renderer.info.render
        setStatus(info.triangles.toLocaleString() + ' triangles · ' + info.calls + ' draw calls')
      })
    }
    const fitBox = (bounds: THREE.Box3, anchor?: THREE.Vector3) => {
      if (bounds.isEmpty()) return
      const center = anchor ?? bounds.getCenter(new THREE.Vector3())
      const radius = Math.max(Math.hypot(
        Math.max(center.x - bounds.min.x, bounds.max.x - center.x),
        Math.max(center.y - bounds.min.y, bounds.max.y - center.y),
        Math.max(center.z - bounds.min.z, bounds.max.z - center.z),
      ), 0.2)
      const vertical = THREE.MathUtils.degToRad(camera.fov) / 2
      const halfFov = Math.min(vertical, Math.atan(Math.tan(vertical) * camera.aspect))
      controls.target.copy(center)
      camera.position.copy(center).add(new THREE.Vector3(1, -1.3, 0.9).normalize().multiplyScalar(radius / Math.sin(halfFov) * 1.15))
      controls.update()
    }
    const update = (fit: boolean) => {
      const next = current.current
      const crossingSelected = next.selection.elements.length === 2 && !next.selection.rejected
      for (const material of Object.values(materials)) {
        if (material.transparent !== crossingSelected) material.needsUpdate = true
        material.transparent = crossingSelected
        material.opacity = crossingSelected ? 0.16 : 1
        material.depthWrite = !crossingSelected
      }
      for (const { descriptor, mesh } of loaded) mesh.visible = next.visible[descriptor.discipline]
      clearOverlay(overlay)
      const selectionBounds = new THREE.Box3()
      let overlapBounds: THREE.Box3 | undefined
      const selectedMeshes: THREE.BufferGeometry[] = []
      for (const [discipline, element] of next.selection.elements) {
        const elementBounds = box(element)
        selectionBounds.union(elementBounds)
        overlapBounds = overlapBounds ? overlapBounds.intersect(elementBounds) : elementBounds.clone()
        if (next.visible[discipline] && next.selection.rejected) {
          overlay.add(new THREE.Box3Helper(box(element), selectedPalette[discipline]))
        }
        for (const { descriptor, mesh } of loaded) {
          if (descriptor.discipline !== discipline) continue
          const range = descriptor.ranges.find(item => item.id === element.id)
          if (!range) continue
          const indices = mesh.geometry.index!.array
          const positions = mesh.geometry.getAttribute('position')
          const selected = new Float32Array(range.count * 9)
          for (let i = 0; i < range.count * 3; i++) {
            const index = indices[range.first * 3 + i]
            selected.set([positions.getX(index), positions.getY(index), positions.getZ(index)], i * 3)
          }
          const geometry = new THREE.BufferGeometry()
          geometry.setAttribute('position', new THREE.BufferAttribute(selected, 3))
          selectedMeshes.push(geometry)
          const translucent = crossingSelected && discipline === 'structure'
          const highlight = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
            color: selectedPalette[discipline], polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
            transparent: crossingSelected, opacity: translucent ? 0.32 : 1, depthWrite: !translucent,
          }))
          highlight.visible = next.visible[discipline]
          highlight.renderOrder = crossingSelected ? (translucent ? 1 : 2) : 0
          overlay.add(highlight)
        }
      }
      let points: THREE.Vector3[] = []
      const showMarkers = next.selection.elements.every(([discipline]) => next.visible[discipline])
      if (crossingSelected && selectedMeshes.length === 2 && overlapBounds) {
        overlapBounds.min.z = Math.max(floorMin, overlapBounds.min.z)
        overlapBounds.max.z = Math.min(floorMax, overlapBounds.max.z)
        points = findCrossingPoints(selectedMeshes[0], selectedMeshes[1], overlapBounds)
        if (points.length && showMarkers) {
          overlay.add(crossingMarkers(points))
        }
      }
      setCrossingHint(crossingSelected && showMarkers && selectedMeshes.length === 2
        ? points.length ? 'Green markers show where the surfaces meet.' : 'No crossing points could be located in this view.'
        : '')
      if (fit) {
        const contacts = new THREE.Box3().setFromPoints(points)
        const middle = contacts.getCenter(new THREE.Vector3())
        const anchor = points.reduce<THREE.Vector3 | undefined>((nearest, point) =>
          !nearest || point.distanceToSquared(middle) < nearest.distanceToSquared(middle) ? point : nearest, undefined)
        const focused = anchor ? contacts.expandByScalar(0.18) : overlapBounds && !overlapBounds.isEmpty()
          ? overlapBounds.expandByScalar(0.5) : selectionBounds
        focused.min.z = Math.max(floorMin, focused.min.z)
        focused.max.z = Math.min(floorMax, focused.max.z)
        if (next.selection.fit && !focused.isEmpty()) fitBox(focused, anchor)
        else fitBox(sceneBounds)
      }
      invalidate()
    }
    sceneTools.current = { update }
    const resize = new ResizeObserver(() => {
      const { clientWidth, clientHeight } = host
      if (!clientWidth || !clientHeight) return
      renderer.setSize(clientWidth, clientHeight)
      camera.aspect = clientWidth / clientHeight
      camera.updateProjectionMatrix()
      invalidate()
    })
    resize.observe(host)
    controls.addEventListener('change', invalidate)
    const ray = new THREE.Raycaster()
    const pointerDown = new THREE.Vector2()
    const start = (event: PointerEvent) => { pointerDown.set(event.clientX, event.clientY) }
    const pick = (event: PointerEvent) => {
      if (event.button !== 0 || pointerDown.distanceTo(new THREE.Vector2(event.clientX, event.clientY)) > 5) return
      const rect = renderer.domElement.getBoundingClientRect()
      ray.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1,
        1 - (event.clientY - rect.top) / rect.height * 2), camera)
      const candidates = loaded.filter(item => item.mesh.visible)
      const hits = ray.intersectObjects(candidates.map(item => item.mesh), false)
      const hit = hits.find(intersection => intersection.point.z >= floorMin && intersection.point.z <= floorMax)
      const batch = candidates.find(item => item.mesh === hit?.object)
      if (!batch || hit?.faceIndex == null) return
      const id = elementAt(batch.descriptor.ranges, hit.faceIndex)
      if (id !== undefined) current.current.onPick(batch.descriptor.discipline, id)
    }
    renderer.domElement.addEventListener('pointerdown', start)
    renderer.domElement.addEventListener('pointerup', pick)
    const disposeChunk = (item: LoadedChunk) => {
      item.mesh.geometry.dispose()
    }
    Promise.allSettled(chunks.map(chunk => loadChunk(bundleId, chunk, materials[chunk.discipline], abort.signal))).then(results => {
      for (const result of results) {
        if (result.status === 'rejected') {
          if (!disposed) setError((result.reason as Error).message)
        } else if (disposed) {
          disposeChunk(result.value)
        } else {
          const item = result.value
          loaded.push(item)
          scene.add(item.mesh)
          sceneBounds.union(item.mesh.geometry.boundingBox!)
        }
      }
      if (!disposed) {
        sceneBounds.min.z = Math.max(floorMin, sceneBounds.min.z)
        sceneBounds.max.z = Math.min(floorMax, sceneBounds.max.z)
        update(true)
      }
    })
    return () => {
      disposed = true
      abort.abort()
      sceneTools.current = null
      cancelAnimationFrame(frame)
      resize.disconnect()
      controls.dispose()
      renderer.domElement.removeEventListener('pointerdown', start)
      renderer.domElement.removeEventListener('pointerup', pick)
      clearOverlay(overlay)
      loaded.forEach(disposeChunk)
      Object.values(materials).forEach(material => material.dispose())
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [bundleId, chunks, floorMin, floorMax])

  useEffect(() => {
    sceneTools.current?.update(selection.fit && previousSelection.current !== selection)
    previousSelection.current = selection
  }, [visible, selection])
  useEffect(() => { sceneTools.current?.update(true) }, [reset])

  return <>
    <div ref={target} className="ifc-canvas" aria-label="Sixty5 3D floor view" />
    <p className="ifc-render-status">{error || status}</p>
    {crossingHint && <p aria-live="polite">{crossingHint}</p>}
  </>
}
