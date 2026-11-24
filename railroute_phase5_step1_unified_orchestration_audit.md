# RailSaathi AI — Phase 5, Step 1: Unified Journey Orchestration Audit

Date: 2026-11-24 · Scope: inspection only (no files modified, no live API calls, zero credits consumed)
Codebase: `backend/` @ commit `d0c9bea` (Phase 3+4), Phase 2 @ `d67fa56`, Phase 1 @ `0a79782`
Test suite verified during audit: **169 passed, 2 skipped** (opt-in live tests, not run) — 4.12s.

---

## 1. Existing architecture summary

### 1.1 Code map

```
backend/app/
├── api/routes.py                              4 POST endpoints + /health + /config/status (288 lines)
├── api/deps.py                                get_railcore_client() — lru_cache process singleton
├── core/config.py                             Settings incl. per-phase budget knobs (120 lines)
├── providers/base.py                           InternalAvailabilityStatus + error taxonomy
├── providers/normalizer.py                     strict STATUS_MAP; payload → model normalization
├── providers/railcore.py                       RailCoreClient: schedule / routes/trains /
│                                               availability/seats (+/batch ≤25 items)
├── models/                                    schedule.py, availability.py, route_search.py
├── schemas/                                   segment_test.py (P1), journey_search.py (P2),
│                                               train_change_search.py (P3), mixed_search.py (P4)
└── services/
    ├── same_train_combination_service.py      Phase 2 engine (903 lines)
    ├── train_change_connection_service.py     Phase 3 engine (1,342 lines)
    ├── mixed_journey_service.py               Phase 4 engine (902 lines, SUBCLASSES P3)
    ├── segment_query_planner.py               shared budget-aware query layer (360 lines)
    ├── segment_availability_service.py        Phase 1 workflow (385 lines)
    └── search_budgets.py                      SearchBudgets / TrainChangeBudgets / MixedJourneyBudgets
```

### 1.2 Engine instantiation and calling convention

| Engine | Service class | Constructor | Entry point | Request model | Response model |
|---|---|---|---|---|---|
| P2 same-train | `SameTrainCombinationService` | `(client, SearchBudgets \| None)` | `.run(request) -> (dict, http_status)` | `SameTrainSearchRequest` | `SameTrainSearchResponse` (`results: list[CombinationOut]`) |
| P3 train-change | `TrainChangeConnectionService` | `(client, TrainChangeBudgets \| None)` | `.run(request) -> (dict, int)` | `TrainChangeSearchRequest` | `TrainChangeSearchResponse` (`results: list[TrainChangeJourneyOut]`, `unverified_candidates`) |
| P4 mixed | `MixedJourneyCombinationService(TrainChangeConnectionService)` | `(client, MixedJourneyBudgets \| None)` | `.run(request) -> (dict, int)` | `MixedSearchRequest` (subclass of P3 request, 4 ranking prefs) | `MixedSearchResponse` (`results: list[MixedJourneyOut]`, `unverified_candidates`) |

- Routes (`app/api/routes.py`) build a service **per request** with settings-derived default budgets.
  Only P3/P4 accept **per-request transfer-window overrides** (`min_transfer_buffer_minutes`,
  `max_transfer_wait_minutes`); **no per-request budget overrides** exist today.
- All engines share the `RailCoreClient` singleton (deps.py) and the shared
  `SegmentQueryPlanner` (per service instance — see §5).

### 1.3 Algorithms in one paragraph each

- **P2**: route discovery (S→D) → class pre-filter → cached schedule fetch → stop-window
  extraction → **availability BFS with pruning** (only stops reached by a confirmed partition
  are expanded) → **iterative-deepening DFS enumeration of all valid partitions** (2..n−1
  segments, no fixed cap) → combination assembly + direct-fare comparison. Only
  CONFIRMED_AVAILABLE edges exist, so every returned combination is all-confirmed by
  construction; no unverified channel.
