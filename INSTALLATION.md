# MT5Services 安装指南

## 快速开始

### 1. 使用虚拟环境（推荐）

```bash
# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Linux/macOS:
source venv/bin/activate
# Windows:
venv\Scripts\activate
```

### 2. 安装依赖

**方法一：使用 pip（推荐）**
```bash
# 安装基础依赖
pip install -r requirements.txt

# 安装开发环境依赖
pip install -r requirements.txt[dev]
```

**方法二：使用安装脚本**
```bash
# 运行安装脚本
python install_dependencies.py

# 按照提示选择环境
```

**方法三：使用 pip 直接安装**
```bash
# 安装核心依赖
pip install fastapi uvicorn pydantic python-dotenv MetaTrader5
pip install psycopg2-binary sqlalchemy pandas numpy
pip install redis prometheus-client structlog
```

### 3. 安装 TA-Lib（技术分析库）

TA-Lib 需要系统依赖，安装方法：

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install build-essential
wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz
tar -xzf ta-lib-0.4.0-src.tar.gz
cd ta-lib/
./configure --prefix=/usr
make
sudo make install
pip install TA-Lib
```

**macOS:**
```bash
brew install ta-lib
pip install TA-Lib
```

**Windows:**
1. 下载预编译包: https://www.lfd.uci.edu/~gohlke/pythonlibs/#ta-lib
2. 根据Python版本选择对应的 `.whl` 文件
3. 安装: `pip install TA_Lib‑0.4.28‑cp3xx‑cp3xx‑win_amd64.whl`

### 4. 验证安装

```bash
# 运行验证脚本
python -c "
import fastapi
import pydantic
import pandas as pd
import numpy as np
import sqlalchemy
import redis
import prometheus_client
print('✅ 所有核心依赖安装成功')
"

# 验证 TA-Lib
python -c "import talib; print(f'✅ TA-Lib 版本: {talib.__version__}')"
```

## 环境配置

### 1. 创建环境变量文件

创建 `.env` 文件：

```bash
cp .env.example .env
```

编辑 `.env` 文件，配置以下变量：

```env
# MT5 连接配置
MT5_LOGIN=12345678
MT5_PASSWORD=your_password
MT5_SERVER=YourBroker-Server
MT5_PATH="C:/Program Files/MetaTrader 5/terminal64.exe"

# 数据库配置
PG_HOST=localhost
PG_PORT=5432
PG_USER=postgres
PG_PASSWORD=postgres
PG_DATABASE=mt5

# 应用配置
DEFAULT_SYMBOL=EURUSD
TIMEZONE=UTC
LOG_LEVEL=INFO
API_HOST=0.0.0.0
API_PORT=8808

# Redis 配置
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=

# 采集配置
INGEST_SYMBOLS=EURUSD,XAUUSD
INGEST_TICK_INTERVAL=0.5
INGEST_OHLC_TIMEFRAMES=M1,H1
```

### 2. 数据库设置

**创建数据库：**
```sql
CREATE DATABASE mt5;
CREATE USER mt5_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE mt5 TO mt5_user;
```

**初始化表结构：**
```bash
# 运行数据库初始化脚本
python -c "
from src.persistence.db import init_db
init_db()
print('✅ 数据库初始化完成')
"
```

## 开发环境设置

### 1. 安装开发工具

```bash
# 安装开发依赖
pip install -r requirements.txt[dev]

# 安装预提交钩子
pre-commit install
```

### 2. 代码格式化配置

项目使用以下工具进行代码质量检查：

- **Black**: 代码格式化
- **isort**: import 排序
- **mypy**: 类型检查
- **flake8**: 代码风格检查

**手动运行检查：**
```bash
# 格式化代码
black src/

# 排序 imports
isort src/

# 类型检查
mypy src/

# 代码风格检查
flake8 src/
```

### 3. 运行测试

```bash
# 运行所有测试
pytest

# 运行特定测试
pytest tests/test_market.py -v

# 运行测试并生成覆盖率报告
pytest --cov=src --cov-report=html
```

## 生产环境部署

### 1. 安装生产依赖

```bash
pip install -r requirements.txt[prod]
```

### 2. 使用 Gunicorn 运行

```bash
# 使用 Gunicorn + Uvicorn Workers
gunicorn src.api:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8808 \
  --timeout 120 \
  --keep-alive 5 \
  --access-logfile - \
  --error-logfile -
```

### 3. 使用 systemd 服务（Linux）

创建 `/etc/systemd/system/mt5services.service`：

```ini
[Unit]
Description=MT5 Market Data Service
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=mt5
WorkingDirectory=/opt/mt5services
Environment="PATH=/opt/mt5services/venv/bin"
ExecStart=/opt/mt5services/venv/bin/gunicorn src.api:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8808 \
  --timeout 120
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**启用服务：**
```bash
sudo systemctl daemon-reload
sudo systemctl enable mt5services
sudo systemctl start mt5services
sudo systemctl status mt5services
```

## 故障排除

### 常见问题

1. **MT5 连接失败**
   - 确保 MT5 终端已登录并运行
   - 检查登录凭据是否正确
   - 验证 MT5 路径配置

2. **数据库连接失败**
   - 检查 PostgreSQL 服务是否运行
   - 验证数据库连接参数
   - 检查防火墙设置

3. **TA-Lib 安装失败**
   - 确保已安装系统依赖
   - Windows 用户使用预编译的 whl 文件
   - 参考 TA-Lib 官方文档

4. **Redis 连接失败**
   - 检查 Redis 服务是否运行
   - 验证 Redis 配置参数
   - 检查网络连接

### 获取帮助

- 查看日志：`tail -f logs/mt5services.log`
- 检查健康状态：`curl http://localhost:8808/health`
- 查看 API 文档：`http://localhost:8808/docs`

## 更新依赖

```bash
# 更新所有依赖
pip install --upgrade -r requirements.txt

# 生成新的依赖列表
pip freeze > requirements.txt

# 使用 pip-tools 管理依赖
pip install pip-tools
pip-compile requirements.in
pip-sync requirements.txt
```

## 下一步

1. 配置环境变量 (`.env` 文件)
2. 初始化数据库
3. 启动服务：`python app.py`
4. 访问 API 文档：`http://localhost:8808/docs`
5. 开始开发！