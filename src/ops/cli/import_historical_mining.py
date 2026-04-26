"""把历史 mining JSON 输出导入 research_mining_runs 表。

背景：2026-04 之前所有挖掘都是 `ops/cli/mining_runner.py --json-output` 写到磁盘
（因为 CLI 当时没有 --persist flag），导致 `research_mining_runs` 表长期 0 行。
本工具把磁盘上的历史 JSON 批量落进 DB，让历史发现有统一入口可查。

用法：
    # dry-run（不写 DB，只解析）
    python -m src.ops.cli.import_historical_mining --environment live

    # 真正入库
    python -m src.ops.cli.import_historical_mining --environment live --execute

    # 指定 experiment_id 标注（默认 "historical_<file_mtime>"）
    python -m src.ops.cli.import_historical_mining --environment live --execute \
        --experiment-prefix historical_2026q1

    # 覆写：如 run_id 已在表里，强制 UPDATE（默认也是如此，ON CONFLICT DO UPDATE）
    （无需额外 flag；SQL 自身幂等）

数据来源（扫描规则）：
    - data/artifacts/mining_*.json
    - data/research/mining_*.json
    - 支持自定义 --glob 覆盖

JSON 结构契约（与 mining_runner.py `_run_single` 输出一致）：
    {
      "symbol": "XAUUSD",
      "results": [
        {"tf": "M15", "start": "...", "end": "...", "run_id": "...",
         "data_summary": {...}, "predictive_power": {...}, ...},
        ...
      ],
      "coverage": {...},                 # 可选
      "cross_tf_analysis": {...},        # 可选
      "candidate_discovery": {...}       # 可选
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

logger = logging.getLogger(__name__)

DEFAULT_GLOBS = (
    "data/artifacts/mining_*.json",
    "data/research/mining_*.json",
)


def _iter_candidate_files(globs: tuple[str, ...]) -> List[Path]:
    found: List[Path] = []
    for pattern in globs:
        found.extend(sorted(ROOT.glob(pattern)))
    # 去重保持顺序
    seen: set[Path] = set()
    unique: List[Path] = []
    for p in found:
        if p in seen:
            continue
        seen.add(p)
        unique.append(p)
    return unique


def _parse_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            try:
                # 容错："2025-10-01" 这种纯日期
                dt = datetime.fromisoformat(s + "T00:00:00")
            except ValueError:
                return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _build_row(
    *,
    tf_result: Dict[str, Any],
    symbol: str,
    experiment_id: Optional[str],
    writer: Any,
) -> Optional[tuple]:
    """把 JSON 里一个 TF 的 result 段转成 INSERT row。

    返回 None 表示该记录缺关键字段（run_id / tf），跳过。
    """
    run_id = tf_result.get("run_id")
    tf = tf_result.get("tf") or tf_result.get("timeframe")
    if not run_id or not tf:
        return None

    data_summary = tf_result.get("data_summary") or {}
    n_bars = data_summary.get("n_bars") if isinstance(data_summary, dict) else None

    # start/end 优先从 tf_result 顶层拿（mining_runner 输出格式），
    # 兜底从 data_summary 里拿（更完整的 DataSummary.to_dict 输出）。
    start_time = _parse_iso(
        tf_result.get("start")
        or (data_summary.get("start_time") if isinstance(data_summary, dict) else None)
    )
    end_time = _parse_iso(
        tf_result.get("end")
        or (data_summary.get("end_time") if isinstance(data_summary, dict) else None)
    )

    top_findings = tf_result.get("top_findings") or []
    if not isinstance(top_findings, list):
        top_findings = []

    # full_result 保留整份 tf_result（包含 predictive_power / mined_rules / threshold_sweeps 等）
    # 为避免双重序列化，只复制 dict 引用交给 _json
    full_payload = dict(tf_result)

    return (
        str(run_id),
        experiment_id,
        str(symbol or "XAUUSD"),
        str(tf),
        start_time,
        end_time,
        int(n_bars) if isinstance(n_bars, (int, float)) else 0,
        "completed",
        writer._json(data_summary) if data_summary else None,
        writer._json(top_findings),
        writer._json(full_payload),
    )


def _import_file(
    *,
    path: Path,
    writer: Any,
    experiment_prefix: str,
    dry_run: bool,
) -> Dict[str, int]:
    """解析一个 JSON 并（可选）入库，返回统计。"""
    stats = {"results": 0, "rows_built": 0, "rows_written": 0, "skipped": 0}

    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except Exception as exc:
        logger.error("Failed to read/parse %s: %s", path.name, exc)
        return stats

    symbol = payload.get("symbol", "XAUUSD")
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        logger.warning("%s: no 'results' list (skipping)", path.name)
        return stats
    stats["results"] = len(results)

    # 用文件 mtime 推 experiment_id 后缀，便于区分不同批次的历史导入
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    experiment_id = f"{experiment_prefix}_{mtime.strftime('%Y%m%d')}"

    rows: List[tuple] = []
    for tf_result in results:
        if not isinstance(tf_result, dict):
            stats["skipped"] += 1
            continue
        row = _build_row(
            tf_result=tf_result,
            symbol=symbol,
            experiment_id=experiment_id,
            writer=writer,
        )
        if row is None:
            stats["skipped"] += 1
            continue
        rows.append(row)
    stats["rows_built"] = len(rows)

    if not rows:
        return stats

    if dry_run:
        for r in rows:
            logger.info(
                "[dry-run] would insert: run_id=%s tf=%s n_bars=%s exp=%s",
                r[0],
                r[3],
                r[6],
                r[1],
            )
        return stats

    from src.persistence.schema.research import INSERT_MINING_RUN_SQL

    writer._batch(INSERT_MINING_RUN_SQL, rows)
    stats["rows_written"] = len(rows)
    logger.info(
        "Persisted %s: %d row(s) (experiment=%s)",
        path.name,
        len(rows),
        experiment_id,
    )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import historical mining JSON artifacts into research_mining_runs"
    )
    parser.add_argument(
        "--environment",
        choices=["live", "demo"],
        required=True,
        help="Target DB environment",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually write to DB (default is dry-run)",
    )
    parser.add_argument(
        "--experiment-prefix",
        default="historical",
        help="Prefix for experiment_id assignment (suffix = file mtime YYYYMMDD)",
    )
    parser.add_argument(
        "--glob",
        action="append",
        default=None,
        help="Override glob patterns (can be passed multiple times). "
        "Default: data/artifacts/mining_*.json + data/research/mining_*.json",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    from src.config.instance_context import set_current_environment

    set_current_environment(args.environment)

    globs = tuple(args.glob) if args.glob else DEFAULT_GLOBS
    files = _iter_candidate_files(globs)
    if not files:
        logger.warning("No JSON files matched globs: %s", ", ".join(globs))
        return

    logger.info(
        "Found %d candidate files:\n%s",
        len(files),
        "\n".join(f"  {p.relative_to(ROOT)}" for p in files),
    )

    from src.ops.cli._persistence import _writer_scope

    total_stats = {
        "files": 0,
        "results": 0,
        "rows_built": 0,
        "rows_written": 0,
        "skipped": 0,
    }

    with _writer_scope(args.environment) as writer:
        # importer 跑一次性写，需确保 schema 存在（CLI 独立进程，没有 AppContainer 启动期 ensure_schema）
        writer.research_repo.ensure_schema()
        for path in files:
            stats = _import_file(
                path=path,
                writer=writer,
                experiment_prefix=args.experiment_prefix,
                dry_run=not args.execute,
            )
            total_stats["files"] += 1
            for k in ("results", "rows_built", "rows_written", "skipped"):
                total_stats[k] += stats[k]

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    logger.info(
        "[%s] files=%d results=%d rows_built=%d rows_written=%d skipped=%d",
        mode,
        total_stats["files"],
        total_stats["results"],
        total_stats["rows_built"],
        total_stats["rows_written"],
        total_stats["skipped"],
    )
    if not args.execute:
        logger.info("Re-run with --execute to actually write rows.")


if __name__ == "__main__":
    main()
