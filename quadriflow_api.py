"""
QuadriFlow API — Quad-dominant remeshing service.

Wraps the QuadriFlow C++ binary behind FastAPI endpoints.
Accepts triangle mesh uploads (.obj) and returns quad-dominant meshes.
"""

import asyncio
import datetime
import logging
import logging.handlers
import os
import shutil
import subprocess
import tempfile
import time
import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
import uvicorn

# ── Logging Setup ──────────────────────────────────────────────────────────────

class TaipeiFormatter(logging.Formatter):
    """Timestamps always in UTC+8 (Taiwan), independent of system timezone."""
    _tz = datetime.timezone(datetime.timedelta(hours=8))

    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, tz=self._tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f",{int(record.msecs):03d}"


class HealthCheckFilter(logging.Filter):
    """Suppress uvicorn.access entries for GET /health."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "GET /health" not in record.getMessage()


def _setup_logging() -> logging.Logger:
    log_dir = "/app/logs"
    os.makedirs(log_dir, exist_ok=True)

    fmt = TaipeiFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    def _rotating(filename: str) -> logging.handlers.TimedRotatingFileHandler:
        h = logging.handlers.TimedRotatingFileHandler(
            os.path.join(log_dir, filename),
            when="midnight",
            backupCount=14,
            encoding="utf-8",
        )
        h.setFormatter(fmt)
        return h

    # app.log — business logic
    app_log = logging.getLogger("quadriflow")
    app_log.setLevel(logging.DEBUG)
    app_log.addHandler(_rotating("app.log"))

    # access.log — HTTP requests (health-check entries filtered out)
    access_log = logging.getLogger("uvicorn.access")
    access_log.addFilter(HealthCheckFilter())
    access_log.addHandler(_rotating("access.log"))

    # uvicorn.log — server startup & errors
    uvicorn_log = logging.getLogger("uvicorn.error")
    uvicorn_log.addHandler(_rotating("uvicorn.log"))

    return app_log


logger = _setup_logging()

# ── Configuration ─────────────────────────────────────────────────────────────

QUADRIFLOW_BIN = os.environ.get("QUADRIFLOW_BIN", "/usr/local/bin/quadriflow")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/app/outputs")
TEMP_DIR = os.environ.get("TEMP_DIR", "/tmp/quadriflow_jobs")
# Auto-cleanup: delete outputs older than this many seconds (default 24h)
OUTPUT_TTL_SECONDS = int(os.environ.get("OUTPUT_TTL_SECONDS", "86400"))

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Process lock — single worker, serialize heavy CPU jobs
process_lock = asyncio.Lock()

# ── Response Schemas ──────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    binary_available: bool = Field(description="QuadriFlow binary is found and executable")
    busy: bool = Field(description="A remeshing job is currently running")


class RemeshResponse(BaseModel):
    status: str
    request_id: str = Field(description="UUID for this request")
    input_url: str = Field(description="Download path for the uploaded input mesh")
    output_url: str = Field(description="Download path for the output quad mesh")
    target_faces: int = Field(description="Requested target face count")
    sharp: bool = Field(description="Sharp edge preservation enabled")
    boundary: bool = Field(description="Boundary edge preservation enabled")
    adaptive: bool = Field(description="Adaptive scale estimation enabled")
    mcf: bool = Field(description="Minimum-cost flow solver enabled")
    sat: bool = Field(description="SAT solver for flip removal enabled")


# ── Error classification ──────────────────────────────────────────────────────

def classify_exception(e: Exception) -> tuple[int, str, str]:
    """
    Classify exception into (http_status_code, error_code, human_message).
    """
    msg = str(e).lower()
    if isinstance(e, FileNotFoundError):
        return 500, "BINARY_NOT_FOUND", "QuadriFlow binary not found on server."
    if isinstance(e, subprocess.TimeoutExpired):
        return 504, "TIMEOUT", "QuadriFlow processing timed out."
    if isinstance(e, subprocess.CalledProcessError):
        return 500, "PROCESS_ERROR", f"QuadriFlow exited with code {e.returncode}."
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return 507, "DISK_FULL", "Server disk is full. Contact administrator."
    if "out of memory" in msg or "cannot allocate" in msg:
        return 503, "OOM", "Server out of memory. Try a smaller mesh or fewer faces."
    return 500, "PROCESSING_ERROR", str(e)


ERROR_RESPONSES = {
    500: {
        "description": "Processing error",
        "content": {
            "application/json": {
                "examples": {
                    "PROCESS_ERROR": {
                        "summary": "QuadriFlow process failed",
                        "value": {"detail": {"error_code": "PROCESS_ERROR", "message": "QuadriFlow exited with code 1."}},
                    },
                }
            }
        },
    },
    503: {
        "description": "Server out of memory, retry later",
        "content": {
            "application/json": {
                "example": {"detail": {"error_code": "OOM", "message": "Server out of memory."}},
            }
        },
    },
    504: {
        "description": "Processing timed out",
        "content": {
            "application/json": {
                "example": {"detail": {"error_code": "TIMEOUT", "message": "QuadriFlow processing timed out."}},
            }
        },
    },
    507: {
        "description": "Disk full",
        "content": {
            "application/json": {
                "example": {"detail": {"error_code": "DISK_FULL", "message": "Server disk is full."}},
            }
        },
    },
}

# ── Background cleanup ────────────────────────────────────────────────────────

async def _cleanup_old_outputs():
    """Periodically remove output directories older than OUTPUT_TTL_SECONDS."""
    while True:
        await asyncio.sleep(3600)  # check every hour
        try:
            now = time.time()
            removed = 0
            for entry in os.scandir(OUTPUT_DIR):
                if entry.is_dir() and (now - entry.stat().st_mtime) > OUTPUT_TTL_SECONDS:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    removed += 1
            if removed:
                logger.info(f"Cleanup: removed {removed} expired output directories (TTL={OUTPUT_TTL_SECONDS}s)")
        except Exception as e:
            logger.warning(f"Cleanup error: {e}")


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_cleanup_old_outputs())
    yield
    task.cancel()


# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(title="QuadriFlow Remeshing API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return {
        "status": "ok",
        "binary_available": os.path.isfile(QUADRIFLOW_BIN) and os.access(QUADRIFLOW_BIN, os.X_OK),
        "busy": process_lock.locked(),
    }


@app.post("/remesh", response_model=RemeshResponse, responses=ERROR_RESPONSES)
async def remesh(
    file: UploadFile = File(..., description="Input triangle mesh (.obj)"),
    faces: int = Form(1000, ge=100, le=1000000, description="Target number of faces in output quad mesh"),
    sharp: bool = Form(False, description="Preserve sharp edges"),
    boundary: bool = Form(False, description="Preserve boundary edges"),
    adaptive: bool = Form(False, description="Adaptive scale estimation"),
    mcf: bool = Form(False, description="Use minimum-cost flow solver instead of default Boykov max-flow"),
    sat: bool = Form(False, description="Enable SAT solver for flip removal (slower, more watertight)"),
    seed: int = Form(0, ge=0, description="RNG seed for reproducibility (0 = default)"),
    timeout_seconds: int = Form(600, ge=30, le=3600, description="Max processing time in seconds"),
):
    """
    Upload a triangle mesh (.obj) and receive a quad-dominant remesh.
    """
    # Validate file extension
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext != ".obj":
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type '{ext}'. Only .obj files are supported.",
        )

    request_id = str(uuid.uuid4())
    input_filename = file.filename or "input.obj"

    # Work in container-local tmpdir; only move to persistent OUTPUT_DIR on success.
    # This prevents half-finished artifacts from polluting the host volume.
    tmp_job_dir = os.path.join(TEMP_DIR, request_id)
    os.makedirs(tmp_job_dir, exist_ok=True)

    tmp_input = os.path.join(tmp_job_dir, input_filename)
    tmp_output = os.path.join(tmp_job_dir, "output.obj")

    try:
        # 1. Save uploaded file to tmpdir
        with open(tmp_input, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        file_size_mb = os.path.getsize(tmp_input) / (1024 * 1024)
        logger.info(f"[{request_id}] Received {input_filename} ({file_size_mb:.2f} MB), target_faces={faces}")

        # 2. Build command
        cmd = [QUADRIFLOW_BIN, "-i", tmp_input, "-o", tmp_output, "-f", str(faces)]
        if sharp:
            cmd.append("-sharp")
        if boundary:
            cmd.append("-boundary")
        if adaptive:
            cmd.append("-adaptive")
        if mcf:
            cmd.append("-mcf")
        if sat:
            cmd.append("-sat")
        if seed > 0:
            cmd.extend(["-seed", str(seed)])

        logger.info(f"[{request_id}] Running: {' '.join(cmd)}")

        # 3. Run QuadriFlow with process lock
        async with process_lock:
            def run_quadriflow():
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )

            result = await run_in_threadpool(run_quadriflow)

        # Log stdout/stderr
        if result.stdout:
            logger.info(f"[{request_id}] stdout:\n{result.stdout}")
        if result.stderr:
            logger.warning(f"[{request_id}] stderr:\n{result.stderr}")

        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )

        # 4. Verify output exists
        if not os.path.exists(tmp_output):
            raise RuntimeError("QuadriFlow completed but output file was not created.")

        output_size_mb = os.path.getsize(tmp_output) / (1024 * 1024)
        logger.info(f"[{request_id}] Output: output.obj ({output_size_mb:.2f} MB)")

        # 5. Move completed results from tmpdir to persistent output dir
        final_dir = os.path.join(OUTPUT_DIR, request_id)
        shutil.move(tmp_job_dir, final_dir)

        return {
            "status": "success",
            "request_id": request_id,
            "input_url": f"/download/{request_id}/{input_filename}",
            "output_url": f"/download/{request_id}/output.obj",
            "target_faces": faces,
            "sharp": sharp,
            "boundary": boundary,
            "adaptive": adaptive,
            "mcf": mcf,
            "sat": sat,
        }

    except Exception as e:
        status, error_code, message = classify_exception(e)
        logger.error(f"[{error_code}] request_id={request_id} | {type(e).__name__}: {e}")
        headers = {"Retry-After": "30"} if status == 503 else {}
        raise HTTPException(
            status_code=status,
            detail={"error_code": error_code, "message": message},
            headers=headers,
        )
    finally:
        # Clean up tmpdir on failure (on success it was already moved)
        shutil.rmtree(tmp_job_dir, ignore_errors=True)


@app.get("/download/{request_id}/{file_name}")
async def download_file(request_id: str, file_name: str):
    """Download an input or output file by request ID."""
    file_path = os.path.join(OUTPUT_DIR, request_id, file_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="application/octet-stream", filename=file_name)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8200)
