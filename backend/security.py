"""P1-1 安全加固：鉴权、SSRF 防护、CORS 配置。"""
import os
import socket
import ipaddress
import logging
from urllib.parse import urlparse
from typing import Optional

logger = logging.getLogger(__name__)

# ---------- 环境变量读取 ----------
READER_API_TOKEN = os.environ.get("READER_API_TOKEN", "").strip()
ALLOWED_BASE_URLS = os.environ.get("ALLOWED_BASE_URLS", "").strip()
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "").strip()


# ---------- 启动检查 ----------
def validate_startup(env: str = "development") -> None:
    """生产环境（READER_ENV=production）必须设置 READER_API_TOKEN，否则拒绝启动。

    不再依赖 uvicorn 并不设置的 UVICORN_HOST 变量做推断，改为显式环境判定。
    """
    if env == "production" and not READER_API_TOKEN:
        raise RuntimeError(
            "生产环境（READER_ENV=production）必须设置 READER_API_TOKEN 环境变量，"
            "否则拒绝启动。开发模式请将 READER_ENV 留空或设为 development。"
        )
    if not READER_API_TOKEN:
        logger.warning("READER_API_TOKEN 未设置，开发模式放行（建议监听 127.0.0.1 或显式设置令牌）")


# ---------- 鉴权 ----------
AUTH_HEADER = "X-Access-Token"


def check_token(header_token: Optional[str]) -> bool:
    """校验请求携带的访问令牌。未设置环境变量且为开发模式时放行。"""
    if not READER_API_TOKEN:
        return True
    return header_token is not None and header_token.strip() == READER_API_TOKEN


# ---------- SSRF 防护 ----------
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _is_loopback(host: str) -> bool:
    try:
        host = host.split(":")[0]
        return host in ("127.0.0.1", "localhost", "::1")
    except Exception:
        return False


def _is_private_host(hostname: str) -> bool:
    """解析主机名到 IP，判断是否为保留/私有地址（防御 DNS 重绑定）。"""
    try:
        ips = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # 无法解析的主机名视为不安全
        return True
    for item in ips:
        addr = item[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        for net in _PRIVATE_NETWORKS:
            if ip.version == net.version and ip in net:
                return True
    return False


def validate_base_url(base_url: str) -> Optional[str]:
    """
    校验客户端传入的 base_url，防止 SSRF。
    返回 None 表示合法；返回字符串表示错误原因。
    三层防护：
      1. 白名单（ALLOWED_BASE_URLS）
      2. 私有/保留地址拒绝
      3. 协议限制：仅 https，本地调试可放行 http://localhost:*
    """
    if not base_url:
        return None

    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return "base_url 格式错误，必须包含协议和主机名"

    # 层 3：协议限制
    hostname = parsed.hostname or ""
    port = parsed.port
    is_localhost = hostname.lower() in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme == "http":
        if not (is_localhost and (port is not None or parsed.netloc == "localhost")):
            return "仅允许 https:// 协议（本地调试可显式使用 http://localhost:*）"
    elif parsed.scheme != "https":
        return "仅允许 https:// 协议"

    # 层 1：白名单
    if ALLOWED_BASE_URLS:
        whitelist = {u.strip().rstrip("/") for u in ALLOWED_BASE_URLS.split(",") if u.strip()}
        normalized = base_url.rstrip("/")
        if normalized not in whitelist:
            return "base_url 不在服务端白名单内"
        return None

    # 层 2：内网/保留地址拒绝（白名单未配置时的兜底）
    if _is_private_host(hostname):
        return "base_url 指向内网或保留地址，已被拒绝"

    return None


# ---------- CORS ----------
def get_cors_origins() -> list:
    """从环境变量读取允许的来源；未配置时开发模式默认允许本地前端地址。"""
    if CORS_ORIGINS:
        return [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
    return ["http://localhost:5173", "http://127.0.0.1:5173"]
