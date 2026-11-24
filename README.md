

# **RailSaathi AI**

### **Intelligent Train Journey Discovery System**

RailSaathi AI is an Indian railway journey-discovery project designed to help passengers explore alternative journeys when a direct train ticket is unavailable or waitlisted.

Instead of searching only for direct trains, RailSaathi AI explores: -\
Multiple station-to-station seat segments on the **same train**. -\
Connections involving **different trains**. - Chronologically valid combinations of train segments.

The backend verifies the availability of each individual segment and only labels a complete combination as available when every segment has an explicit `AVAILABLE` response from the railway data provider.

> **Project status:** Partially functional backend prototype. Phases 1--3 are implemented and tested. The mixed-journey engine, AI agent interface, and complete end-to-end application are planned work, not completed features.

## **The problem**

A passenger searching for a long-distance journey may find that the direct train is waitlisted or unavailable. However, individual portions of the same train route may have separate availability, or a journey may be possible by changing trains at an intermediate station.

Conventional direct-route searches may not reveal these alternatives.

RailSaathi AI explores these possibilities and presents candidate journeys with their segment details, timings, availability status, and fares where provided by the data source.

## **How it works**

### **1. Railway data integration --- Phase 1**

The backend integrates with the RailCore railway data API to retrieve train schedules and segment-level seat availability.

Implemented capabilities: - Train discovery and schedule retrieval. - Single and batch seat-availability requests. - Availability response normalization. - Retry and rate-limit handling. - Query deduplication and API-credit tracking. - Search-budget enforcement. - Optional HMAC request signing.

Only an explicit provider status of `AVAILABLE` is treated as confirmed availability. Unknown responses and provider errors are not treated as unavailable or available.

### **2. Same-train combination engine --- Phase 2**

This engine looks for alternative journeys using multiple station-to-station segments on the same train.

For example:

```
MYS ─────── ASK ─────── DWR
       Train A
    Segment 1   Segment 2
```

The passenger remains on the same train, but the journey is represented by separate seat segments. The engine checks each segment individually and combines only valid, chronologically ordered segments.

Implemented capabilities: - Train discovery and candidate filtering. - Full schedule retrieval and station-stop validation. - Intermediate-station segment generation. - Availability-guided search expansion. - Overnight journey and day-offset handling. - Arbitrary-size segment partitions without a fixed seat-change cap. - Fare aggregation and comparison with direct-route fare when available. - Cheapest, fastest, and fewest-seat-changes ranking. - Deduplication and configurable search budgets. - Honest partial-search statuses when limits are reached.

Endpoint:

```
POST /api/v1/journeys/same-train/search
```

### **3. Train-change connection engine --- Phase 3**

This engine discovers journeys that require changing trains at an interchange station.

Example:

```
MYS ───── SBC       Train A
             │
             │ Change trains
             ▼
         SBC ───── UBL ───── DWR
                  Train B
```

The engine uses time-dependent chronological search over schedule-derived train legs.

Implemented capabilities: - Candidate train and schedule discovery. - Interchange-station validation. - Configurable minimum transfer buffer and maximum transfer wait. - Chronological connection validation. - Overnight and calendar-day handling. - Multiple train changes without a fixed change-count cap. - Cycle prevention and duplicate handling. - Availability verification for individual legs. - Fare, duration, waiting-time, and change-count calculations. - Search-budget and search-horizon reporting.

Endpoint:

```
POST /api/v1/journeys/train-change/search
```

## **Current implementation status**

| Component                                      | Status                     |
| ---------------------------------------------- | -------------------------- |
| RailCore API integration                       | Implemented                |
| Same-train combination engine                  | Implemented                |
| Train-change connection engine                 | Implemented                |
| Automated backend tests                        | Implemented                |
| Mixed journey engine                           | Planned --- Phase 4        |
| Alternative railway API providers              | Under evaluation           |
| Station search and station resolution          | Planned                    |
| PostgreSQL persistence and saved searches      | Planned                    |
| Journey refresh and comparison endpoints       | Planned                    |
| Cross-class combinations                       | Planned                    |
| AI agent / conversational interface            | Planned                    |
| Complete frontend and end-to-end integration   | Not yet confirmed complete |
| Railway-rule verification for separate tickets | Pending                    |

