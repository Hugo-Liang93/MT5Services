#!/usr/bin/env python3
"""测试通过 Python 传入参数启动"""

import sys
import os

# 确保在当前目录
project_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_dir)

from app import main

def test_direct_call():
    """直接调用 main 函数"""
    print("=== 测试直接调用 ===")
    
    # 测试简单模式
    print("1. 测试简单模式启动...")
    try:
        # 注意：这里会实际启动服务器，我们需要在后台运行或快速停止
        # 在实际使用中，你可能需要控制服务器生命周期
        print("调用 main(mode_override='simple')")
        # main(mode_override='simple')  # 取消注释实际运行
        print("简单模式调用成功（已注释实际启动）")
    except Exception as e:
        print(f"错误: {e}")
    
    print("\n2. 测试增强模式启动...")
    try:
        print("调用 main(mode_override='enhanced')")
        # main(mode_override='enhanced')  # 取消注释实际运行
        print("增强模式调用成功（已注释实际启动）")
    except Exception as e:
        print(f"错误: {e}")
    
    print("\n3. 测试默认模式启动...")
    try:
        print("调用 main()")
        # main()  # 取消注释实际运行
        print("默认模式调用成功（已注释实际启动）")
    except Exception as e:
        print(f"错误: {e}")

def test_resolve_target():
    """测试解析运行目标"""
    print("\n=== 测试解析运行目标 ===")
    
    from app import resolve_runtime_target
    
    # 测试不同模式的目标解析
    modes = ["default", "simple", "enhanced"]
    
    for mode in modes:
        try:
            target, host, port = resolve_runtime_target(mode_override=mode)
            print(f"模式 '{mode}':")
            print(f"  目标应用: {target}")
            print(f"  主机: {host}")
            print(f"  端口: {port}")
        except Exception as e:
            print(f"模式 '{mode}' 解析错误: {e}")

if __name__ == "__main__":
    print("MT5Services 参数启动测试")
    print("=" * 50)
    
    test_resolve_target()
    test_direct_call()
    
    print("\n" + "=" * 50)
    print("测试完成")
    print("\n实际启动示例（取消注释后运行）:")
    print("1. 简单模式: main(mode_override='simple')")
    print("2. 增强模式: main(mode_override='enhanced')")
    print("3. 默认模式: main() 或 main(mode_override='default')")