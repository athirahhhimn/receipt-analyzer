# Receipt & Expense Analyzer

A full-stack web application that extracts structured data from receipt images and PDFs using Google Gemini Vision AI, stores the data in SQLite, analyses spending patterns, and generates personalised money-saving insights.

---

## Project Overview

### Problem Statement

Tracking daily expenses manually is tedious and error-prone. People collect paper receipts that pile up, get lost, or are forgotten. Even when someone tries to record their spending, they have to type out every item, categorise it, and calculate totals themselves. Most people give up after a few days because the effort outweighs the benefit.

Malaysian receipts add extra complexity: they mix English and Malay (Bahasa Malaysia), use local terms like "Jumlah" for total and "Cukai" for tax, and follow date formats that differ from international standards. Generic expense trackers don't handle these well.

### Target Users

This system is built for individuals in Malaysia who want to understand where their money goes without the effort of manual data entry. The typical user takes a photo of their receipt after a purchase and uploads it. The AI handles the rest — reading the receipt, extracting items and prices, categorising spending, and offering advice.

### System Goal

Turn a receipt photo into actionable financial insight in under 10 seconds, with zero manual data entry. The user uploads a receipt image or PDF, and the system automatically extracts the merchant name, date, itemised list with prices and categories, and total amount. Over time, as more receipts are uploaded, the system builds a spending profile and generates AI-powered tips to help the user save money.

---

## System Architecture

### Data Flow

```
Receipt Image/PDF
        │
        ▼
┌─────────────────┐
│   Frontend      │  User uploads file via browser
│   (FastAPI +    │  Proxies request to backend
│    Jinja2)      │
│   Port 8000     │
└────────┬────────┘
         │ HTTP POST /receipts/upload
         ▼
┌─────────────────┐
│   Backend       │  1. Validates file (type, size, duplicates)
│   (FastAPI)     │  2. Sends to Gemini Vision AI for extraction
│   Port 8001     │  3. Validates extracted JSON with Pydantic
│                 │  4. Saves receipt + items to SQLite
│                 │  5. Saves original image to disk
│                 │  6. Returns structured JSON to frontend
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌────────┐ ┌────────────┐
│ SQLite │ │ Gemini AI  │
│  (.db) │ │ (Google)   │
└────────┘ └────────────┘
```

**Upload flow:** Browser → Frontend proxy → Backend → Gemini AI → Pydantic validation → SQLite → Response with extracted data.

**Analytics flow:** Browser → Frontend proxy → Backend → SQLite aggregation queries → Response with category breakdown and totals.

**Insights flow:** Browser → Frontend proxy → Backend → SQLite (last 30 days summary) → Gemini AI (spending tips) → Pydantic validation → Cached response.

### Module Breakdown

The backend has four Python modules inside `backend/src/`:

| Module | Responsibility |
|---|---|
| `database.py` | All SQLite operations — schema creation, CRUD for receipts and items, analytics queries, duplicate checking. No other module writes SQL. |
| `extractor.py` | Gemini Vision AI integration — sends receipt images/PDFs to Gemini, parses the JSON response, validates it with Pydantic, handles retries and rate limits. Defines the `ReceiptResult` and `ReceiptItem` data models. |
| `analyzer.py` | AI spending insights — queries the database for 30-day spending, computes factual metrics (top category, biggest expense), sends a summary to Gemini for personalised saving tips, caches results for 1 hour. |
| `app.py` | FastAPI server — all HTTP endpoints, error handling, file upload processing, image storage, request validation. Wires the other three modules together. |

The frontend has two files inside `frontend/src/`:

| File | Responsibility |
|---|---|
| `main.py` | Small FastAPI server that serves the HTML template and passes the backend URL. |
| `templates/chat_page.html` | Single-page dashboard with Bootstrap — upload form, spending summary, category breakdown, receipt list, receipt detail modal with image preview, AI insights panel. |

---

## Setup & Installation

### Prerequisites

