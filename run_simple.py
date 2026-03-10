#!/usr/bin/env python3
"""
简化版服务启动脚本 - 不依赖pydantic
"""

import sys
import os
import logging

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

def check_dependencies():
    """检查依赖"""
    print("=" * 60)
    print("检查依赖...")
    print("=" * 60)
    
    dependencies = [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("configparser", "configparser"),  # Python内置
    ]
    
    missing = []
    for name, package in dependencies:
        try:
            __import__(package)
            print(f"✅ {name}")
        except ImportError:
            print(f"❌ {name}")
            missing.append(name)
    
    # 检查可选依赖
    print("\n可选依赖:")
    optional_deps = [
        ("psutil", "psutil"),  # 内存管理需要
        ("psycopg2", "psycopg2"),  # 数据库需要
    ]
    
    for name, package in optional_deps:
        try:
            __import__(package)
            print(f"   ✅ {name} (可选)")
        except ImportError:
            print(f"   ⚠️  {name} (可选，未安装)")
    
    if missing:
        print(f"\n❌ 缺少必需依赖: {', '.join(missing)}")
        print("请运行: pip install fastapi uvicorn")
        return False
    
    print("\n✅ 所有必需依赖已安装")
    return True

def test_simple_config():
    """测试简化配置"""
    print("\n" + "=" * 60)
    print("测试简化配置...")
    print("=" * 60)
    
    try:
        from src.config.simple_config import (
            get_cached_market_settings,
            get_cached_indicator_settings,
            get_cached_ingest_settings,
        )
        
        market_settings = get_cached_market_settings()
        indicator_settings = get_cached_indicator_settings()
        ingest_settings = get_cached_ingest_settings()
        
        print(f"✅ 市场设置加载成功: {len(market_settings)} 项")
        print(f"✅ 指标设置加载成功: {len(indicator_settings)} 项")
        print(f"✅ 采集设置加载成功: {len(ingest_settings)} 项")
        
        print(f"\n交易品种: {ingest_settings.get('ingest_symbols', ['XAUUSD'])}")
        print(f"时间框架: {ingest_settings.get('ingest_timeframes', ['M1', 'H1'])}")
        
        return True
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        return False

def test_simple_app():
    """测试简化应用"""
    print("\n" + "=" * 60)
    print("测试简化应用...")
    print("=" * 60)
    
    try:
        # 测试导入
        from src.api.app_simple import app
        
        print("✅ 简化应用导入成功")
        print(f"✅ FastAPI应用创建成功: {app.title}")
        
        # 检查路由
        routes = []
        for route in app.routes:
            if hasattr(route, "path"):
                routes.append(route.path)
        
        print(f"✅ 注册路由数量: {len(routes)}")
        
        return True
    except Exception as e:
        print(f"❌ 应用测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def start_simple_service():
    """启动简化服务"""
    print("\n" + "=" * 60)
    print("启动简化服务...")
    print("=" * 60)
    
    try:
        import uvicorn
        
        print("启动参数:")
        print(f"  主机: 0.0.0.0")
        print(f"  端口: 8810")
        print(f"  应用: src.api.app_simple:app")
        print(f"  日志级别: info")
        
        print("\n服务将在以下地址可用:")
        print("  http://localhost:8810")
        print("  http://localhost:8810/health")
        print("  http://localhost:8810/api/market/ohlc/closed")
        
        print("\n按 Ctrl+C 停止服务")
        
        # 启动服务
        uvicorn.run(
            "src.api.app_simple:app",
            host="0.0.0.0",
            port=8810,
            reload=False,
            log_level="info",
        )
        
    except KeyboardInterrupt:
        print("\n\n服务已停止")
    except Exception as e:
        print(f"\n❌ 服务启动失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True

def main():
    """主函数"""
    print("MT5Services简化版启动器")
    print("=" * 60)
    print("此版本不依赖pydantic，适用于测试和开发环境")
    print("=" * 60)
    
    # 检查依赖
    if not check_dependencies():
        sys.exit(1)
    
    # 测试配置
    if not test_simple_config():
        print("\n⚠️  配置测试失败，但将继续尝试启动...")
    
    # 测试应用
    if not test_simple_app():
        print("\n❌ 应用测试失败，无法启动服务")
        sys.exit(1)
    
    # 启动服务
    start_simple_service()

if __name__ == "__main__":
    main()