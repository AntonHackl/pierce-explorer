"""IFC pilot assets and live joins."""
import json
import re
import struct

from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .runner import ROOT, DemoError

router = APIRouter(prefix="/api/ifc")
IFC_ROOT = ROOT / ".demo" / "ifc"


def bundle(bundle_id):
    if not re.fullmatch(r"[a-f0-9]{32}", bundle_id):
        raise HTTPException(404, "IFC bundle not found.")
    directory = IFC_ROOT / "runs" / bundle_id
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HTTPException(404, "IFC bundle not found.") from error
    return directory, manifest


@router.get("/current")
def current():
    try:
        pointer = json.loads((IFC_ROOT / "current.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HTTPException(404, "Prepare an IFC pilot bundle with python -m backend.prepare_ifc.") from error
    return bundle(pointer["id"])[1]


@router.get("/{bundle_id}/mesh/{name}")
def mesh(bundle_id: str, name: str):
    directory, manifest = bundle(bundle_id)
    if name not in {chunk["file"] for chunk in manifest["chunks"]}:
        raise HTTPException(404, "IFC mesh chunk not found.")
    return FileResponse(directory / name, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400, immutable"})


@router.get("/{bundle_id}/pairs")
def pairs(bundle_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    directory, manifest = bundle(bundle_id)
    total = manifest["pairCount"]
    if offset >= total:
        return {"total": total, "pairs": []}
    with (directory / "pairs.bin").open("rb") as stream:
        stream.seek(offset * 8)
        data = stream.read(min(limit, total - offset) * 8)
    return {"total": total, "pairs": list(struct.iter_unpack("<II", data))}


def electrical_view(directory, generation):
    if not re.fullmatch(r"[a-f0-9]{32}", generation):
        raise HTTPException(404, "Electrical generation not found.")
    try:
        return json.loads((directory / generation / "viewer.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HTTPException(404, "Prepare the electrical viewer geometry again.") from error


def electrical_scene(directory, generation):
    view = electrical_view(directory, generation)
    view["chunks"] = [{**chunk, "file": f"{generation}/{chunk['file']}"} for chunk in view["chunks"]]
    return view


@router.get("/{bundle_id}/electrical")
def electrical_layer(bundle_id: str):
    directory, _ = bundle(bundle_id)
    try:
        electrical = json.loads((directory / "electrical.json").read_text())
    except FileNotFoundError:
        return None
    return electrical_scene(directory, electrical["generation"])


@router.get("/{bundle_id}/mesh/{generation}/{name}")
def electrical_mesh(bundle_id: str, generation: str, name: str):
    directory, _ = bundle(bundle_id)
    view = electrical_view(directory, generation)
    if name not in {chunk["file"] for chunk in view["chunks"]}:
        raise HTTPException(404, "Electrical mesh chunk not found.")
    return FileResponse(directory / generation / name, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400, immutable"})


@router.post("/{bundle_id}/live/{discipline}")
def live(bundle_id: str, discipline: Literal["ventilation", "electrical"], request: Request):
    directory, manifest = bundle(bundle_id)
    vent = f"ifc-{bundle_id}-ventilation"
    try:
        electrical = json.loads((directory / "electrical.json").read_text())
    except FileNotFoundError:
        electrical = None
    elec = f"ifc-{bundle_id}-electrical-{electrical['generation']}" if electrical else None
    if discipline == "electrical" and electrical is None:
        raise HTTPException(503, "Prepare the electrical model with backend.prepare_ifc_electrical.")
    if discipline == "electrical":
        generation = electrical["generation"]
        view = electrical_scene(directory, generation)
        key = elec
        target = directory / generation / "electrical.pre"
        count = electrical["count"]
    else:
        view = None
        key = vent
        target = directory / "ventilation.pre"
        count = len(manifest["elements"]["ventilation"])
    structure = directory / "structure.pre"
    if not structure.is_file() or not target.is_file():
        raise HTTPException(503, "Prepared Sixty5 files are missing.")
    try:
        result = request.app.state.pierce_runner.run_ifc(
            key, f"ifc-{bundle_id}-", (structure, target), (len(manifest["elements"]["structure"]), count))
        return {**result, "scene": view}
    except DemoError as error:
        raise HTTPException(error.status_code, str(error)) from error


@router.post("/live/reset")
def reset_live(request: Request):
    try:
        request.app.state.pierce_runner.reset_ifc()
    except DemoError as error:
        raise HTTPException(error.status_code, str(error)) from error
    return {"ok": True}
