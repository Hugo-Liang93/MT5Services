# Main Branch Code Review (2026-03-17)

## Scope
- Branch reviewed: `work` (only local branch available in this environment).
- Focus: API bootstrap/auth middleware, runtime config coupling, and startup safety.

## Findings

### 1) CORS wildcard origin + credentials is an unsafe/incompatible default (fixed)
- **Severity:** Medium
- **Location:** `src/api/__init__.py`
- **Issue:** The app enabled `allow_origins=["*"]` while also enabling `allow_credentials=True`. Browsers reject wildcard origin with credentials, and this can produce confusing cross-origin behavior for cookie/credentialed requests.
- **Risk:** Frontend clients may see CORS failures despite server-side CORS middleware being enabled.
- **Action taken:** Updated middleware bootstrap to disable credentials when wildcard origins are used.

### 2) API config snapshot is captured at import time
- **Severity:** Low
- **Location:** `src/api/__init__.py`
- **Issue:** `api_config = get_api_config()` is evaluated once at import. If runtime config reloading is expected, middleware behavior (auth/cors/docs toggles) will not update unless process restarts.
- **Recommendation:** Resolve mutable runtime flags through a request-time accessor or explicit refresh path when reloading config.

### 3) Health endpoint couples to all major services
- **Severity:** Low
- **Location:** `src/api/__init__.py`
- **Issue:** `/health` depends on both market service and trading service plus ingestor queue stats. If one subsystem is degraded, health may still return `success=True` while embedding errors in payload.
- **Recommendation:** Consider split probes (`/health/live`, `/health/ready`) and explicit readiness status computation.

## Validation notes
- Full `pytest` was attempted, but environment is missing runtime dependencies (`fastapi`, `pydantic`), so suite could not be fully executed.
