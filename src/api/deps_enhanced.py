"""
增强的依赖与单例：使用优化后的组件
"""

from contextlib import asynccontextmanager
import logging

from src.core.market_service import MarketDataService
from src.core.account_service import AccountService
from src.core.trading_service import TradingService
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter
from src.ingestion.ingestor import BackgroundIngestor
from src.clients.mt5_market import MT5MarketClient
from src.indicators.optimized.worker_enhanced import EnhancedIndicatorWorker
from src.monitoring.health_check import get_health_monitor, get_monitoring_manager
from src.config import (
    load_mt5_settings,
    load_db_settings,
    load_ingest_settings,
    load_storage_settings,
    load_market_settings,
    load_indicator_settings,
)

logger = logging.getLogger(__name__)

# 加载配置
mt5_settings = load_mt5_settings()
db_settings = load_db_settings()
ingest_settings = load_ingest_settings()
storage_settings = load_storage_settings()
market_settings = load_market_settings()
indicator_settings = load_indicator_settings()

# 创建核心服务
mt5_client = MT5MarketClient(mt5_settings)
service = MarketDataService(client=mt5_client, market_settings=market_settings)
storage_writer = StorageWriter(TimescaleWriter(db_settings), storage_settings=storage_settings)

# 创建数据采集器
ingestor = BackgroundIngestor(
    service=service, 
    storage=storage_writer, 
    ingest_settings=ingest_settings
)

# 创建增强的指标计算工作器
indicator_worker = EnhancedIndicatorWorker(
    service=service,
    symbols=ingest_settings.ingest_symbols,
    timeframes=ingest_settings.ingest_ohlc_timeframes,
    tasks=[],  # 由 config/indicators.ini 热加载
    indicator_settings=indicator_settings,
    storage=storage_writer,
    event_store_db_path="events.db"
)

# 创建账户和交易服务
account_service = AccountService()
trading_service = TradingService()

# 创建监控系统
health_monitor = get_health_monitor("health_monitor.db")
monitoring_manager = get_monitoring_manager(health_monitor, check_interval=60)


def get_market_service() -> MarketDataService:
    return service


def get_account_service() -> AccountService:
    return account_service


def get_trading_service() -> TradingService:
    return trading_service


def get_indicator_worker() -> EnhancedIndicatorWorker:
    return indicator_worker


def get_ingestor() -> BackgroundIngestor:
    return ingestor


def get_health_monitor_instance():
    return health_monitor


def get_monitoring_manager_instance():
    return monitoring_manager


@asynccontextmanager
async def lifespan_enhanced(_app):
    """
    增强的生命周期管理
    """
    logger.info("Starting enhanced system...")
    
    try:
        # 启动存储写入器
        storage_writer.start()
        logger.info("Storage writer started")
        
        # 启动数据采集器
        ingestor.start()
        logger.info("Data ingestor started")
        
        # 启动指标计算工作器
        indicator_worker.start()
        logger.info("Enhanced indicator worker started")
        
        # 注册监控组件
        monitoring_manager.register_component(
            "data_ingestion",
            ingestor,
            ["queue_stats"]
        )
        
        monitoring_manager.register_component(
            "indicator_calculation", 
            indicator_worker,
            ["indicator_freshness", "cache_stats", "performance_stats"]
        )
        
        monitoring_manager.register_component(
            "market_data",
            service,
            ["data_latency"]
        )
        
        # 启动监控管理器
        monitoring_manager.start()
        logger.info("Monitoring manager started")
        
        # 记录启动事件
        health_monitor.record_metric(
            "system",
            "startup",
            1.0,
            {"version": "enhanced", "timestamp": "now"}
        )
        
        logger.info("Enhanced system startup completed")
        
    except Exception as exc:
        logger.exception("Failed to start enhanced system: %s", exc)
        raise
    
    try:
        yield
    finally:
        logger.info("Shutting down enhanced system...")
        
        # 停止监控管理器
        monitoring_manager.stop()
        logger.info("Monitoring manager stopped")
        
        # 停止指标计算工作器
        indicator_worker.stop()
        logger.info("Enhanced indicator worker stopped")
        
        # 停止数据采集器
        ingestor.stop()
        logger.info("Data ingestor stopped")
        
        # 停止存储写入器
        storage_writer.stop()
        logger.info("Storage writer stopped")
        
        # 记录关闭事件
        health_monitor.record_metric(
            "system",
            "shutdown",
            1.0,
            {"timestamp": "now"}
        )
        
        logger.info("Enhanced system shutdown completed")


# 向后兼容的别名
lifespan = lifespan_enhanced