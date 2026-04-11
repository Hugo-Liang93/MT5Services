"""Paper trading phase builders."""

from __future__ import annotations

from src.app_runtime.container import AppContainer
import logging


logger = logging.getLogger(__name__)


def build_paper_trading_layer(container: AppContainer) -> None:
    """Construct Paper Trading bridge and tracker if enabled."""
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

        market_service = container.market_service
        bridge = PaperTradingBridge(
            config=pt_config,
            market_quote_fn=market_service.get_quote,
            on_trade_closed=tracker.on_trade_closed,
            on_trade_opened=tracker.on_trade_opened,
        )
        container.paper_trading_bridge = bridge

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
