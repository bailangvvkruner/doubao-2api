# /app/core/config.py
import os
import uuid
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator
from typing import Optional, List, Dict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding='utf-8',
        extra="ignore"
    )

    APP_NAME: str = "doubao-2api"
    APP_VERSION: str = "1.0.0"
    DESCRIPTION: str = "一个将 doubao.com 转换为兼容 OpenAI 格式 API 的高性能代理，内置 a_bogus 签名解决方案。"

    # --- 核心安全与部署配置 ---
    API_MASTER_KEY: Optional[str] = "1"
    NGINX_PORT: int = 8088
    
    # --- Doubao 凭证 ---
    DOUBAO_COOKIES: List[str] = []

    # --- 核心变更: 静态设备指纹配置 ---
    # 从您提供的有效请求中提取的静态设备指纹，这比动态嗅探稳定得多
    # 如果未来失效，只需从浏览器抓取新的请求并更新此处的值
    DOUBAO_DEVICE_ID: Optional[str] = None
    DOUBAO_FP: Optional[str] = None
    DOUBAO_TEA_UUID: Optional[str] = None
    DOUBAO_WEB_ID: Optional[str] = None

    # --- 上游 API 配置 ---
    API_REQUEST_TIMEOUT: int = 180
    
    # --- 会话管理 ---
    SESSION_CACHE_TTL: int = 3600

    # --- 模型配置 ---
    DEFAULT_MODEL: str = "doubao-pro-chat"
    MODEL_MAPPING: Dict[str, str] = {
        "doubao-pro-chat": "7338286299411103781", # 默认模型 Bot ID
    }

    @model_validator(mode='after')
    def validate_settings(self) -> 'Settings':
        # 从环境变量 DOUBAO_COOKIE_1, DOUBAO_COOKIE_2, ... 加载 cookies
        i = 1
        while True:
            cookie_str = os.getenv(f"DOUBAO_COOKIE_{i}")
            if cookie_str:
                self.DOUBAO_COOKIES.append(cookie_str)
                i += 1
            else:
                break
        
        if not self.DOUBAO_COOKIES:
            raise ValueError("必须至少配置一个有效的 DOUBAO_COOKIE 环境变量 (例如 DOUBAO_COOKIE_1)")

        # --- 核心变更: 优先从环境变量读取设备指纹 ---
        # 如果环境变量中有值，则使用环境变量的值覆盖 .env 文件中的值
        device_id_from_env = os.getenv("DOUBAO_DEVICE_ID")
        fp_from_env = os.getenv("DOUBAO_FP")
        tea_uuid_from_env = os.getenv("DOUBAO_TEA_UUID")
        web_id_from_env = os.getenv("DOUBAO_WEB_ID")
        
        if device_id_from_env:
            self.DOUBAO_DEVICE_ID = device_id_from_env
        if fp_from_env:
            self.DOUBAO_FP = fp_from_env
        if tea_uuid_from_env:
            self.DOUBAO_TEA_UUID = tea_uuid_from_env
        if web_id_from_env:
            self.DOUBAO_WEB_ID = web_id_from_env

        # --- 验证设备指纹是否已配置 ---
        if not all([self.DOUBAO_DEVICE_ID, self.DOUBAO_FP, self.DOUBAO_TEA_UUID, self.DOUBAO_WEB_ID]):
            raise ValueError("必须配置完整的设备指纹参数 (DOUBAO_DEVICE_ID, DOUBAO_FP, DOUBAO_TEA_UUID, DOUBAO_WEB_ID)")
        
        return self

settings = Settings()
