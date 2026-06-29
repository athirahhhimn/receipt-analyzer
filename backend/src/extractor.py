import json
import os
import re
import time
from collections.abc import Callable

import pymupdf  
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, ValidationError

MODEL: str = "gemini-2.5-flash" 
MAX_ATTEMPTS: int = 5  
RATE_LIMIT_WAIT_SECONDS: int = 65  
RATE_LIMIT_CODES: frozenset[int] = frozenset({429, 503})  


VALID_CATEGORIES: tuple[str, ...] = (
    "food",
    "transport",
    "utilities",
    "shopping",
    "entertainment",
    "health",
    "education",
    "other",
)


class ReceiptItem(BaseModel):

    name: str
    price: float
    category: str


class ReceiptResult(BaseModel):
    """full structured result extracted from one receipt."""

    merchant: str | None = None
    date: str | None = None  
    total: float | None = None
    currency: str = "MYR"
    items: list[ReceiptItem] = Field(default_factory=list)


class ExtractorError(Exception):
    """Base class for any problem during extraction."""


class MissingApiKeyError(ExtractorError):
    """Raised when GOOGLE_API_KEY is not set (app.py -> 500)."""


class UnsupportedFileTypeError(ExtractorError):
    """Raised when the upload is neither an image nor a PDF (app.py -> 400)."""


class UnreadableReceiptError(ExtractorError):
    """Raised when Gemini says the receipt cannot be read (app.py -> 422)."""


class ExtractionFailedError(ExtractorError):
    """Raised after all retries fail / JSON stays invalid (app.py -> 422)."""


_CATEGORIES_TEXT = ", ".join(VALID_CATEGORIES)

# The base instruction tells Gemini exactly what JSON to produce 

_BASE_INSTRUCTION = f"""\
You are a precise data-extraction engine for Malaysian retail receipts.
Malaysian receipts often mix English and Malay (Bahasa Malaysia).

Return ONLY one JSON object and nothing else: no markdown, no code fences,
no commentary.

The JSON object must have exactly these keys:
  "merchant": the shop/business name as a string, or null if unreadable.
  "date":     the date as a string "YYYY-MM-DD", or null if missing/ambiguous.
  "total":    the final amount payable as a number, or null if unreadable.
  "currency": the 3-letter currency code; use "MYR" if not shown.
  "items":    a list of objects, each {{"name", "price", "category"}}.

Rules you MUST follow:
- NEVER guess or invent a price or total. If a number is unclear, use null
  for total, or leave that item out. Accuracy matters more than completeness.
- "category" must be EXACTLY one of: {_CATEGORIES_TEXT}.
  Pick the closest match; use "other" when unsure.
- Prices/totals are plain numbers, no currency symbol or thousands separator.
- Do NOT list taxes, subtotals, discounts, rounding or payment lines as items
  (Malay: 'Cukai'/'SST'/'GST' = tax, 'Jumlah'/'Jumlah Besar' = total,
  'Tunai' = cash, 'Baki' = change, 'Diskaun' = discount).
- Convert dates like '28/06/2026' or '28 Jun 2026' to '2026-06-28'.
- If the document is NOT a receipt, or is too blurry/damaged to extract
  anything, return exactly: {{"unreadable": true, "reason": "<short reason>"}}.
"""

# Used only after a parse/validation failure, to push harder for clean JSON.
_STRICT_INSTRUCTION = _BASE_INSTRUCTION + (
    "\nSTRICT MODE: your previous answer could not be parsed. Output MUST be a"
    " single valid JSON object with no text before or after it. Use double"
    " quotes for all strings and spell every key exactly as specified."
)

# The short user-turn message that accompanies the image or text.
_USER_PREFIX = "Extract the receipt data and return the JSON object."


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    
    global _client
    if _client is not None:
        return _client

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise MissingApiKeyError(
            "GOOGLE_API_KEY is not set."
        )
    try:
        _client = genai.Client(api_key=api_key)
    except Exception as exc:  # pragma: no cover - depends on the SDK internals
        raise ExtractorError(f"Failed to initialise the Gemini client: {exc}") from exc
    return _client


