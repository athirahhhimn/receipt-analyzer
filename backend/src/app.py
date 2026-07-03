import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware


from . import database
from . import extractor

from dotenv import load_dotenv
load_dotenv()

# auto-load .env BEFORE anything reads env vars
load_dotenv()


# Configuration

# Where uploaded receipt images are saved on disk.
_DEFAULT_UPLOADS = Path(__file__).resolve().parent.parent / "data" / "uploads"
UPLOADS_DIR: Path = Path(os.environ.get("UPLOADS_DIR", str(_DEFAULT_UPLOADS)))

# Map of content-type to file extension for saving images.
_EXT_MAP: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}


def _max_file_size_mb() -> int:
    """Read MAX_FILE_SIZE_MB from the environment, defaulting to 10."""
    try:
        return int(os.environ.get("MAX_FILE_SIZE_MB", "10"))
    except ValueError:
        return 10


MAX_FILE_SIZE_MB: int = _max_file_size_mb()
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024


# Error helpers


def _error(status_code: int, message: str, detail: str = "") -> JSONResponse:
    """Build a structured JSON error response."""
    return JSONResponse(
        status_code=status_code,
        content={"error": message, "detail": str(detail)},
    )


def _log(endpoint: str, exc: Exception) -> None:
    """Print a one-line log of an endpoint failure."""
    print(f"[backend] {endpoint} failed: {type(exc).__name__}: {exc}")


_ERROR_MAP: tuple[tuple[type[Exception], int, str], ...] = (
    (database.DuplicateReceiptError, 409, "Duplicate receipt"),
    (extractor.UnsupportedFileTypeError, 400, "Unsupported file type"),
    (extractor.UnreadableReceiptError, 422, "Receipt could not be read"),
    (extractor.ExtractionFailedError, 422, "Could not extract the receipt"),
    (extractor.MissingApiKeyError, 500, "Server is not configured"),
    (database.DatabaseError, 500, "Database error"),
    (ValueError, 400, "Invalid input"),
)


def _to_response(endpoint: str, exc: Exception) -> JSONResponse:
    """Log an exception and convert it into the right JSON error response."""
    _log(endpoint, exc)
    for exc_type, status_code, message in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return _error(status_code, message, str(exc))
    return _error(500, "Internal server error", str(exc))


# Image storage helpers


def _save_image(receipt_id: int, file_bytes: bytes, content_type: str) -> str | None:
    """Save the uploaded file to disk as data/uploads/{receipt_id}.ext."""
    ext = _EXT_MAP.get((content_type or "").lower().strip())
    if ext is None:
        return None
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = UPLOADS_DIR / f"{receipt_id}{ext}"
    filepath.write_bytes(file_bytes)
    return ext


def _get_image_path(receipt_id: int, image_ext: str | None) -> Path | None:
    """Return the path to a saved receipt image, or None if it doesn't exist."""
    if not image_ext:
        return None
    filepath = UPLOADS_DIR / f"{receipt_id}{image_ext}"
    return filepath if filepath.is_file() else None


