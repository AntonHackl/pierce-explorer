# Pierce Explorer

A local React + FastAPI demo with a persistent native C++ Pierce worker on an NVIDIA GPU. **Load workload** reads and builds the datasets once; **Run query** reuses their GPU data and indexes; **Unload workload** releases the datasets. The page shows dataset load time, complete query execution time, and unique object-pair count.

The demo code lives in this repository. `Pierce/` is a pinned Git submodule containing the reusable runtime, original benchmark CLI, and persistent worker. `.gitmodules` names `demo` as the development branch; normal submodule updates use the pinned commit.

Two workloads can be resident simultaneously:

| Workload | Objects A × B | Generation | Selectivity |
| --- | --- | --- | --- |
| Small | 1,000 × 1,000 | Existing deterministic cube groups | Exactly 0.005; 5,000 pairs |
| Large | 200,000 × 200,000 | Pierce random generator, sizes 1–2, seed 42 | Target 0.000025; measured after querying |

The small workload has 200 separated groups, each with five A and five B cubes producing 25 pairs. The large workload uses `generate_cubes_by_selectivity.py` with streaming forced on, reservoir size 100,000 and 100,000 verification samples. Its sampled selectivity is diagnostic only: at this low probability the sample contains very few matches. Explorer displays actual selectivity as the native query's result count divided by A × B.

## One-time Windows setup

Run these commands from the repository root in PowerShell. Prerequisites: Conda, Visual Studio 2022 with Desktop development with C++, OptiX SDK (9.0 tested), and an NVIDIA GPU/driver compatible with CUDA 12.8. Use Node.js 24 LTS and pnpm 11.25.0 for the frontend.

```powershell
git submodule update --init --recursive
conda env create -f Pierce/pierce/environment-windows.yml
.\Pierce\build_windows.ps1
conda activate pierce_windows_build
python Pierce/pierce/scripts/smoke_test.py
python -m pip install -r backend/requirements-dev.txt
python -m backend.prepare
```

Skip `conda env create` if `pierce_windows_build` already exists. The build script auto-discovers OptiX in the standard Windows installation directory; alternatively pass `-OptixInstallDir 'C:\path\to\OptiX SDK'`. Build products and their PTX modules must remain in their generated locations. Preparation writes OBJ inputs and preprocessed geometry under ignored `.demo/data/`. `python -m backend.prepare` defaults to the small workload and preserves `.demo/data/a.pre` and `b.pre`. For the large workload or both, run:

```powershell
python -m backend.prepare --workload large
python -m backend.prepare --workload all
```

Large preparation uses versioned generation directories and publishes `.demo/data/large/current.json` only after both prepared files validate. Its manifest records parameters, engine revision/dirty state, generator and preprocessor hashes, input/output hashes, counts, file sizes, and grid settings. A matching complete preparation is reused; failures leave the previous manifest and its inputs usable. Each native/generation step has a 30-minute timeout and a log in its generation directory. Old and failed generations are retained for diagnosis; do not remove a generation referenced by the current manifest.

Install frontend dependencies (if pnpm is missing, install it with `npm install --global pnpm@11.25.0`):

```powershell
Set-Location frontend
pnpm install --frozen-lockfile
```

## Start the demo

**Terminal 1**, from the repository root:

