"""Prepare a recorded IFC surface-crossing experiment for the browser."""
import argparse
from array import array
import csv
from itertools import chain
import json
import os
from pathlib import Path
import re
import struct
import sys
import uuid

from .prepare import digest, preprocess, run_step
from .runner import ROOT, pierce_executable

IFC_ROOT = ROOT / ".demo" / "ifc"
SOURCE = "https://huggingface.co/datasets/sylvainHellin/ifc-bench/tree/main/projects/sixty5"
DISCIPLINES = {"structure": ("IfcBeam", "IfcColumn", "IfcSlab", "IfcWall"),
               "ventilation": ("IfcFlowSegment", "IfcFlowFitting")}
MAX_TRIANGLES = 1_000_000


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def bounds(vertices):
    return [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()]


def storeys(model, classes):
    from ifcopenshell.util.element import get_container
    from ifcopenshell.util.placement import get_storey_elevation
    from ifcopenshell.util.unit import calculate_unit_scale

    scale = calculate_unit_scale(model)
    groups = {s.GlobalId: {"id": s.GlobalId, "name": s.Name or s.GlobalId,
                           "elevation": get_storey_elevation(s) * scale, "elements": []}
              for s in model.by_type("IfcBuildingStorey")}
    for element in sorted(set(chain.from_iterable(model.by_type(c) for c in classes)),
                          key=lambda e: (e.GlobalId, e.id())):
        floor = get_container(element, ifc_class="IfcBuildingStorey")
        if floor and floor.GlobalId in groups:
            groups[floor.GlobalId]["elements"].append(element)
    return groups


def audit(vertices, faces):
    import numpy as np

    if (not len(vertices) or not len(faces) or not np.isfinite(vertices).all()
            or faces.min() < 0 or faces.max() >= len(vertices)):
        return "invalid positions or triangle indices"
    triangles = vertices[faces].astype(np.float64)
    if np.any(np.all(np.cross(triangles[:, 1] - triangles[:, 0],
                              triangles[:, 2] - triangles[:, 0]) == 0, axis=1)):
        return "degenerate triangle"
    directed = np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    _, inverse, counts = np.unique(np.sort(directed, axis=1), axis=0,
                                   return_inverse=True, return_counts=True)
    if np.any(counts != 2):
        return "open or non-manifold edge"
    if np.any(np.bincount(inverse, weights=(directed[:, 0] < directed[:, 1])) != 1):
        return "inconsistent winding"
    return None


def tessellate(model, floor):
    import ifcopenshell.geom
    import numpy as np

    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    settings.set("weld-vertices", True)
    settings.set("disable-opening-subtractions", False)
    elements = floor["elements"]
    if not elements:
        return [], []
    iterator = ifcopenshell.geom.iterator(settings, model, min(8, os.cpu_count() or 1),
                                         include=elements)
    converted = {}
    if iterator.initialize():
        while True:
            shape = iterator.get()
            geometry = shape.geometry
            converted[shape.id] = (np.asarray(geometry.verts, dtype=np.float64).reshape(-1, 3),
                                   np.asarray(geometry.faces, dtype=np.int64).reshape(-1, 3))
            if not iterator.next():
                break
    meshes, excluded = [], []
    for element in elements:
        info = {"globalId": element.GlobalId, "class": element.is_a(), "name": element.Name or ""}
        result = converted.get(element.id())
        if result is None:
            excluded.append({**info, "reason": "IfcOpenShell produced no geometry"})
        elif not len(result[0]) or not np.isfinite(result[0]).all():
            excluded.append({**info, "reason": "empty or non-finite positions"})
        else:
            meshes.append((info, *result))
    return meshes, excluded