- **P3**: seed discovery S→D (through trains become reference `direct_trains` + corridor
  learning) → first-leg pair discovery (nearest-corridor-first, budget-capped) → first legs →
  **chronological earliest-arrival heap expansion** over schedule-derived legs (transfer
  buffer/max-wait validated before any availability query; no station revisits; consecutive
  trains differ) → assembly + transfers. PROVIDER_ERROR/UNKNOWN legs are traversed and their
  journeys land in `unverified_candidates` (never in `results`).
- **P4**: subclasses P3; overrides `run`/`_explore`/`_build_journey`/`_rank_key` and adds ONE
  edge type: **same-run continuation legs** (`_same_run_continuation_legs`) — keep riding the
  same train run after alighting = seat change (no transfer window). Adds journey dedup by
  segment signature (`_journey_signature`) and a defensive re-validation
  (`_validate_mixed_sequence`) that drops malformed journeys.

### 1.4 Availability normalization (shared)

`providers/normalizer.py` STATUS_MAP — strict; anything unmapped → UNKNOWN:

| Provider string | Internal status | Qualifies as confirmed? |
|---|---|---|
| `AVAILABLE` | `CONFIRMED_AVAILABLE` | **yes — the only one** |
| `RAC` | `RAC` | no (definitive) |
| `WAITLIST` / `WL` | `WAITLIST` | no (definitive) |
| `REGRET` / `NOT_AVAILABLE` / `TRAIN_CANCELLED` / `TRAIN_DEPARTED` | `NOT_AVAILABLE` | no (definitive) |
| anything else / missing / class not offered / failed batch item / missing batch result | `UNKNOWN` | no (**unverified**) |
| HTTP/batch failure | `PROVIDER_ERROR` | no (**unverified** — never "unavailable") |

---

## 2. Current compatibility problems (field-level, verified programmatically)

### 2.1 Journey-level objects

| Aspect | P2 `CombinationOut` | P3 `TrainChangeJourneyOut` | P4 `MixedJourneyOut` |
|---|---|---|---|
| Identifier | `combination_id` = `{train}-{date}-{stops…}` | `journey_id` = `{date}-{train@from}-…-{dest}` | `journey_id` (same format as P3) |
| `journey_type` | `SAME_TRAIN_SEGMENTS` | `TRAIN_CHANGE` | `SAME_TRAIN_SEGMENTS` / `TRAIN_CHANGE` / `MIXED` |
| `journey_status` | **absent** (response-level `SearchStatus` only) | `AVAILABLE_COMBINATION` / `PARTIALLY_VERIFIED` | same as P3 |
| `from_station` / `to_station` | **absent** (only in `search` echo) | present | present |
| `departure_datetime` / `arrival_datetime` (absolute, IST) | **absent** | present | present |
| `total_duration_minutes` | present (day-offset math) | present | present |
| `total_fare`, `fares_complete`, `segments_missing_fare` | present | present | present |
| `seat_changes` | present | **absent** (always 0 — derivable) | present |
| `train_changes` | **absent** (always 0 — derivable) | present | present |
| `total_transfer_wait_minutes` | **absent** (always 0) | present | present |
| Transition blocks | **absent** | `transfers` (TRAIN_CHANGE only) | `transitions` (both kinds; `is_train_change`, `onboard_halt_minutes`) |
| `distinct_trains` / `distinct_train_count` | **absent** | **absent** | present |
| `unverified_segment_numbers` | **absent** | present | present |
| `all_segments_confirmed_available` | always `True` (by construction) | real flag | real flag |
| **P2-only**: `train_number`, `train_name`, `train_run_origin_departure_date`, `fare_comparison` (vs direct through-fare) | present | — | — |

### 2.2 Segment-level objects

