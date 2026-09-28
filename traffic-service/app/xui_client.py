# traffic-service/app/xui_client.py
import os
import re
import ipaddress
import logging
import urllib.parse
from typing import Optional, List, Any
import httpx

logger = logging.getLogger(__name__)


def get_httpx_proxy_config() -> dict:
    """
    Checks USE_3XUI_PROXY boolean flag from .env.
    """
    use_proxy_flag = os.getenv("USE_3XUI_PROXY", "false").lower().strip() in ("true", "1", "yes", "on")
    
    if use_proxy_flag:
        proxy_url = (
            os.getenv("XUI_HTTP_PROXY") or 
            os.getenv("HTTPS_PROXY") or 
            os.getenv("HTTP_PROXY") or 
            os.getenv("https_proxy") or 
            os.getenv("http_proxy") or
            "http://host.docker.internal:10808"
        ).strip()
        return {"proxy": proxy_url, "trust_env": True}
    
    return {"proxy": None, "trust_env": False}


class XUIClient:
    TIMEOUT = float(os.getenv("XUI_REQUEST_TIMEOUT", "15.0"))

    @classmethod
    def get_timeout(cls) -> float:
        return cls.TIMEOUT

    @classmethod
    def set_timeout(cls, seconds: float):
        cls.TIMEOUT = max(1.0, float(seconds))

    @staticmethod
    async def get_client_traffic(
        api_url: str, 
        api_token: str, 
        email: str, 
        version: Optional[str] = None
    ) -> Optional[int]:
        base_url = api_url.rstrip("/")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token.strip()}"
        }
        client_kwargs = get_httpx_proxy_config()
        is_v3 = version and (version.lower().startswith("v3") or "3." in version)
        safe_email = urllib.parse.quote(email.strip(), safe='')

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            # 1. 3X-UI v3.x OpenAPI
            if is_v3:
                url = f"{base_url}/panel/api/clients/traffic/{safe_email}"
                try:
                    response = await client.get(url, headers=headers, timeout=XUIClient.get_timeout())
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("success") and result.get("obj"):
                            obj = result["obj"]
                            return (obj.get("up") or 0) + (obj.get("down") or 0)
                        return 0
                except Exception as e:
                    logger.error(f"Error fetching traffic (v3) for {email} from {api_url}: {e}")
                    return None

            # 2. 3X-UI v2.x Legacy
            url = f"{base_url}/panel/api/inbounds/getClientTraffics/{safe_email}"
            try:
                response = await client.get(url, headers=headers, timeout=XUIClient.get_timeout())
                if response.status_code == 200:
                    result = response.json()
                    if result.get("success") and result.get("obj"):
                        obj = result["obj"]
                        return (obj.get("up") or 0) + (obj.get("down") or 0)
                    return 0
                return None
            except Exception as e:
                logger.error(f"Error fetching traffic (v2) for {email} from {api_url}: {e}")
                return None

    @staticmethod
    async def get_client_ips(
        api_url: str,
        api_token: str,
        email: str,
        version: Optional[str] = None
    ) -> List[str]:
        """
        Polls ALL active or recently connected client IP addresses for this config.
        Correctly parses dictionaries ({"ip": "...", ...}), timestamps, and lists,
        and guarantees strict IP address validation.
        """
        base_url = api_url.rstrip("/")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token.strip()}"
        }
        client_kwargs = get_httpx_proxy_config()
        safe_email = urllib.parse.quote(email.strip(), safe='')

        def extract_single_ip(item: Any) -> Optional[str]:
            if not item:
                return None
            candidate = None
            if isinstance(item, dict):
                candidate = item.get("ip") or item.get("address") or item.get("clientIp")
            elif isinstance(item, str):
                candidate = item

            if not candidate or not isinstance(candidate, str):
                return None

            clean = candidate.strip()
            if "(" in clean:
                clean = clean.split("(")[0].strip()
            clean = clean.split()[0].strip()

            if clean.startswith("[") and "]" in clean:
                clean = clean.split("]")[0].lstrip("[")
            elif clean.count(":") == 1:
                clean = clean.split(":")[0].strip()

            try:
                ip_obj = ipaddress.ip_address(clean)
                return str(ip_obj)
            except ValueError:
                match_v4 = re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', str(item))
                if match_v4:
                    try:
                        ip_obj = ipaddress.ip_address(match_v4.group(0))
                        return str(ip_obj)
                    except ValueError:
                        pass
                return None

        def parse_ip_collection(obj: Any) -> List[str]:
            raw_items = []
            if isinstance(obj, list):
                raw_items.extend(obj)
            elif isinstance(obj, dict):
                if "ip" in obj or "address" in obj:
                    raw_items.append(obj)
                else:
                    raw_items.extend(obj.keys())
            elif isinstance(obj, str):
                raw_items.extend(obj.replace(",", "\n").splitlines())

            valid_ips = []
            for entry in raw_items:
                ip = extract_single_ip(entry)
                if ip and ip not in valid_ips:
                    valid_ips.append(ip)
            return valid_ips

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            # 1. 3X-UI v3 OpenAPI endpoint (POST required per OpenAPI spec)
            url_v3 = f"{base_url}/panel/api/clients/ips/{safe_email}"
            try:
                res = await client.post(url_v3, headers=headers, timeout=XUIClient.get_timeout())
                if res.status_code == 200:
                    data = res.json()
                    if data.get("success") and data.get("obj") is not None:
                        ips = parse_ip_collection(data["obj"])
                        if ips:
                            return ips
            except Exception as e:
                logger.debug(f"3x-ui v3 POST /clients/ips check failed for {email}: {e}")

            # 2. Inbounds clientIps fallback endpoint (POST)
            url_inb = f"{base_url}/panel/api/inbounds/clientIps/{safe_email}"
            try:
                res = await client.post(url_inb, headers=headers, timeout=XUIClient.get_timeout())
                if res.status_code == 200:
                    data = res.json()
                    if data.get("success") and data.get("obj") is not None:
                        ips = parse_ip_collection(data["obj"])
                        if ips:
                            return ips
            except Exception as e:
                logger.debug(f"3x-ui inbounds clientIps check failed for {email}: {e}")

            # 3. Aggregated cluster table fallback: GET /panel/api/server/clientIps
            url_server = f"{base_url}/panel/api/server/clientIps"
            try:
                res = await client.get(url_server, headers=headers, timeout=XUIClient.get_timeout())
                if res.status_code == 200:
                    data = res.json()
                    if data.get("success") and isinstance(data.get("obj"), list):
                        for row in data["obj"]:
                            if row.get("clientEmail") == email:
                                ips = parse_ip_collection(row.get("ips"))
                                if ips:
                                    return ips
            except Exception as e:
                logger.debug(f"3x-ui /server/clientIps fallback failed for {email}: {e}")

        return []