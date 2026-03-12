#!/usr/bin/env python3
"""
MT5Services 依赖安装脚本
支持按环境安装依赖
"""

import subprocess
import sys
import os
from pathlib import Path


def run_command(cmd: str, check: bool = True) -> bool:
    """运行命令并处理输出"""
    print(f"🚀 执行: {cmd}")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=check,
            capture_output=True,
            text=True
        )
        if result.stdout:
            print(f"📤 输出: {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ 错误: {e.stderr.strip()}")
        return False


def install_base_dependencies():
    """安装基础依赖"""
    print("\n" + "="*50)
    print("📦 安装基础依赖")
    print("="*50)
    
    # 读取 requirements-updated.txt 中的基础依赖
    with open("requirements-updated.txt", "r") as f:
        lines = f.readlines()
    
    # 提取基础依赖（排除环境组）
    base_deps = []
    in_section = False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            break  # 遇到环境组就停止
        base_deps.append(line)
    
    # 安装每个依赖
    for dep in base_deps:
        cmd = f"pip install {dep}"
        if not run_command(cmd, check=False):
            print(f"⚠️  跳过: {dep}")
    
    print("✅ 基础依赖安装完成")


def install_environment_deps(env: str):
    """安装环境特定依赖"""
    print(f"\n" + "="*50)
    print(f"🌍 安装 {env} 环境依赖")
    print("="*50)
    
    cmd = f"pip install -r requirements-updated.txt[{env}]"
    if not run_command(cmd, check=False):
        print(f"⚠️  {env} 环境依赖安装失败，尝试手动安装")
        return False
    return True


def check_ta_lib():
    """检查并安装 TA-Lib"""
    print("\n" + "="*50)
    print("📊 检查 TA-Lib")
    print("="*50)
    
    try:
        import talib
        print(f"✅ TA-Lib 已安装: {talib.__version__}")
        return True
    except ImportError:
        print("❌ TA-Lib 未安装")
        print("💡 TA-Lib 需要系统依赖，请根据系统安装：")
        print("\nUbuntu/Debian:")
        print("  sudo apt-get update")
        print("  sudo apt-get install build-essential")
        print("  wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz")
        print("  tar -xzf ta-lib-0.4.0-src.tar.gz")
        print("  cd ta-lib/")
        print("  ./configure --prefix=/usr")
        print("  make")
        print("  sudo make install")
        print("  pip install TA-Lib")
        
        print("\nmacOS:")
        print("  brew install ta-lib")
        print("  pip install TA-Lib")
        
        print("\nWindows:")
        print("  下载预编译包: https://www.lfd.uci.edu/~gohlke/pythonlibs/#ta-lib")
        print("  pip install TA_Lib‑0.4.28‑cp3xx‑cp3xx‑win_amd64.whl")
        
        return False


def create_venv():
    """创建虚拟环境"""
    print("\n" + "="*50)
    print("🐍 创建虚拟环境")
    print("="*50)
    
    venv_path = Path("venv")
    if venv_path.exists():
        print(f"✅ 虚拟环境已存在: {venv_path}")
        return True
    
    # 创建虚拟环境
    if run_command("python -m venv venv"):
        print("✅ 虚拟环境创建成功")
        print("\n激活虚拟环境:")
        print("  Linux/macOS: source venv/bin/activate")
        print("  Windows: venv\\Scripts\\activate")
        return True
    return False


def verify_installation():
    """验证安装"""
    print("\n" + "="*50)
    print("🔍 验证安装")
    print("="*50)
    
    test_imports = [
        ("fastapi", "FastAPI"),
        ("pydantic", "BaseModel"),
        ("pandas", "DataFrame"),
        ("sqlalchemy", "create_engine"),
        ("redis", "Redis"),
        ("prometheus_client", "Counter"),
    ]
    
    all_ok = True
    for module, attr in test_imports:
        try:
            __import__(module)
            print(f"✅ {module} 导入成功")
        except ImportError as e:
            print(f"❌ {module} 导入失败: {e}")
            all_ok = False
    
    return all_ok


def main():
    """主函数"""
    print("="*60)
    print("MT5Services 依赖安装工具")
    print("="*60)
    
    # 检查当前目录
    current_dir = Path.cwd()
    if not (current_dir / "requirements-updated.txt").exists():
        print("❌ 请在 MT5Services 目录下运行此脚本")
        sys.exit(1)
    
    # 询问是否创建虚拟环境
    create_venv_choice = input("\n是否创建虚拟环境？(y/n, 默认n): ").strip().lower()
    if create_venv_choice == 'y':
        if not create_venv():
            print("❌ 虚拟环境创建失败")
            sys.exit(1)
        print("\n⚠️  请先激活虚拟环境，然后重新运行此脚本")
        sys.exit(0)
    
    # 选择环境
    print("\n选择安装环境:")
    print("1. 开发环境 (dev)")
    print("2. 测试环境 (test)")
    print("3. 生产环境 (prod)")
    print("4. 仅基础依赖")
    
    choice = input("请输入选择 (1-4, 默认1): ").strip()
    env_map = {"1": "dev", "2": "test", "3": "prod", "4": None}
    env = env_map.get(choice, "dev")
    
    # 安装基础依赖
    install_base_dependencies()
    
    # 安装环境特定依赖
    if env:
        install_environment_deps(env)
    
    # 检查 TA-Lib
    check_ta_lib()
    
    # 验证安装
    if verify_installation():
        print("\n" + "="*50)
        print("🎉 依赖安装完成！")
        print("="*50)
        print("\n下一步:")
        print("1. 配置环境变量 (.env 文件)")
        print("2. 运行数据库迁移: alembic upgrade head")
        print("3. 启动服务: python app.py")
        print("4. 访问文档: http://localhost:8808/docs")
    else:
        print("\n⚠️  部分依赖安装失败，请检查错误信息")


if __name__ == "__main__":
    main()