- **P3 `TrainSegmentOut` ≡ P4 `MixedSegmentOut`: identical 30-field sets** (same names, same
  annotations) — the only difference is P3 always sets `segment_type="TRAIN"` while P4 uses
  `FIRST_SEGMENT` / `SAME_TRAIN_SEGMENT` / `TRAIN_CHANGE`.
- **P2 `SegmentDetailOut` is a strict 25-field subset** of those; missing exactly:
  `segment_type`, `train_name`, `departure_datetime`, `arrival_datetime`, `booking_url`.
  All five are derivable or defaulted:
  - `segment_type`: first segment → `FIRST_SEGMENT`, others → `SAME_TRAIN_SEGMENT` (a P2
    combination is single-train by definition);
  - absolute datetimes: `journey_date + (day − window[0].day) + HH:MM` in IST (same semantics
    P2 already uses in `_boarding_date` / `_duration_minutes` — must be reused, not
    reinvented, to avoid off-by-one-day bugs on overnight boardings);
  - `booking_url`: `None` (RailCore provides none — same as P3/P4);
  - `train_name`: from the combination-level field.

### 2.3 Enum / semantics mismatches (traps for the unified layer)

1. **`FEWEST_CHANGES` means different things**: in P2 it sorts by **segment count**
   (= seat changes), in P3 by **train changes**. A unified `FEWEST_TRAIN_CHANGES` /
   `FEWEST_SEAT_CHANGES` must be **recomputed from normalized values**, never taken from a
   per-engine ordering.
2. **Status enums differ**: P2 `SearchStatus` (`COMPLETED`, `COMPLETED_NO_COMBINATIONS`,
   `PARTIAL_BUDGET_EXHAUSTED`, `PARTIAL_TIME_LIMIT`, `PARTIAL_PROVIDER_ERROR`,
   `NO_CANDIDATE_TRAINS`, `PROVIDER_ERROR`, `CONFIG_ERROR`) vs P3/P4
   (`AVAILABLE_COMBINATION`, `PARTIALLY_VERIFIED`, `NO_COMPLETE_JOURNEY`,
   `SEARCH_LIMIT_REACHED`, `NO_CANDIDATE_TRAINS`, `PROVIDER_ERROR`, `CONFIG_ERROR`).
   A normalization map is required (e.g. P2 `PARTIAL_*` → unified `SEARCH_LIMIT_REACHED` /
   `PROVIDER_ERROR`; P2 `COMPLETED*` → engine-level completed).
3. **Response-level naming collision**: P2's `candidates` field is a **per-train processing
   report** (`TrainSearchReportOut`), while P3/P4's `unverified_candidates` are
   **journeys**. The unified schema must not reuse the name `candidates` ambiguously.
4. **Ranking tie-breaks differ**: P2 appends `(train_number, combination_id)`; P3 appends
   `journey_id`; P4 appends `(segment_count, journey_id)`. The unified ranker needs ONE
   deterministic tie-break definition (suggested: `segment_count`, then `journey_id`).
5. **Dedup identity**: `combination_id` / `journey_id` strings are not comparable across
   engines. The correct shared key already exists — P4's `_journey_signature`:
   `tuple((train_number, from_seq, to_seq, boarding_date) per segment)`; P2 and P3 segments
   both carry the needed `sequence` values in `StationPointOut`, so the signature is
   computable for all three engines.
6. **P2 has no unverified channel at all** (unavailable/unverified branches are simply not
   enumerated), so a merged `unverified_candidates` list can only be fed by P3/P4. This is
   safe (P2 results are all-confirmed by construction) but must be documented.

---

## 3. Availability validation audit (can an unverified segment appear confirmed?)

**No — verified end-to-end:**

- Normalizer: only provider `AVAILABLE` → `CONFIRMED_AVAILABLE`; anything ambiguous →
  `UNKNOWN`; HTTP failure → `PROVIDER_ERROR` (distinct from `NOT_AVAILABLE`).
