# **RailSaathi AI**

### **Intelligent Train Journey Discovery System**

RailSaathi AI is an Indian railway journey-discovery project designed to help passengers explore alternative journeys when a direct train ticket is unavailable or waitlisted.

Instead of searching only for direct trains, RailSaathi AI explores:
- Multiple station-to-station seat segments on the **same train**.
- Connections involving **different trains**.
- Chronologically valid combinations of train segments (mixed journeys).
- Unified multi-engine orchestration with deduplication, budget safeguards, and single-rule re-ranking.

The backend verifies the availability of each individual segment and only labels a complete combination as available when every segment has an explicit `AVAILABLE` response from the railway data provider.

> **Project status:** Phases 1–5 backend implementation is fully completed and tested (204 tests passing, zero failures, zero live credits required).

---

## **The problem**

A passenger searching for a long-distance journey may find that the direct train is waitlisted or unavailable. However, individual portions of the same train route may have separate availability, or a journey may be possible by changing trains at an intermediate station.

Conventional direct-route searches may not reveal these alternatives.

RailSaathi AI explores these possibilities and presents candidate journeys with their segment details, timings, availability status, and fares where provided by the data source.

---

## **Repository structure & deliverables**

```
RailSaathiAI/
├── api/                                    # Vercel serverless entry point
│   └── index.py                           #   imports the FastAPI app (ASGI)
├── backend/                               # Complete FastAPI backend application (Phases 1–6)
│   ├── app/
│   │   ├── main.py                        # FastAPI entry point (Swagger at /docs)
│   │   ├── api/routes.py                  # Endpoints (journeys/search, agent/chat, ...)
│   │   ├── agent/                         # Conversational agent (orchestrator, extractor,
│   │   │                                 #   station resolver, LLM clients: gemini/demo/mock)
│   │   ├── core/config.py                 # Settings & budget limits (unified_* envelope)
│   │   ├── services/                      # Journey discovery engines & orchestrator
│   │   ├── schemas/                       # Pydantic v2 validation models
│   │   ├── models/                        # Internal domain models
│   │   └── providers/                     # RailCore client + offline demo provider
│   ├── tests/                             # Comprehensive test suite
│   ├── .env.example                       # Documented environment variables template
│   ├── pytest.ini                         # Test runner configuration
│   └── requirements.txt                   # Backend dependencies
├── index.html                              # Frontend (single-file React app, served at /)
├── vercel.json                             # Vercel routing: static frontend + Python API
├── requirements.txt                       # Root deps for the Vercel Python function
├── railroute_phase2_credit_report.json    # Phase 2 credit report
├── railroute_phase3_report.json           # Phase 3 delivery report
├── railroute_phase4_report.json           # Phase 4 delivery report
├── railroute_phase5_step1_unified_orchestration_audit.md
├── railroute_phase5_step2_unified_orchestration_implementation_report.md
├── railroute_same_train_search_real_*.json
├── railroute_mixed_search_sample.json
└── pytest.ini                             # Root test discovery configuration
```

---

## **How it works**

### **1. Railway data integration — Phase 1**

The backend integrates with the RailCore railway data API to retrieve train schedules and segment-level seat availability.

Implemented capabilities:
- Train discovery and schedule retrieval.
- Single and batch seat-availability requests.
- Availability response normalization.
- Retry and rate-limit handling.
- Query deduplication and API-credit tracking.
- Search-budget enforcement.
- Optional HMAC request signing.

Only an explicit provider status of `AVAILABLE` is treated as confirmed availability. Unknown responses and provider errors are never converted to available or unavailable.

### **2. Same-train combination engine — Phase 2**

This engine looks for alternative journeys using multiple station-to-station segments on the same train.

```
MYS ─────── ASK ─────── DWR
       Train A
    Segment 1   Segment 2
```

The passenger remains on the same train, but the journey is covered by separate seat segments. The engine checks each segment individually and combines only valid, chronologically ordered segments.

Endpoint:
```http
POST /api/v1/journeys/same-train/search
```

### **3. Train-change connection engine — Phase 3**

This engine discovers journeys that require changing trains at an interchange station.

```
MYS ───── SBC       Train A
             │
             │ Change trains
             ▼
         SBC ───── UBL ───── DWR
                  Train B
```

The engine uses time-dependent chronological search over schedule-derived train legs with validated transfer buffers.

Endpoint:
```http
POST /api/v1/journeys/train-change/search
```

### **4. Mixed journey engine — Phase 4**

Combines both same-train seat changes and train changes into one coherent journey, maintaining chronological event ordering, station cycle prevention, and independent ticket segment verification.

Endpoint:
```http
POST /api/v1/journeys/mixed/search
```

### **5. Unified journey orchestration — Phase 5**

