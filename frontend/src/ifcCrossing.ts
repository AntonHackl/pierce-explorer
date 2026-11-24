import { Box3, Ray, Vector3 } from 'three'
import type { BufferGeometry } from 'three'

type Triangle = { vertices: [Vector3, Vector3, Vector3]; bounds: Box3 }

function trianglesIn(geometry: BufferGeometry, region: Box3): Triangle[] {
  const position = geometry.getAttribute('position')
  const result: Triangle[] = []
  for (let i = 0; i < position.count; i += 3) {
    const vertices: Triangle['vertices'] = [
      new Vector3().fromBufferAttribute(position, i),
      new Vector3().fromBufferAttribute(position, i + 1),
      new Vector3().fromBufferAttribute(position, i + 2),
    ]
    const bounds = new Box3().setFromPoints(vertices)
    if (bounds.intersectsBox(region)) result.push({ vertices, bounds })
  }
  return result
}

/** Find the edge–triangle contacts used to locate a confirmed surface-crossing pair. */
export function findCrossingPoints(first: BufferGeometry, second: BufferGeometry, region: Box3) {
  if (region.isEmpty()) return []
  const points: Vector3[] = []
  const seen = new Set<string>()
  const ray = new Ray()
  const hit = new Vector3()
  const edgesAgainstTriangle = (source: Triangle, target: Triangle) => {
    const [a, b, c] = target.vertices
    for (let i = 0; i < 3; i++) {
      const from = source.vertices[i]
      const to = source.vertices[(i + 1) % 3]
      ray.origin.copy(from)
      ray.direction.subVectors(to, from)
      const length = ray.direction.length()
      if (length === 0) continue
      ray.direction.divideScalar(length)
      if (!ray.intersectTriangle(a, b, c, false, hit) ||
          hit.distanceToSquared(from) > (length + 1e-7) ** 2 || !region.containsPoint(hit)) continue
      const key = hit.toArray().map(coord => Math.round(coord * 100000)).join(',')
      if (!seen.has(key)) {
        seen.add(key)
        points.push(hit.clone())
      }
    }
  }

  const left = trianglesIn(first, region)
  const right = trianglesIn(second, region)
  for (const a of left) {
    for (const b of right) {
      if (!a.bounds.intersectsBox(b.bounds)) continue
      edgesAgainstTriangle(a, b)
      edgesAgainstTriangle(b, a)
    }
  }
  return points
}