- P2: the DFS enumeration only extends along `CONFIRMED_AVAILABLE` edges;
  `_build_combination` emits `all_segments_confirmed_available=True` which is guaranteed by
  construction (never a claim).
- P3/P4: only confirmed legs extend into `state.journeys`; PROVIDER_ERROR/UNKNOWN legs are
  traversed but every journey containing them goes to `state.candidates` → returned as
  `unverified_candidates` with `journey_status=PARTIALLY_VERIFIED` and per-segment
  `unverified_segment_numbers`. Response `success=True` only when ≥1 **fully confirmed**
  result exists.
- P4 additionally re-validates every assembled journey defensively
  (`_validate_mixed_sequence`: endpoints, continuity, no revisits, same-run forward
  movement, transfer windows) and drops violators.
- Planner: failed batch items / missing batch results become `UNKNOWN` with an error
  payload — surfaced, never hidden.
- Availability is never cached across searches (per-instance memo only); timestamps
  (`checked_at`, `last_updated_at` → `oldest_availability_update_at`) are preserved
  per segment and per journey.

**One nuance to carry into Phase 5**: P2 treats unverified legs as dead branches (drops
them), P3/P4 keep them as candidates. A unified response must keep unverified journeys out
of the confirmed bucket regardless of which engine produced them.

---

## 4. Ranking audit

**Existing logic** (all deterministic, all treat missing fare as `inf`, missing duration as
`inf`, never estimate fares):

| Engine | Preferences | Primary key order | Tie-break |
|---|---|---|---|
| P2 `_rank` | CHEAPEST / FASTEST / FEWEST_CHANGES(=segments) | e.g. CHEAPEST: (fare, duration, segments) | train_number, combination_id |
| P3 `_rank_key` | CHEAPEST / FASTEST / FEWEST_CHANGES(=train changes) | e.g. FASTEST: (duration, fare, changes) | journey_id |
| P4 `_rank_key` | CHEAPEST / FASTEST / FEWEST_TRAIN_CHANGES / FEWEST_SEAT_CHANGES | e.g. FEWEST_SEAT_CHANGES: (seats, changes, duration, fare) | segment_count, journey_id |

**Are duration, total fare, seat changes, train changes available?** Yes — after
normalization: `total_duration_minutes` (all engines, overnight-safe), `total_fare` +
`fares_complete` (all engines, null when any fare unknown), `seat_changes` (P2 native;
P3 = 0; P4 native), `train_changes` (P2 = 0; P3/P4 native), `total_transfer_wait_minutes`
(P2 = 0; P3/P4 native), `distinct_trains` (computable everywhere; native in P4).

**Missing for reliable unified ranking:**

1. A **single normalized journey object** (see §2) — the four metrics live in differently
   shaped objects today.
2. Absolute timestamps for P2 journeys (needed for cross-engine arrival comparisons, e.g.
   showing "arrives at" consistently) — derivable (§2.2).
3. A **single deterministic tie-break** definition (avoid composing per-engine orderings).
4. Documented policy for unknown-fare journeys (currently: ranked below all known-fare
   journeys via `inf` — consistent across engines; keep it, but state it in the unified
   response).
5. `FEWEST_SEAT_CHANGES` is not offered by P2/P3 request enums — the unified request owns
   the 4-value enum and the orchestrator maps it per engine (P2/P3 fall back to their
   nearest preference; final ordering is recomputed by the unified ranker anyway).

---

## 5. Budgets and API usage audit

**Enforcement (all engines, before any HTTP request):**

- `SegmentQueryPlanner.requests_available()` / `credits_available(n)` are checked before
  schedule fetches, discovery calls, and every availability batch;
  `_BudgetExhaustedError` aborts cache-miss fetches pre-flight.
- Batch chunk size = `min(batch_size, credits_room)` — a batch never overruns the credit
  budget.
- Every early stop is surfaced as `incomplete_reasons` / engine status — never silent.

**Defaults per single search** (settings-driven, no per-request override today):

