import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import database
import extractor


def _max_file_size_mb() -> int:
 
    try:
        return int(os.environ.get("MAX_FILE_SIZE_MB", "10"))
    except ValueError:
        print("[backend] MAX_FILE_SIZE_MB is not a number; defaulting to 10")
        return 10


MAX_FILE_SIZE_MB: int = _max_file_size_mb()
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024


def _error(status_code: int, message: str, detail: str = "") -> JSONResponse:
    
    return JSONResponse(
        status_code=status_code,
        content={"error": message, "detail": str(detail)},
    )


def _log(endpoint: str, exc: Exception) -> None:
   
    print(f"[backend] {endpoint} failed: {type(exc).__name__}: {exc}")


_ERROR_MAP: tuple[tuple[type[Exception], int, str], ...] = (
    (extractor.UnsupportedFileTypeError, 400, "Unsupported file type"),
    (extractor.UnreadableReceiptError, 422, "Receipt could not be read"),
    (extractor.ExtractionFailedError, 422, "Could not extract the receipt"),
    (extractor.MissingApiKeyError, 500, "Server is not configured"),
    (database.DatabaseError, 500, "Database error"),
    (ValueError, 400, "Invalid input"),
)


def _to_response(endpoint: str, exc: Exception) -> JSONResponse:
    _log(endpoint, exc)
    for exc_type, status_code, message in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return _error(status_code, message, str(exc))
    return _error(500, "Internal server error", str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    uid = database.get_default_user_id()
    print(f"[backend] database ready at {database.DB_PATH}")
    print(f"[backend] default user id: {uid}")
    print(f"[backend] max upload size: {MAX_FILE_SIZE_MB} MB")
    yield


app = FastAPI(
    title="Receipt & Expense Analyzer API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request, exc: RequestValidationError) -> JSONResponse:
    print(f"[backend] validation error on {request.url.path}: {exc}")
    return _error(422, "Invalid request", "One or more fields are missing or invalid")


@app.exception_handler(Exception)
async def _unhandled_handler(request, exc: Exception) -> JSONResponse:
    print(
        f"[backend] unhandled error on {request.url.path}: {type(exc).__name__}: {exc}"
    )
    return _error(500, "Internal server error", str(exc))


@app.get("/")
def health() -> dict:
    return {"status": "ok"}


@app.post("/receipts/upload")
def upload_receipt(file: UploadFile = File(...)):
    endpoint = "POST /receipts/upload"
    try:
        user_id = database.get_default_user_id()

        content = file.file.read()

        if len(content) == 0:
            return _error(400, "Empty file", "The uploaded file has no content")
        if len(content) > MAX_FILE_SIZE_BYTES:
            return _error(
                400,
                "File too large",
                f"Maximum allowed size is {MAX_FILE_SIZE_MB} MB",
            )

        result = extractor.extract_receipt(content, file.content_type or "")
        data = result.model_dump()

        saved = database.create_receipt(
            user_id=user_id,
            merchant=data["merchant"],
            date=data["date"],
            total=data["total"],
            currency=data["currency"],
            items=data["items"],
        )

        data["receipt_id"] = saved["id"]
        return data
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.get("/receipts")
def list_receipts():
    endpoint = "GET /receipts"
    try:
        user_id = database.get_default_user_id()
        return database.get_receipts_for_user(user_id)
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: int):
    endpoint = f"GET /receipts/{receipt_id}"
    try:
        receipt = database.get_receipt(receipt_id)
        if receipt is None:
            return _error(404, "Receipt not found", f"No receipt with id {receipt_id}")
        return receipt
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.delete("/receipts/{receipt_id}")
def delete_receipt(receipt_id: int):
    endpoint = f"DELETE /receipts/{receipt_id}"
    try:
        deleted = database.delete_receipt(receipt_id)
        if not deleted:
            return _error(404, "Receipt not found", f"No receipt with id {receipt_id}")
        return {"deleted": True, "id": receipt_id}
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.get("/analytics")
def get_analytics():
    endpoint = "GET /analytics"
    try:
        user_id = database.get_default_user_id()
        return database.get_analytics(user_id)
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.get("/analytics/insights")
def get_insights():
    endpoint = "GET /analytics/insights"
    try:
        user_id = database.get_default_user_id()

        try:
            import analyzer
        except ImportError as exc:
            _log(endpoint, exc)
            return _error(
                503,
                "Insights not available yet",
                "analyzer.py has not been added.",
            )

        insight = analyzer.generate_insights(user_id)
        return insight.model_dump()
    except Exception as exc:
        return _to_response(endpoint, exc)