def normalize(meshes, origin, excluded):
    import numpy as np

    accepted = []
    for info, vertices, faces in meshes:
        positions = (vertices - origin).astype("<f4")
        # Weld positions after conversion, where distinct input points can coincide.
        positions, remap = np.unique(positions, axis=0, return_inverse=True)
        if faces.size and faces.min() >= 0 and faces.max() < len(vertices):
            faces = remap[faces]
            problem = audit(positions, faces)
        else:
            problem = "empty or invalid triangle indices"
        if problem:
            excluded.append({**info, "reason": problem})
        else:
            accepted.append(({**info, "bounds": bounds(positions)}, positions, faces))
    return accepted


def intersects(a, b):
    return all(a[0][axis] <= b[1][axis] and b[0][axis] <= a[1][axis] for axis in range(3))


def choose_region(meshes):
    import numpy as np

    all_meshes = [mesh for side in meshes.values() for mesh in side]
    lo = np.min([m[0]["bounds"][0] for m in all_meshes], axis=0)
    hi = np.max([m[0]["bounds"][1] for m in all_meshes], axis=0)
    if sum(len(m[2]) for m in all_meshes) <= MAX_TRIANGLES:
        return meshes, None
    # Subdivide the floor deterministically and keep whole elements touching the region.
    regions = [(lo, hi)]
    for _ in range(8):
        next_regions = []
        for low, high in regions:
            axis = int(np.argmax((high - low)[:2]))
            mid = (low[axis] + high[axis]) / 2
            left, right = high.copy(), low.copy()
            left[axis] = right[axis] = mid
            for region in ((low, left), (right, high)):
                chosen = {d: [m for m in side if intersects(m[0]["bounds"], region)]
                          for d, side in meshes.items()}
                if all(chosen.values()) and sum(len(m[2]) for side in chosen.values()
                                                for m in side) <= MAX_TRIANGLES:
                    return chosen, [part.tolist() for part in region]
                if all(chosen.values()):
                    next_regions.append(region)
        regions = next_regions
    raise ValueError("No contiguous region fits the pilot triangle budget")


def export_side(directory, discipline, meshes):
    import numpy as np

    elements, chunks = [], []
    centers = np.asarray([(v.min(axis=0) + v.max(axis=0)) / 2 for _, v, _ in meshes])
    lo, span = centers.min(axis=0), np.maximum(centers.max(axis=0) - centers.min(axis=0), 1e-6)
    tiles = np.minimum(((centers - lo) / span * 4).astype(int), 3)
    groups = {}
    with (directory / f"{discipline}.obj").open("w", encoding="ascii") as obj:
        offset = 1
        for index, ((info, vertices, faces), tile) in enumerate(zip(meshes, tiles)):
            elements.append({**info, "id": index})
            groups.setdefault(tuple(tile[:2]), []).append((index, vertices, faces))
            obj.write(f"o element_{index}\n")
            np.savetxt(obj, vertices, fmt="v %.9g %.9g %.9g")
            np.savetxt(obj, faces + offset, fmt="f %d %d %d")
            offset += len(vertices)
    for batch, group in enumerate(groups.values()):
        name = f"{discipline}-{batch}.bin"
        ranges, offset, triangle = [], 0, 0
        with (directory / name).open("wb") as stream:
            for index, vertices, faces in group:
                stream.write(vertices.tobytes())
                ranges.append({"id": index, "first": triangle, "count": len(faces)})
                triangle += len(faces)
            for _, vertices, faces in group:
                stream.write((faces + offset).astype("<u4").tobytes())
                offset += len(vertices)
        chunks.append({"file": name, "discipline": discipline, "vertices": offset,
                       "triangles": triangle, "ranges": ranges})
    return elements, chunks


def read_pairs(directory, sides):
    output = (directory / "overlap.log").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^Unique object pairs: (\d+)\s*$", output, re.M)
    if not match or "truncated" in output.lower():
        raise ValueError("Pierce result count is missing or the output may be truncated")
    with (directory / "pairs.csv").open(newline="") as stream:
        pairs = [tuple(map(int, (row["object_id_mesh1"], row["object_id_mesh2"])))
                 for row in csv.DictReader(stream)]
    if len(set(pairs)) != len(pairs) or len(pairs) != int(match[1]):
        raise ValueError("Exported pairs are duplicated or the count differs from Pierce")
    if any(not (0 <= a < len(sides[0]) and 0 <= b < len(sides[1])) for a, b in pairs):
        raise ValueError("Exported pair has an unknown object ID")
    return sorted(pairs)


