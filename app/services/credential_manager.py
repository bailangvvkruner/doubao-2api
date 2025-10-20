# /app/services/credential_manager.py
import threading
from typing import List
from loguru import logger

class CredentialManager:
    def __init__(self, credentials: List[str]):
        if not credentials:
            raise ValueError("凭证列表不能为空。")
        self.credentials = credentials
        self.index = 0
        self.lock = threading.Lock()
        logger.info(f"凭证管理器已初始化，共加载 {len(self.credentials)} 个凭证。")

    def get_credential(self) -> str:
        with self.lock:
            credential = self.credentials[self.index]
            self.index = (self.index + 1) % len(self.credentials)
            logger.debug(f"轮询到凭证索引: {self.index}")
            return credential
