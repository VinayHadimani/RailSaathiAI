# RailSaathi AI — Phase 5, Step 2: Unified Journey Orchestration Implementation Report

Date: 2026-11-24 · Baseline: commit `d0c9bea` (Phases 2–4 complete)
Test result after implementation: **204 passed, 2 skipped** (169 pre-existing + 35 new unified tests; the 2 skipped are the unchanged opt-in live tests — NOT run).

---

## 1. Files created and modified

**Created (4):**

| File | Purpose |
|---|---|
| `app/schemas/unified_search.py` | `UnifiedSearchRequest` / `UnifiedSearchResponse` + engine selection, ranking, status, dedup, per-engine, completeness schemas |
| `app/services/journey_normalizer.py` | P2/P3/P4 response → `UnifiedJourneyOut` normalization (P2 timestamp derivation, segment re-typing, transition derivation) |
| `app/services/unified_journey_service.py` | `UnifiedJourneyOrchestrationService` — engine orchestration, shared budget, merge, dedup, validation, ranking, response assembly |
| `tests/test_unified_journey_engine.py` | 35 fully-mocked tests covering every required scenario |

**Modified (3 — routing / configuration / budget only, exactly as authorized):**

| File | Change |
|---|---|
| `app/api/routes.py` | Added `POST /api/v1/journeys/search` (docs, response_model, 502/503 declarations); module docstring |
| `app/core/config.py` | Added `unified_*` settings (requests 60, credits 150, time 45 s, results 30, TTLs, pacing) |
| `app/services/search_budgets.py` | Added `UnifiedJourneyBudgets` (frozen dataclass, clamps, `from_settings`) |

**Engine files untouched:** `same_train_combination_service.py`, `train_change_connection_service.py`, `mixed_journey_service.py`, `segment_query_planner.py` — zero diffs. All 169 pre-existing tests pass unmodified.

## 2. Architecture implemented

`POST /api/v1/journeys/search` → `UnifiedJourneyOrchestrationService.run()`:

1. Engines run in the fixed order **SAME_TRAIN → TRAIN_CHANGE → MIXED** (P2 first — cheapest, warms the shared schedule/discovery caches; the request's `engines` field selects a subset, never the order).
2. Each engine service is constructed exactly as its own endpoint constructs it (**constructor compatibility preserved**), then **one shared `SegmentQueryPlanner` is injected post-construction** (`service.planner = shared_planner`) — no engine code was changed for this.
3. Engine responses (the same dicts their own endpoints serialize) are normalized by `journey_normalizer` into `UnifiedJourneyOut`.
4. Merge → dedup by segment signature → defensive re-validation → recomputed availability partition → unified re-ranking → capped by `max_results`.
5. Failure isolation: a provider error, budget skip or even an unexpected engine exception is recorded per engine while the remaining engines continue (while the shared budget permits); confirmed results from successful engines survive.

Response carries: `success, status, message, search, ranked_by, results, total_results, results_truncated, unverified_candidates, total_unverified_candidates, deduplication, per_engine, direct_trains, search_completeness, api_usage, availability_checked_at, errors, notes`.

Unified status precedence (mirrors the engines' own): confirmed results → `AVAILABLE_COMBINATION`; else `CONFIG_ERROR` (503); else limit statuses → `SEARCH_LIMIT_REACHED`; else unverified only → `PARTIALLY_VERIFIED`; else failure statuses → `PROVIDER_ERROR` (502); else `NO_CANDIDATE_TRAINS` / `NO_COMPLETE_JOURNEY`. A failed/incomplete engine is always reflected in `per_engine`, `errors` and `search_completeness.engines_reporting_incomplete` — including engines whose own early-exit responses report `complete=true` (the engine status itself becomes the honest reason).

## 3. Shared budget implementation (one global envelope)

- **One `UnifiedJourneyBudgets` envelope per unified search**: `max_api_requests`, `max_credits` (item-count estimate), `time_budget_seconds`. Defaults 60 / 150 / 45 s from `unified_*` settings; optional per-request overrides via `request.budget`.
- **Enforcement BEFORE any provider call** by the shared `SegmentQueryPlanner` (unchanged engine code): request/credit guards precede every schedule fetch, discovery call and availability batch; batches are chunked to `min(batch_size, credits_room)` so a batch never overruns the credit budget.
- **Counters never reset between engines**: all three engines draw from the same planner stats; proven by `test_shared_budget_counters_never_reset_between_engines` (8-request envelope: P2 consumes 4, P3 continues counting to exactly 8, P4 is skipped; client-side call count == 8 == envelope).
- **Wall-clock**: one absolute deadline for the whole search. Each engine receives only the *remaining* time as its per-engine time budget; an engine whose turn arrives with < 0.5 s remaining (or no requests/credits) is **skipped with an honest status** (`SKIPPED_TIME_BUDGET_EXHAUSTED` / `SKIPPED_SHARED_BUDGET_EXHAUSTED`) instead of being started — so the global budget is never exceeded even when an engine fails or times out.
- **Cross-engine query deduplication**: the shared planner's availability memo keys `(train, from, to, date, class, quota)` — the same physical item queried by two engines is sent (and paid for) **once**. On the reference network P4 ran with **zero** additional provider calls (all discovery/schedules from the shared TTL caches, all availability from the shared memo): 17 requests / 29 estimated credits for all three engines.
- Engine-local exploration caps (trains, discovery pairs, states, journeys, per-engine results) remain sourced from each engine's own settings; only shared provider usage and total time follow the envelope.

## 4. Normalization and deduplication behavior

**Normalization** (nothing dropped, per the field matrix from the Step 1 audit):
- P3/P4 map 1:1 (identical 30-field segments; P3 segments re-labelled `FIRST_SEGMENT`/`TRAIN_CHANGE`, transfers become unified train-change transitions; P4 transitions map directly).
- P2 is a strict subset, mechanically completed: `segment_type` = `FIRST_SEGMENT`/`SAME_TRAIN_SEGMENT`; `train_name` from the combination; `booking_url` = None (same as P3/P4); **absolute timestamps derived from P2's own overnight boarding-date semantics** — `departure_datetime` = `boarding_query_date` + scheduled departure clock (the engine's own IRCTC date), arrival advances by the engine's arrival-day − departure-day difference; no new date calculation invented. Derived `SAME_TRAIN_SEAT_CHANGE` transitions (onboard halt from the engine's halt data, timestamp difference as fallback; never labelled transfers). P2 extras preserved: `train_number`, `train_run_origin_departure_date`, `fare_comparison` (only present when the journey came from / was first found by the same-train engine).

**Deduplication** by segment signature `tuple((train_number, from_sequence, to_sequence, boarding_date))` per segment — never journey ids or station codes alone. First-discovering engine's representation is kept (fixed engine order); `source_engines` and `engine_journey_ids` record every contributor; each removal increments `duplicates_removed` (with a separate unverified-channel count and abridged `signature_collisions`). Different partitions keep different signatures and remain distinct. Overnight boarding dates match across engines, so overnight journeys dedup correctly too. The unverified channel is deduplicated with the same rule.

**Availability validation** is **recomputed** from the normalized segments (never trusted from engine fields): only every-segment-`CONFIRMED_AVAILABLE` journeys stay in `results`; anything containing WAITLIST / RAC / NOT_AVAILABLE / UNKNOWN / PROVIDER_ERROR moves to `unverified_candidates` with per-segment error payloads, `PARTIALLY_VERIFIED` status and recomputed `unverified_segment_numbers`. A provider error is never an unavailable seat. A defensive re-validation (endpoints, continuity, forward movement, same-stop seat changes, no station revisits, per-segment arrival-after-departure, transfer-window booleans) drops malformed journeys with recorded errors (unreachable by construction). No journey is ever labelled `BOOKING_CONFIRMED` and no booking claim exists anywhere.

## 5. Ranking implementation

One ranker over the **merged** set (per-engine orderings never concatenated), with the single deterministic rule:

1. Primary requested metric — CHEAPEST `(fare, duration, changes, seats)` / FASTEST `(duration, fare, changes, seats)` / FEWEST_TRAIN_CHANGES `(changes, duration, fare, seats)` / FEWEST_SEAT_CHANGES `(seats, changes, duration, fare)`;
2. secondary metrics (the remaining tuple, phase 4 ordering);
3. segment count;
4. journey id.