def verify_object_mapping(path, meshes):
    import numpy as np

    counts = [len(faces) for _, _, faces in meshes]
    with path.open("rb") as stream:
        magic, version, vertices, triangles, mappings, _, _, _, _ = struct.unpack(
            "<II5QBB6x", stream.read(56))
        if (magic, version, triangles, mappings) != (0x52334442, 4, sum(counts), sum(counts)):
            raise ValueError(f"Prepared geometry has unexpected counts: {path.name}")
        stream.seek(56 + (vertices + triangles) * 12)
        ids = np.fromfile(stream, dtype="<i4", count=mappings)
    expected = np.repeat(np.arange(len(counts), dtype=np.int32), counts)
    if not np.array_equal(ids, expected):
        raise ValueError(f"Prepared triangle IDs differ from viewer element IDs: {path.name}")


def rejected_examples(sides, pairs, maximum=8):
    import numpy as np

    if not sides[1] or maximum <= 0:
        return []
    confirmed = set(pairs)
    boxes = np.asarray([element["bounds"] for element in sides[1]])
    examples = []
    for a in sides[0]:
        low, high = a["bounds"]
        matches = np.flatnonzero(np.all((boxes[:, 0] <= high) & (boxes[:, 1] >= low), axis=1))
        for index in matches:
            b = sides[1][index]
            pair = (a["id"], b["id"])
            if pair not in confirmed:
                examples.append(pair)
                if len(examples) == maximum:
                    return examples
    return examples


