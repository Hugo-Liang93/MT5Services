# MT5Services 依赖文档

## Python依赖总览

### 核心依赖（必须安装）
| 包名 | 版本 | 用途 | 重要性 |
|------|------|------|--------|
| `fastapi` | >=0.104.0 | Web框架，提供RESTful API | 高 |
| `uvicorn` | >=0.24.0 | ASGI服务器，运行FastAPI应用 | 高 |
| `pydantic` | >=2.5.0 | 数据验证和设置管理 | 高 |
| `psycopg2-binary` | >=2.9.0 | PostgreSQL/TimescaleDB驱动 | 高 |
| `python-multipart` | >=0.0.6 | 表单数据处理（FastAPI依赖） | 中 |
| `configparser` | Python内置 | INI配置文件解析 | 高 |
| `sqlite3` | Python内置 | SQLite数据库（事件存储） | 高 |

### 可选依赖（功能增强）
| 包名 | 版本 | 用途 | 重要性 |
|------|------|------|--------|
| `psutil` | >=5.9.0 | 系统监控和内存管理 | 中（优化功能需要） |
| `watchdog` | >=3.0.0 | 文件系统事件监控 | 低（配置热加载备用） |

### 开发依赖
| 包名 | 版本 | 用途 | 重要性 |
|------|------|------|--------|
| `pytest` | >=7.4.0 | 测试框架 | 低 |
| `pytest-asyncio` | >=0.21.0 | 异步测试支持 | 低 |
| `black` | >=23.0.0 | 代码格式化 | 低 |
| `mypy` | >=1.7.0 | 静态类型检查 | 低 |

## 依赖安装命令

### 基础安装（最小化）
```bash
pip install fastapi uvicorn pydantic psycopg2-binary python-multipart
```

### 完整安装（包含优化功能）
```bash
pip install fastapi uvicorn pydantic psycopg2-binary python-multipart psutil
```

### 开发环境安装
```bash
pip install fastapi uvicorn pydantic psycopg2-binary python-multipart psutil watchdog pytest pytest-asyncio black mypy
```

## 依赖关系图

### 核心依赖链
```
MT5Services
├── fastapi (Web框架)
│   ├── pydantic (数据验证)
│   └── python-multipart (表单处理)
├── uvicorn (ASGI服务器)
├── psycopg2-binary (TimescaleDB驱动)
└── 标准库依赖
    ├── configparser (配置解析)
    ├── sqlite3 (事件存储)
    ├── threading (多线程)
    ├── logging (日志系统)
    └── datetime (时间处理)
```

### 优化功能依赖
```
优化模块
├── psutil (系统监控)
│   └── 用于内存管理和性能监控
├── watchdog (文件监控)
│   └── 用于配置热加载（备用方案）
└── 标准库
    ├── sqlite3 (事件持久化)
    └── threading (后台监控)
```

## 各模块依赖详情

### 1. API层 (`src/api/`)
```python
# 必需依赖
import fastapi
from fastapi import Depends, APIRouter, Query, HTTPException
from pydantic import BaseModel
import uvicorn

# 可选依赖（监控API）
import psutil  # 仅用于监控API
```

### 2. 配置管理 (`src/config/`)
```python
# 必需依赖
import configparser  # Python内置
from pydantic import BaseModel, Field  # 配置验证

# 高级配置管理器
import os
import threading
import time
# 无外部依赖
```

### 3. 核心业务 (`src/core/`)
```python
# 必需依赖
from src.clients.mt5_market import MT5MarketClient  # 内部依赖
from src.config import MarketSettings  # 内部依赖

# 标准库
from collections import deque
import queue
from datetime import datetime
```

### 4. 指标计算 (`src/indicators/`)
```python
# 基础指标函数（无外部依赖）
import math
from typing import Dict, List, Any

# 指标工作器
import importlib
import threading
import time
from collections import deque

# 优化模块
from concurrent.futures import ThreadPoolExecutor  # 并行计算
```

### 5. 数据采集 (`src/ingestion/`)
```python
# 必需依赖
import threading
import time
from src.clients.mt5_market import MT5MarketClient  # 内部依赖
from src.core.market_service import MarketDataService  # 内部依赖
```

### 6. 监控系统 (`src/monitoring/`)
```python
# 必需依赖
import sqlite3  # Python内置
import threading
import time
import logging

# 可选依赖
try:
    import psutil  # 系统监控
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
```

### 7. 工具函数 (`src/utils/`)
```python
# 事件存储
import sqlite3  # Python内置
import threading
import time

# 内存管理
try:
    import psutil  # 系统监控
    import gc  # 垃圾回收
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
```

### 8. 客户端库 (`src/clients/`)
```python
# MT5客户端（假设有MT5 Python API）
# 这里可能依赖MetaTrader5的Python包
# 但具体实现未提供，假设为抽象接口
```

## 依赖冲突和兼容性

