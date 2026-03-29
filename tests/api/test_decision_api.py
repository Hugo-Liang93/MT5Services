from __future__ import annotations

from src.api.decision import decision_brief
from src.api.schemas import DecisionBriefRequest


def test_decision_brief_returns_structured_remote_response() -> None:
    request = DecisionBriefRequest.model_validate(
        {
            "context": {
                "generatedAt": "2026-03-29T12:00:00+00:00",
                "focus": {
                    "workflowId": "decision",
                    "employeeRole": "risk_officer",
                    "title": "风控官",
                    "subtitle": "当前关注决策区",
                    "focusRoles": ["风控官", "交易员"],
                },
                "market": {
                    "symbol": "XAUUSD",
                    "bid": 3025.1,
                    "ask": 3025.4,
                    "spread": 0.3,
                    "balance": 10000,
                    "equity": 10080,
                    "openPositionCount": 1,
                    "openPnl": 80,
                },
                "signals": {
                    "formalCount": 2,
                    "previewCount": 1,
                    "recentCount": 3,
                    "buyCount": 2,
                    "sellCount": 0,
                    "holdCount": 0,
                    "dominantBias": "偏多",
                },
                "risks": {
                    "activeGuardCount": 0,
                    "highImpactWindowCount": 0,
                    "activeGuardLabels": [],
                    "topWarnings": [],
                },
                "operations": {
                    "activeRoleCount": 4,
                    "abnormalRoleCount": 0,
                    "disconnectedRoleCount": 0,
                    "queuePressureCount": 0,
                    "queuePressures": [],
                },
                "system": {
                    "healthStatus": "healthy",
                    "currentTasks": [
                        {"role": "风控官", "status": "approved", "task": "风控通过"}
                    ],
                },
                "recentEvents": [],
            }
        }
    )

    response = decision_brief(request)

    assert response.success is True
    assert response.data is not None
    assert response.data.source == "remote"
    assert response.data.focusTitle == "风控官"
    assert response.data.stance == "偏多"
    assert response.data.recommendedAction == "准备小仓位试单"
    assert len(response.data.evidence) == 4
