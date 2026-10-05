from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .runner import DemoError, PierceRunner


class Metrics(BaseModel):
    overallTimeMs: float
    queryTimeMs: float
    resultCount: int


class Health(BaseModel):
    ready: bool
    running: bool
    issues: list[str]


app = FastAPI(title="Pierce local demo")
runner = PierceRunner()


@app.get("/api/health", response_model=Health)
def health():
    return runner.health()


@app.post("/api/run", response_model=Metrics)
def run():
    # FastAPI executes regular def endpoints in its worker thread pool.
    try:
        return runner.run()
    except DemoError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