| Knob | P2 | P3 | P4 |
|---|---|---|---|
| max_api_requests | 40 | 40 | 40 |
| max_credits (estimate) | 80 | 120 | 120 |
| time_budget_seconds | 30 | 30 | 30 |
| max_trains | 5 | 15 | 15 |
| discovery pairs / states / journeys / horizon | — | 20 / 300 / 25 / 2 d | 20 / 300 / 25 / 2 d |
| max_results | 50 | 20 | 20 |

**Can all three engines run without exceeding provider limits?** Yes — *if* a Phase 5
orchestrator introduces **one shared budget envelope**. Today each engine instance builds
its own `SegmentQueryPlanner`, so three sequential runs would sum to ~120 requests / ~320
estimated credits / up to 90 s worst case. A unified search must:
1. hold ONE planner/budget across engines (or aggregate counters and gate between engine
   runs),
2. cap total wall-clock (e.g. one 45–60 s deadline shared, not 3×30 s),
3. optionally allocate per-engine sub-envelopes so P2 exhaustion cannot starve P3/P4.

**Deduplication and caching (already present):**

- Availability memo per planner instance, key `(train, from, to, date, class, quota)` —
  dedups duplicate queries **within one search**, proven not to double-spend credits
  (`deduplicated_queries` stat; scenario-29 test).
- **Process-wide schedule cache (TTL 600 s) and discovery cache (TTL 300 s) are shared
  across engines** — proven by `test_cross_search_schedule_and_discovery_cache_reuse`.
  Running three engines in one unified search therefore re-uses the S→D seed discovery and
  schedules after the first engine touches them (within TTL).
- Availability is intentionally **never** cached across searches (freshness) — preserved
  timestamps prove this.
- Credit-critical gap: identical availability items queried by two engines in one unified
  search are **not** deduplicated today (separate memos). A shared planner (or a
  cross-engine memo) is the remaining credit-saving opportunity.

---

## 6. API architecture recommendation

**Endpoint**: `POST /api/v1/journeys/search` (unified). Keep the three existing endpoints
untouched and independently usable (strict requirement).

**Where**: new route in `app/api/routes.py`; orchestrator service
`app/services/unified_journey_service.py`; schemas `app/schemas/unified_search.py`;
normalization adapters (P2/P3 → unified journey) inside the orchestrator or a small
`journey_normalizer.py`.

**Request schema (proposed `UnifiedSearchRequest`, extends `MixedSearchRequest`):**

```json
POST /api/v1/journeys/search
{
  "from_station": "MYS",
  "to_station": "DWR",
  "journey_date": "2026-11-27",
  "travel_class": "3A",
  "quota": "GN",
  "ranking_preference": "CHEAPEST",          // CHEAPEST | FASTEST | FEWEST_TRAIN_CHANGES | FEWEST_SEAT_CHANGES
  "via_stations": [],                        // inherited hints (max 3)
  "min_transfer_buffer_minutes": null,      // inherited override
  "max_transfer_wait_minutes": null,        // inherited override
  "engines": ["SAME_TRAIN", "TRAIN_CHANGE", "MIXED"],   // default: all three
  "budget": {                                // optional per-request shared envelope
    "max_api_requests": 60,
    "max_credits": 150,
    "time_budget_seconds": 45
  },
  "max_results": 30
}
```

**Response schema (proposed `UnifiedSearchResponse`):**

- `success`, `status` (unified enum), `message`, `search` (echo), `ranked_by`
- `results: list[UnifiedJourneyOut]` — normalized journeys (superset ≈ `MixedJourneyOut`
  30-field segments + `transitions` + P2 extras where available:
  `fare_comparison`, `train_run_origin_departure_date`) with
  `source_engines: ["SAME_TRAIN","MIXED"]` attribution on each journey;
