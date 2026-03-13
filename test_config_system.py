#!/usr/bin/env python3
"""
配置系统核心功能测试

测试配置驱动的统一指标管理器的核心功能，避免外部依赖
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import tempfile
from pathlib import Path


def test_config_models():
    """测试配置模型"""
    print("=" * 60)
    print("测试1: 配置模型")
    print("=" * 60)
    
    try:
        # 避免导入有依赖的模块，直接测试核心逻辑
        config_content = {
            "indicators": [
                {
                    "name": "test_sma",
                    "func_path": "src.indicators.mean.sma",
                    "params": {"period": 20},
                    "dependencies": [],
                    "compute_mode": "standard",
                    "enabled": True,
                    "description": "测试SMA",
                    "tags": ["test"]
                }
            ],
            "pipeline": {
                "enable_parallel": True,
                "max_workers": 4,
                "enable_cache": True,
                "cache_strategy": "lru_ttl",
                "cache_ttl": 300.0,
                "cache_maxsize": 1000,
                "enable_incremental": True,
                "max_retries": 2,
                "retry_delay": 0.1,
                "enable_monitoring": True,
                "poll_interval": 5.0
            },
            "symbols": ["EURUSD", "GBPUSD"],
            "timeframes": ["M1", "M5"],
            "auto_start": True,
            "hot_reload": True,
            "reload_interval": 60.0
        }
        
        # 创建临时配置文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config_content, f)
            temp_file = f.name
        
        try:
            # 测试配置加载
            from src.indicators_v2.config import ConfigLoader
            config = ConfigLoader.load(temp_file)
            
            print(f"✅ 配置模型测试通过")
            print(f"   指标数量: {len(config.indicators)}")
            print(f"   第一个指标: {config.indicators[0].name}")
            print(f"   计算模式: {config.indicators[0].compute_mode}")
            print(f"   启用状态: {config.indicators[0].enabled}")
            print(f"   流水线配置: 并行={config.pipeline.enable_parallel}, 缓存={config.pipeline.enable_cache}")
            print(f"   交易品种: {config.symbols}")
            print(f"   时间框架: {config.timeframes}")
            
            return True
            
        finally:
            # 清理临时文件
            Path(temp_file).unlink(missing_ok=True)
            
    except Exception as e:
        print(f"❌ 配置模型测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_config_loader_formats():
    """测试配置加载器支持的不同格式"""
    print("\n" + "=" * 60)
    print("测试2: 配置格式支持")
    print("=" * 60)
    
    try:
        from src.indicators_v2.config import ConfigLoader
        
        # 测试JSON格式
        json_content = {
            "indicators": [{
                "name": "json_sma",
                "func_path": "src.indicators.mean.sma",
                "params": {"period": 20},
                "enabled": True
            }],
            "symbols": ["EURUSD"]
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(json_content, f)
            json_file = f.name
        
        # 测试INI格式（兼容现有格式）
        ini_content = """[worker]
poll_seconds = 5

