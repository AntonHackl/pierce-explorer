"""Geometry and object identity checks for the IFC pilot."""
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from backend import ifc_api, prepare_ifc

try:
    import numpy as np
except ImportError:
    np = None


@unittest.skipIf(np is None, "Install requirements-ifc.txt for IFC preparation tests")
class GeometryTests(unittest.TestCase):
    def test_common_frame_and_deterministic_object_mapping(self):
        vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
        faces = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
        origin = np.array([500000, 5300000, 100])
        excluded = []
        structure = prepare_ifc.normalize([({"globalId": "A"}, vertices + origin, faces)], origin, excluded)
        ventilation = prepare_ifc.normalize([
            ({"globalId": "crosses"}, vertices + origin + 0.1, faces),
            ({"globalId": "box-only"}, vertices + origin + 0.65, faces),
            ({"globalId": "open"}, vertices + origin, faces[:3]),
        ], origin, excluded)
        self.assertEqual(structure[0][0]["bounds"], [[0, 0, 0], [1, 1, 1]])
        self.assertEqual(excluded, [{"globalId": "open", "reason": "open or non-manifold edge"}])
        with tempfile.TemporaryDirectory() as path:
            directory = Path(path)
            a, _ = prepare_ifc.export_side(directory, "structure", structure)
            b, chunks = prepare_ifc.export_side(directory, "ventilation", ventilation)
            self.assertEqual([e["globalId"] for e in b], ["crosses", "box-only"])
            self.assertEqual(prepare_ifc.rejected_examples([a, b], [(0, 0)]), [(0, 1)])
            for chunk in chunks:
                data = (directory / chunk["file"]).read_bytes()
                self.assertEqual(len(data), 12 * (chunk["vertices"] + chunk["triangles"]))
                self.assertEqual(chunk["ranges"][0]["first"], 0)

    def test_winding_and_degenerate_triangles(self):
        vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]])
        faces = np.array([[0, 1, 2], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
        self.assertEqual(prepare_ifc.audit(vertices, faces), "inconsistent winding")
        faces[0] = [0, 0, 2]
        self.assertEqual(prepare_ifc.audit(vertices, faces), "degenerate triangle")


class ApiTests(unittest.TestCase):
    def test_bundle_pagination_and_private_file_exclusion(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as path, patch.object(ifc_api, "IFC_ROOT", Path(path)):
            app = FastAPI()
            app.include_router(ifc_api.router)
            client = TestClient(app)
            self.assertEqual(client.get("/api/ifc/current").status_code, 404)
            bundle_id = "a" * 32
            directory = Path(path) / "runs" / bundle_id
            directory.mkdir(parents=True)
            (Path(path) / "current.json").write_text(json.dumps({"id": bundle_id}))
            (directory / "manifest.json").write_text(json.dumps({
                "id": bundle_id, "chunks": [{"file": "structure-0.bin"}], "pairCount": 2,
            }))
            (directory / "pairs.bin").write_bytes(struct.pack("<4I", 0, 3, 1, 4))
            (directory / "structure-0.bin").write_bytes(b"mesh")
            endpoint = f"/api/ifc/{bundle_id}"
            self.assertEqual(client.get("/api/ifc/current").json()["id"], bundle_id)
            self.assertEqual(client.get(endpoint + "/pairs?offset=1&limit=1").json(),
                             {"total": 2, "pairs": [[1, 4]]})
            self.assertEqual(client.get(endpoint + "/pairs?offset=2").json()["pairs"], [])
            self.assertEqual(client.get(endpoint + "/mesh/structure-0.bin").content, b"mesh")
            self.assertEqual(client.get(endpoint + "/mesh/pairs.bin").status_code, 404)