- Python 3.14
- [uv](https://docs.astral.sh/uv/) package manager
- A Google Gemini API key (get one at [Google AI Studio](https://aistudio.google.com/apikey))

### Step 1 — Clone and install dependencies

```bash
cd backend
uv sync

cd ../frontend
uv sync
```

### Step 2 — Set up environment variables

Create a `.env` file inside the `backend/` folder:

```
GOOGLE_API_KEY=your_gemini_api_key_here
```

The backend loads this automatically on startup via `python-dotenv`.

### Step 3 — Start the backend (Terminal 1)

```bash
cd backend
uv run uvicorn src.app:app --host 0.0.0.0 --port 8001
```


### Step 4 — Start the frontend (Terminal 2)

```bash
cd frontend
uv run uvicorn --app-dir src main:app --host 0.0.0.0 --port 8000
```

### Step 5 — Open the dashboard

Go to `http://localhost:8000` in your browser.

### API Documentation

The backend has auto-generated interactive API docs at `http://localhost:8001/docs` where you can test every endpoint directly.

---

## Features

### 1. Receipt Upload and AI Extraction

Upload a receipt image (JPG, PNG, WebP) or PDF. The system sends it to Google Gemini 2.5 Flash, which reads the receipt and returns structured data: merchant name, date, total, currency, and every line item with its price and category. The extraction handles Malaysian receipts that mix English and Malay, recognises local terms (Jumlah = total, Cukai = tax, Tunai = cash, Baki = change), and converts Malaysian date formats to ISO standard.

The AI is instructed to never guess — if a price or field is unclear, it returns null rather than risk an incorrect value. Accuracy over completeness.

### 2. Receipt Image Preview

When a receipt is uploaded, the original image is saved to disk alongside the extracted data. When viewing a receipt's details, the original image is displayed at the top of the modal so the user can visually verify that the AI extracted the data correctly. Images are served via a dedicated endpoint and cleaned up when the receipt is deleted.

### 3. Spending Summary

The dashboard shows the total amount spent (in RM) and the number of receipts uploaded. This updates automatically after every upload or deletion.

### 4. Category Breakdown

All spending is grouped into 8 categories: food, transport, utilities, shopping, entertainment, health, education, and other. The dashboard shows coloured progress bars for each category, sorted by highest spending. Categories are assigned by the AI during extraction and normalised by the backend so invalid values can never enter the database.

### 5. Recent Receipts List

A list of all uploaded receipts showing merchant name, date, and total amount, newest first. Each receipt is clickable and opens a detail modal showing the original image, all extracted items in a table with category badges, and a delete button.

### 6. Receipt Deletion

Any receipt can be deleted from the detail modal. Deletion removes the receipt record, all its line items, and the saved image file from disk. The dashboard refreshes immediately after deletion.

### 7. Duplicate Detection

Before saving a new receipt, the backend checks if a receipt with the same merchant, date, and total already exists. If so, it returns a 409 error with a clear message instead of creating a duplicate. This prevents accidental double-uploads of the same receipt.

### 8. AI Spending Insights

The AI Insight panel analyses the last 30 days of spending and provides a monthly summary (2-3 sentences), the top spending category, the single biggest expense, exactly 3 personalised saving tips tailored to the user's actual categories, and any unusual spending patterns. The factual numbers (totals, top category, biggest item) are computed in Python from the database so they are always correct. Only the subjective parts (tips, summary wording) come from Gemini. Results are cached for 1 hour to avoid unnecessary API calls.

### 9. Structured Error Handling

Every error returns a consistent JSON shape: `{"error": "short message", "detail": "explanation"}`. The frontend displays these as clear messages to the user. Specific cases handled include: file too large (>10 MB), unsupported file type, empty file, unreadable/blurry receipt, duplicate receipt, Gemini rate limiting, and missing API key.

### 10. Retry and Rate Limit Handling

If Gemini returns a malformed JSON response, the system retries with a stricter prompt (up to 5 attempts). If Gemini returns a 429 (rate limit) or 503 (service unavailable), the system waits 65 seconds and retries. This makes the extraction robust against temporary API issues.

---

### Trade-offs

| Decision | Benefit | Trade-off |
|---|---|---|
| No user authentication | Simpler to set up and use | Only one person can use the system |
| SQLite instead of PostgreSQL | Zero setup, single file | Cannot handle concurrent multi-user writes |
| Saving images to disk | Simple file I/O, easy to debug | Images are not backed up with the database |
| 1-hour insight cache | Saves API quota and responds instantly | New uploads may not reflect in insights for up to 1 hour (mitigated by clearing cache on upload) |
| Retry with stricter prompt | Recovers from malformed AI output | Adds latency on failure cases (up to 5 retries) |
| All categories assigned by AI | No manual categorisation needed | AI may miscategorise ambiguous items (mitigated by falling back to "other") |

---

## Limitations

### Known Issues

- **Scanned PDFs are not supported.** If a PDF contains only scanned images (no selectable text), the system returns an error asking the user to upload as an image instead. A future improvement would automatically convert PDF pages to images and send them to Gemini Vision.

- **Insights only cover the last 30 days.** If all uploaded receipts are older than 30 days, the AI insights return a generic placeholder message. There is no option to change the analysis window.

- **No offline mode.** Both the receipt extraction and the insights generation require an active internet connection and a valid Gemini API key. The system cannot process receipts offline.

- **Single currency assumption.** Although the database stores a currency field, the analytics sum everything as if it were MYR. Receipts in other currencies are not converted before aggregation.

- **No receipt editing.** If the AI extracts an item name or price incorrectly, the user must delete the entire receipt and re-upload it. There is no way to manually correct individual fields or items.

### Future Improvements

- **Date filtering on the dashboard** — Add filter buttons (Week / Month / Year / All) to the spending summary. The backend already supports `GET /analytics?start=&end=` query parameters; only the frontend needs the buttons.

- **Spending chart over time** — Add a stacked bar chart showing weekly or monthly spending trends by category. The backend already has a `GET /analytics/timeline?period=weekly` endpoint ready for this; it needs a Chart.js chart on the frontend.

- **Receipt editing** — Allow users to correct extracted data (fix a wrong price, change a category) without re-uploading the entire receipt.

- **Multi-user support** — Add user accounts with authentication so multiple people can use the same system with separate data.

- **Budget alerts** — Let users set monthly spending limits per category and receive warnings when they approach or exceed them.