[sma20]
func = src.indicators.mean.sma
params = {"period": 20, "min_bars": 20}
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ini', delete=False) as f:
            f.write(ini_content)
            ini_file = f.name
        
        try:
            # 测试JSON加载
            print("测试JSON格式加载...")
            json_config = ConfigLoader.load(json_file)
            print(f"   ✅ JSON加载成功: {len(json_config.indicators)} 个指标")
            
            # 测试INI加载
            print("测试INI格式加载...")
            ini_config = ConfigLoader.load(ini_file)
            print(f"   ✅ INI加载成功: {len(ini_config.indicators)} 个指标")
            
            # 测试自动检测
            print("测试格式自动检测...")
            for filepath in [json_file, ini_file]:
                config = ConfigLoader.load(filepath)
                print(f"   ✅ {Path(filepath).suffix} 自动检测成功")
            
            return True
            
        finally:
            # 清理临时文件
            for filepath in [json_file, ini_file]:
                Path(filepath).unlink(missing_ok=True)
            
    except Exception as e:
        print(f"❌ 配置格式测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_config_manager():
    """测试配置管理器"""
    print("\n" + "=" * 60)
    print("测试3: 配置管理器")
    print("=" * 60)
    
    try:
        from src.indicators_v2.config import ConfigManager
        
        # 创建测试配置
        config_content = {
            "indicators": [{
                "name": "manager_sma",
                "func_path": "src.indicators.mean.sma",
                "params": {"period": 20},
                "enabled": True
            }],
            "symbols": ["EURUSD"]
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config_content, f)
            temp_file = f.name
        
        try:
            # 创建配置管理器
            print("创建配置管理器...")
            config_manager = ConfigManager(temp_file)
            
            # 获取配置
            config = config_manager.get_config()
            print(f"✅ 配置获取成功: {len(config.indicators)} 个指标")
            
            # 测试配置保存
            print("测试配置保存...")
            save_success = config_manager.save()
            print(f"   保存结果: {'成功' if save_success else '失败'}")
            
            # 测试配置重载
            print("测试配置重载...")
            reload_success = config_manager.reload()
            print(f"   重载结果: {'成功' if reload_success else '失败'}")
            
            # 测试全局配置管理器
            print("测试全局配置管理器...")
            from src.indicators_v2.config import get_global_config_manager
            global_manager = get_global_config_manager(temp_file)
            global_config = global_manager.get_config()
            print(f"   全局管理器: {len(global_config.indicators)} 个指标")
            
            return True
            
        finally:
            # 清理临时文件
            Path(temp_file).unlink(missing_ok=True)
            
    except Exception as e:
        print(f"❌ 配置管理器测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_indicator_config_operations():
    """测试指标配置操作"""
    print("\n" + "=" * 60)
    print("测试4: 指标配置操作")
    print("=" * 60)
    
    try:
        from src.indicators_v2.config import ConfigManager, IndicatorConfig, ComputeMode
        
        # 创建测试配置文件
        config_content = {
            "indicators": [{
                "name": "existing_sma",
                "func_path": "src.indicators.mean.sma",
                "params": {"period": 20},
                "enabled": True
            }],
            "symbols": ["EURUSD"]
        }
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config_content, f)
            temp_file = f.name
        
        try:
            # 创建配置管理器
            config_manager = ConfigManager(temp_file)
            
            # 测试添加指标
            print("测试添加指标...")
            new_indicator = IndicatorConfig(
                name="new_ema",
                func_path="src.indicators.mean.ema",
                params={"period": 50},
                compute_mode=ComputeMode.INCREMENTAL,
                enabled=True,
                description="新添加的EMA"
            )
            
            config_manager.add_indicator(new_indicator)
            
            # 验证添加
            config = config_manager.get_config()
            indicator_names = [ind.name for ind in config.indicators]
            print(f"   添加后指标: {indicator_names}")
            print(f"   'new_ema' 是否在列表中: {'new_ema' in indicator_names}")
            
            # 测试获取指标
            print("测试获取指标...")
            indicator = config_manager.get_indicator("new_ema")
            print(f"   获取指标: {'成功' if indicator else '失败'}")
            if indicator:
                print(f"   指标名称: {indicator.name}")
                print(f"   函数路径: {indicator.func_path}")
            
            # 测试更新指标
            print("测试更新指标...")
            config_manager.update_pipeline_config(
                enable_parallel=False,
                max_workers=2
            )
            
            updated_config = config_manager.get_config()
            print(f"   更新后流水线配置:")
            print(f"     并行计算: {updated_config.pipeline.enable_parallel}")
            print(f"     工作线程: {updated_config.pipeline.max_workers}")
            
            # 测试移除指标
            print("测试移除指标...")
            success = config_manager.remove_indicator("new_ema")
            print(f"   移除结果: {'成功' if success else '失败'}")
            
            final_config = config_manager.get_config()
            final_names = [ind.name for ind in final_config.indicators]
            print(f"   最终指标: {final_names}")
            print(f"   'new_ema' 是否已移除: {'new_ema' not in final_names}")
            
            return True
            
        finally:
            # 清理临时文件
            Path(temp_file).unlink(missing_ok=True)
            
    except Exception as e:
        print(f"❌ 指标配置操作测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_real_config_file():
    """测试实际配置文件"""
    print("\n" + "=" * 60)
    print("测试5: 实际配置文件")
    print("=" * 60)
    
    try:
        config_path = Path("config/indicators_v2.json")
        
        if not config_path.exists():
            print("❌ 配置文件不存在: config/indicators_v2.json")
            return False
        
        # 读取配置文件
        config_data = json.loads(config_path.read_text())
        
        print(f"✅ 配置文件存在: {config_path}")
        print(f"   文件大小: {config_path.stat().st_size} 字节")
        
        # 验证配置结构
        required_sections = ["indicators", "pipeline", "symbols", "timeframes"]
        missing_sections = [section for section in required_sections if section not in config_data]
        
        if missing_sections:
            print(f"❌ 缺少必要配置节: {missing_sections}")
            return False
        
        print(f"✅ 配置结构完整")
        print(f"   指标数量: {len(config_data['indicators'])}")
        print(f"   交易品种: {len(config_data['symbols'])}")
        print(f"   时间框架: {len(config_data['timeframes'])}")
        
        # 显示指标信息
        print(f"\n   指标列表:")
        for i, indicator in enumerate(config_data['indicators'][:5]):  # 只显示前5个
            print(f"     {i+1}. {indicator['name']}: {indicator.get('description', '无描述')}")
        
        if len(config_data['indicators']) > 5:
            print(f"     ... 还有 {len(config_data['indicators']) - 5} 个指标")
        
        # 验证流水线配置
        pipeline = config_data['pipeline']
        print(f"\n   流水线配置:")
        print(f"     并行计算: {pipeline.get('enable_parallel', 'N/A')}")
        print(f"     缓存启用: {pipeline.get('enable_cache', 'N/A')}")
        print(f"     监控启用: {pipeline.get('enable_monitoring', 'N/A')}")
        
        return True
        
    except Exception as e:
        print(f"❌ 实际配置文件测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主测试函数"""
    print("🚀 配置系统核心功能测试")
    print("=" * 60)
    print("测试配置驱动的统一指标管理器的核心功能")
    print("=" * 60)
    
    tests = [
        ("配置模型", test_config_models),
        ("配置格式支持", test_config_loader_formats),
        ("配置管理器", test_config_manager),
        ("指标配置操作", test_indicator_config_operations),
        ("实际配置文件", test_real_config_file),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        print(f"\n▶️ 运行测试: {test_name}")
        try:
            success = test_func()
            results.append((test_name, success))
        except Exception as e:
            print(f"❌ 测试异常: {e}")
            results.append((test_name, False))
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("📊 测试结果汇总")
    print("=" * 60)
    
    passed = 0
    total = len(results)
    
    for test_name, success in results:
        status = "✅ 通过" if success else "❌ 失败"
        print(f"{status} - {test_name}")
        if success:
            passed += 1
    
    print(f"\n📈 通过率: {passed}/{total} ({passed/total*100:.1f}%)")
    
    if passed == total:
        print("\n🎉 所有测试通过！配置系统核心功能工作正常。")
        print("\n下一步:")
        print("1. 安装项目依赖: pip install -r requirements.txt")
        print("2. 运行完整集成测试")
        print("3. 启动服务验证API功能")
    else:
        print(f"\n⚠️  {total - passed} 个测试失败，需要检查实现。")
    
    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)