**Important:** The project does not book tickets, reserve seats, or process payments. An availability result is a point-in-time response from the data provider; it is not a booking confirmation.

The legality and operational suitability of travelling on multiple separate tickets, including same-train seat changes, must be independently verified with the relevant railway rules and booking provider before passengers rely on such an itinerary.

## **Live demonstration**

During Phase 2 testing, the engine was verified against live RailCore data for:

- Train: `12781`
- Journey date: `2026-11-27`
- Route: `MYS → DWR`
- Class: `3A`
- Direct journey: reported waitlisted
- Alternative segments: `MYS → UBL` and `UBL → DWR`
- Both alternative segments: explicitly reported available
- Combined reported fare: ₹1,400

This is a point-in-time API test result, not a booked ticket or a guarantee of future availability.

## **Testing and verification**

The reported test results after Phase 3:

- **126 tests passed**
- **1 opt-in live test skipped by default**
- Phase 1 and Phase 2 tests remained passing after Phase 3 integration.
- Phase 3 development used deterministic mocked provider responses.
- No additional live RailCore credits were consumed during Phase 3 development.

The automated tests cover areas including: - Schedule parsing and station order. - Availability normalization. - Batch requests and query deduplication. - Search-budget enforcement before provider calls. - Overnight schedules and date offsets. - Same-train segment combinations. - Train interchange and transfer buffers. - Invalid connections and cycles. - Ranking and missing fare handling. - Provider errors and partially verified results. - Search horizon and budget exhaustion.

Run the project's test suite from the backend directory using the test command configured in the repository. The reported test suite uses `pytest`.

### **Live tests**

Live API tests are opt-in and should be run only when valid provider credentials and sufficient API quota are available. The Phase 3 live test is marked `live_api` and uses `RUN_LIVE_API_TESTS=1` to opt in, as documented in the test configuration.

Do not run broad live searches merely to verify local code changes. Prefer deterministic tests and mocks.

## **Technical architecture**

The current backend is built around Python and FastAPI.

Major technologies and components: - **Python** --- backend implementation and journey-search algorithms. - **FastAPI** --- HTTP API and interactive Swagger documentation. - **Pydantic** --- request and response schemas. - **RailCore API** --- external train schedule and seat-availability data. - **pytest** --- deterministic automated testing. - **Time-dependent search / graph-based traversal** --- chronological train-connection discovery. - **Caching, query planning and request budgets** --- reduce repeated provider calls and control API usage.

The journey-search logic is deterministic and rule-based. A trained machine-learning model is not currently part of the implemented search engines. A natural-language AI agent is planned as a future interface to the deterministic search APIs.

## **API endpoints**

The implemented endpoints reported for the current backend include:

| Method | Endpoint                               | Purpose                               |
| ------ | -------------------------------------- | ------------------------------------- |
| `GET`  | `/api/v1/health`                       | Health check                          |
| `GET`  | `/api/v1/config/status`                | Configuration status                  |
| `POST` | `/api/v1/test/segment-availability`    | Segment-availability feasibility test |
| `POST` | `/api/v1/journeys/same-train/search`   | Same-train segment combinations       |
| `POST` | `/api/v1/journeys/train-change/search` | Multi-train connections               |

Open the FastAPI Swagger UI on the locally running server to inspect the exact request and response schemas. The mixed-journey endpoint is not implemented yet.

## **Availability and result semantics**

RailSaathi AI distinguishes between a journey that appears available and one whose segments have not all been verified.

Typical segment statuses include:

- `CONFIRMED_AVAILABLE`
- `NOT_AVAILABLE`
- `WAITLIST`
- `RAC`
- `UNKNOWN`
- `PROVIDER_ERROR`

Typical journey-level statuses include:

- `AVAILABLE_COMBINATION`
- `PARTIALLY_VERIFIED`
- `NO_COMPLETE_JOURNEY`
- `SEARCH_LIMIT_REACHED`
- `NO_CANDIDATE_TRAINS`
- `PROVIDER_ERROR`
- `CONFIG_ERROR`

Exact status availability may vary by endpoint and schema.

A journey must not be presented as a confirmed available combination unless every individual segment has explicit confirmed availability. Provider errors, missing data, and unknown statuses must never be silently converted into availability or unavailability.

`BOOKING_CONFIRMED` is not a supported status because the application does not book tickets.

## **Configuration and local development**

The backend repository contains an `.env.example` file. Use it as the source of truth for the environment variables required by the current code.

General setup:

1. Clone the repository.
2. Open the backend directory.
3. Create and activate a Python virtual environment.
4. Install dependencies from the repository's dependency file.
5. Copy `.env.example` to `.env`.
6. Add your own valid provider credentials to the local `.env`.
7. Start the FastAPI application using the command documented in the repository.
8. Open the Swagger documentation exposed by the running application.

Example commands (adjust the paths and startup command to match the repository):

```
git clone <your-repository-url>
cd <repository-directory>/backend

python -m venv .venv
```

Windows:

```
.venv\Scripts\activate
```

macOS / Linux:

```
source .venv/bin/activate
```

Install dependencies using the repository's dependency file:

```
pip install -r requirements.txt
```

Do not commit `.env`, API keys, tokens, or other secrets. Keep `.env.example` free of real credentials. If a credential has ever been exposed, revoke or rotate it with the provider.

## **API usage and provider limitations**

The current implementation uses RailCore as its railway data provider. Provider quotas, rate limits, pricing, coverage, and response freshness can change.

The project includes query deduplication, batching, caching, and budget enforcement to reduce unnecessary requests. The free-plan quota used during development was nearly exhausted; check the provider dashboard before running live tests.

The architecture is intended to support additional railway data providers in the future. Alternative-provider integration is not yet completed.

## **Known limitations**

- The mixed journey engine is not yet implemented.
- The current railway data integration depends on RailCore.
- Availability can become stale after a provider response.
- Search results are constrained by configured budgets and time horizons.
- Some routes may require explicit via-station hints in the current train-change discovery implementation.
- Missing fare data is represented as unknown rather than estimated.
- Searches currently use one travel class and quota at a time.
- Caching is currently in-process.
- Transfers are validated at the same station; nearby-station transfers are not supported.
- Station-name-to-code resolution, database persistence, and the AI agent interface remain planned work.
- Railway rules governing separate tickets and same-train seat changes have not yet been independently verified.

## **Roadmap**

1. **Phase 4 --- Mixed journey engine:** combine same-train seat changes and train changes in one journey.
2. **Provider resilience:** evaluate and integrate alternative railway data providers through a common provider interface.
3. **Station manager:** station search, aliases, and name-to-code resolution.
4. **Persistence:** PostgreSQL, saved searches, and journey history.
5. **Additional journey capabilities:** cross-class combinations where supported.
6. **AI agent interface:** natural-language interaction with the deterministic search engines.
7. **Frontend integration, deployment, and end-to-end testing.**
8. **Railway-rule verification:** establish what ticket combinations are operationally and legally suitable before presenting them as actionable itineraries.

## **Team and acknowledgements**

RailSaathi AI is being developed by a two-member team for the **Data Sprint 2.0 --- Open Innovation Track**, organized by Anveshan, the Applied Research & AI/ML Club of The National Institute of Engineering, Mysuru.

The project uses RailCore as an external railway data provider and open-source development technologies including Python, FastAPI, and Pydantic.

**Disclaimer:** RailSaathi AI is an independent journey-discovery project and is not an official IRCTC or Indian Railways application. It does not issue tickets, reserve seats, or guarantee availability. Passengers must verify current availability, fares, ticket conditions, and applicable railway rules with the authorized booking provider before travel.
