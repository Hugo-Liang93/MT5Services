#!/usr/bin/env python3
"""
检查 Git 忽略状态
"""

import subprocess
import os
from pathlib import Path


def run_git_command(cmd: str) -> str:
    """运行 Git 命令并返回输出"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f"错误: {e.stderr.strip()}"


def check_ignored(path: str) -> bool:
    """检查路径是否被 Git 忽略"""
    result = run_git_command(f"git check-ignore {path}")
    return result == path


def list_ignored_files():
    """列出所有被忽略的文件"""
    print("📋 被 Git 忽略的文件:")
    print("=" * 50)
    
    result = run_git_command("git status --ignored --porcelain")
    if result:
        for line in result.split('\n'):
            if line.startswith('!!'):
                file_path = line[3:].strip()
                print(f"  ❌ {file_path}")
    else:
        print("  (无被忽略的文件)")


def check_common_ignores():
    """检查常见目录是否被忽略"""
    common_paths = [
        "venv/",
        ".venv/",
        "env/",
        "__pycache__/",
        ".env",
        ".vscode/",
        ".idea/",
        "logs/",
        "data/",
        "*.log",
    ]
    
    print("\n🔍 检查常见忽略项:")
    print("=" * 50)
    
    for path in common_paths:
        if check_ignored(path):
            print(f"  ✅ {path:30} 已忽略")
        else:
            print(f"  ❌ {path:30} 未忽略")


def show_gitignore_content():
    """显示 .gitignore 内容"""
    gitignore_path = Path(".gitignore")
    if gitignore_path.exists():
        print("\n📄 .gitignore 内容:")
        print("=" * 50)
        with open(gitignore_path, 'r') as f:
            content = f.read()
            print(content)
    else:
        print("\n❌ .gitignore 文件不存在")


def check_virtual_env():
    """检查虚拟环境状态"""
    print("\n🐍 虚拟环境检查:")
    print("=" * 50)
    
    venv_paths = ["venv", ".venv", "env"]
    for venv_path in venv_paths:
        if Path(venv_path).exists():
            print(f"  📁 找到虚拟环境: {venv_path}")
            if check_ignored(venv_path + "/"):
                print(f"    ✅ {venv_path}/ 已被 Git 忽略")
            else:
                print(f"    ⚠️  {venv_path}/ 未被 Git 忽略！")
            
            # 检查虚拟环境大小
            try:
                total_size = sum(f.stat().st_size for f in Path(venv_path).rglob('*') if f.is_file())
                size_mb = total_size / (1024 * 1024)
                print(f"    📊 大小: {size_mb:.1f} MB")
            except:
                print("    📊 大小: 无法计算")
        else:
            print(f"  📭 未找到: {venv_path}")


def check_tracked_files():
    """检查是否有应该忽略的文件被跟踪"""
    print("\n📊 Git 状态摘要:")
    print("=" * 50)
    
    # 检查未跟踪的文件
    untracked = run_git_command("git status --porcelain")
    if untracked:
        print("未跟踪的文件:")
        for line in untracked.split('\n'):
            if line.startswith('??'):
                file_path = line[3:].strip()
                print(f"  📄 {file_path}")
    else:
        print("无未跟踪的文件")


def main():
    """主函数"""
    print("=" * 60)
    print("Git 忽略状态检查工具")
    print("=" * 60)
    
    # 检查是否在 Git 仓库中
    if not Path(".git").exists():
        print("❌ 当前目录不是 Git 仓库")
        return
    
    # 运行检查
    show_gitignore_content()
    check_common_ignores()
    check_virtual_env()
    list_ignored_files()
    check_tracked_files()
    
    print("\n" + "=" * 60)
    print("检查完成！")
    print("\n建议:")
    print("1. 确保 venv/, .env, __pycache__/ 等目录被忽略")
    print("2. 如果发现应该忽略的文件被跟踪，使用:")
    print("   git rm --cached <file>")
    print("3. 定期检查 .gitignore 文件")
    print("=" * 60)


if __name__ == "__main__":
    main()