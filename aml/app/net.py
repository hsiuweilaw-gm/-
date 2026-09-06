"""來源位址的判定與比對。

獨立成模組，是因為登入流程、稽核軌跡與權限檢查都需要它，
若留在路由模組裡會造成相依循環。
"""
from __future__ import annotations

import ipaddress
from functools import lru_cache

from fastapi import Request

from .config import get_settings


def client_ip(request: Request) -> str | None:
    """判定請求的真實來源位址。

    X-Forwarded-For 的左半段由客戶端自行填寫，取最左邊那一段等於讓對方
    自報來源——稽核軌跡的位址會被偽造，來源位址限流與白名單也一併失效。
    正確作法是從右邊往回數，數幾層由 trusted_proxy_hops 指定；
    設為 0 時完全忽略此標頭，一律以連線對端為準。
    """
    peer = request.client.host if request.client else None
    hops = get_settings().trusted_proxy_hops
    if hops <= 0:
        return peer

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(parts) < hops:
        # 標頭比預期短，代表未經預期的代理鏈，寧可退回連線對端。
        return peer
    return parts[-hops]


@lru_cache(maxsize=8)
def _parse_allowlist(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """把設定字串解析成網段。單一位址視為 /32（或 /128）。

    解析不了的項目直接略過並不記錄——設定寫錯時寧可讓白名單「少一項」
    而被擋下，也不要因為整串解析失敗而變成「不啟用」，那會靜默地
    把限制整個關掉。
    """
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def ip_allowed(ip: str | None, allowlist: str) -> bool:
    """位址是否落在白名單內。白名單為空字串時視為未啟用，一律允許。"""
    networks = _parse_allowlist(allowlist)
    if not networks:
        return True
    if not ip:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in networks)