Unknown fares rank below known fares for CHEAPEST (`fares_complete=False` → `inf`; `total_fare` stays null); missing durations likewise; **fares are never estimated** (documented in `notes`). Per-engine preference mapping (unified 4-value enum → engines' 3-value enum, both FEWEST_* → FEWEST_CHANGES) only steers the engines' internal capped ordering; final ordering is recomputed from normalized data.

## 6. Test results

**204 passed, 2 skipped, 0 failed** (~6 s). The 2 skips are the pre-existing opt-in live tests (unchanged, not run).

35 new tests (all mocked via the existing `make_handler`/`make_client`/TestClient harness): all three engines contributing + merge/attribution; engine subsets + `engines` validation (empty/duplicate/unknown → 422); P2/P3/P4 normalization field-by-field; overnight datetime conversion (duration==timestamp-diff==478, day-2 boarding dates) + overnight cross-engine dedup; minimal cross-engine dedup with exact signature collisions; partitions remain distinct; WAITLIST/RAC/NOT_AVAILABLE parametrized rejection + global "every result segment is CONFIRMED" invariants; unverified separation with preserved `BOOKING_NOT_OPEN` diagnostics; provider-error-never-unavailability (all-503 batch → unverified candidates, `SEARCH_LIMIT_REACHED` per the engines' precedence); all four ranking preferences cross-engine and within-engine; journey-id tie-break; missing-fare ranking; shared query deduplication (call-log ground truth: identical batch items sent exactly once); shared request budget (2 requests, no availability calls); shared credit budget (3 credits, 1 item); budget exhaustion before API requests (1 discovery call only, zero availability items); shared wall-clock deadline (slow provider, later engines skipped at the deadline); counters never reset between engines; one engine provider-failing while others succeed; engine exception isolation (monkeypatched crash); API happy path + full response shape; OpenAPI documentation (paths, refs, 13 Unified components, enums); request validation 422s; missing-key 503; discovery-failure 502; regression of all three existing endpoints (same-train / train-change / mixed still behave identically); `UnifiedJourneyBudgets` settings wiring and clamps.

## 7. Zero live credits consumed

Confirmed. Every new test uses `httpx.MockTransport` against the synthetic 998xx network (no train is real); the full suite never sets `RUN_LIVE_API_TESTS` and the two live tests remain skipped; no API credentials were read, modified or printed; the only HTTP traffic is against `https://railcore.test/v1` in-process mocks. RailCore free-plan balance remains 4 credits, untouched.

## 8. Remaining limitations (inherited or by design, all reported honestly in responses)

1. **Fixed engine order, no per-engine sub-envelopes**: SAME_TRAIN runs first; a generous first engine can consume the shared envelope before TRAIN_CHANGE/MIXED start (they are then *skipped with a reported reason*). Sub-envelope reservation is future work.
2. **Sequential wall-clock**: engines run sequentially; the unified deadline bounds the sum. Parallel engine execution would need budget coordination.
3. **P2 cannot produce unverified candidates** (unavailable/unverified branches are never enumerated there); the merged `unverified_candidates` comes only from TRAIN_CHANGE/MIXED — an asymmetric but documented channel.
4. **Same-run continuation limitation inherited from Phase 4**: boarding a *different run* of the same train number is not recognized as a seat change.
5. **Availability freshness is per-search**: the shared memo returns the *same* outcome object for the same item within one unified search (consistency across engines is a feature); a genuinely fresh re-check within one search is deliberately not performed.
6. **Header-reported `api_usage.credits_cost`** depends on provider response headers; the authoritative budget figure is the item-count `credits_used_estimate` in `search_completeness` (same convention as the engines).
7. **Journey count per engine is bounded by engine-local caps** (journeys/states/pairs from engine settings), surfaced via `per_engine` and aggregate `incomplete_reasons`.
8. **Ranking only re-orders already-truncated engine outputs**: engines cap their own results before merging; per-engine caps were raised to `max(engine default, request.max_results)` to reduce, not eliminate, pre-merge truncation.

**Phase 5 implementation is complete. Not starting Phase 6 — awaiting review.**