- `unverified_candidates` + `total_unverified_candidates` (fed by P3/P4 only);
- `deduplication: {duplicates_removed: int, signature_collisions: [...]}` — merge report;
- `per_engine: [{engine, status, total_results, results_before_merge, search_completeness
  (abridged)}]` — transparency without duplicating full diagnostics;
- `direct_trains` (union), `search_completeness` (aggregate reasons + shared budget usage),
  one `api_usage` block (deltas over the whole unified search), `availability_checked_at`,
  `errors`, `notes`.
- Unified status precedence proposal: any confirmed results → `AVAILABLE_COMBINATION`
  (plus `partial: true` flag if any engine reported incompleteness); else limit reasons →
  `SEARCH_LIMIT_REACHED`; else unverified only → `PARTIALLY_VERIFIED`; else provider error →
  `PROVIDER_ERROR`; else `NO_COMPLETE_JOURNEY`; config → `CONFIG_ERROR`.

**Frontend consumption:** one call replaces three; render a timeline from
`segments` + `transitions` (seat changes are onboard halts, train changes are transfers);
availability banner from `all_segments_confirmed_available` +
`unverified_segment_numbers`; honesty banners from `search_completeness.incomplete_reasons`
and `per_engine[].status`; sort chips for the four `ranked_by` values; fares banner from
`fares_complete` / `segments_missing_fare`; optional deep-links to the per-engine
endpoints for full diagnostics.

---

## 7. Test plan

**Existing tests that must continue passing (regression baseline):**
`tests/test_railcore_client.py` (23), `tests/test_segment_availability.py` (25),
`tests/test_same_train_engine.py` (34), `tests/test_train_change_engine.py` (37),
`tests/test_mixed_journey_engine.py` (43, incl. API-shape/OpenAPI tests, the
cross-engine consistency test `test_phase2_phase3_and_mixed_engines_consistent_on_the_same_network`
and the shared-cache reuse test), plus the 2 opt-in live tests (still skipped by default).
**Current baseline verified: 169 passed / 2 skipped.**

**Proposed new suite `tests/test_unified_journey_engine.py` (all mocked via the existing
`make_handler` / `make_client` / `routes_auto` harness; zero live calls):**

Merging & normalization:
1. All three engines contribute results to one merged list; journey shapes normalized
   (P2 journey gains segment_type/absolute datetimes/train_name).
2. P2-derived fields preserved (`fare_comparison`, `train_run_origin_departure_date`).
3. P2 absolute datetime derivation equals its day-offset duration (overnight case).
4. Status normalization map (P2 `COMPLETED_NO_COMBINATIONS` + P3 `AVAILABLE_COMBINATION`
   → unified result; P2 `PARTIAL_*` → `SEARCH_LIMIT_REACHED`/`PROVIDER_ERROR`).

Deduplication:
5. The same physical journey found by P2 and P4 appears once (segment-signature equality);
   `duplicates_removed` reported.
6. Signature distinguishes boundary variants of the same train (different partitions are
   distinct journeys — not over-deduplicated).
7. Duplicate queries across engines consume one credit total (shared planner /
   cross-engine memo) — `api_usage.credits_cost` and `deduplicated_queries` assertions.

Validation (merged set):
8. Every returned journey re-validates: endpoints, continuity, forward movement, no
   station revisits, transfer windows, per-segment boarding dates.
9. Unverified journeys never in `results`, never `all_segments_confirmed_available=true`
   (crafted PROVIDER_ERROR / UNKNOWN on one segment).
10. WAITLIST / RAC / NOT_AVAILABLE segments never qualify in any engine's contribution.

Ranking:
11. CHEAPEST / FASTEST / FEWEST_TRAIN_CHANGES / FEWEST_SEAT_CHANGES over a merged
    multi-engine result set, incl. cross-engine ties broken by ONE rule.
12. Unknown-fare journeys rank below known-fare ones (documented `inf` policy).

