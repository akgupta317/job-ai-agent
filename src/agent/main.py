from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.api.routes import router
from agent.api.v1 import router as v1_router
from agent.llm.ollama import LLMBusy

app = FastAPI(title="ai-visibility-agent")
app.include_router(router)
app.include_router(v1_router)


@app.exception_handler(LLMBusy)
async def _busy(_: Request, e: LLMBusy) -> JSONResponse:
    """One handler for every route: the model slot is taken, so ask the caller to retry."""
    return JSONResponse({"detail": str(e)}, status_code=429, headers={"Retry-After": "30"})
