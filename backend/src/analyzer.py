import json
import re
import time

from google.genai import errors, types
from pydantic import BaseModel, ValidationError, field_validator

import database
import extractor

# Configuration

INSIGHT_TEMPERATURE: float = 0.2
CACHE_TTL_SECONDS: int = 60 * 60  # 1 hour
REQUIRED_TIP_COUNT: int = 3

# Pydantic model

class SpendingInsight(BaseModel):
    """The AI-generated spending insight returned to the frontend."""

    top_category: str
    total_spent: float
    biggest_expense: str
    saving_tips: list[str]
    unusual_patterns: list[str]
    monthly_summary: str

    @field_validator("saving_tips")
    @classmethod
    def _must_have_three_tips(cls, value: list[str]) -> list[str]:
        """Enforce exactly 3 saving tips."""
        if len(value) != REQUIRED_TIP_COUNT:
            raise ValueError(
                f"saving_tips must contain exactly {REQUIRED_TIP_COUNT} tips, "
                f"got {len(value)}"
            )
        return value


# Errors

class AnalyzerError(Exception):
    """Raised when insights cannot be generated after all retries."""


# Simple cache: one (timestamp, SpendingInsight) pair

_CACHE: tuple[float, SpendingInsight] | None = None


def _get_cached() -> SpendingInsight | None:
    """Return the cached insight if it is still fresh (< 1 hour old)."""
    global _CACHE
    if _CACHE is None:
        return None
    cached_at, insight = _CACHE
    if time.time() - cached_at < CACHE_TTL_SECONDS:
        return insight
    _CACHE = None
    return None


def _store_cache(insight: SpendingInsight) -> None:
    """Save an insight in the cache with the current timestamp."""
    global _CACHE
    _CACHE = (time.time(), insight)


def clear_cache() -> None:
    """Clear the cached insight (useful after new uploads)."""
    global _CACHE
    _CACHE = None


# Prompts

_BASE_INSTRUCTION = f"""\
You are a friendly Malaysian personal-finance assistant. You will receive a
short summary of spending over the last 30 days. All amounts are in
Malaysian Ringgit (RM).

Based ONLY on the summary, return ONLY a JSON object with exactly these keys:
  "saving_tips": a list of EXACTLY {REQUIRED_TIP_COUNT} short, practical,
      specific money-saving tips tailored to the categories shown.
  "unusual_patterns": a list of short strings describing anything notable.
      Use an empty list [] if nothing stands out.
  "monthly_summary": a friendly 2-3 sentence summary of the month's spending.

Rules:
- Return ONLY the JSON object: no markdown, no code fences, no extra text.
- "saving_tips" MUST contain exactly {REQUIRED_TIP_COUNT} items.
- Keep tips concrete and relevant to the categories shown.
- Do NOT invent spending or numbers that are not in the summary.
"""

_STRICT_INSTRUCTION = _BASE_INSTRUCTION + (
    "\nSTRICT MODE: your previous answer was invalid. Output MUST be a single"
    f" valid JSON object with exactly {REQUIRED_TIP_COUNT} saving_tips and no"
    " text before or after the JSON."
)


# Helpers

def _parse_json(raw: str) -> dict:
    """Parse Gemini's text into a JSON object, tolerating ``` code fences."""
    text = raw.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("Top-level JSON is not an object", text, 0)
    return data


def _build_summary_text(summary: dict, top_category: str, biggest_expense: str) -> str:
    """Turn the database summary into a short block for Gemini."""
    lines = [
        f"Period: {summary['period_start']} to {summary['period_end']} (last 30 days)",
        f"Total spent: RM{summary['total_spent']:.2f} across "
        f"{summary['receipt_count']} receipt(s)",
        "Spending by category:",
    ]
    for category, amount in sorted(
        summary["category_totals"].items(), key=lambda kv: kv[1], reverse=True
    ):
        lines.append(f"  - {category}: RM{amount:.2f}")
    lines.append(f"Top category: {top_category}")
    lines.append(f"Biggest single item: {biggest_expense}")
    return "\n".join(lines)


