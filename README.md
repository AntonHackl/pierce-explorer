# Pierce Explorer

A local React + FastAPI demo that executes native Windows Pierce on an NVIDIA GPU. Click **Run Pierce** to run a fixed two-pass overlap query and display overall time, query time, and unique object-pair count.

The demo code lives in this repository. `Pierce/` is a separate Git submodule; its sources are not modified. The cube workload has **1,000 objects in each collection and exactly 5,000 overlap pairs**: selectivity is `5,000 / (1,000 × 1,000) = 0.005` (0.5%). It is deterministic: 200 spatially separated groups each have five A cubes overlapping all five B cubes, with no containment and no matches between groups. This controlled workload verifies integration, not benchmark performance.

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

Skip `conda env create` if `pierce_windows_build` already exists. The build script auto-discovers OptiX in the standard Windows installation directory; alternatively pass `-OptixInstallDir 'C:\path\to\OptiX SDK'`. Build products and their PTX modules must remain in their generated locations. Preparation writes OBJ inputs and preprocessed geometry under ignored `.demo/data/`; it can be run again.

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

Open [the local demo](http://127.0.0.1:5173). Vite proxies `/api` to port 8000. The page shows prerequisite errors if the executable or prepared inputs are missing; after fixing them, use **Check connection again**. Both servers bind only to loopback. Stop each terminal with Ctrl+C.

## API and timing definitions

- `GET /api/health` → `{ ready, running, issues }`. Readiness checks the executable, its overlap PTX module, and prepared files. Actual DLL/GPU execution is verified when running the query.
- `POST /api/run` takes no parameters and returns `{ overallTimeMs, queryTimeMs, resultCount }`. It invokes `pierce_overlap_two_pass.exe --runs 1 --warmup-runs 0 --no-export` on fixed prepared inputs.
- **Overall time:** Pierce JSON `total.duration_us / 1000`. Includes its data reading, GPU setup, query, output handling, and cleanup. Excludes process startup, HTTP overhead, and offline preprocessing.
- **Query time:** the existing exact-overlap benchmark definition: sum `raytrace_*`, `gpu deduplication`, and `download results` durations. Read `duration_us` and divide by 1000, preserving small phases that the native rounded millisecond fields would lose. Do not add the enclosing `query` phase again. This metric is the benchmark's phase sum, not the full query wall-clock duration.
- **Result count:** the `Unique object pairs` stdout summary. A legitimate zero is accepted; absent metrics or summary cause an error.

Every invocation gets a unique directory under ignored `.demo/runs/` containing timing JSON and stdout/stderr logs. Failures return HTTP 500, missing prerequisites 503, an overlapping invocation 409, and a 120-second timeout 504. Timed-out child processes are killed and reaped. Run files remain available for diagnosis and can be removed when no queries are running.

## Checks

From the repository root, with the Conda environment active:

```powershell
python -m unittest discover -s backend/tests -v
python Pierce/pierce/scripts/smoke_test.py
```

Frontend type checking and production build:

```powershell
Set-Location frontend
pnpm build
```

Parser and API tests use controlled subprocess fixtures and need no GPU. The native smoke test and browser button exercise the real GPU. The production build is a build check; the documented demo runs through Vite's development proxy.
