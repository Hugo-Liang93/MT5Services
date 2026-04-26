"""§0dd P3 sentinel: research-system.md 主流程不再包含已删除的 Paper Trading
阶段（ADR-010 后职责重定位到 demo_validation）。
"""
from __future__ import annotations

from pathlib import Path


def test_research_system_doc_does_not_reference_deleted_paper_trading_stage() -> None:
    """ADR-010 已删除 paper_trading 模块，但 docs/research-system.md 仍在
    "系统定位" 主流程图中标 Paper Trading → 误导研究/运维/架构对当前 demo
    职责的理解。文档必须改为反映 demo_validation 主流程。
    """
    repo_root = Path(__file__).resolve().parents[2]
    doc = (repo_root / "docs" / "research-system.md").read_text(encoding="utf-8")

    # 主流程区段不能再写 Paper Trading 阶段
    # 允许在历史/迁移注释中提及"已删除 Paper Trading 模块"等说明
    forbidden_lines = [
        line for line in doc.splitlines()
        if "Paper Trading" in line and not any(
            tag in line.lower()
            for tag in ("已删除", "removed", "adr-010", "deprecated", "历史")
        )
    ]
    assert not forbidden_lines, (
        "research-system.md 不能再把 Paper Trading 当正式阶段——ADR-010 已"
        "重定位到 demo_validation。\n命中:\n"
        + "\n".join(f"  {line}" for line in forbidden_lines)
    )
