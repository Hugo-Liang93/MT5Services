#!/usr/bin/env python3
"""
启动增强版的MT5服务
包含优化的事件存储、缓存一致性和监控系统
"""

import uvicorn
import logging
import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('enhanced_service.log')
    ]
)

logger = logging.getLogger(__name__)


def main():
    """启动增强版服务"""
    logger.info("Starting enhanced MT5 service...")
    
    # 检查配置文件
    config_files = [
        "config/app.ini",
        "config/mt5.ini", 
        "config/db.ini",
        "config/indicators.ini"
    ]
    
    for config_file in config_files:
        if not os.path.exists(config_file):
            logger.warning(f"Config file not found: {config_file}")
    
    # 启动服务
    try:
        uvicorn.run(
            "src.api.app_enhanced:app",
            host="0.0.0.0",
            port=8810,  # 使用不同的端口，避免与原始服务冲突
            reload=False,
            log_level="info",
            access_log=True
        )
    except Exception as e:
        logger.error(f"Failed to start enhanced service: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()