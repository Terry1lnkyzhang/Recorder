from __future__ import annotations

import os


# 数据库用户名（环境变量：AT_DB_USER）。
DB_USER = os.getenv("AT_DB_USER", "root")
# 数据库密码（环境变量：AT_DB_PASSWORD）。
DB_PASSWORD = os.getenv("AT_DB_PASSWORD", "Svv_2016")
# 数据库地址与端口（环境变量：AT_DB_HOST）。
DB_HOST = os.getenv("AT_DB_HOST", "130.147.129.203:3306")
# 数据库名（环境变量：AT_DB_NAME）。
DB_NAME = os.getenv("AT_DB_NAME", "ATFrameworkDB")

# SQLAlchemy / PyMySQL 兼容连接串。
DB_URL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}/{DB_NAME}?charset=utf8mb4"
