"""Paper trading phase builders."""

from __future__ import annotations

from src.app_runtime.container import AppContainer
import logging


logger = logging.getLogger(__name__)


def build_paper_trading_layer(container: AppContainer) -> None:
    """Construct Paper Trading bridge and tracker if enabled."""
    runtime_identity = container.runtime_identity
    if runtime_identity is not None and runtime_identity.instance_role == "executor":
        logger.info("Paper trading skipped for executor instance: %s", runtime_identity.instance_name)
        return
    try:
        from src.backtesting.paper_trading.bridge import PaperTradingBridge
        from src.backtesting.paper_trading.config import load_paper_trading_config
        from src.backtesting.paper_trading.tracker import PaperTradeTracker

        pt_config = load_paper_trading_config()
        if not pt_config.enabled:
            logger.debug("Paper trading disabled (enabled=false)")
            return

        if container.market_service is None:
            logger.warning("Paper trading: no market_service, skipping")
            return

        db_writer = (
            container.storage_writer.db
            if container.storage_writer is not None
            else None
        )
        tracker = PaperTradeTracker(
            db_writer=db_writer,
            flush_interval=pt_config.persist_flush_interval,
        )
        container.paper_trade_tracker = tracker

        # 构造 recover_fn：仅当 config.resume_active_session=True 且 db_writer 可用时封装一份。
        # bridge.start() 会在 config flag 开启时尝试调用，返回 None 走 fresh start。
        recover_fn = None
        if pt_config.resume_active_session and db_writer is not None:
            def _recover() -> dict | None:
                try:
                    repo = db_writer.paper_trading_repo
                    session_row = repo.fetch_latest_active_session()
                    if not session_row:
                        return None
                    session_id = str(session_row["session_id"])
                    # 把 DB 的 trade dict 转回 PaperTradeRecord，供 portfolio.restore_open_trade 使用
                    from datetime import datetime as _dt
                    from src.backtesting.paper_trading.models import PaperTradeRecord
                    open_rows = repo.fetch_open_trades(session_id)
                    open_records = []
                    for row in open_rows:
                        entry_time = row.get("entry_time")
                        if isinstance(entry_time, str):
                            try:
                                entry_time = _dt.fromisoformat(entry_time)
                            except Exception:
                                entry_time = _dt.utcnow()
                        open_records.append(PaperTradeRecord(
                            trade_id=str(row["trade_id"]),
                            session_id=str(row["session_id"]),
                            strategy=str(row.get("strategy") or ""),
                            direction=str(row.get("direction") or ""),
                            symbol=str(row.get("symbol") or ""),
                            timeframe=str(row.get("timeframe") or ""),
                            entry_time=entry_time,
                            entry_price=float(row.get("entry_price") or 0.0),
                            stop_loss=float(row.get("stop_loss") or 0.0),
                            take_profit=float(row.get("take_profit") or 0.0),
                            position_size=float(row.get("position_size") or 0.0),
                            confidence=float(row.get("confidence") or 0.0),
                            regime=str(row.get("regime") or ""),
                            signal_id=str(row.get("signal_id") or ""),
                            current_sl=row.get("stop_loss"),
                            current_tp=row.get("take_profit"),
                            breakeven_activated=bool(row.get("breakeven_activated") or False),
                            trailing_activated=bool(row.get("trailing_activated") or False),
                            max_favorable_excursion=float(
                                row.get("max_favorable_excursion") or 0.0
                            ),
                            max_adverse_excursion=float(
                                row.get("max_adverse_excursion") or 0.0
                            ),
                        ))
                    return {"session": session_row, "open_trades": open_records}
                except Exception:
                    logger.warning(
                        "Paper trading recover_fn failed, fallback to fresh start",
                        exc_info=True,
                    )
                    return None
            recover_fn = _recover

        market_service = container.market_service
        bridge = PaperTradingBridge(
            config=pt_config,
            market_quote_fn=market_service.get_quote,
            on_trade_closed=tracker.on_trade_closed,
            on_trade_opened=tracker.on_trade_opened,
            recover_fn=recover_fn,
        )
        container.paper_trading_bridge = bridge

        # 让 tracker 在每个 flush 周期主动从 bridge 拉取 active session 最新 metrics。
        # 这样即使没有新交易 close，session 表也会持续更新运行状态；
        # 进程意外退出时 DB 中仍保留最近一次 flush 的 session 快照。
        tracker.set_session_snapshot_provider(bridge.snapshot_active_session)
        # 同理拉取 open trades 最新字段（MFE/MAE/current_sl/bars_held 等），
        # 让 DB 里 open 状态的交易数据随价格推进同步 —— 避免 DB 只留入场 snapshot。
        tracker.set_open_trades_snapshot_provider(bridge.snapshot_open_trades)

        if container.signal_runtime is not None:
            container.signal_runtime.add_signal_listener(bridge.on_signal_event)

        logger.info(
            "Paper trading wired: balance=%.0f, max_pos=%d, min_conf=%.2f",
            pt_config.initial_balance,
            pt_config.max_positions,
            pt_config.min_confidence,
        )
    except Exception:
        logger.warning("Paper trading setup failed", exc_info=True)
