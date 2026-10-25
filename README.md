# Invoice Processing Tool — Autonomous AP & Invoice Verification Engine 📄

An autonomous, 8-stage Accounts Payable (AP) invoice processing and verification engine designed to eliminate manual invoice handling while safeguarding financial integrity through fraud checks, PO matching, and configurable business rules.

Built with **Python 3.12/3.13**, **FastAPI**, **pdfplumber / PyMuPDF + Tesseract OCR**, **Groq LLM** (with full offline caching), and a **Single-Page Dark Executive UI**.

---

## 🏗️ Architecture & 8-Stage Pipeline

```
[ Upload PDF ]
      │
      ▼
┌────────────────────────────────────────────────────────┐
│ Stage 1: Ingestion & OCR Detection                     │
│   • pdfplumber text stream                             │
│   • Automatic fallback to PyMuPDF + Tesseract OCR      │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Stage 2: Structured LLM Extraction                     │
│   • Extracts 12+ invoice fields with confidence scores │
│   • SHA-256 PDF caching (offline demo ready)           │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Stage 3: Validation & Mathematical Integrity           │
│   • Required fields (Inv #, Total, Date)               │
│   • Subtotal + Tax = Total math reconciliation         │
│   • Line items sum cross-check                         │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Stage 4: Vendor Master Verification & Fraud Shield     │
│   • RapidFuzz fuzzy name matching against vendors.csv  │
│   • Tax ID cross-referencing                           │
│   • Bank account modification fraud detection          │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Stage 5: Duplicate Invoice Detection                   │
│   • Normalized invoice number hashing                  │
│   • Rolling date window duplicate prevention           │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Stage 6: Purchase Order (PO) Matching & Ledger         │
│   • Explicit PO reference lookup                       │
│   • Inferred matching by vendor & line item proximity  │
│   • Cumulative split billing balance tracking          │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Stage 7: Deterministic 11-Rule Business Engine         │
│   • Tolerance windows (2% or $500 / ₹ fixed)           │
│   • Line item unit price cross-check                   │
│   • Auto-approval ceilings and currency safety         │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Stage 8: Decisioning & Natural Language Explanation    │
│   • Outcomes: AUTO_APPROVED, APPROVED_WITH_NOTE,       │
│     HOLD_FOR_REVIEW, REJECTED, REQUEST_INFO            │
│   • Plain-English finance summary & vendor email draft │
└────────────────────────────────────────────────────────┘
```

---

## 🚀 Running Locally

### 1. Prerequisites & Environment
Ensure you are in the project folder and the virtual environment is active:
```bash
# On Linux / macOS:
source .venv/bin/activate

# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
```

### 2. Configure Environment (`.env`)
Copy `.env.example` to `.env`:
```ini
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-120b
GROQ_FALLBACK_MODEL=openai/gpt-oss-20b
TESSERACT_CMD=tesseract
RATE_LIMIT=60/hour
PORT=7860
```
*(Note: If no Groq API key is set, the app seamlessly starts and runs offline using pre-computed sample extractions in `data/sample_cache.json`.)*

### 3. Run the Test Suite
All 27 integration tests run offline without external network calls:
```bash
pytest tests/ -v
```

### 4. Start the Application Server
```bash
uvicorn app.main:app --host 127.0.0.1 --port 7860 --reload
```

Open your browser to:
**`http://localhost:7860`**

---

## 🌐 Deploying to Hugging Face Spaces (Docker SDK)

1. Create a new Space on [Hugging Face](https://huggingface.co/spaces) with **Docker** SDK.
2. Push this repository:
```bash
git remote add space https://huggingface.co/spaces/<your-username>/<space-name>
git push space main
```
3. Set your Space secrets in **Settings > Variables and secrets**:
   - `GROQ_API_KEY`: *(Optional for live LLM extraction; offline cache works automatically)*
   - `RATE_LIMIT`: `60/hour`
4. The Dockerfile automatically binds to port `7860` and runs as non-root user (UID 1000).

---

## 📡 API Reference

- `POST /api/upload`: Upload PDF invoice (rate limited at 60/hr/IP).
- `POST /api/run-sample`: Trigger processing for pre-loaded sample.
- `GET /api/stream/{run_id}`: Real-time Server-Sent Events (SSE) telemetry for all 8 stages.
- `GET /api/runs`: List historical runs with search and filtering.
- `GET /api/runs/{run_id}`: Retrieve full stored run details and audit log.
- `POST /api/runs/{run_id}/override`: Manual human-in-the-loop decision override.
- `GET /api/metrics`: Aggregated financial throughput and hours saved KPIs.
- `GET /api/rules` & `POST /api/rules`: View and dynamically update business thresholds.
