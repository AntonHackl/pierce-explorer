from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from .runner import DemoError, PierceRunner
from .ifc_api import router as ifc_router

runner = PierceRunner()


@asynccontextmanager
async def lifespan(app):
    yield
    runner.close()


class WorkloadRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workloadId: Literal['small', 'large']


app = FastAPI(title='Pierce local demo', lifespan=lifespan)
app.include_router(ifc_router)
app.state.pierce_runner = runner


def invoke(action):
    try:
        return action()
    except DemoError as error:
        detail = {'message': str(error), 'code': error.code, **(error.details or {})} if error.code else str(error)
        raise HTTPException(status_code=error.status_code, detail=detail) from error


@app.get('/api/health')
def health():
    return runner.health()


@app.get('/api/workloads')
def workloads():
    return runner.workloads()


@app.post('/api/load')
def load(body: WorkloadRequest):
    return invoke(lambda: runner.load(body.workloadId))


@app.post('/api/run')
def run(body: WorkloadRequest):
    return invoke(lambda: runner.run(body.workloadId))


@app.post('/api/unload')
def unload(body: WorkloadRequest):
    return invoke(lambda: runner.unload(body.workloadId))

class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preset: Literal['live', 'large'] = 'live'


@app.post('/api/experiments/shared-bvh')
def shared_bvh(body: ExperimentRequest):
    from .experiments import run_experiment

    def perform():
        if not runner.lock.acquire(blocking=False):
            raise DemoError("Pierce is busy. Wait for the current operation to finish.", 409)
        try:
            return run_experiment(body.preset)
        finally:
            runner.lock.release()

    return invoke(perform)


class MemoryExperimentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workload: Literal['vessels', 'cubes'] = 'vessels'


@app.post('/api/experiments/memory')
def memory_experiment(body: MemoryExperimentRequest):
    from .memory_experiment import run_memory

    def perform():
        if not runner.lock.acquire(blocking=False):
            raise DemoError("Pierce is busy. Wait for the current operation to finish.", 409)
        try:
            return run_memory(body.workload)
        finally:
            runner.lock.release()

    return invoke(perform)
