# RailSaathi AI (RailRoute) — deliverables

## Backend source tree

- `backend/` — the complete FastAPI backend source (Phases 1–5):
  - `app/` — `main.py`, `api/routes.py`, `core/config.py`, `services/*`
    (three engines + unified orchestration, shared query planner, budgets,
    normalizer), `schemas/*`, `models/*`, `providers/*`
  - `tests/` — full mocked suite (204 tests + 2 opt-in live tests,
    skipped by default); `conftest.py` with the synthetic RailCore mocks
  - `README.md`, `requirements.txt`, `pytest.ini`, `.gitignore`,
    `.env.example` (all settings documented; Phase 5 `UNIFIED_*` included)
- `railsaathi-backend-source.zip` — the same tree, zipped.

Notes:
- The real `.env` (API key) is deliberately NOT included. To run:
  `cp .env.example .env`, fill in your key, then
  `pip install -r requirements.txt && python -m pytest`.
- Without any key configured, 2 tests that assert "API key configured"
  fail and the rest pass (204 total pass once a key is present; verified
  in this exact tree with a dummy key — zero live calls, live tests stay
  skipped unless `RUN_LIVE_API_TESTS=1`).

## Phase reports and API samples

- `railroute_phase2_credit_report.json`, `railroute_phase3_report.json`,
  `railroute_phase4_report.json`, `railroute_railcore_feasibility_results.json`
- `railroute_phase5_step1_unified_orchestration_audit.md` (Step 1 audit),
  `railroute_phase5_step2_unified_orchestration_implementation_report.md`
  (Step 2 implementation report)
- Real-API search samples (Phase 1 feasibility, consumed the documented
  credits at the time): `railroute_same_train_search_real_*.json`,
  `railroute_mixed_search_sample.json`
