"""§0dd P3 + §0dj sentinel: research-system.md 不再引用已删除的 Paper Trading
心智模型 / 已重命名的 paper_shadow_required 字段。
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


def test_research_system_doc_uses_demo_validation_required_field() -> None:
    """§0dj sentinel：deployment 合同字段已重命名 paper_shadow_required →
    demo_validation_required（§0dh）。research-system.md 的 deployment 字段
    指南必须使用新名，否则按文档配置 guarded/tf_specific 时旧名被静默丢失，
    护栏失效（user §0di 报告的 P2 #3 问题根因）。
    """
    repo_root = Path(__file__).resolve().parents[2]
    doc = (repo_root / "docs" / "research-system.md").read_text(encoding="utf-8")

    # 合同字段说明区段不能含旧名 paper_shadow_required = ...
    # 仅允许在迁移注释中提及（字符串带"已"或"原"等迁移标记）
    bad_lines = []
    for line in doc.splitlines():
        if "paper_shadow_required" not in line:
            continue
        # 允许显式说明"已移除/已重命名"等迁移文本
        marker_keywords = ("已移除", "已物理移除", "已重命名", "原 ", "原`",
                            "removed", "renamed", "deprecated")
        if not any(kw in line.lower() if kw.isascii() else kw in line for kw in marker_keywords):
            bad_lines.append(line)
    assert not bad_lines, (
        "research-system.md 不能再把 paper_shadow_required 当正式字段名指导——"
        "§0dh 已物理改为 demo_validation_required。\n命中:\n"
        + "\n".join(f"  {line}" for line in bad_lines)
    )


def test_deployment_contract_no_longer_accepts_paper_shadow_required() -> None:
    """§0dj sentinel：StrategyDeployment.from_dict 不再识别 paper_shadow_required
    旧名——配置中残留旧名时 demo_validation_required 默认 False，
    active_guarded/tf_specific 校验会显式 fail，避免护栏静默失效。
    """
    from src.signals.contracts.deployment import StrategyDeployment

    deployment = StrategyDeployment.from_dict(
        "test_strategy",
        {"status": "active", "paper_shadow_required": "true"},
    )
    # 旧名被忽略 → demo_validation_required 仍是默认 False
    assert deployment.demo_validation_required is False, (
        "from_dict 不能再识别旧名 paper_shadow_required（§0dh 已重命名）；"
        "保留双轨支持会让用户写错名时护栏静默失效"
    )
    # 新名仍正常识别
    deployment_new = StrategyDeployment.from_dict(
        "test_strategy",
        {"status": "active", "demo_validation_required": "true"},
    )
    assert deployment_new.demo_validation_required is True
