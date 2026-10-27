from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from .runner import DemoError, PierceRunner

runner = PierceRunner()


@asynccontextmanager
async def lifespan(app):
    yield
    runner.close()


class WorkloadRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workloadId: Literal['small', 'large']


app = FastAPI(title='Pierce local demo', lifespan=lifespan)


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
