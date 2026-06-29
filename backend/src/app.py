import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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


class UserCreate(BaseModel):
    """Body for POST /users."""

    username: str


def _error(status_code: int, message: str, detail: str = "") -> JSONResponse:
   
    return JSONResponse(
        status_code=status_code,
        content={"error": message, "detail": str(detail)},
    )


def _log(endpoint: str, exc: Exception) -> None:
   
    print(f"[backend] {endpoint} failed: {type(exc).__name__}: {exc}")


_ERROR_MAP: tuple[tuple[type[Exception], int, str], ...] = (
    (database.DuplicateUsernameError, 409, "Username already exists"),
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
    print(f"[backend] database ready at {database.DB_PATH}")
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


@app.get("/users")
def list_users():

    try:
        return database.get_all_users()
    except Exception as exc:
        return _to_response("GET /users", exc)


@app.post("/users")
def create_user(payload: UserCreate):

    try:
        user = database.create_user(payload.username)
        return JSONResponse(status_code=201, content=user)
    except Exception as exc:
        return _to_response("POST /users", exc)


@app.post("/receipts/upload")
def upload_receipt(user_id: int = Form(...), file: UploadFile = File(...)):
    
    endpoint = "POST /receipts/upload"
    try:
        # 1) The user must exist.
        if not database.user_exists(user_id):
            return _error(404, "User not found", f"No user with id {user_id}")

        # 2) Read the file bytes (sync read of the underlying file object).
        content = file.file.read()

        # 3) Reject empty or oversized files BEFORE calling the AI.
        if len(content) == 0:
            return _error(400, "Empty file", "The uploaded file has no content")
        if len(content) > MAX_FILE_SIZE_BYTES:
            return _error(
                400,
                "File too large",
                f"Maximum allowed size is {MAX_FILE_SIZE_MB} MB",
            )

        # 4) Extract structured data (raises UnsupportedFileTypeError, UnreadableReceiptError, ExtractionFailedError, etc.).
        result = extractor.extract_receipt(content, file.content_type or "")
        data = result.model_dump()

        # 5) Save the receipt and its items.
        saved = database.create_receipt(
            user_id=user_id,
            merchant=data["merchant"],
            date=data["date"],
            total=data["total"],
            currency=data["currency"],
            items=data["items"],
        )

        # 6) Return the ReceiptResult JSON plus the new database id so the frontend can link straight to the detail page.
        data["receipt_id"] = saved["id"]
        return data
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.get("/receipts")
def list_receipts(user_id: int):

    endpoint = "GET /receipts"
    try:
        if not database.user_exists(user_id):
            return _error(404, "User not found", f"No user with id {user_id}")
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


@app.get("/analytics/{user_id}")
def get_analytics(user_id: int):
   
    endpoint = f"GET /analytics/{user_id}"
    try:
        if not database.user_exists(user_id):
            return _error(404, "User not found", f"No user with id {user_id}")
        return database.get_analytics(user_id)
    except Exception as exc:
        return _to_response(endpoint, exc)


@app.get("/analytics/{user_id}/insights")
def get_insights(user_id: int):
    
    endpoint = f"GET /analytics/{user_id}/insights"
    try:
        if not database.user_exists(user_id):
            return _error(404, "User not found", f"No user with id {user_id}")

        try:
            import analyzer
        except ImportError as exc:
            _log(endpoint, exc)
            return _error(
                503,
                "Insights not available yet",
                "analyzer.py has not been added (Day 2 / step 6).",
            )

        insight = analyzer.generate_insights(user_id)
        return insight.model_dump()
    except Exception as exc:
        return _to_response(endpoint, exc)