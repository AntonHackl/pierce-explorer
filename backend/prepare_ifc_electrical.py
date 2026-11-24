"""Prepare the electrical side of an existing Sixty5 floor bundle."""
import argparse
import json
from pathlib import Path
import uuid

from .prepare_ifc import (IFC_ROOT, digest, export_side, normalize, pierce_executable, preprocess,
                          ROOT, storeys, tessellate, verify_object_mapping, write_json)


def main():
    import ifcopenshell
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("electrical", type=Path)
    args = parser.parse_args()
    bundle_id = json.loads((IFC_ROOT / "current.json").read_text())["id"]
    folder = IFC_ROOT / "runs" / bundle_id
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    floor = manifest["inputs"][0]["storey"]
    model = ifcopenshell.open(str(args.electrical))
    groups = storeys(model, ("IfcDistributionElement",))
    matching = [s for s in groups.values() if abs(floor["elevation"] - s["elevation"]) <= 0.25]
    if len(matching) != 1:
        raise ValueError("Expected a unique electrical storey at the structural floor elevation")
    raw, excluded = tessellate(model, matching[0])
    meshes = normalize(raw, manifest["origin"], excluded)
    if not meshes:
        raise ValueError("No valid electrical geometry at the selected storey")
    generation = uuid.uuid4().hex
    directory = folder / generation
    directory.mkdir()
    write_json(directory / "audit.json", excluded)
    elements, chunks = export_side(directory, "electrical", meshes)
    output = directory / "electrical.pre"
    preprocess(pierce_executable(ROOT / "Pierce/pierce", "preprocess", "pierce_preprocess"),
               directory / "electrical.obj", output, directory, "electrical")
    verify_object_mapping(output, meshes)
    write_json(directory / "viewer.json", {"elements": elements, "chunks": chunks, "exclusions": excluded})
    pending = folder / (generation + ".json")
    write_json(pending, {"generation": generation, "count": len(meshes), "excluded": len(excluded),
                         "triangles": sum(len(f) for _, _, f in meshes), "sha256": digest(args.electrical)})
    pending.replace(folder / "electrical.json")
    print(f"Prepared {len(meshes)} electrical elements, excluded {len(excluded)}")


if __name__ == "__main__":
    main()