```powershell
conda activate pierce_windows_build
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Use one backend worker so the process-local lock enforces one GPU query at a time. Activating Conda makes the native DLLs available to Pierce. Avoid launching with a different Python environment.

**Terminal 2**, from the repository root:

```powershell
Set-Location frontend
pnpm dev
```

Open [the local demo](http://127.0.0.1:5173). Select Small or Large, load it, and run queries. Load the other workload too, then switch freely: selection changes only the page's view and never loads, uploads, or rebuilds native resources. Each view retains its last successful result and query counter, including after refresh. Unload affects only the selected pair. The worker starts on the first Load; the context and pipeline are shared until shutdown.

A fatal worker failure clears both resident pairs; explicitly load them again to recover. Both servers bind only to loopback. Stop each terminal with Ctrl+C; backend shutdown stops its worker.

## API and timing definitions

All POST endpoints require JSON `{ "workloadId": "small" }` or `{ "workloadId": "large" }`. Unknown IDs, missing bodies, and extra fields (including file paths) are rejected with 422. Workload inputs are resolved by the fixed server-side catalog.

- `GET /api/health`: engine prerequisites, worker availability, current operation/workload, latest memory snapshot/allocation checkpoints, and errors. It never waits for GPU work. Readiness does not require both workloads to be prepared.
- `GET /api/workloads`: metadata and preparation readiness for each workload, loaded state, per-workload load timings, explicitly owned GPU bytes, completed-query count, last successful metrics, and errors.
- `POST /api/load`: load the selected pair atomically; return its workload state including `workloadId`. Repeating Load returns the resident state without work.
- `POST /api/run`: return `{ workloadId, queryExecutionMs, benchmarkPhaseSumMs, resultCount, completedQueries, actualSelectivity, rawHitCount, resultBufferBytes }`. No loading, upload, BVH build, pipeline creation, or warmup occurs.
- `POST /api/unload`: release the selected pair and return its unloaded workload state. Repeated Unload is harmless. Other pairs and the shared context/pipeline remain resident.

**Dataset load time** measures native reading, uploading, and index building for both datasets, with GPU synchronization at its boundaries. **Runtime initialization time** measures context and pipeline creation once per worker and is shown separately; its original value is retained on reload. Both exclude process startup and offline preprocessing.

**Query execution time** is synchronized native elapsed time including fresh parameters, temporary buffer allocation, prefix scans, ray tracing, deduplication, download, and temporary buffer release. It excludes HTTP, worker communication, and log output. The first query can be slower due to lazy CUDA/driver initialization; no warmup is hidden.

**Benchmark phase sum** retains the existing exact-overlap definition: ray-tracing phases + GPU deduplication + result download. It excludes some query work and is returned for diagnostics, not displayed as complete query latency. The original benchmark CLI retains its arguments, phase names, CSV export, and timing JSON format. The former demo field `overallTimeMs` has been removed.

One operation runs at a time; overlapping operations and querying before loading return HTTP 409. Missing prerequisites return 503, native/protocol failures 500, and a 120-second operation timeout 504. Fatal errors and timeouts kill and reap the child, invalidate every resident pair handle, and require explicit loading again. Structured native errors leave the worker usable. Memory-budget rejection returns HTTP 422 with `detail.code = "memory_budget_exceeded"`, an actionable message, stage, required bytes, available bytes after reserve, and reserved bytes. Existing pairs and their last successful metrics survive a rejected operation.

## GPU memory budget

The persistent runtime reserves `max(512 MiB, 10% of total CUDA device memory)`. It checks mesh/edge upload requirements and OptiX's reported BVH output/build workspace before allocation. Query checks cover count/offset buffers and, after hit counting, the raw result buffer plus an estimated sorting workspace of twice its size. Byte arithmetic is checked for overflow. The original benchmark CLI keeps its default allocation behavior.

The page displays device name, free/total memory, reserved headroom, per-workload resident buffers, and the last successful raw-hit/result-buffer sizes. Free-memory snapshots are observations after operations, not exact peak measurements. Allocation checkpoints are retained in protocol logs. Driver/context and internal OptiX allocations are excluded from explicitly owned bytes. These conservative preflight checks cannot guarantee against allocations racing with another GPU application; unexpected CUDA/OptiX failures still restart the worker.

On this RTX 5070 Laptop GPU, the seeded large workload produced **1,029,162 pairs** (actual selectivity **0.00002572905**), owns **574,586,368 bytes** of resident geometry/BVHs/edges, and used a **97,534,224-byte** raw result buffer. Both workloads together owned **577,464,960 bytes** (about 551 MiB). These are observed results for the prepared inputs, not a guarantee for arbitrary scenes.

Each worker has an ignored `.demo/runs/worker-*` directory with continuously drained `stderr.log`, protocol request/response records in `requests.jsonl`, and complete backend operation durations in `operations.jsonl`. Backend durations include startup/communication where applicable. These logs are excluded from native timings. Files remain for diagnosis and can be removed when no worker is running.

The C++ runtime and protocol are documented in [Pierce runtime notes](Pierce/pierce/query/RUNTIME.md).

## Checks

From the repository root, with the Conda environment active:

```powershell
python -m unittest discover -s backend/tests -v
python Pierce/pierce/scripts/smoke_test.py
python -m backend.verify_gpu
```

Frontend type checking and production build:

```powershell
Set-Location frontend
pnpm build
```

Parser/API/preparation tests use a controlled persistent subprocess and need no GPU. GPU verification compares full pair sets against the CLI, retains the small analytical and zero-result checks, and runs 100 alternating native queries plus 100 alternating real API/worker queries when Large is prepared. It checks four dataset reads/uploads/BVH builds, one pipeline creation, independent metrics, unload/reload, stale handles, memory-budget rejections and recovery using an injected memory provider, atomic failure, and shutdown.

Verification outputs and observed memory snapshots are under `.demo/verification/`, including `workload-report.json` and `multi-runtime.log`. Without a prepared Large workload, the script runs the small-workload checks only. To also compare with an old small CSV, run `python -m backend.verify_gpu --baseline path/to/mesh_overlap_results.csv`.

Global CUDA free-memory regression checks allow 16 MiB for driver caches and other GPU clients; explicitly owned resident bytes must return exactly to their prior values. The production frontend build is a build/type check. Browser interaction must be verified separately when browser automation is unavailable.