### 已知兼容性问题
1. **pydantic v1 vs v2**: 项目使用pydantic v2语法，确保安装v2.x
2. **Python版本**: 需要Python 3.8+，推荐Python 3.9+
3. **系统库**: 某些系统可能需要安装开发库：
   ```bash
   # Ubuntu/Debian
   sudo apt-get install python3-dev libpq-dev
   
   # CentOS/RHEL
   sudo yum install python3-devel postgresql-devel
   ```

### 版本锁定建议
```txt
# requirements.txt 建议版本
fastapi==0.104.0
uvicorn==0.24.0
pydantic==2.5.0
psycopg2-binary==2.9.0
python-multipart==0.0.6
psutil==5.9.0
```

## 环境配置

### 开发环境
```bash
# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

### 生产环境
```bash
# 最小化安装
pip install fastapi uvicorn pydantic psycopg2-binary

# 如果需要优化功能
pip install psutil
```

### Docker环境
```dockerfile
FROM python:3.9-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 运行应用
CMD ["python", "app.py"]
```

## 依赖检查脚本

### 检查依赖是否安装
```python
# check_dependencies.py
import importlib
import sys

def check_dependency(name, package_name=None):
    """检查依赖是否安装"""
    package_name = package_name or name
    try:
        importlib.import_module(package_name)
        print(f"✅ {name}")
        return True
    except ImportError:
        print(f"❌ {name} (包名: {package_name})")
        return False

# 检查核心依赖
dependencies = [
    ("FastAPI", "fastapi"),
    ("Uvicorn", "uvicorn"),
    ("Pydantic", "pydantic"),
    ("Psycopg2", "psycopg2"),
    ("Python-multipart", "python_multipart"),
    ("Psutil", "psutil"),
]

print("检查依赖安装情况:")
all_ok = True
for name, package in dependencies:
    if not check_dependency(name, package):
        all_ok = False

if all_ok:
    print("\n所有依赖已安装，系统可以正常运行。")
else:
    print("\n部分依赖未安装，请运行: pip install -r requirements.txt")
    sys.exit(1)
```

### 测试依赖功能
```python
# test_dependencies.py
def test_sqlite():
    """测试SQLite功能"""
    import sqlite3
    conn = sqlite3.connect(':memory:')
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)')
    cursor.execute('INSERT INTO test (name) VALUES (?)', ('test',))
    conn.commit()
    conn.close()
    print("✅ SQLite功能正常")

def test_psutil():
    """测试psutil功能"""
    try:
        import psutil
        memory = psutil.virtual_memory()
        print(f"✅ Psutil功能正常，内存使用: {memory.percent}%")
    except ImportError:
        print("⚠️  Psutil未安装，部分优化功能不可用")

# 运行测试
test_sqlite()
test_psutil()
```

## 故障排除

### 常见问题

#### 1. `ModuleNotFoundError: No module named 'pydantic'`
**解决方案**:
```bash
pip install pydantic
```

#### 2. `Error: pg_config executable not found`
**解决方案** (Ubuntu/Debian):
```bash
sudo apt-get install libpq-dev python3-dev
```

**解决方案** (CentOS/RHEL):
```bash
sudo yum install postgresql-devel python3-devel
```

#### 3. `ImportError: cannot import name 'BaseModel' from 'pydantic'`
**原因**: 安装了pydantic v1，但代码需要v2
**解决方案**:
```bash
pip install --upgrade pydantic
```

#### 4. `sqlite3.OperationalError: unable to open database file`
**原因**: 数据库文件权限问题
**解决方案**:
```bash
# 检查文件权限
ls -la *.db
# 修复权限
chmod 644 *.db
```

### 依赖更新策略

#### 安全更新
```bash
# 更新所有依赖到最新安全版本
pip install --upgrade fastapi uvicorn pydantic psycopg2-binary
```

#### 测试更新
```bash
# 在测试环境更新
pip install --upgrade --pre -r requirements.txt
# 运行测试
python test_enhancements.py
python test_indicator_usage.py
```

## 性能优化依赖

### 1. Psutil（系统监控）
**用途**: 内存管理、进程监控、性能分析
**安装**: `pip install psutil`
**影响**: 启用智能内存管理和系统监控功能

### 2. Watchdog（文件监控）
**用途**: 配置热加载（备用方案）
**安装**: `pip install watchdog`
**影响**: 提供更高效的文件变更检测

### 3. 标准库优化
- **`concurrent.futures`**: 并行计算支持
- **`sqlite3`**: 轻量级持久化存储
- **`threading`**: 多线程处理

## 生产环境建议

### 最小化部署
```bash
# 只安装必需依赖
pip install fastapi uvicorn pydantic psycopg2-binary
```

### 完整功能部署
```bash
# 安装所有依赖（包括优化功能）
pip install fastapi uvicorn pydantic psycopg2-binary psutil
```

### 监控增强部署
```bash
# 安装监控相关依赖
pip install fastapi uvicorn pydantic psycopg2-binary psutil watchdog
```

---

**最后更新**: 2026-03-10  
**维护建议**: 定期检查依赖更新，特别是安全更新  
**测试建议**: 更新依赖后运行`test_enhancements.py`验证功能