Budget exhaustion:
13. Shared envelope stops all engines before limits; honest `SEARCH_LIMIT_REACHED` +
    `incomplete_reasons`; no HTTP request beyond the budget (assert request count).
14. Time budget: unified deadline respected across sequential engine runs.
15. Per-engine failure isolation: one engine's `PROVIDER_ERROR` does not suppress other
    engines' confirmed results; errors surface per engine.
16. Budgets enforced BEFORE requests (craft a tiny envelope; assert zero availability
    calls) — mirrors scenario-27 pattern.

API:
17. `POST /api/v1/journeys/search` happy path, response shape, OpenAPI documentation,
    request validation errors, missing-key 503, provider-failure 502.
18. `engines` subset selection (e.g. `["SAME_TRAIN"]` runs only P2).
19. Regression: the three existing endpoints still behave identically (run one existing
    happy-path test per endpoint through the app).

---

## 8. Potential risks and edge cases

1. **P2 datetime derivation** — overnight day-offset math must reuse P2's own semantics
   (`_boarding_date`, window[0].day anchoring); off-by-one-day bugs are the classic failure.
2. **Over/under deduplication** — dedup by segment signature (not ids, not station codes
   alone); circular services revisit codes but differ by stop `sequence`.
3. **Fair budget allocation** — sequential engine runs can let the first engine exhaust the
   shared envelope; decide per-engine sub-envelopes or ordering (suggested: P2 first —
   cheapest, warms the shared schedule cache for through trains — then P3/P4).
4. **Wall-clock** — 3×30 s defaults → cap the unified deadline (45–60 s) and interleave.
5. **Enum collisions** — `FEWEST_CHANGES` semantics differ between P2 and P3 (seat vs train
   changes); unified preferences must be recomputed from normalized data.
6. **Naming collision** — P2 `candidates` (per-train reports) vs P3/P4
   `unverified_candidates` (journeys); do not blend them under one name.
7. **Rank stability** — do not concatenate per-engine orderings; one unified sort.
8. **Constructor change for shared planner** — the cleanest credit dedup requires a small,
   behavior-preserving change (optional `planner=None` constructor parameter on the three
   services). Engines' public behavior is unchanged; flagged for approval before touching
   engine files (strict requirement).
9. **Credit safety** — only 4 free-plan credits remain; ALL Phase 5 development and tests
   must stay on the mocked harness; any live unified verification is effectively
   unaffordable — keep live tests opt-in and skipped.
10. **Response size** — three engines' diagnostics + merged results can get large; keep
    per-engine sections abridged and cap lists (engines already cap by `max_results`).
11. **Asymmetric unverified semantics** — P2 can never produce unverified journeys;
    merged `unverified_candidates` comes only from P3/P4 (document, don't "fix").
12. **Same-run continuation limitation inherited** (P4): boarding a different run of the
    same train number is not a seat change — a documented limitation that Phase 5 inherits
    verbatim.
13. **Freshness** — availability is per-search, never cached across searches; the unified
    search is one search (one availability snapshot per segment) — keep timestamps intact
    and report the oldest per journey.
14. **No booking claims** — every response continues to carry the point-in-time / no-booking
    notes; the unified notes list should merge the three engines' notes.

---

## 9. Verdict

The three engines are **structurally ready for unified orchestration**: P3 and P4 journey
objects are already field-compatible, P2 requires a mechanical (derivable, semantics-
preserving) normalization, one shared dedup key (segment signature) already exists, and
the shared schedule/discovery caches make a three-engine run credit-efficient. The only
genuine gaps are (a) a shared budget envelope + optional shared planner injection,
(b) a unified ranker with one tie-break rule, (c) status-enum normalization, and
(d) P2 absolute-timestamp derivation. No engine rewrite is required; Phase 5 is an additive
orchestration layer, exactly as the architecture intended.

**No files were modified, no live API calls were made, zero RailCore credits consumed
during this audit. Awaiting approval before any implementation.**