def _call_gemini(client: genai.Client, contents: list, strict: bool) -> str:
    
    config = types.GenerateContentConfig(
        temperature=0.0,  #
        response_mime_type="application/json",  
        system_instruction=_STRICT_INSTRUCTION if strict else _BASE_INSTRUCTION,
    )
    response = client.models.generate_content(
        model=MODEL, contents=contents, config=config
    )
    text = response.text
    if not text or not text.strip():
        raise ExtractionFailedError("Gemini returned an empty response.")
    return text


def _parse_json(raw: str) -> dict:
    
    text = raw.strip()

    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    data = json.loads(text)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("Top-level JSON is not an object", text, 0)
    return data


def _extract_with_retries(build_contents: Callable[[], list]) -> ReceiptResult:
   
    client = _get_client()
    use_strict = False
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = _call_gemini(client, build_contents(), strict=use_strict)
            data = _parse_json(raw)

            if data.get("unreadable") is True:
                reason = str(data.get("reason", "The receipt could not be read."))
                raise UnreadableReceiptError(reason)

            return ReceiptResult.model_validate(data)

        except UnreadableReceiptError:
            raise

        except errors.APIError as exc:
            last_error = exc
            if exc.code in RATE_LIMIT_CODES:
                print(
                    f"[extractor] rate limit ({exc.code}); waiting "
                    f"{RATE_LIMIT_WAIT_SECONDS}s (attempt {attempt}/{MAX_ATTEMPTS})"
                )
                time.sleep(RATE_LIMIT_WAIT_SECONDS)
            else:
                print(
                    f"[extractor] API error {exc.code} on attempt "
                    f"{attempt}/{MAX_ATTEMPTS}: {exc}"
                )
            continue

        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            use_strict = True  
            print(
                f"[extractor] invalid JSON on attempt {attempt}/{MAX_ATTEMPTS}; "
                "retrying with a stricter prompt"
            )
            continue

        except Exception as exc:  
            last_error = exc
            print(
                f"[extractor] unexpected error on attempt "
                f"{attempt}/{MAX_ATTEMPTS}: {type(exc).__name__}: {exc}"
            )
            continue

    raise ExtractionFailedError(
        f"Could not extract a valid receipt after {MAX_ATTEMPTS} attempts. "
        f"Last error: {type(last_error).__name__}: {last_error}"
    )


def _pdf_to_text(pdf_bytes: bytes) -> str:
 
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            pages = [page.get_text() for page in doc]
    except Exception as exc:
        raise ExtractionFailedError(f"Could not read the PDF file: {exc}") from exc

    text = "\n".join(pages).strip()
    if not text:
        raise UnreadableReceiptError(
            "This PDF has no selectable text (it may be a scanned image). "
            "Please upload it as an image (PNG/JPG) instead."
        )
    return text


def extract_from_text(text: str) -> ReceiptResult:
    
    if not text or not text.strip():
        raise UnreadableReceiptError("No text could be read from the document.")

    snippet = text.strip()
    return _extract_with_retries(
        lambda: [f"{_USER_PREFIX}\n\nRECEIPT TEXT:\n{snippet}"]
    )


def extract_from_image(image_bytes: bytes, mime_type: str) -> ReceiptResult:

    return _extract_with_retries(
        lambda: [
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            _USER_PREFIX,
        ]
    )


def extract_from_pdf(pdf_bytes: bytes) -> ReceiptResult:
   
    return extract_from_text(_pdf_to_text(pdf_bytes))


def extract_receipt(file_bytes: bytes, content_type: str) -> ReceiptResult:
    
    content_type = (content_type or "").lower().strip()

    if content_type == "application/pdf":
        return extract_from_pdf(file_bytes)
    if content_type.startswith("image/"):
        return extract_from_image(file_bytes, content_type)

    raise UnsupportedFileTypeError(
        f"Unsupported file type '{content_type}'. "
        "Please upload an image (PNG/JPG) or a PDF."
    )