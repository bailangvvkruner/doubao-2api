# /Dockerfile
# ====================================================================
# Dockerfile for doubao-2api (v1.4 - Patched for User Permissions)
# ====================================================================

# 使用一个稳定、广泛支持的 Debian 版本作为基础镜像
FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# 关键修正: 一次性、完整地安装所有系统依赖
# 合并了 Playwright 官方建议的核心库和我们之前发现的字体库
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright 核心依赖
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libatspi2.0-0 \
    # 官方错误日志中明确提示缺少的关键库
    libpango-1.0-0 libcairo2 \
    # 解决字体问题的包
    fonts-unifont fonts-liberation \
    # 清理 apt 缓存以减小镜像体积
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建并切换到非 root 用户
# 这一步很关键，我们先创建用户，然后以该用户身份安装浏览器
RUN useradd --create-home appuser && \
    chown -R appuser:appuser /app
USER appuser

# 核心修复：以 appuser 的身份安装 Chromium 浏览器
# 这可以确保浏览器安装在 /home/appuser/.cache/ms-playwright/ 目录下，
# 与应用运行时查找的路径一致，从而解决 "Executable doesn't exist" 错误。
RUN playwright install chromium

# 暴露端口并启动
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