def _call_gemini(client, contents: list, strict: bool) -> str:
    """Make a single Gemini call for insights."""
    config = types.GenerateContentConfig(
        temperature=INSIGHT_TEMPERATURE,
        response_mime_type="application/json",
        system_instruction=_STRICT_INSTRUCTION if strict else _BASE_INSTRUCTION,
    )
    response = client.models.generate_content(
        model=extractor.MODEL, contents=contents, config=config
    )
    text = response.text
    if not text or not text.strip():
        raise AnalyzerError("Gemini returned an empty insights response.")
    return text


def _generate_via_gemini(summary: dict, facts: dict) -> SpendingInsight:
    """Ask Gemini for the subjective fields and assemble a SpendingInsight."""
    client = extractor._get_client()
    summary_text = _build_summary_text(
        summary, facts["top_category"], facts["biggest_expense"]
    )
    user_message = (
        "Here is the spending summary. Return the JSON object as instructed.\n\n"
        f"{summary_text}"
    )

    use_strict = False
    last_error: Exception | None = None

    for attempt in range(1, extractor.MAX_ATTEMPTS + 1):
        try:
            raw = _call_gemini(client, [user_message], strict=use_strict)
            data = _parse_json(raw)
            return SpendingInsight(
                top_category=facts["top_category"],
                total_spent=facts["total_spent"],
                biggest_expense=facts["biggest_expense"],
                saving_tips=data.get("saving_tips", []),
                unusual_patterns=data.get("unusual_patterns", []),
                monthly_summary=data.get("monthly_summary", ""),
            )
        except errors.APIError as exc:
            last_error = exc
            if exc.code in extractor.RATE_LIMIT_CODES:
                print(
                    f"[analyzer] rate limit ({exc.code}); waiting "
                    f"{extractor.RATE_LIMIT_WAIT_SECONDS}s "
                    f"(attempt {attempt}/{extractor.MAX_ATTEMPTS})"
                )
                time.sleep(extractor.RATE_LIMIT_WAIT_SECONDS)
            else:
                print(
                    f"[analyzer] API error {exc.code} on attempt "
                    f"{attempt}/{extractor.MAX_ATTEMPTS}: {exc}"
                )
            continue
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            use_strict = True
            print(
                f"[analyzer] invalid insight on attempt "
                f"{attempt}/{extractor.MAX_ATTEMPTS}; retrying stricter"
            )
            continue
        except Exception as exc:
            last_error = exc
            print(
                f"[analyzer] unexpected error on attempt "
                f"{attempt}/{extractor.MAX_ATTEMPTS}: {type(exc).__name__}: {exc}"
            )
            continue

    raise AnalyzerError(
        f"Could not generate insights after {extractor.MAX_ATTEMPTS} attempts. "
        f"Last error: {type(last_error).__name__}: {last_error}"
    )


def _empty_insight() -> SpendingInsight:
    """Return a placeholder insight when there is no recent spending."""
    return SpendingInsight(
        top_category="none",
        total_spent=0.0,
        biggest_expense="None",
        saving_tips=[
            "Upload your receipts so your spending can be tracked.",
            "Set a simple monthly budget for food and transport.",
            "Review your spending weekly to catch small leaks early.",
        ],
        unusual_patterns=[],
        monthly_summary=(
            "No spending was recorded in the last 30 days. Upload a few "
            "receipts to get personalised insights."
        ),
    )


# Public API (called by app.py)

def generate_insights(use_cache: bool = True) -> SpendingInsight:
    """Generate (or return a cached) spending insight.

    No user_id needed — analyses all receipts in the database.

    Args:
        use_cache: if True (default), return a cached insight when fresh.

    Returns:
        SpendingInsight: the validated insight.
    """
    if use_cache:
        cached = _get_cached()
        if cached is not None:
            print("[analyzer] returning cached insight")
            return cached

    summary = database.get_spending_last_30_days()

    if summary["receipt_count"] == 0 or not summary["category_totals"]:
        insight = _empty_insight()
        _store_cache(insight)
        return insight

    category_totals = summary["category_totals"]
    top_category = max(category_totals, key=category_totals.get)
    biggest = summary["biggest_expense"]
    biggest_expense = (
        f"{biggest['name']} (RM{biggest['price']:.2f})" if biggest else "Unknown"
    )
    facts = {
        "top_category": top_category,
        "total_spent": summary["total_spent"],
        "biggest_expense": biggest_expense,
    }

    insight = _generate_via_gemini(summary, facts)
    _store_cache(insight)
    return insight