# App setup


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run once on startup: ensure database, tables, and upload folder exist."""
    database.init_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[backend] database ready at {database.DB_PATH}")
    print(f"[backend] uploads dir: {UPLOADS_DIR}")
    print(f"[backend] max upload size: {MAX_FILE_SIZE_MB} MB")
    yield


app = FastAPI(
    title="Receipt & Expense Analyzer API",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request, exc: RequestValidationError) -> JSONResponse:
    """Return structured error when FastAPI rejects a bad request."""
    print(f"[backend] validation error on {request.url.path}: {exc}")
    return _error(422, "Invalid request", "One or more fields are missing or invalid")


@app.exception_handler(Exception)
async def _unhandled_handler(request, exc: Exception) -> JSONResponse:
    """Catch-all so unexpected errors still return clean JSON."""
    print(
        f"[backend] unhandled error on {request.url.path}: {type(exc).__name__}: {exc}"
    )
    return _error(500, "Internal server error", str(exc))


# Health check


@app.get("/")
def health() -> dict:
    """Health check — returns {"status": "ok"}."""
    return {"status": "ok"}


# Receipts


@app.post("/receipts/upload")
def upload_receipt(file: UploadFile = File(...)):

    endpoint = "POST /receipts/upload"
    try:
        content = file.file.read()

        if len(content) == 0:
            return _error(400, "Empty file", "The uploaded file has no content")
        if len(content) > MAX_FILE_SIZE_BYTES:
            return _error(
                400, "File too large", f"Maximum allowed size is {MAX_FILE_SIZE_MB} MB"
            )

        # Extract structured data from the receipt
        result = extractor.extract_receipt(content, file.content_type or "")
        data = result.model_dump()

        # --- Feature 4: duplicate detection ---
        existing = database.check_duplicate(
            data["merchant"], data["date"], data["total"]
        )
        if existing is not None:
            return _error(
                409,
                "Duplicate receipt",
                f"A receipt from {existing['merchant'] or 'unknown'} on "
                f"{existing['date'] or 'unknown date'} for "
                f"{existing['total'] or '?'} already exists (id {existing['id']}). "
            )

        # Save to database (image_ext is set after we know the receipt_id)
        saved = database.create_receipt(
            merchant=data["merchant"],
            receipt_date=data["date"],
            total=data["total"],
            currency=data["currency"],
            items=data["items"],
        )

        # save the original image for preview
        ext = _save_image(saved["id"], content, file.content_type or "")
        if ext:
            try:
                with database.get_connection() as conn:
                    conn.execute(
                        "UPDATE receipts SET image_ext = ? WHERE id = ?",
                        (ext, saved["id"]),
                    )
            except Exception:
                pass

        # Clear insights cache so new spending is reflected
        try:
            from . import analyzer

            analyzer.clear_cache()
        except ImportError:
            pass

        data["receipt_id"] = saved["id"]
        data["has_image"] = ext is not None
        return data
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.get("/receipts")
def list_receipts():
    """List all receipts, newest first (without items)."""
    try:
        return database.get_all_receipts()
    except Exception as exc:
        return _to_response("GET /receipts", exc)


@app.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: int):
    """Get one receipt with all of its items."""
    endpoint = f"GET /receipts/{receipt_id}"
    try:
        receipt = database.get_receipt(receipt_id)
        if receipt is None:
            return _error(404, "Receipt not found", f"No receipt with id {receipt_id}")
        receipt["has_image"] = receipt.get("image_ext") is not None
        return receipt
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.get("/receipts/{receipt_id}/image")
def get_receipt_image(receipt_id: int):
    """Serve the original uploaded receipt image.

    Returns the image file if it was saved, or 404.
    """
    endpoint = f"GET /receipts/{receipt_id}/image"
    try:
        receipt = database.get_receipt(receipt_id)
        if receipt is None:
            return _error(404, "Receipt not found", f"No receipt with id {receipt_id}")

        image_path = _get_image_path(receipt_id, receipt.get("image_ext"))
        if image_path is None:
            return _error(404, "No image", "No image was saved for this receipt")

        # Determine the content type from the extension
        ext = receipt.get("image_ext", "")
        media_types = {
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".pdf": "application/pdf",
        }
        media_type = media_types.get(ext, "application/octet-stream")

        return FileResponse(path=str(image_path), media_type=media_type)
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.delete("/receipts/{receipt_id}")
def delete_receipt(receipt_id: int):
    """Delete a receipt, its items, and its saved image."""
    endpoint = f"DELETE /receipts/{receipt_id}"
    try:
        # Get the receipt first to find the image file
        receipt = database.get_receipt(receipt_id)
        if receipt is None:
            return _error(404, "Receipt not found", f"No receipt with id {receipt_id}")

        # Delete from database
        database.delete_receipt(receipt_id)

        # Delete the saved image file
        image_path = _get_image_path(receipt_id, receipt.get("image_ext"))
        if image_path is not None:
            try:
                image_path.unlink()
            except OSError:
                pass  # non-critical

        # Clear insights cache
        try:
            from . import analyzer

            analyzer.clear_cache()
        except ImportError:
            pass

        return {"deleted": True, "id": receipt_id}
    except Exception as exc:
        return _to_response(endpoint, exc)


# Analytics


@app.get("/analytics")
def get_analytics(
    start: str | None = Query(None, description="Start date YYYY-MM-DD"),
    end: str | None = Query(None, description="End date YYYY-MM-DD"),
):
    """Return spending breakdown, with optional date filtering.

    Query params:
      ?start=2026-06-01        from this date
      ?end=2026-06-30          up to this date
      ?start=2026-06-01&end=2026-06-30   between these dates
    """
    try:
        return database.get_analytics(start=start, end=end)
    except Exception as exc:
        return _to_response("GET /analytics", exc)


@app.get("/analytics/insights")
def get_insights():
    """Return AI-generated spending insights."""
    endpoint = "GET /analytics/insights"
    try:
        try:
            from . import analyzer
        except ImportError as exc:
            _log(endpoint, exc)
            return _error(503, "Insights not available yet", "analyzer.py not found.")

        insight = analyzer.generate_insights()
        return insight.model_dump()
    except Exception as exc:
        return _to_response(endpoint, exc)

