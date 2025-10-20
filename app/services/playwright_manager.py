# /app/services/playwright_manager.py
import asyncio
import json
import uuid
from typing import Optional, Dict, List
from urllib.parse import urlencode, urlparse

from playwright_stealth import stealth_async
from playwright.async_api import async_playwright, Browser, Page, ConsoleMessage, TimeoutError, Route, Request
from loguru import logger

from app.core.config import settings # 导入 settings

def handle_console_message(msg: ConsoleMessage):
    """将浏览器控制台日志转发到 Loguru，并过滤已知噪音"""
    log_level = msg.type.upper()
    text = msg.text
    # 过滤掉常见的、无害的浏览器噪音
    if "Failed to load resource" in text or "net::ERR_FAILED" in text:
        return
    if "WebSocket connection" in text:
        return
    if "Content Security Policy" in text:
        return
    if "Scripts may close only the windows that were opened by them" in text:
        return
    if "Ignoring too frequent calls to print()" in text:
        return

    log_message = f"[Browser Console] {text}"
    if log_level == "ERROR":
        logger.error(log_message)
    elif log_level == "WARNING":
        logger.warning(log_message)
    else:
        pass

class PlaywrightManager:
    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(PlaywrightManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    async def initialize(self, cookies: List[str]):
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            logger.info("正在初始化 Playwright 管理器 (签名服务模式)...")
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            self.page = await self.browser.new_page()

            await stealth_async(self.page)
            self.page.on("console", handle_console_message)
            await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            self.static_device_fingerprint = {
                'device_id': settings.DOUBAO_DEVICE_ID,
                'fp': settings.DOUBAO_FP,
                'web_id': settings.DOUBAO_WEB_ID,
                'tea_uuid': settings.DOUBAO_TEA_UUID
            }
            logger.success(f"已从配置中加载静态设备指纹: {self.static_device_fingerprint}")
            
            self.ms_token = None

            async def _handle_response(response):
                try:
                    if 'x-ms-token' in response.headers:
                        token = response.headers['x-ms-token']
                        if token != self.ms_token:
                            self.ms_token = token
                            logger.success(f"通过响应头捕获到新的 msToken: {self.ms_token}")
                except Exception as e:
                    logger.warning(f"处理响应时出错: {e} (URL: {response.url})")

            self.page.on("response", _handle_response)

            if not cookies:
                raise ValueError("Playwright 初始化需要至少一个有效的 Cookie。")
            
            logger.info("正在为初始页面加载设置 Cookie...")
            initial_cookie_str = cookies[0]
            try:
                cookie_list = [
                    {"name": c.split('=')[0].strip(), "value": c.split('=', 1)[1].strip(), "domain": ".doubao.com", "path": "/"}
                    for c in initial_cookie_str.split(';') if '=' in c
                ]
                await self.page.context.add_cookies(cookie_list)
                logger.success("初始 Cookie 设置完成。")
            except IndexError as e:
                logger.error(f"解析 Cookie 时出错: '{initial_cookie_str}'. 请确保 Cookie 格式正确。错误: {e}")
                raise ValueError("Cookie 格式无效，无法进行初始化。") from e

            try:
                logger.info("正在导航到豆包官网以加载签名脚本 (超时时间: 60秒)...")
                await self.page.goto(
                    "https://www.doubao.com/chat/",
                    wait_until="load",
                    timeout=60000
                )
                logger.info("页面导航完成 (load 事件触发)。")
            except TimeoutError as e:
                logger.error(f"导航到豆包官网超时: {e}")
                raise RuntimeError("无法访问豆包官网，初始化失败。") from e

            try:
                logger.info("正在等待关键签名函数 (window.byted_acrawler.frontierSign) 加载 (超时时间: 30秒)...")
                await self.page.wait_for_function(
                    "() => typeof window.byted_acrawler?.frontierSign === 'function'",
                    timeout=30000
                )
                logger.success("关键签名函数已在启动时成功加载！")
            except TimeoutError:
                logger.error("等待签名函数超时！这很可能是因为 Cookie 无效或已过期。")
                raise RuntimeError("无法加载豆包签名函数，请检查并更新 Cookie。")
            
            if not self.ms_token:
                logger.info("等待 msToken 出现，最长等待 10 秒...")
                await asyncio.sleep(10)
                if not self.ms_token:
                    logger.warning("在额外等待后，依然未能捕获到初始 msToken。后续请求将依赖响应头更新。")

            logger.success("Playwright 管理器 (签名服务模式) 初始化完成。")
            self._initialized = True

    def update_ms_token(self, token: str):
        self.ms_token = token

    async def get_signed_url(self, base_url: str, cookie: str, base_params: Dict[str, str]) -> Optional[str]:
        async with self._lock:
            if not self._initialized:
                raise RuntimeError("PlaywrightManager 未初始化。")
            
            try:
                logger.info("正在使用 Playwright 生成 a_bogus 签名...")
                
                final_params = base_params.copy()
                final_params.update(self.static_device_fingerprint)
                
                final_params['web_tab_id'] = str(uuid.uuid4())
                if self.ms_token:
                    final_params['msToken'] = self.ms_token
                else:
                    logger.error("msToken 未被初始化，无法构建有效请求！")
                    return None

                # --- 核心修复: 对参数进行字母排序，以生成正确的签名 ---
                sorted_params = dict(sorted(final_params.items()))
                final_query_string = urlencode(sorted_params)
                url_with_params = f"{base_url}?{final_query_string}"

                logger.info(f"正在使用静态指纹和排序后的参数调用 window.byted_acrawler.frontierSign: \"{final_query_string}\"")
                signature_obj = await self.page.evaluate(f'window.byted_acrawler.frontierSign("{final_query_string}")')
                
                if isinstance(signature_obj, dict) and ('a_bogus' in signature_obj or 'X-Bogus' in signature_obj):
                    bogus_value = signature_obj.get('a_bogus') or signature_obj.get('X-Bogus')
                    logger.success(f"成功解析签名对象，获取到 a_bogus: {bogus_value}")
                    
                    signed_url = f"{url_with_params}&a_bogus={bogus_value}"
                    return signed_url
                else:
                    logger.error(f"调用签名函数失败，返回值不是预期的字典格式或缺少 a_bogus: {signature_obj}")
                    return None

            except Exception as e:
                logger.error(f"Playwright 签名时发生严重错误: {e}", exc_info=True)
                return None

    async def close(self):
        if self._initialized:
            async with self._lock:
                if self.browser:
                    await self.browser.close()
                if self.playwright:
                    await self.playwright.stop()
                self._initialized = False
                logger.info("Playwright 管理器已关闭。")
