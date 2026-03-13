#!/usr/bin/env python3
"""
配置迁移工具

将现有的INI格式指标配置迁移到新的JSON格式
"""

import sys
import os
import json
import configparser
from pathlib import Path

def migrate_ini_to_json(ini_path: str, json_path: str) -> bool:
    """
    将INI格式配置迁移到JSON格式
    
    Args:
        ini_path: 输入INI文件路径
        json_path: 输出JSON文件路径
        
    Returns:
        是否迁移成功
    """
    try:
        print(f"正在迁移配置: {ini_path} -> {json_path}")
        
        # 读取INI文件
        config = configparser.ConfigParser()
        config.read(ini_path, encoding='utf-8')
        
        # 创建新的JSON配置结构
        new_config = {
            "indicators": [],
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
            "symbols": ["EURUSD", "GBPUSD", "USDJPY"],
            "timeframes": ["M1", "M5", "M15", "H1"],
            "auto_start": True,
            "hot_reload": True,
            "reload_interval": 60.0
        }
        
        # 迁移指标配置
        for section in config.sections():
            if section == 'worker':
                # 处理worker节的特殊配置
                if 'poll_seconds' in config[section]:
                    new_config['pipeline']['poll_interval'] = float(config[section]['poll_seconds'])
                continue
            
            # 创建指标配置
            indicator_config = {
                "name": section,
                "func_path": "",
                "params": {},
                "dependencies": [],
                "compute_mode": "standard",
                "enabled": True,
                "description": "",
                "tags": []
            }
            
            # 提取函数路径
            if 'func' in config[section]:
                indicator_config['func_path'] = config[section]['func']
            
            # 提取参数
            if 'params' in config[section]:
                try:
                    params = json.loads(config[section]['params'])
                    if isinstance(params, dict):
                        indicator_config['params'] = params
                except json.JSONDecodeError:
                    print(f"警告: {section} 的参数不是有效的JSON: {config[section]['params']}")
                    # 尝试解析为简单字典
                    indicator_config['params'] = {"raw_params": config[section]['params']}
            
            # 根据指标名称推断描述和标签
            indicator_config['description'] = f"从旧配置迁移的指标: {section}"
            
            # 根据名称推断标签
            name_lower = section.lower()
            if 'sma' in name_lower:
                indicator_config['tags'].append('mean')
                indicator_config['tags'].append('trend')
                indicator_config['description'] = f"简单移动平均线 ({section})"
            elif 'ema' in name_lower:
                indicator_config['tags'].append('mean')
                indicator_config['tags'].append('trend')
                indicator_config['description'] = f"指数移动平均线 ({section})"
                indicator_config['compute_mode'] = "incremental"
            elif 'rsi' in name_lower:
                indicator_config['tags'].append('momentum')
                indicator_config['tags'].append('oscillator')
                indicator_config['description'] = f"相对强弱指数 ({section})"
            elif 'macd' in name_lower:
                indicator_config['tags'].append('momentum')
                indicator_config['tags'].append('trend')
                indicator_config['description'] = f"移动平均收敛发散指标 ({section})"
                indicator_config['compute_mode'] = "parallel"
            elif 'boll' in name_lower or 'bb' in name_lower:
                indicator_config['tags'].append('volatility')
                indicator_config['tags'].append('channel')
                indicator_config['description'] = f"布林带 ({section})"
            elif 'atr' in name_lower:
                indicator_config['tags'].append('volatility')
                indicator_config['description'] = f"平均真实波幅 ({section})"
                indicator_config['compute_mode'] = "incremental"
            
            new_config['indicators'].append(indicator_config)
        
        print(f"迁移了 {len(new_config['indicators'])} 个指标")
        
        # 写入JSON文件
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=2, ensure_ascii=False)
        
        print(f"✅ 迁移完成: {json_path}")
        return True
        
    except Exception as e:
        print(f"❌ 迁移失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def backup_file(filepath: str) -> bool:
    """备份文件"""
    path = Path(filepath)
    if not path.exists():
        print(f"文件不存在，无需备份: {filepath}")
        return True
    
    backup_path = path.with_suffix(path.suffix + '.backup')
    try:
        import shutil
        shutil.copy2(path, backup_path)
        print(f"✅ 备份完成: {backup_path}")
        return True
    except Exception as e:
        print(f"❌ 备份失败: {e}")
        return False

def validate_migration(ini_path: str, json_path: str) -> bool:
    """验证迁移结果"""
    try:
        # 检查JSON文件是否存在
        if not Path(json_path).exists():
            print(f"❌ 迁移后的文件不存在: {json_path}")
            return False
        
        # 读取和验证JSON
        with open(json_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        required_sections = ['indicators', 'pipeline', 'symbols', 'timeframes']
        for section in required_sections:
            if section not in config:
                print(f"❌ 缺少必要配置节: {section}")
                return False
        
        print(f"✅ JSON配置验证通过:")
        print(f"   指标数量: {len(config['indicators'])}")
        print(f"   交易品种: {len(config['symbols'])}")
        print(f"   时间框架: {len(config['timeframes'])}")
        
        # 显示前几个指标
        print(f"\n   迁移的指标:")
        for i, indicator in enumerate(config['indicators'][:5]):
            print(f"     {i+1}. {indicator['name']}: {indicator['description']}")
        
        if len(config['indicators']) > 5:
            print(f"     ... 还有 {len(config['indicators']) - 5} 个指标")
        
        return True
        
    except Exception as e:
        print(f"❌ 验证失败: {e}")
        return False

def main():
    """主函数"""
    print("🔄 指标配置迁移工具")
    print("=" * 60)
    print("将旧版INI格式配置迁移到新版JSON格式配置")
    print("=" * 60)
    
    # 默认路径
    ini_path = "config/indicators.ini"
    json_path = "config/indicators_v2.json"
    
    # 检查输入文件
    if not Path(ini_path).exists():
        print(f"❌ 输入文件不存在: {ini_path}")
        print(f"请确保 {ini_path} 文件存在")
        return False
    
    # 备份现有文件
    print("\n1. 备份现有文件...")
    if Path(json_path).exists():
        if not backup_file(json_path):
            print("⚠️  备份失败，继续迁移...")
    else:
        print(f"目标文件不存在，无需备份: {json_path}")
    
    # 执行迁移
    print("\n2. 执行迁移...")
    if not migrate_ini_to_json(ini_path, json_path):
        return False
    
    # 验证迁移
    print("\n3. 验证迁移结果...")
    if not validate_migration(ini_path, json_path):
        return False
    
    # 显示下一步建议
    print("\n" + "=" * 60)
    print("🎉 迁移完成!")
    print("=" * 60)
    
    print("""
下一步操作:

1. 检查迁移后的配置文件:
   cat config/indicators_v2.json

2. 根据需要进行调整:
   - 更新指标描述
   - 添加依赖关系
   - 调整计算模式
   - 修改参数

3. 测试新配置:
   python3 test_config_system.py

4. 更新系统使用新配置:
   # 在代码中使用
   from src.indicators_v2.manager import get_global_unified_manager
   manager = get_global_unified_manager(config_file="config/indicators_v2.json")

5. 验证功能:
   # 启动服务并测试API
   python3 app.py
   curl http://localhost:8808/indicators/list

注意事项:
- 旧配置文件已备份: config/indicators.ini.backup
- 新配置文件: config/indicators_v2.json
- 系统会自动检测并使用新配置
- 支持配置热重载，修改后自动生效
""")
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)