def prepare(paths, models, floors):
    import numpy as np

    run_id = uuid.uuid4().hex
    directory = IFC_ROOT / "runs" / run_id
    directory.mkdir(parents=True)
    try:
        if abs(floors[0]["elevation"] - floors[1]["elevation"]) > 0.25:
            raise ValueError("Storey elevations differ by more than 0.25 m")
        tessellated = []
        for discipline, model, floor in zip(DISCIPLINES, models, floors):
            print(f"Tessellating {len(floor['elements'])} {discipline} elements...", flush=True)
            tessellated.append(tessellate(model, floor))
        if any(not meshes for meshes, _ in tessellated):
            raise ValueError("A storey produced no tessellated elements")
        low = np.min([v.min(axis=0) for meshes, _ in tessellated for _, v, _ in meshes], axis=0)
        high = np.max([v.max(axis=0) for meshes, _ in tessellated for _, v, _ in meshes], axis=0)
        origin = (low + high) / 2
        meshes = {d: normalize(raw, origin, excluded)
                  for d, (raw, excluded) in zip(DISCIPLINES, tessellated)}
        exclusions = {d: excluded for d, (_, excluded) in zip(DISCIPLINES, tessellated)}
        write_json(directory / "audit.json", exclusions)
        if not all(meshes.values()):
            raise ValueError("A discipline has no valid closed geometry")
        merged_bounds = [[np.min([m[0]["bounds"][0] for m in side], axis=0),
                          np.max([m[0]["bounds"][1] for m in side], axis=0)] for side in meshes.values()]
        if not intersects(*merged_bounds):
            raise ValueError("Selected discipline bounds do not overlap after placement")
        floor_counts = {discipline: len(side) for discipline, side in meshes.items()}
        meshes, region = choose_region(meshes)
        engine = ROOT / "Pierce" / "pierce"
        pre = pierce_executable(engine, "preprocess", "pierce_preprocess")
        overlap = pierce_executable(engine, "query", "pierce_overlap")
        compare = pierce_executable(engine, "query", "pierce_bvh_compare")
        inputs, sides, chunks = [], [], []
        for discipline, side in meshes.items():
            elements, batches = export_side(directory, discipline, side)
            sides.append(elements)
            chunks.extend(batches)
            inputs.append(directory / f"{discipline}.pre")
            preprocess(pre, directory / f"{discipline}.obj", inputs[-1], directory, discipline)
            verify_object_mapping(inputs[-1], side)
        print("Running the Pierce surface-crossing join and object-pair verifier...", flush=True)
        run_step([overlap, "--mesh1", inputs[0], "--mesh2", inputs[1], "--ptx",
                  overlap.parent / "mesh_overlap_edges.ptx", "--runs", "1", "--warmup-runs", "0",
                  "--output", directory / "timing.json", "--pairs-output", directory / "pairs.csv"],
                 directory, "overlap.log")
        pairs = read_pairs(directory, sides)
        run_step([compare, *inputs, compare.parent, "1", "16", "--parallel-only"],
                 directory, "compare.log")
        verification = re.findall(r"^verified,0,(\d+)\s*$",
                                  (directory / "compare.log").read_text(errors="replace"), re.M)
        if verification != [str(len(pairs))]:
            raise ValueError("Object-pair verifier did not confirm the complete result count")
        rejected = rejected_examples(sides, pairs)
        packed = array("I", chain.from_iterable(pairs))
        if sys.byteorder != "little":
            packed.byteswap()
        (directory / "pairs.bin").write_bytes(packed.tobytes())
        timing = json.loads((directory / "timing.json").read_text())
        manifest = {"id": run_id, "source": SOURCE, "attribution": "Sixty5 S1 · Stam + De Koning · CC BY 4.0",
                    "predicate": "Surface crossings", "origin": origin.tolist(), "region": region,
                    "inputs": [{"file": p.name, "sha256": digest(p),
                                "storey": {k: v for k, v in floor.items() if k != "elements"}}
                               for p, floor in zip(paths, floors)],
                    "elements": dict(zip(DISCIPLINES, sides)), "exclusions": exclusions, "chunks": chunks,
                    "floorCounts": floor_counts,
                    "pairCount": len(pairs), "rejected": rejected, "verified": True,
                    "demoReady": bool(pairs and rejected), "timing": timing}
        write_json(directory / "manifest.json", manifest)
        pending = IFC_ROOT / f"{run_id}.json"
        write_json(pending, {"id": run_id})
        pending.replace(IFC_ROOT / "current.json")
    except Exception as error:
        write_json(directory / "failure.json", {"error": str(error)})
        raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structure", type=Path, required=True)
    parser.add_argument("--ventilation", type=Path, required=True)
    parser.add_argument("--list-storeys", action="store_true")
    parser.add_argument("--structure-storey")
    parser.add_argument("--ventilation-storey")
    args = parser.parse_args()
    import ifcopenshell

    paths = [args.structure.resolve(), args.ventilation.resolve()]
    models = [ifcopenshell.open(str(path)) for path in paths]
    groups = [storeys(model, classes) for model, classes in zip(models, DISCIPLINES.values())]
    if args.list_storeys:
        for discipline, group in zip(DISCIPLINES, groups):
            print(discipline)
            for floor in sorted(group.values(), key=lambda f: f["elevation"]):
                print(f"  {floor['id']}  {floor['elevation']:7.2f} m  "
                      f"{len(floor['elements']):5} elements  {floor['name']}")
        return
    try:
        floors = [group[key] for group, key in zip(groups, [args.structure_storey, args.ventilation_storey])]
    except KeyError:
        parser.error("Select a valid storey ID for each input. Use --list-storeys to find IDs.")
    result = prepare(paths, models, floors)
    print(f"Bundle {result['id']} | {result['pairCount']} verified pairs | "
          f"{len(result['rejected'])} rejected examples")


if __name__ == "__main__":
    main()