Orchestrates all three discovery engines under a single shared budget envelope, performs segment-signature deduplication, re-validates availability partitions, and re-ranks journeys using a deterministic primary preference (`CHEAPEST`, `FASTEST`, `FEWEST_TRAIN_CHANGES`, `FEWEST_SEAT_CHANGES`).

Endpoint:
```http
POST /api/v1/journeys/search
```

---

## **Current implementation status**

| Component | Status | Notes |
|---|---|---|
| RailCore API integration (Phase 1) | Implemented | Verified on live and mock data |
| Same-train combination engine (Phase 2) | Implemented | 34 automated tests passing |
| Train-change connection engine (Phase 3) | Implemented | 37 automated tests passing |
| Mixed journey engine (Phase 4) | Implemented | 43 automated tests passing |
| Unified journey orchestration (Phase 5) | Implemented | 35 automated tests passing |
| Conversational agent (Phase 6) | Implemented | Multi-turn chat, ambiguity clarification, corrections, preference re-ranking; Gemini / offline demo LLM |
| Frontend UI (`index.html`) | Integrated | Chat + direct form search, journey cards, details, booking review; tested end-to-end in a real browser against the demo backend |
| Offline demo mode (zero credits) | Working | `ENVIRONMENT=demo LLM_PROVIDER=demo` replays the real captured corridor data through the full pipeline with no RailCore usage |
| Vercel deployment | Ready | `vercel.json` + `api/index.py` + root `requirements.txt` (see below) |

---

## **Configuration and local development**

1. **Configure environment:**
   Copy `backend/.env.example` to `backend/.env` and set your keys:
   ```env
   RAILCORE_API_KEY=rk_live_your_api_key_here
   # comma-separated list enables multi-key rotation + failover
   ```

2. **Offline demo mode (ZERO RailCore credits, no keys needed):**
   The demo provider replays the real captured corridor data (trains 17301 /
   12781 / 20676 / 16210 / 11005 / 17309, real station sequence and real
   fares recorded during the live runs) through the complete pipeline:
   same client code, same normalizers, same journey engines, same response
   contracts — only the HTTP transport is in-process and free.
   ```bash
   cd backend
   ENVIRONMENT=demo LLM_PROVIDER=demo uvicorn app.main:app --reload
   # open http://127.0.0.1:8000/  (frontend served same-origin)
   ```

3. **Live mode (real RailCore data):**
   ```bash
   cd backend
   uvicorn app.main:app --reload          # uses backend/.env keys
   ```
   Swagger UI is accessible at `http://127.0.0.1:8000/docs`.

4. **Run test suite:**
   ```bash
   pytest
   ```

5. **Frontend-only development:** serve the static file and keep the API on
   port 8000 (CORS is pre-configured for localhost:5500/3000):
   ```bash
   python3 -m http.server 5500     # http://localhost:5500/index.html
   ```

---

## **Deploying to Vercel**

The repository is Vercel-ready as a single project: the static frontend and
the Python serverless API are deployed together.

1. Push this repository to GitHub and import it in Vercel (framework preset:
   *Other*).
2. Add the environment variables from `backend/.env.example` in the Vercel
   project settings (Production + Preview): `RAILCORE_API_KEY` (or the
   comma-separated pool), `GEMINI_API_KEY`, `LLM_PROVIDER=gemini`,
   `LLM_MODEL`, `RAILCORE_BASE_URL`, and any budget overrides you want.
   Secrets live only in the Vercel dashboard — never in the repo.
3. Deploy. Routing (already configured in `vercel.json`):
   - `/` and `/index.html` serve the static frontend;
   - `/api/v1/*`, `/docs`, `/openapi.json` are rewritten to the Python
     function `api/index.py`, which mounts the FastAPI app (maxDuration 60s
     so the unified search budget fits the Hobby plan).
4. Notes for serverless: the in-memory session store is per-instance and
   ephemeral (fine for demos; swap in Redis for durable multi-instance
   sessions later), and cold starts add ~1–2s to the first request.

For a zero-credit Vercel demo, set `ENVIRONMENT=demo` and
`LLM_PROVIDER=demo` as the project's environment variables instead — the
full experience runs with no RailCore usage.

---

## **Team and acknowledgements**

RailSaathi AI is being developed for the **Data Sprint 2.0 — Open Innovation Track**, organized by Anveshan, the Applied Research & AI/ML Club of The National Institute of Engineering, Mysuru.

**Disclaimer:** RailSaathi AI is an independent journey-discovery project and is not an official IRCTC or Indian Railways application. It does not issue tickets, reserve seats, or guarantee availability. Passengers must verify current availability, fares, ticket conditions, and applicable railway rules with authorized booking providers before travel.
