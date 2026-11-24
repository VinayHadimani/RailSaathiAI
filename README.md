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
├── backend/                               # Complete FastAPI backend application (Phases 1–5)
│   ├── app/
│   │   ├── main.py                        # FastAPI entry point (Swagger at /docs)
│   │   ├── api/routes.py                  # Endpoints (including POST /api/v1/journeys/search)
│   │   ├── core/config.py                 # Settings & budget limits (unified_* envelope)
│   │   ├── services/                      # Journey discovery engines & orchestrator
│   │   ├── schemas/                       # Pydantic v2 validation models
│   │   ├── models/                        # Internal domain models
│   │   └── providers/                     # RailCore provider client & normalizer
│   ├── tests/                             # Comprehensive test suite (204 tests + 2 opt-in live tests)
│   ├── .env.example                       # Documented environment variables template
│   ├── pytest.ini                         # Test runner configuration
│   └── requirements.txt                   # Production & testing dependencies
├── railroute_phase2_credit_report.json    # Phase 2 credit report
├── railroute_phase3_report.json           # Phase 3 delivery report
├── railroute_phase4_report.json           # Phase 4 delivery report
├── railroute_phase5_step1_unified_orchestration_audit.md
├── railroute_phase5_step2_unified_orchestration_implementation_report.md
├── railroute_same_train_search_real_*.json
├── railroute_mixed_search_sample.json
├── railsaathis.html                       # Frontend prototype / showcase
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
| Automated backend test suite | Implemented | **204 passed, 2 skipped (opt-in live), 0 failures** |
| AI Agent interface / Frontend UI | In development | Prototype in `railsaathis.html` |

---

## **Configuration and local development**

1. **Activate virtual environment:**
   ```powershell
   .venv\Scripts\activate
   ```

2. **Configure environment:**
   Copy `backend/.env.example` to `backend/.env` (or configure in your environment) and set your RailCore API key:
   ```env
   RAILCORE_API_KEY=rk_live_your_api_key_here
   ```
   *(For testing with mock data, any dummy string works; real keys are only needed if `RUN_LIVE_API_TESTS=1` is explicitly set).*

3. **Run test suite:**
   ```powershell
   pytest
   ```
   *Expected result: 204 passed, 2 skipped in ~5 seconds.*

4. **Start the API server:**
   ```powershell
   cd backend
   uvicorn app.main:app --reload
   ```
   Swagger UI is accessible at `http://127.0.0.1:8000/docs`.

---

## **Team and acknowledgements**

RailSaathi AI is being developed for the **Data Sprint 2.0 — Open Innovation Track**, organized by Anveshan, the Applied Research & AI/ML Club of The National Institute of Engineering, Mysuru.

**Disclaimer:** RailSaathi AI is an independent journey-discovery project and is not an official IRCTC or Indian Railways application. It does not issue tickets, reserve seats, or guarantee availability. Passengers must verify current availability, fares, ticket conditions, and applicable railway rules with authorized booking providers before travel.
