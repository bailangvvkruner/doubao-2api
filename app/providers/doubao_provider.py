# /app/providers/doubao_provider.py
import json
import re
import time
import uuid
from typing import Dict, Any, AsyncGenerator, List

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from loguru import logger

from app.core.config import settings
from app.providers.base_provider import BaseProvider
from app.services.credential_manager import CredentialManager
from app.services.playwright_manager import PlaywrightManager
from app.services.session_manager import SessionManager
from app.utils.sse_utils import create_sse_data, create_chat_completion_chunk, DONE_CHUNK


class DoubaoProvider(BaseProvider):
    def __init__(self):
        self.credential_manager = CredentialManager(settings.DOUBAO_COOKIES)
        self.session_manager = SessionManager()
        self.playwright_manager = PlaywrightManager()
        self.client: httpx.AsyncClient = None

    async def initialize(self):
        self.client = httpx.AsyncClient(timeout=settings.API_REQUEST_TIMEOUT)
        await self.playwright_manager.initialize(self.credential_manager.credentials)

    async def close(self):
        if self.client:
            await self.client.aclose()
        await self.playwright_manager.close()

    def _get_dynamic_cookie(self, base_cookie: str) -> str:
        """
        用 Playwright 捕获的最新 msToken 更新基础 Cookie 字符串。
        这是确保签名和请求头一致性的关键。
        """
        latest_ms_token = self.playwright_manager.ms_token
        if not latest_ms_token:
            logger.warning("动态 Cookie 更新失败：Playwright 管理器中没有可用的 msToken。将使用原始 Cookie。")
            return base_cookie

        if 'msToken=' in base_cookie:
            new_cookie = re.sub(r'msToken=[^;]+', f'msToken={latest_ms_token}', base_cookie)
            logger.info("成功将动态 msToken 更新到 Cookie 头中。")
        else:
            new_cookie = f"{base_cookie.strip(';')}; msToken={latest_ms_token}"
            logger.info("原始 Cookie 中未找到 msToken，已追加最新的 msToken。")
        
        return new_cookie

    async def chat_completion(self, request_data: Dict[str, Any]):
        """
        根据请求中的 'stream' 参数，分发到流式或非流式处理函数。
        """
        is_stream = request_data.get("stream", True)

        if is_stream:
            return StreamingResponse(self._stream_generator(request_data), media_type="text/event-stream")
        else:
            return await self._non_stream_completion(request_data)

    async def _non_stream_completion(self, request_data: Dict[str, Any]) -> JSONResponse:
        """
        处理非流式聊天补全请求。
        """
        session_id = request_data.get("user", f"session-{uuid.uuid4().hex}")
        messages = request_data.get("messages", [])
        user_model = request_data.get("model", settings.DEFAULT_MODEL)

        bot_id = settings.MODEL_MAPPING.get(user_model)
        if not bot_id:
            raise HTTPException(status_code=400, detail=f"不支持的模型: {user_model}")

        session_data = self.session_manager.get_session(session_id) or {}
        conversation_id = session_data.get("conversation_id", "0")
        is_new_conversation = conversation_id == "0"

        request_id = f"chatcmpl-{uuid.uuid4()}"
        new_conversation_id = None
        full_content = []
        streamed_any_data = False

        try:
            base_cookie = self.credential_manager.get_credential()
            final_cookie = self._get_dynamic_cookie(base_cookie)
            base_url = "https://www.doubao.com/samantha/chat/completion"
            base_params = {
                "aid": "497858", "device_platform": "web", "language": "zh",
                "pc_version": "2.41.0", "pkg_type": "release_version", "real_aid": "497858",
                "region": "CN", "samantha_web": "1", "sys_region": "CN",
                "use-olympus-account": "1", "version_code": "20800",
            }
            headers = self._prepare_headers(final_cookie)
            payload = self._prepare_payload(messages, bot_id, conversation_id)

            log_headers = headers.copy()
            log_headers["Cookie"] = "[REDACTED FOR SECURITY]"
            logger.info("--- 准备向上游发送的完整请求包 (非流式) ---")
            logger.info(f"请求方法: POST")
            logger.info(f"基础URL: {base_url}")
            logger.info(f"请求头 (Headers):\n{json.dumps(log_headers, indent=2)}")
            logger.info(f"请求载荷 (Payload):\n{json.dumps(payload, indent=2, ensure_ascii=False)}")
            logger.info("------------------------------------")

            signed_url = await self.playwright_manager.get_signed_url(base_url, final_cookie, base_params)
            if not signed_url:
                raise Exception("无法获取 a_bogus 签名, Playwright 服务可能异常。")

            logger.info(f"签名成功，最终请求 URL: {signed_url}")

            async with self.client.stream("POST", signed_url, headers=headers, json=payload) as response:
                new_ms_token = response.headers.get("x-ms-token")
                if new_ms_token:
                    self.playwright_manager.update_ms_token(new_ms_token)
                    logger.success(f"从响应头中捕获并更新了 msToken: {new_ms_token}")

                if response.status_code != 200:
                    error_content = await response.aread()
                    logger.error(f"上游服务器返回错误状态码: {response.status_code}。")
                    logger.error(f"上游服务器响应内容: {error_content.decode(errors='ignore')}")
                    response.raise_for_status()

                logger.success(f"成功连接到上游服务器, 状态码: {response.status_code}. 开始接收响应...")

                async for line in response.aiter_lines():
                    # [诊断日志] 打印从上游收到的每一行原始数据
                    logger.info(f"上游原始响应行: {line}")
                    streamed_any_data = True
                    if not line.startswith("data:"):
                        continue
                    content_str = line[len("data:"):].strip()
                    if not content_str:
                        continue

                    try:
                        data = json.loads(content_str)
                        if data.get("event_type") == 2002 and not new_conversation_id:
                            event_data = json.loads(data.get("event_data", "{}"))
                            new_conversation_id = event_data.get("conversation_id")
                            logger.info(f"捕获到新会话 ID: {new_conversation_id}")

                        if data.get("event_type") == 2001:
                            event_data = json.loads(data.get("event_data", "{}"))
                            message_data = event_data.get("message", {})
                            content_json = json.loads(message_data.get("content", "{}"))
                            delta_content = content_json.get("text", "")
                            if delta_content:
                                full_content.append(delta_content)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"解析 SSE 数据块时跳过: {e}, 内容: {content_str}")
                        continue

            if not streamed_any_data:
                logger.error("上游服务器返回了 200 OK，但没有发送任何数据流。这通常是由于反爬虫策略触发。")
                raise Exception("服务器连接成功但未返回数据流，请求可能被上游服务拦截。请检查Cookie是否过期或IP是否被限制。")

            if is_new_conversation and new_conversation_id:
                self.session_manager.update_session(session_id, {"conversation_id": new_conversation_id})
                logger.info(f"为用户 '{session_id}' 保存了新的会话 ID: {new_conversation_id}")

            final_text = "".join(full_content)

            # 按照用户要求，将完整的响应内容打印到终端
            print("\n--- [非流式] 完整响应内容 ---")
            print(final_text)
            print("---------------------------------\n")

            response_data = {
                "id": request_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": user_model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": final_text},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
            return JSONResponse(content=response_data)

        except Exception as e:
            logger.error(f"处理非流式请求时发生严重错误: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": {"message": f"内部服务器错误: {str(e)}", "type": "server_error", "code": None}}
            )

    async def _stream_generator(self, request_data: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
        """
        处理流式聊天补全请求。
        """
        session_id = request_data.get("user", f"session-{uuid.uuid4().hex}")
        messages = request_data.get("messages", [])
        user_model = request_data.get("model", settings.DEFAULT_MODEL)

        bot_id = settings.MODEL_MAPPING.get(user_model)
        if not bot_id:
            # This should be handled before calling the generator, but as a safeguard:
            error_chunk = create_chat_completion_chunk(f"chatcmpl-{uuid.uuid4()}", user_model, f"不支持的模型: {user_model}", "stop")
            yield create_sse_data(error_chunk)
            yield DONE_CHUNK
            return

        session_data = self.session_manager.get_session(session_id) or {}
        conversation_id = session_data.get("conversation_id", "0")
        is_new_conversation = conversation_id == "0"

        request_id = f"chatcmpl-{uuid.uuid4()}"
        new_conversation_id = None
        streamed_any_data = False

        try:
            base_cookie = self.credential_manager.get_credential()
            final_cookie = self._get_dynamic_cookie(base_cookie)
            base_url = "https://www.doubao.com/samantha/chat/completion"
            base_params = {
                "aid": "497858", "device_platform": "web", "language": "zh",
                "pc_version": "2.41.0", "pkg_type": "release_version", "real_aid": "497858",
                "region": "CN", "samantha_web": "1", "sys_region": "CN",
                "use-olympus-account": "1", "version_code": "20800",
            }
            headers = self._prepare_headers(final_cookie)
            payload = self._prepare_payload(messages, bot_id, conversation_id)

            log_headers = headers.copy()
            log_headers["Cookie"] = "[REDACTED FOR SECURITY]"
            logger.info("--- 准备向上游发送的完整请求包 (流式) ---")
            logger.info(f"请求方法: POST")
            logger.info(f"基础URL: {base_url}")
            logger.info(f"请求头 (Headers):\n{json.dumps(log_headers, indent=2)}")
            logger.info(f"请求载荷 (Payload):\n{json.dumps(payload, indent=2, ensure_ascii=False)}")
            logger.info("------------------------------------")

            signed_url = await self.playwright_manager.get_signed_url(base_url, final_cookie, base_params)
            if not signed_url:
                raise Exception("无法获取 a_bogus 签名, Playwright 服务可能异常。")

            logger.info(f"签名成功，最终请求 URL: {signed_url}")

            # 按照用户要求，在流式输出前打印一个标识
            print("\n--- [流式] 响应内容 ---")

            async with self.client.stream("POST", signed_url, headers=headers, json=payload) as response:
                new_ms_token = response.headers.get("x-ms-token")
                if new_ms_token:
                    self.playwright_manager.update_ms_token(new_ms_token)
                    logger.success(f"从响应头中捕获并更新了 msToken: {new_ms_token}")

                if response.status_code != 200:
                    error_content = await response.aread()
                    logger.error(f"上游服务器返回错误状态码: {response.status_code}。")
                    logger.error(f"上游服务器响应内容: {error_content.decode(errors='ignore')}")
                    response.raise_for_status()

                logger.success(f"成功连接到上游服务器, 状态码: {response.status_code}. 开始接收响应...")

                async for line in response.aiter_lines():
                    # [诊断日志] 打印从上游收到的每一行原始数据
                    logger.info(f"上游原始响应行: {line}")
                    streamed_any_data = True
                    if not line.startswith("data:"):
                        continue
                    content_str = line[len("data:"):].strip()
                    if not content_str:
                        continue

                    try:
                        data = json.loads(content_str)
                        if data.get("event_type") == 2002 and not new_conversation_id:
                            event_data = json.loads(data.get("event_data", "{}"))
                            new_conversation_id = event_data.get("conversation_id")
                            logger.info(f"捕获到新会话 ID: {new_conversation_id}")

                        if data.get("event_type") == 2001:
                            event_data = json.loads(data.get("event_data", "{}"))
                            message_data = event_data.get("message", {})
                            content_json = json.loads(message_data.get("content", "{}"))
                            delta_content = content_json.get("text", "")
                            if delta_content:
                                # 按照用户要求，将流式数据块直接打印到终端
                                print(delta_content, end="", flush=True)
                                chunk = create_chat_completion_chunk(request_id, user_model, delta_content)
                                yield create_sse_data(chunk)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"解析 SSE 数据块时跳过: {e}, 内容: {content_str}")
                        continue
            
            # 在流式输出结束后打印换行符和结束标识
            if streamed_any_data:
                print("\n--------------------------\n")

            if not streamed_any_data:
                logger.error("上游服务器返回了 200 OK，但没有发送任何数据流。这通常是由于反爬虫策略触发。")
                error_message = "服务器连接成功但未返回数据流，请求可能被上游服务拦截。请检查Cookie是否过期或IP是否被限制。"
                error_chunk = create_chat_completion_chunk(request_id, user_model, error_message, "stop")
                yield create_sse_data(error_chunk)
                yield DONE_CHUNK
                return

            if is_new_conversation and new_conversation_id:
                self.session_manager.update_session(session_id, {"conversation_id": new_conversation_id})
                logger.info(f"为用户 '{session_id}' 保存了新的会话 ID: {new_conversation_id}")

            final_chunk = create_chat_completion_chunk(request_id, user_model, "", "stop")
            yield create_sse_data(final_chunk)
            yield DONE_CHUNK

        except Exception as e:
            logger.error(f"处理流时发生严重错误: {e}", exc_info=True)
            # 在流式输出结束后打印换行符和结束标识
            print("\n--- [流式] 发生错误 ---\n")
            error_chunk = create_chat_completion_chunk(request_id, user_model, f"内部服务器错误: {str(e)}", "stop")
            yield create_sse_data(error_chunk)
            yield DONE_CHUNK

    def _prepare_headers(self, cookie: str) -> Dict[str, str]:
        return {
            "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json", "Cookie": cookie,
            "Origin": "https://www.doubao.com", "Referer": "https://www.doubao.com/chat/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
            "agw-js-conv": "str, str",
            "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
            "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
        }

    def _prepare_payload(self, messages: List[Dict[str, Any]], bot_id: str, conversation_id: str) -> Dict[str, Any]:
        last_user_message = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if not last_user_message:
            raise HTTPException(status_code=400, detail="未找到用户消息。")

        payload = {
            "messages": [{"content": json.dumps({"text": last_user_message["content"]}), "content_type": 2001, "attachments": [], "references": []}],
            "completion_option": {
                "is_regen": False, "with_suggest": True, "need_create_conversation": conversation_id == "0",
                "launch_stage": 1, "is_replace": False, "is_delete": False, "message_from": 0,
                "action_bar_skill_id": 0, "use_deep_think": False, "use_auto_cot": True,
                "resend_for_regen": False, "enable_commerce_credit": False, "event_id": "0"
            },
            "evaluate_option": {"web_ab_params": ""},
            "conversation_id": conversation_id,
            "local_conversation_id": f"local_{uuid.uuid4().hex}",
            "local_message_id": str(uuid.uuid4())
        }

        if conversation_id != "0":
            payload["bot_id"] = bot_id
        
        return payload

    async def get_models(self) -> JSONResponse:
        return JSONResponse(content={
            "object": "list",
            "data": [{"id": name, "object": "model", "created": int(time.time()), "owned_by": "lzA6"} for name in settings.MODEL_MAPPING.keys()]
        })
