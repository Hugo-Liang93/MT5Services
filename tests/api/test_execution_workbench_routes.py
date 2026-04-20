"""路由层测试：GET /v1/execution/workbench（P9 Phase 1）。

直接调用路由函数 + 注入 stub WorkbenchReadModel，不走 TestClient。
"""

from __future__ import annotations

from src.api.execution import execution_workbench


class _StubWorkbench:
    def __init__(self, account_alias: str = "live-main") -> None:
        self._account_alias = account_alias
        self.last_call: dict | None = None

    def build(self, *, account_alias, symbol=None, include=None) -> dict:
        if account_alias != self._account_alias:
            raise KeyError(
                f"account_alias not configured for this instance: {account_alias}"
            )
        self.last_call = {
            "account_alias": account_alias,
            "symbol": symbol,
            "include": include,
        }
        payload = {
            "account_alias": account_alias,
            "observed_at": "2026-04-20T10:00:00+00:00",
            "state_version": None,
            "source": {
                "kind": "live",
                "fallback_applied": False,
                "fallback_reason": None,
            },
            "freshness_hints": {},
        }
        if include is None or "execution" in include:
            payload["execution"] = {
                "tier": "T0",
                "tradability": {"verdict": "tradable"},
            }
        if include is None or "exposure" in include:
            payload["exposure"] = {"tier": "T2", "symbols": [], "hotspots": []}
        return payload


def test_workbench_returns_success_response_with_payload() -> None:
    workbench = _StubWorkbench(account_alias="live-main")

    response = execution_workbench(
        account_alias="live-main",
        symbol=None,
        include=None,
        workbench=workbench,
    )

    assert response.success is True
    assert response.data.account_alias == "live-main"
    assert response.data.execution["tier"] == "T0"
    assert response.metadata["operation"] == "execution_workbench"
    assert workbench.last_call == {
        "account_alias": "live-main",
        "symbol": None,
        "include": None,
    }


def test_workbench_returns_account_not_found_when_alias_mismatches() -> None:
    workbench = _StubWorkbench(account_alias="live-main")

    response = execution_workbench(
        account_alias="other-account",
        symbol=None,
        include=None,
        workbench=workbench,
    )

    assert response.success is False
    assert response.error["code"] == "account_not_found"
    assert response.error["details"]["account_alias"] == "other-account"


def test_workbench_validates_unknown_block_in_include() -> None:
    workbench = _StubWorkbench(account_alias="live-main")

    response = execution_workbench(
        account_alias="live-main",
        symbol=None,
        include="execution,nonsense_block",
        workbench=workbench,
    )

    assert response.success is False
    assert response.error["code"] == "invalid_parameter_value"
    assert "nonsense_block" in response.error["message"]
    assert "execution" in response.error["details"]["valid_blocks"]


def test_workbench_propagates_include_filter_to_read_model() -> None:
    workbench = _StubWorkbench(account_alias="live-main")

    response = execution_workbench(
        account_alias="live-main",
        symbol="XAUUSD",
        include="execution,exposure",
        workbench=workbench,
    )

    assert response.success is True
    assert workbench.last_call["include"] == ["execution", "exposure"]
    assert workbench.last_call["symbol"] == "XAUUSD"
    assert response.metadata["blocks"] == ["execution", "exposure"]
    assert response.metadata["symbol"] == "XAUUSD"
