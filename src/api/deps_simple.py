"""
简化的依赖与单例 - 不依赖pydantic
用于在没有pydantic的情况下运行系统
"""

from contextlib import asynccontextmanager
import logging
from typing import Dict, Any

from src.core.market_service import MarketDataService
from src.core.account_service import AccountService
from src.core.trading_service import TradingService
from src.persistence.db import TimescaleWriter
from src.persistence.storage_writer import StorageWriter
from src.ingestion.ingestor import BackgroundIngestor
from src.clients.mt5_market import MT5MarketClient
from src.indicators.optimized.worker_enhanced import EnhancedIndicatorWorker
from src.monitoring.health_check import get_health_monitor, get_monitoring_manager
from src.config.simple_config import (
    get_cached_market_settings,
    get_cached_indicator_settings,
    get_cached_ingest_settings,
    get_cached_mt5_settings,
)

logger = logging.getLogger(__name__)

# 加载简化配置
mt5_settings = get_cached_mt5_settings()
market_settings = get_cached_market_settings()
ingest_settings = get_cached_ingest_settings()
indicator_settings = get_cached_indicator_settings()

# 创建MT5客户端
_mt5_client: MT5MarketClient = None

def get_mt5_client() -> MT5MarketClient:
    """获取MT5客户端单例"""
    global _mt5_client
    if _mt5_client is None:
        _mt5_client = MT5MarketClient(
            login=mt5_settings.get("mt5_login"),
            password=mt5_settings.get("mt5_password"),
            server=mt5_settings.get("mt5_server"),
            path=mt5_settings.get("mt5_path"),
            timeout_seconds=10.0,
        )
    return _mt5_client

# 创建市场数据服务
_market_service: MarketDataService = None

def get_market_service() -> MarketDataService:
    """获取市场数据服务单例"""
    global _market_service
    if _market_service is None:
        # 创建自定义的MarketSettings对象
        class SimpleMarketSettings:
            def __init__(self, settings_dict: Dict[str, Any]):
                for key, value in settings_dict.items():
                    setattr(self, key, value)
        
        simple_settings = SimpleMarketSettings(market_settings)
        _market_service = MarketDataService(
            client=get_mt5_client(),
            market_settings=simple_settings,
        )
    return _market_service

# 创建账户服务
_account_service: AccountService = None

def get_account_service() -> AccountService:
    """获取账户服务单例"""
    global _account_service
    if _account_service is None:
        _account_service = AccountService(client=get_mt5_client())
    return _account_service

# 创建交易服务
_trading_service: TradingService = None

def get_trading_service() -> TradingService:
    """获取交易服务单例"""
    global _trading_service
    if _trading_service is None:
        _trading_service = TradingService(client=get_mt5_client())
    return _trading_service

# 创建Timescale写入器
_timescale_writer: TimescaleWriter = None

def get_timescale_writer() -> TimescaleWriter:
    """获取Timescale写入器单例"""
    global _timescale_writer
    if _timescale_writer is None:
        # 简化数据库配置
        db_config = {
            "host": "localhost",
            "port": 5432,
            "database": "mt5",
            "user": "postgres",
            "password": "postgres",
            "pool_size": 5,
            "max_overflow": 10,
        }
        _timescale_writer = TimescaleWriter(**db_config)
    return _timescale_writer

# 创建存储写入器
_storage_writer: StorageWriter = None

def get_storage_writer() -> StorageWriter:
    """获取存储写入器单例"""
    global _storage_writer
    if _storage_writer is None:
        _storage_writer = StorageWriter(
            timescale_writer=get_timescale_writer(),
            queue_size=1000,
        )
    return _storage_writer

# 创建数据采集器
_ingestor: BackgroundIngestor = None

def get_ingestor() -> BackgroundIngestor:
    """获取数据采集器单例"""
    global _ingestor
    if _ingestor is None:
        # 创建自定义的IngestSettings对象
        class SimpleIngestSettings:
            def __init__(self, settings_dict: Dict[str, Any]):
                for key, value in settings_dict.items():
                    setattr(self, key, value)
        
        simple_settings = SimpleIngestSettings(ingest_settings)
        _ingestor = BackgroundIngestor(
            client=get_mt5_client(),
            service=get_market_service(),
            settings=simple_settings,
        )
    return _ingestor

# 创建增强指标工作器
_enhanced_indicator_worker: EnhancedIndicatorWorker = None

def get_enhanced_indicator_worker() -> EnhancedIndicatorWorker:
    """获取增强指标工作器单例"""
    global _enhanced_indicator_worker
    if _enhanced_indicator_worker is None:
        # 创建自定义的IndicatorSettings对象
        class SimpleIndicatorSettings:
            def __init__(self, settings_dict: Dict[str, Any]):
                for key, value in settings_dict.items():
                    setattr(self, key, value)
        
        simple_settings = SimpleIndicatorSettings(indicator_settings)
        _enhanced_indicator_worker = EnhancedIndicatorWorker(
            service=get_market_service(),
            symbols=ingest_settings.get("ingest_symbols", ["XAUUSD"]),
            timeframes=ingest_settings.get("ingest_timeframes", ["M1", "H1"]),
            tasks=[],  # 从配置文件加载
            indicator_settings=simple_settings,
            storage=get_storage_writer(),
        )
    return _enhanced_indicator_worker

# 创建健康监控器
_health_monitor = None

def get_health_monitor_simple():
    """获取健康监控器单例"""
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = get_health_monitor(":memory:")  # 使用内存数据库
    return _health_monitor

# 创建监控管理器
_monitoring_manager = None

def get_monitoring_manager_simple():
    """获取监控管理器单例"""
    global _monitoring_manager
    if _monitoring_manager is None:
        _monitoring_manager = get_monitoring_manager()
    return _monitoring_manager

@asynccontextmanager
async def lifespan(app):
    """应用生命周期管理"""
    logger.info("启动MT5Services简化版...")
    
    # 启动服务
    get_ingestor().start()
    get_enhanced_indicator_worker().start()
    
    # 启动监控
    health_monitor = get_health_monitor_simple()
    health_monitor.start_monitoring()
    
    yield
    
    # 关闭服务
    logger.info("关闭MT5Services简化版...")
    get_ingestor().stop()
    get_enhanced_indicator_worker().stop()
    if health_monitor:
        health_monitor.stop_monitoring()