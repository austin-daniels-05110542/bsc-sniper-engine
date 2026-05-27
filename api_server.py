"""HTTP API + dashboard for BSC sniper PairCreated testing."""
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bsc_sniper import BSCTokenSniper
from pair_history import clear_history, list_history

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

app = FastAPI(title="BSC Sniper Dashboard", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_bot: Optional[BSCTokenSniper] = None


def get_bot() -> BSCTokenSniper:
    global _bot
    if _bot is None:
        _bot = BSCTokenSniper(str(CONFIG_PATH))
    return _bot


class CreatePairRequest(BaseModel):
    token_a: Optional[str] = Field(None, description="First ERC20 address")
    token_b: Optional[str] = Field(None, description="Second ERC20 address")
    deploy_new_token: bool = Field(
        False,
        description="Deploy fresh test ERC20 and pair it with quote_token",
    )
    quote_token: Optional[str] = Field(
        None,
        description="Quote side when deploy_new_token=true (defaults to WBNB)",
    )


class ScanRequest(BaseModel):
    lookback_blocks: Optional[int] = Field(None, ge=1, le=10000)
    from_block: Optional[int] = Field(None, ge=0)
    to_block: Optional[int] = Field(None, ge=0)


@app.get("/api/status")
def api_status():
    try:
        bot = get_bot()
        return {"ok": True, **bot.get_status()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/history")
def api_history(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    snipeable_only: bool = Query(False),
    source: Optional[str] = Query(None),
):
    return list_history(limit=limit, offset=offset, snipeable_only=snipeable_only, source=source)


@app.delete("/api/history")
def api_clear_history():
    removed = clear_history()
    return {"ok": True, "removed": removed}


@app.post("/api/scan")
def api_scan(body: ScanRequest):
    try:
        bot = get_bot()
        latest = bot.w3.eth.block_number
        confirmation = int(bot.config.get("confirmationBlocks", 1))
        to_block = body.to_block if body.to_block is not None else max(latest - confirmation, 0)

        if body.from_block is not None:
            from_block = body.from_block
        else:
            lookback = body.lookback_blocks or int(bot.config.get("initialLookbackBlocks", 200))
            from_block = max(to_block - lookback, 0)

        if from_block > to_block:
            raise HTTPException(status_code=400, detail="from_block must be <= to_block")

        span = to_block - from_block + 1
        if span > 5000:
            raise HTTPException(
                status_code=400,
                detail="Block range too large (max 5000). Reduce lookback_blocks.",
            )
        events = bot.scan_block_range(from_block, to_block)
        records = bot.record_events(events, source="manual_scan")
        bot.last_scanned_block = to_block
        return {
            "ok": True,
            "from_block": from_block,
            "to_block": to_block,
            "found": len(records),
            "items": records,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Scan failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/scan/live")
def api_scan_live():
    """Run one sniper detection cycle (same as CLI --once)."""
    try:
        bot = get_bot()
        records = bot.run_once()
        return {"ok": True, "found": len(records), "items": records}
    except Exception as exc:
        logger.exception("Live scan failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/create-pair")
def api_create_pair(body: CreatePairRequest):
    try:
        bot = get_bot()
        if body.deploy_new_token:
            result = bot.create_pair_with_new_token(body.quote_token)
        else:
            if not body.token_a or not body.token_b:
                raise HTTPException(
                    status_code=400,
                    detail="token_a and token_b are required unless deploy_new_token is true",
                )
            result = bot.create_pair(body.token_a, body.token_b)
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("createPair failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api_server:app", host="127.0.0.1", port=8765, reload=False)
