"""
HTTP连接池管理
"""

import logging
import threading
from typing import Dict, Any, Optional, Mapping
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

@dataclass
class PoolStats:
    """连接池统计信息"""
    
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    connection_errors: int = 0
    timeout_errors: int = 0
    
    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.total_requests == 0:
            return 0.0
        return (self.successful_requests / self.total_requests) * 100
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'total_requests': self.total_requests,
            'successful_requests': self.successful_requests,
            'failed_requests': self.failed_requests,
            'connection_errors': self.connection_errors,
            'timeout_errors': self.timeout_errors,
            'success_rate': f"{self.success_rate:.1f}%"
        }

class ConnectionPool:
    """HTTP连接池"""
    _BODY_PREVIEW_CHARS = 1200
    
    def __init__(self, 
                 pool_size: int = 10,
                 max_retries: int = 3,
                 timeout: int = 10,
                 backoff_factor: float = 0.3):
        """
        初始化连接池
        
        Args:
            pool_size: 连接池大小
            max_retries: 最大重试次数
            timeout: 超时时间（秒）
            backoff_factor: 退避因子
        """
        self.pool_size = pool_size
        self.max_retries = max_retries
        self.timeout = timeout
        self.backoff_factor = backoff_factor
        
        # 创建Session
        self.session = self._create_session()
        
        # 统计信息
        self.stats = PoolStats()
        self.stats_lock = threading.Lock()
        self._session_headers_lock = threading.Lock()
        
        # 日志
        self.logger = logging.getLogger(__name__)
        
        self.logger.info(f"HTTP连接池初始化，大小: {pool_size}")
    
    def _create_session(self) -> requests.Session:
        """创建并配置Session"""
        session = requests.Session()
        
        # 配置重试策略
        retry_strategy = Retry(
            total=self.max_retries,
            backoff_factor=self.backoff_factor,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE"]
        )
        
        # 创建适配器
        adapter = HTTPAdapter(
            pool_connections=self.pool_size,
            pool_maxsize=self.pool_size,
            max_retries=retry_strategy
        )
        
        # 挂载适配器
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        # 设置默认请求头
        session.headers.update({
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'FN-Log-Monitor/1.0',
            'Accept': 'application/json'
        })
        
        return session
    
    def post(
        self,
        url: str,
        data: Dict[str, Any],
        headers: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        发送POST请求
        
        Returns:
            {"success": bool, "response": dict|None, "error": str|None}
            成功时 response 为接口返回体，失败时 error 为原因描述
        """
        with self.stats_lock:
            self.stats.total_requests += 1
        out = {"success": False, "response": None, "error": None}
        try:
            kwargs: Dict[str, Any] = {"json": data, "timeout": self.timeout}
            if headers:
                kwargs["headers"] = dict(headers)
            response = self.session.post(url, **kwargs)
            response.raise_for_status()
            result = None
            try:
                result = response.json()
            except Exception:
                result = None
            body = result if isinstance(result, dict) else {}
            # 企业微信/钉钉等含 errcode，非 0 视为失败但保留返回体
            if isinstance(body, dict) and "errcode" in body and body.get("errcode") != 0:
                self.logger.error(f"API返回错误: {body}")
                with self.stats_lock:
                    self.stats.failed_requests += 1
                out["response"] = body
                out["error"] = body.get("errmsg") or f"errcode={body.get('errcode')}"
                return out
            with self.stats_lock:
                self.stats.successful_requests += 1
            out["success"] = True
            out["response"] = body if body is not None else {}
            return out
        except requests.exceptions.Timeout:
            self.logger.error(f"POST请求超时 (timeout={self.timeout}s): {url}")
            with self.stats_lock:
                self.stats.timeout_errors += 1
                self.stats.failed_requests += 1
            out["error"] = "请求超时"
            return out
        except requests.exceptions.ConnectionError as e:
            self.logger.error(f"POST连接错误: {url} - {str(e)}")
            with self.stats_lock:
                self.stats.connection_errors += 1
                self.stats.failed_requests += 1
            out["error"] = "连接错误: " + (str(e)[:80] or "未知")
            return out
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            self.logger.error(f"POST HTTP错误: {url} - 状态码: {code}")
            with self.stats_lock:
                self.stats.failed_requests += 1
            body_dict = None
            body_text = ""
            if e.response is not None:
                try:
                    body_dict = e.response.json()
                except Exception:
                    body_dict = None
                try:
                    body_text = e.response.text or ""
                except Exception:
                    body_text = ""
            # 失败也保留接口返回体（优先 JSON，否则 text 预览）
            resp_obj: Dict[str, Any] = {"status_code": code}
            if isinstance(body_dict, dict):
                resp_obj["json"] = body_dict
                msg = body_dict.get("errmsg") or body_dict.get("message") or ""
                out["error"] = f"HTTP {code}" + (f": {msg}" if msg else "")
            else:
                preview = (body_text or "")[: self._BODY_PREVIEW_CHARS]
                resp_obj["text_preview"] = preview
                out["error"] = f"HTTP {code}"
            out["response"] = resp_obj
            return out
        except Exception as e:
            self.logger.error(f"POST请求异常: {url} - {type(e).__name__}: {str(e)}", exc_info=True)
            with self.stats_lock:
                self.stats.failed_requests += 1
            out["error"] = f"{type(e).__name__}: {(str(e) or '')[:80]}"
            return out

    def get(self, url: str) -> Dict[str, Any]:
        """
        发送GET请求（用于Bark等）
        Returns:
            {"success": bool, "response": dict|None, "error": str|None}
        """
        with self.stats_lock:
            self.stats.total_requests += 1
        out = {"success": False, "response": None, "error": None}
        try:
            with self._session_headers_lock:
                original_content_type = self.session.headers.get("Content-Type")
                if original_content_type:
                    del self.session.headers["Content-Type"]
            try:
                response = self.session.get(url, timeout=self.timeout)
            finally:
                with self._session_headers_lock:
                    if original_content_type:
                        self.session.headers["Content-Type"] = original_content_type
            if response.status_code < 400:
                with self.stats_lock:
                    self.stats.successful_requests += 1
                out["success"] = True
                try:
                    out["response"] = response.json() if response.content else {}
                except Exception:
                    out["response"] = {"status_code": response.status_code, "text_preview": (response.text or "")[: self._BODY_PREVIEW_CHARS]}
                return out
            self.logger.error(f"GET请求失败: {response.status_code}")
            with self.stats_lock:
                self.stats.failed_requests += 1
            # 失败也保留接口返回体（优先 JSON，否则 text 预览）
            resp_obj: Dict[str, Any] = {"status_code": response.status_code}
            try:
                body = response.json() if response.content else None
            except Exception:
                body = None
            if isinstance(body, dict):
                resp_obj["json"] = body
            else:
                resp_obj["text_preview"] = (response.text or "")[: self._BODY_PREVIEW_CHARS]
            out["response"] = resp_obj
            out["error"] = f"HTTP {response.status_code}"
            return out
        except requests.exceptions.Timeout:
            self.logger.error(f"GET请求超时 (timeout={self.timeout}s): {url}")
            with self.stats_lock:
                self.stats.timeout_errors += 1
                self.stats.failed_requests += 1
            out["error"] = "请求超时"
            return out
        except requests.exceptions.ConnectionError as e:
            self.logger.error(f"GET连接错误: {url} - {str(e)}")
            with self.stats_lock:
                self.stats.connection_errors += 1
                self.stats.failed_requests += 1
            out["error"] = "连接错误: " + (str(e)[:80] or "未知")
            return out
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            with self.stats_lock:
                self.stats.failed_requests += 1
            out["error"] = f"HTTP {code}"
            # 失败也尽量带上返回体
            if e.response is not None:
                resp_obj: Dict[str, Any] = {"status_code": code}
                try:
                    body = e.response.json()
                except Exception:
                    body = None
                if isinstance(body, dict):
                    resp_obj["json"] = body
                else:
                    try:
                        resp_obj["text_preview"] = (e.response.text or "")[: self._BODY_PREVIEW_CHARS]
                    except Exception:
                        pass
                out["response"] = resp_obj
            return out
        except Exception as e:
            self.logger.error(f"GET请求异常: {url} - {type(e).__name__}: {str(e)}", exc_info=True)
            with self.stats_lock:
                self.stats.failed_requests += 1
            out["error"] = f"{type(e).__name__}: {(str(e) or '')[:80]}"
            return out
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self.stats_lock:
            return self.stats.to_dict()
    
    def close(self):
        """关闭连接池"""
        self.session.close()
        self.logger.info("HTTP连接池已关闭")
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.close()
