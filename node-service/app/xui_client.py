# backend/node-service/app/xui_client.py
import os
import json
import time
import uuid
import re
import ipaddress
import logging
import urllib.parse
import secrets
from typing import Optional, List, Tuple, Dict, Any
import httpx

logger = logging.getLogger(__name__)


class AddClientResult:
    """
    Transparent result wrapper that evaluates as a boolean (for backward compatibility: `if created:`)
    while exposing the final email, uuid, and any error message.
    """
    def __init__(self, success: bool, email: str, uuid: str, error_msg: Optional[str] = None):
        self.success = success
        self.email = email
        self.uuid = uuid
        self.error_msg = error_msg

    def __bool__(self):
        return self.success


def get_httpx_proxy_config() -> dict:
    """
    Checks USE_3XUI_PROXY boolean flag from .env.
    - If 'true', '1', or 'yes': activates proxy routing via XUI_HTTP_PROXY (or HTTP_PROXY).
    - If 'false', '0', or 'no': completely bypasses proxies and connects directly.
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
    
    # Direct connection: disable reading inherited system proxy environment variables
    return {"proxy": None, "trust_env": False}


def is_v3_version(version_str: Optional[str]) -> bool:
    """Checks if the configured server version is v3.x or higher."""
    if not version_str:
        return False
    clean = version_str.lower().strip()
    return clean.startswith("v3") or "3." in clean or "v3.5" in clean


def persist_server_log(
    endpoint: str,
    method: str,
    status: str,
    description: str,
    status_code: Optional[int] = None,
    response_time_ms: Optional[int] = None,
    server_id: Optional[uuid.UUID] = None,
    server_name: Optional[str] = None,
    event_type: str = "http_call",
    details: Optional[dict] = None
):
    try:
        from app.database import SessionLocal
        from app.models import ServerLog
        db = SessionLocal()
        try:
            log_item = ServerLog(
                id=uuid.uuid4(),
                server_id=server_id,
                server_name=server_name or "3x-ui Node",
                endpoint=endpoint,
                method=method.upper(),
                status_code=status_code,
                response_time_ms=response_time_ms,
                status=status,
                event_type=event_type,
                description=description,
                details=details or {}
            )
            db.add(log_item)
            db.commit()
        finally:
            db.close()
    except Exception as err:
        logger.error(f"Failed to record server HTTP log: {err}")


class XUIClient:
    TIMEOUT = float(os.getenv("XUI_REQUEST_TIMEOUT", "15.0"))

    @classmethod
    def get_timeout(cls) -> float:
        return cls.TIMEOUT

    @classmethod
    def set_timeout(cls, seconds: float):
        cls.TIMEOUT = max(1.0, float(seconds))

    @staticmethod
    def _get_headers(api_token: str) -> dict:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token.strip()}"
        }

    @staticmethod
    def _clean_url(api_url: str) -> str:
        return api_url.rstrip("/")

    @staticmethod
    async def check_server_reachable(
        api_url: str, 
        api_token: str, 
        server_id: Optional[uuid.UUID] = None, 
        server_name: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        base_url = XUIClient._clean_url(api_url)
        url = f"{base_url}/panel/api/inbounds/list"
        start_time = time.time()
        client_kwargs = get_httpx_proxy_config()

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            try:
                response = await client.get(
                    url,
                    headers=XUIClient._get_headers(api_token),
                    timeout=XUIClient.get_timeout()
                )
                elapsed_ms = int((time.time() - start_time) * 1000)

                if response.status_code in (200, 201):
                    persist_server_log(
                        endpoint=url,
                        method="GET",
                        status="Success",
                        status_code=response.status_code,
                        response_time_ms=elapsed_ms,
                        server_id=server_id,
                        server_name=server_name,
                        event_type="health_check",
                        description=f"Health probe passed for {server_name or base_url}",
                        details={"status_code": response.status_code, "proxy_used": client_kwargs.get("proxy")}
                    )
                    return True, None
                else:
                    err_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                    persist_server_log(
                        endpoint=url,
                        method="GET",
                        status="Error",
                        status_code=response.status_code,
                        response_time_ms=elapsed_ms,
                        server_id=server_id,
                        server_name=server_name,
                        event_type="health_check",
                        description=f"Health probe failed ({response.status_code}) on {server_name or base_url}",
                        details={"response": response.text[:300], "proxy_used": client_kwargs.get("proxy")}
                    )
                    return False, err_msg
            except Exception as e:
                elapsed_ms = int((time.time() - start_time) * 1000)
                persist_server_log(
                    endpoint=url,
                    method="GET",
                    status="Error",
                    status_code=None,
                    response_time_ms=elapsed_ms,
                    server_id=server_id,
                    server_name=server_name,
                    event_type="health_check",
                    description=f"Health probe connection error on {server_name or base_url}: {str(e)}",
                    details={"error": str(e), "proxy_used": client_kwargs.get("proxy")}
                )
                return False, str(e)

    @staticmethod
    async def fetch_inbounds_list(
        api_url: str, 
        api_token: str, 
        server_id: Optional[uuid.UUID] = None, 
        server_name: Optional[str] = None
    ) -> List[dict]:
        base_url = XUIClient._clean_url(api_url)
        url = f"{base_url}/panel/api/inbounds/list"
        start_time = time.time()
        client_kwargs = get_httpx_proxy_config()

        def extract_header_host(headers_obj: Any) -> str:
            if isinstance(headers_obj, dict):
                for k, v in headers_obj.items():
                    if k.lower() == "host" and isinstance(v, str):
                        return v.strip()
            elif isinstance(headers_obj, str):
                try:
                    parsed = json.loads(headers_obj)
                    if isinstance(parsed, dict):
                        return extract_header_host(parsed)
                except Exception:
                    pass
            return ""

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            try:
                response = await client.get(
                    url,
                    headers=XUIClient._get_headers(api_token),
                    timeout=XUIClient.TIMEOUT
                )
                elapsed_ms = int((time.time() - start_time) * 1000)

                if response.status_code != 200:
                    persist_server_log(
                        endpoint=url, method="GET", status="Error", status_code=response.status_code,
                        response_time_ms=elapsed_ms, server_id=server_id, server_name=server_name,
                        event_type="detect_inbounds", description=f"Failed to fetch inbounds ({response.status_code})"
                    )
                    return []

                result = response.json()
                raw_inbounds = result.get("obj") or []

                parsed_inbounds = []
                for inb in raw_inbounds:
                    if not inb.get("enable", True):
                        continue
                    inb_id = inb.get("id")
                    port = inb.get("port")
                    protocol = (inb.get("protocol") or "vless").lower()

                    stream_settings = inb.get("streamSettings") or {}
                    if isinstance(stream_settings, str):
                        try:
                            stream_settings = json.loads(stream_settings)
                        except Exception:
                            stream_settings = {}

                    network = str(stream_settings.get("network", "tcp")).lower()
                    security = str(stream_settings.get("security", "none")).lower()

                    # Parse nested transport settings objects safely
                    xhttp_settings = stream_settings.get("xhttpSettings") or stream_settings.get("splithttpSettings") or {}
                    if isinstance(xhttp_settings, str):
                        try: xhttp_settings = json.loads(xhttp_settings)
                        except: xhttp_settings = {}

                    ws_settings = stream_settings.get("wsSettings") or {}
                    if isinstance(ws_settings, str):
                        try: ws_settings = json.loads(ws_settings)
                        except: ws_settings = {}

                    grpc_settings = stream_settings.get("grpcSettings") or {}
                    if isinstance(grpc_settings, str):
                        try: grpc_settings = json.loads(grpc_settings)
                        except: grpc_settings = {}

                    http_settings = stream_settings.get("httpSettings") or stream_settings.get("httpupgradeSettings") or {}
                    if isinstance(http_settings, str):
                        try: http_settings = json.loads(http_settings)
                        except: http_settings = {}

                    tls_settings = stream_settings.get("tlsSettings") or {}
                    if isinstance(tls_settings, str):
                        try: tls_settings = json.loads(tls_settings)
                        except: tls_settings = {}

                    reality_settings = stream_settings.get("realitySettings") or {}
                    if isinstance(reality_settings, str):
                        try: reality_settings = json.loads(reality_settings)
                        except: reality_settings = {}

                    # 1. Determine Protocol Identifier
                    proto_id = protocol
                    if security == "reality":
                        proto_id = "vless_reality"
                    elif network in ["xhttp", "splithttp"]:
                        proto_id = "xhttp"
                    elif network == "ws":
                        proto_id = "vless_ws" if protocol == "vless" else "vmess" if protocol == "vmess" else "ws"
                    elif protocol == "trojan":
                        proto_id = "trojan"
                    elif protocol == "vmess":
                        proto_id = "vmess"

                    # 2. Detect Path
                    detected_path = "/"
                    if network in ["xhttp", "splithttp"]:
                        detected_path = xhttp_settings.get("path") or "/"
                    elif network == "ws":
                        detected_path = ws_settings.get("path") or "/"
                    elif network == "grpc":
                        detected_path = grpc_settings.get("serviceName") or "/"
                    elif network in ["http", "httpupgrade"]:
                        detected_path = http_settings.get("path") or "/"

                    # 3. Detect Host / SNI
                    detected_host = ""
                    if network in ["xhttp", "splithttp"]:
                        detected_host = (
                            xhttp_settings.get("host") or 
                            extract_header_host(xhttp_settings.get("headers")) or 
                            ""
                        )
                    elif network == "ws":
                        detected_host = (
                            ws_settings.get("host") or 
                            extract_header_host(ws_settings.get("headers")) or 
                            ""
                        )
                    elif network == "grpc":
                        detected_host = grpc_settings.get("authority") or ""
                    elif network in ["http", "httpupgrade"]:
                        detected_host = (
                            http_settings.get("host") or 
                            extract_header_host(http_settings.get("headers")) or 
                            ""
                        )

                    # Fallback to TLS serverName or Reality SNI if host header was empty
                    if not detected_host:
                        if security == "tls":
                            detected_host = tls_settings.get("serverName") or ""
                        elif security == "reality":
                            server_names = reality_settings.get("serverNames") or []
                            if isinstance(server_names, list) and len(server_names) > 0:
                                detected_host = str(server_names[0]).strip()
                            elif isinstance(server_names, str) and server_names.strip():
                                detected_host = server_names.split(",")[0].strip()
                            
                            if not detected_host and reality_settings.get("dest"):
                                dest = str(reality_settings.get("dest")).strip()
                                detected_host = dest.split(":")[0].strip()

                    tag = inb.get("remark") or inb.get("tag") or f"{proto_id.upper()} Port {port}"

                    parsed_inbounds.append({
                        "inbound_id": inb_id,
                        "tag": tag,
                        "protocol": proto_id,
                        "port": port,
                        "domain_or_ip": detected_host.strip(),
                        "path": detected_path.strip(),
                        "user_limit": 10,
                        "used_limit": 0
                    })

                persist_server_log(
                    endpoint=url, method="GET", status="Success", status_code=200,
                    response_time_ms=elapsed_ms, server_id=server_id, server_name=server_name,
                    event_type="detect_inbounds", 
                    description=f"Discovered {len(parsed_inbounds)} active inbounds (with Hosts & Paths) on {server_name or base_url}",
                    details={"inbounds_found": len(parsed_inbounds)}
                )
                return parsed_inbounds
            except Exception as e:
                elapsed_ms = int((time.time() - start_time) * 1000)
                persist_server_log(
                    endpoint=url, method="GET", status="Error", status_code=None,
                    response_time_ms=elapsed_ms, server_id=server_id, server_name=server_name,
                    event_type="detect_inbounds", description=f"Error inspecting inbounds: {str(e)}",
                    details={"error": str(e)}
                )
                return []

    @staticmethod
    async def add_client(
        api_url: str, 
        api_token: str, 
        inbound_id: int, 
        uuid: str, 
        email: str,
        protocol: Optional[str] = None,
        enable: bool = True,
        version: Optional[str] = None,
        sub_id: Optional[str] = None,
        server_id: Optional[uuid.UUID] = None,
        server_name: Optional[str] = None
    ) -> AddClientResult:
        base_url = XUIClient._clean_url(api_url)
        headers = XUIClient._get_headers(api_token)
        start_time = time.time()
        client_kwargs = get_httpx_proxy_config()

        proto_lower = (protocol or "").lower().strip()
        is_reality = "reality" in proto_lower or proto_lower in ["vless_reality", "reality"]
        flow_value = "xtls-rprx-vision" if is_reality else ""

        current_email = email
        current_uuid = uuid
        current_sub_id = sub_id or current_uuid.replace("-", "")[:16]

        max_attempts = 3
        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            for attempt in range(max_attempts):
                if is_v3_version(version):
                    # 1. 3X-UI v3.x OpenAPI Path
                    url = f"{base_url}/panel/api/clients/add"
                    payload = {
                        "client": {
                            "id": current_uuid,
                            "email": current_email,
                            "subId": current_sub_id,
                            "totalGB": 0,
                            "expiryTime": 0,
                            "enable": bool(enable),
                            "limitIp": 0,
                            "flow": flow_value
                        },
                        "inboundIds": [inbound_id]
                    }
                else:
                    # 2. 3X-UI v2.x Legacy Path
                    url = f"{base_url}/panel/api/inbounds/addClient"
                    payload = {
                        "id": inbound_id,
                        "settings": json.dumps({
                            "clients": [{
                                "id": current_uuid,
                                "email": current_email,
                                "subId": current_sub_id,
                                "enable": bool(enable),
                                "limitIp": 0,
                                "totalGB": 0,
                                "expiryTime": 0,
                                "flow": flow_value
                            }]
                        })
                    }

                try:
                    response = await client.post(url, headers=headers, json=payload, timeout=XUIClient.TIMEOUT)
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    result = response.json() if response.status_code == 200 else {}
                    success = response.status_code == 200 and result.get("success", False)

                    # Intercept duplicate email error from 3X-UI and auto-retry with unique nonce
                    error_str = f"{result.get('msg', '')} {response.text}".lower()
                    is_duplicate_email = any(phrase in error_str for phrase in [
                        "duplicate email", "already exist", "already in use", "duplicate"
                    ])

                    if not success and is_duplicate_email and attempt < max_attempts - 1:
                        nonce = secrets.token_hex(2)
                        current_uuid = str(uuid.uuid4())
                        current_sub_id = current_uuid.replace("-", "")[:16]
                        current_email = f"{email}_{nonce}"
                        logger.warning(
                            f"[3X-UI] Duplicate email detected on inbound #{inbound_id} for '{email}'. "
                            f"Auto-resolving collision: Retrying with unique email '{current_email}' (attempt {attempt + 2}/{max_attempts})..."
                        )
                        continue

                    persist_server_log(
                        endpoint=url, 
                        method="POST", 
                        status="Success" if success else "Error",
                        status_code=response.status_code, 
                        response_time_ms=elapsed_ms, 
                        server_id=server_id,
                        server_name=server_name, 
                        event_type="add_client",
                        description=f"Provisioned client {current_email} on 3x-ui (enable={enable}, flow={flow_value})",
                        details={"inbound_id": inbound_id, "email": current_email, "enable": enable, "flow": flow_value, "response": result}
                    )

                    if success and not enable:
                        await XUIClient.update_client_status(
                            api_url=api_url,
                            api_token=api_token,
                            inbound_id=inbound_id,
                            uuid=current_uuid,
                            email=current_email,
                            enable=False,
                            version=version,
                            server_id=server_id,
                            server_name=server_name
                        )

                    return AddClientResult(
                        success=success,
                        email=current_email,
                        uuid=current_uuid,
                        error_msg=result.get("msg")
                    )

                except Exception as e:
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    persist_server_log(
                        endpoint=url, method="POST", status="Error", status_code=None,
                        response_time_ms=elapsed_ms, server_id=server_id, server_name=server_name,
                        event_type="add_client", description=f"Failed to add client {current_email}: {str(e)}"
                    )
                    return AddClientResult(
                        success=False,
                        email=current_email,
                        uuid=current_uuid,
                        error_msg=str(e)
                    )

        return AddClientResult(
            success=False,
            email=current_email,
            uuid=current_uuid,
            error_msg="Max retry attempts exceeded."
        )

    @staticmethod
    async def get_client_uri(
        api_url: str, 
        api_token: str, 
        inbound_id: int, 
        email: str, 
        client_uuid: str,
        sub_id: Optional[str] = None,
        version: Optional[str] = None
    ) -> Optional[str]:
        base_url = XUIClient._clean_url(api_url)
        headers = XUIClient._get_headers(api_token)
        client_kwargs = get_httpx_proxy_config()

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            try:
                safe_email = urllib.parse.quote(email, safe='')
                links_url = f"{base_url}/panel/api/clients/links/{safe_email}"
                resp = await client.get(links_url, headers=headers, timeout=XUIClient.get_timeout())
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success") and data.get("obj"):
                        obj = data["obj"]
                        if isinstance(obj, list) and len(obj) > 0:
                            return obj[0].strip()
                        elif isinstance(obj, str) and obj.strip():
                            return obj.strip()
            except Exception as e:
                logger.warning(f"Failed to fetch client link from 3x-ui for {email}: {e}")

            if sub_id:
                try:
                    sub_url = f"{base_url}/panel/api/clients/subLinks/{sub_id}"
                    resp = await client.get(sub_url, headers=headers, timeout=5.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("success") and data.get("obj"):
                            obj = data["obj"]
                            if isinstance(obj, list) and len(obj) > 0:
                                return obj[0].strip()
                except Exception:
                    pass

        return None

    @staticmethod
    async def remove_client(
        api_url: str, 
        api_token: str, 
        inbound_id: int, 
        uuid: str,
        email: Optional[str] = None,
        version: Optional[str] = None,
        server_id: Optional[uuid.UUID] = None,
        server_name: Optional[str] = None
    ) -> bool:
        base_url = XUIClient._clean_url(api_url)
        headers_json = XUIClient._get_headers(api_token)
        headers_no_body = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token.strip()}"
        }
        start_time = time.time()
        client_kwargs = get_httpx_proxy_config()

        clean_uuid = (uuid or "").strip()
        clean_email = (email or "").strip()
        safe_email = urllib.parse.quote(clean_email, safe='') if clean_email else ""

        endpoints = []

        # 1. 3X-UI v3 OpenAPI global client deletion endpoints
        if safe_email:
            endpoints.append((f"{base_url}/panel/api/clients/del/{safe_email}?keepTraffic=0", "POST"))
            endpoints.append((f"{base_url}/panel/api/clients/del/{safe_email}", "POST"))
            endpoints.append((f"{base_url}/panel/api/clients/delete/{safe_email}", "POST"))

        # 2. 3X-UI v2 / MHSanaei inbound-specific deletion
        if inbound_id:
            if clean_uuid:
                endpoints.append((f"{base_url}/panel/api/inbounds/{inbound_id}/delClient/{clean_uuid}", "POST"))
                endpoints.append((f"{base_url}/panel/api/inbounds/delClient/{clean_uuid}", "POST"))
            if safe_email:
                endpoints.append((f"{base_url}/panel/api/inbounds/{inbound_id}/delClientByEmail/{safe_email}", "POST"))
                endpoints.append((f"{base_url}/panel/api/inbounds/{inbound_id}/delClient/{safe_email}", "POST"))
                endpoints.append((f"{base_url}/panel/api/inbounds/delClientByEmail/{safe_email}", "POST"))

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            # Try each endpoint with both empty JSON and standard headers
            for url, method in endpoints:
                for req_headers, req_json in [(headers_json, {}), (headers_no_body, None)]:
                    try:
                        if req_json is not None:
                            response = await client.post(url, headers=req_headers, json=req_json, timeout=XUIClient.TIMEOUT)
                        else:
                            response = await client.post(url, headers=req_headers, timeout=XUIClient.TIMEOUT)

                        if response.status_code == 200:
                            data = response.json()
                            if data.get("success") is True:
                                elapsed_ms = int((time.time() - start_time) * 1000)
                                persist_server_log(
                                    endpoint=url, method="POST", status="Success",
                                    status_code=200, response_time_ms=elapsed_ms, server_id=server_id,
                                    server_name=server_name, event_type="remove_client",
                                    description=f"Removed client {clean_email or clean_uuid} on {server_name or base_url}"
                                )
                                return True
                    except Exception:
                        pass

            # 3. Fallback: Search all inbounds via /panel/api/inbounds/list to find true inbound ID
            try:
                list_resp = await client.get(f"{base_url}/panel/api/inbounds/list", headers=headers_no_body, timeout=XUIClient.get_timeout())
                if list_resp.status_code == 200:
                    inbounds = list_resp.json().get("obj") or []
                    for inb in inbounds:
                        inb_id = inb.get("id")
                        raw_settings = inb.get("settings") or {}
                        if isinstance(raw_settings, str):
                            try:
                                raw_settings = json.loads(raw_settings)
                            except Exception:
                                raw_settings = {}
                        clients = raw_settings.get("clients") or []
                        matched = False
                        filtered_clients = []
                        for c in clients:
                            cid = str(c.get("id", "")).strip().lower()
                            cemail = str(c.get("email", "")).strip().lower()
                            if (clean_uuid and cid == clean_uuid.lower()) or (clean_email and cemail == clean_email.lower()):
                                matched = True
                            else:
                                filtered_clients.append(c)

                        if matched:
                            # Try deleting using this discovered inbound ID
                            if clean_uuid:
                                del_url = f"{base_url}/panel/api/inbounds/{inb_id}/delClient/{clean_uuid}"
                                await client.post(del_url, headers=headers_no_body, timeout=XUIClient.get_timeout())
                            if safe_email:
                                del_url = f"{base_url}/panel/api/inbounds/{inb_id}/delClientByEmail/{safe_email}"
                                await client.post(del_url, headers=headers_no_body, timeout=XUIClient.get_timeout())

                            # Direct fallback: rewrite settings without the client
                            raw_settings["clients"] = filtered_clients
                            update_payload = {
                                "id": inb_id,
                                "settings": json.dumps(raw_settings)
                            }
                            up_resp = await client.post(
                                f"{base_url}/panel/api/inbounds/update/{inb_id}",
                                headers=headers_json,
                                json=update_payload,
                                timeout=XUIClient.TIMEOUT
                            )
                            if up_resp.status_code == 200 and up_resp.json().get("success") is True:
                                return True
            except Exception as e:
                logger.debug(f"Direct inbound settings update fallback error: {e}")

        return False

    @staticmethod
    async def purge_user_old_configs(
        api_url: str,
        api_token: str,
        email_prefix: str,
        active_client_uuid: str,
        active_client_email: str,
        known_old_uuids: Optional[List[str]] = None,
        known_old_emails: Optional[List[str]] = None,
        server_id: Optional[uuid.UUID] = None,
        server_name: Optional[str] = None
    ) -> int:
        """
        Inspects all inbounds on this 3X-UI server and purges any client belonging to this user
        except the active client (active_client_uuid and active_client_email).
        Guarantees only the last config is kept for this user in this region.
        """
        base_url = XUIClient._clean_url(api_url)
        headers_no_body = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token.strip()}"
        }
        client_kwargs = get_httpx_proxy_config()
        removed_count = 0

        active_uuid_clean = (active_client_uuid or "").strip().lower()
        active_email_clean = (active_client_email or "").strip().lower()
        prefix_clean = (email_prefix or "").strip().lower()

        old_uuids_set = {u.strip().lower() for u in (known_old_uuids or []) if u}
        old_emails_set = {e.strip().lower() for e in (known_old_emails or []) if e}

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            try:
                res = await client.get(f"{base_url}/panel/api/inbounds/list", headers=headers_no_body, timeout=XUIClient.TIMEOUT)
                if res.status_code != 200:
                    return 0
                inbounds = res.json().get("obj") or []

                for inb in inbounds:
                    inb_id = inb.get("id")
                    raw_settings = inb.get("settings") or {}
                    if isinstance(raw_settings, str):
                        try:
                            raw_settings = json.loads(raw_settings)
                        except Exception:
                            raw_settings = {}

                    clients_list = raw_settings.get("clients") or []
                    for c in clients_list:
                        c_id = str(c.get("id", "")).strip()
                        c_email = str(c.get("email", "")).strip()
                        c_id_lower = c_id.lower()
                        c_email_lower = c_email.lower()

                        # Protect the active config!
                        if c_id_lower == active_uuid_clean or c_email_lower == active_email_clean:
                            continue

                        # Only delete configs that are explicitly in the replaced/blocked history
                        is_target = False
                        if c_id_lower in old_uuids_set or c_email_lower in old_emails_set:
                            is_target = True

                        if is_target:
                            ok = await XUIClient.remove_client(
                                api_url=api_url,
                                api_token=api_token,
                                inbound_id=inb_id,
                                uuid=c_id,
                                email=c_email,
                                server_id=server_id,
                                server_name=server_name
                            )
                            if ok:
                                removed_count += 1
                                logger.info(f"Purged old client {c_email} ({c_id}) from inbound #{inb_id} on {server_name or base_url}")

            except Exception as e:
                logger.error(f"Error purging old user configs on {base_url}: {e}")

        return removed_count

    @staticmethod
    async def sweep_and_remove_all_payg_clients(
        api_url: str,
        api_token: str,
        known_client_emails: Optional[List[str]] = None,
        known_client_uuids: Optional[List[str]] = None,
        server_id: Optional[uuid.UUID] = None,
        server_name: Optional[str] = None,
        sweep_all_payg: bool = False
    ) -> int:
        """
        Inspects all inbounds on the 3X-UI panel and removes all PAYG client configs,
        including any orphaned or replaced client credentials.
        """
        base_url = XUIClient._clean_url(api_url)
        headers = XUIClient._get_headers(api_token)
        client_kwargs = get_httpx_proxy_config()
        removed_count = 0

        known_emails_lower = {e.lower().strip() for e in (known_client_emails or []) if e}
        known_uuids_lower = {u.lower().strip() for u in (known_client_uuids or []) if u}

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            try:
                res = await client.get(f"{base_url}/panel/api/inbounds/list", headers=headers, timeout=XUIClient.TIMEOUT)
                if res.status_code != 200:
                    return 0
                inbounds = res.json().get("obj") or []

                for inb in inbounds:
                    inb_id = inb.get("id")
                    clients_to_remove = []

                    # 1. Inspect clientStats
                    for s in (inb.get("clientStats") or []):
                        c_email = (s.get("email") or "").strip()
                        c_email_lower = c_email.lower()
                        is_match = (
                            c_email_lower in known_emails_lower
                            or "-payg" in c_email_lower
                            or "_payg" in c_email_lower
                            or c_email_lower.startswith("usr_")
                            or "@payg" in c_email_lower
                        )
                        if is_match:
                            clients_to_remove.append((inb_id, "", c_email))

                    # 2. Inspect settings.clients
                    raw_settings = inb.get("settings") or {}
                    if isinstance(raw_settings, str):
                        try:
                            raw_settings = json.loads(raw_settings)
                        except Exception:
                            raw_settings = {}
                    for c in (raw_settings.get("clients") or []):
                        c_id = (c.get("id") or "").strip()
                        c_email = (c.get("email") or "").strip()
                        c_email_lower = c_email.lower()
                        c_id_lower = c_id.lower()

                        is_match = False
                        if c_email_lower in known_emails_lower or c_id_lower in known_uuids_lower:
                            is_match = True
                        elif sweep_all_payg:
                            if (
                                "-payg" in c_email_lower
                                or "_payg" in c_email_lower
                                or c_email_lower.startswith("usr_")
                                or "@payg" in c_email_lower
                            ):
                                is_match = True
                                
                        if is_match:
                            clients_to_remove.append((inb_id, c_id, c_email))

                    # Remove all matched clients on 3X-UI
                    for inb_id_val, c_id_val, c_email_val in clients_to_remove:
                        try:
                            ok = await XUIClient.remove_client(
                                api_url, api_token, inb_id_val, c_id_val, email=c_email_val,
                                server_id=server_id, server_name=server_name
                            )
                            if ok:
                                removed_count += 1
                        except Exception:
                            pass

            except Exception as e:
                logger.error(f"Error sweeping 3X-UI panel {api_url}: {e}")

        return removed_count


    @staticmethod
    async def update_client_status(
        api_url: str, 
        api_token: str, 
        inbound_id: int, 
        uuid: str, 
        email: str, 
        enable: bool,
        version: Optional[str] = None,
        server_id: Optional[uuid.UUID] = None,
        server_name: Optional[str] = None
    ) -> bool:
        base_url = XUIClient._clean_url(api_url)
        headers = XUIClient._get_headers(api_token)
        start_time = time.time()
        client_kwargs = get_httpx_proxy_config()

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            if is_v3_version(version):
                action = "bulkEnable" if enable else "bulkDisable"
                url = f"{base_url}/panel/api/clients/{action}"
                payload = {"emails": [email]}
                try:
                    response = await client.post(url, headers=headers, json=payload, timeout=XUIClient.TIMEOUT)
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    success = response.status_code == 200
                    persist_server_log(
                        endpoint=url, method="POST", status="Success" if success else "Error",
                        status_code=response.status_code, response_time_ms=elapsed_ms, server_id=server_id,
                        server_name=server_name, event_type=f"client_{action}",
                        description=f"Set client {email} enable={enable} on 3x-ui v3"
                    )
                    return success
                except Exception:
                    pass

            url = f"{base_url}/panel/api/inbounds/updateClient/{uuid}"
            payload = {
                "id": inbound_id,
                "settings": json.dumps({"clients": [{"id": uuid, "email": email, "enable": enable}]})
            }
            try:
                response = await client.post(url, headers=headers, json=payload, timeout=XUIClient.TIMEOUT)
                elapsed_ms = int((time.time() - start_time) * 1000)
                success = response.status_code == 200
                persist_server_log(
                    endpoint=url, method="POST", status="Success" if success else "Error",
                    status_code=response.status_code, response_time_ms=elapsed_ms, server_id=server_id,
                    server_name=server_name, event_type="update_client_v2",
                    description=f"Set client {email} enable={enable} on 3x-ui v2"
                )
                return success
            except Exception as e:
                elapsed_ms = int((time.time() - start_time) * 1000)
                persist_server_log(
                    endpoint=url, method="POST", status="Error", status_code=None,
                    response_time_ms=elapsed_ms, server_id=server_id, server_name=server_name,
                    event_type="update_client_v2", description=f"Failed updating client {email}: {str(e)}"
                )
                return False

    @staticmethod
    async def get_client_traffic(
        api_url: str, 
        api_token: str, 
        email: str,
        version: Optional[str] = None,
        server_id: Optional[uuid.UUID] = None,
        server_name: Optional[str] = None
    ) -> Optional[int]:
        base_url = XUIClient._clean_url(api_url)
        headers = XUIClient._get_headers(api_token)
        start_time = time.time()
        client_kwargs = get_httpx_proxy_config()
        safe_email = urllib.parse.quote(email.strip(), safe='')

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            # 1. Try 3X-UI v3.x OpenAPI: GET /panel/api/clients/traffic/{email}
            url_v3 = f"{base_url}/panel/api/clients/traffic/{safe_email}"
            try:
                response = await client.get(url_v3, headers=headers, timeout=XUIClient.TIMEOUT)
                elapsed_ms = int((time.time() - start_time) * 1000)
                if response.status_code == 200:
                    result = response.json()
                    if result.get("success") and result.get("obj"):
                        obj = result["obj"]
                        total_bytes = (obj.get("up") or 0) + (obj.get("down") or 0)
                        persist_server_log(
                            endpoint=url_v3, method="GET", status="Success", status_code=200,
                            response_time_ms=elapsed_ms, server_id=server_id, server_name=server_name,
                            event_type="sync_traffic_v3", description=f"Polled traffic for {email}: {total_bytes} bytes"
                        )
                        return total_bytes
                    return 0
            except Exception as e:
                logger.debug(f"v3 traffic check failed for {email}, trying v2: {e}")

            # 2. Fallback to 3X-UI v2.x Legacy: GET /panel/api/inbounds/getClientTraffics/{email}
            url_v2 = f"{base_url}/panel/api/inbounds/getClientTraffics/{safe_email}"
            try:
                response = await client.get(url_v2, headers=headers, timeout=XUIClient.TIMEOUT)
                elapsed_ms = int((time.time() - start_time) * 1000)
                if response.status_code == 200:
                    result = response.json()
                    if result.get("success") and result.get("obj"):
                        obj = result["obj"]
                        return (obj.get("up") or 0) + (obj.get("down") or 0)
                    return 0
                return None
            except Exception as e:
                logger.error(f"Error polling traffic for {email}: {e}")
                return None

    @staticmethod
    async def get_all_clients_traffic(
        api_url: str,
        api_token: str,
        server_id: Optional[uuid.UUID] = None,
        server_name: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Dict[str, int], Dict[str, bool], int, Optional[int]]:
        """
        Fetches all client traffic and live enable states in 1 single HTTP request.
        Returns: (is_reachable, error_message, {email: total_bytes}, {email: is_enabled}, elapsed_ms, status_code)
        """
        base_url = XUIClient._clean_url(api_url)
        url = f"{base_url}/panel/api/inbounds/list"
        start_time = time.time()
        client_kwargs = get_httpx_proxy_config()

        async with httpx.AsyncClient(verify=False, **client_kwargs) as client:
            try:
                response = await client.get(url, headers=XUIClient._get_headers(api_token), timeout=XUIClient.TIMEOUT)
                elapsed_ms = int((time.time() - start_time) * 1000)

                if response.status_code not in (200, 201):
                    err_msg = f"HTTP {response.status_code}"
                    persist_server_log(
                        endpoint=url,
                        method="GET",
                        status="Error",
                        status_code=response.status_code,
                        response_time_ms=elapsed_ms,
                        server_id=server_id,
                        server_name=server_name,
                        event_type="sync_traffic",
                        description=f"Batch poll failed with HTTP {response.status_code} on {server_name or base_url}",
                        details={"status_code": response.status_code, "response": response.text[:300]}
                    )
                    return False, err_msg, {}, {}, elapsed_ms, response.status_code

                result = response.json()
                inbounds = result.get("obj") or []
                traffic_by_email: Dict[str, int] = {}
                enabled_by_email: Dict[str, bool] = {}

                for inb in inbounds:
                    stats = inb.get("clientStats") or []
                    for s in stats:
                        email = s.get("email")
                        if email:
                            total = (s.get("up") or 0) + (s.get("down") or 0)
                            traffic_by_email[email] = total
                            enabled_by_email[email] = bool(s.get("enable", True))

                    # Parse inbound settings.clients to discover all clients even if stats are 0
                    raw_settings = inb.get("settings") or {}
                    if isinstance(raw_settings, str):
                        try:
                            raw_settings = json.loads(raw_settings)
                        except Exception:
                            raw_settings = {}
                    clients_list = raw_settings.get("clients") or []
                    for c in clients_list:
                        email = c.get("email")
                        if email:
                            if email not in enabled_by_email:
                                enabled_by_email[email] = bool(c.get("enable", True))
                            if email not in traffic_by_email:
                                traffic_by_email[email] = 0

                return True, None, traffic_by_email, enabled_by_email, elapsed_ms, response.status_code

            except Exception as e:
                elapsed_ms = int((time.time() - start_time) * 1000)
                persist_server_log(
                    endpoint=url,
                    method="GET",
                    status="Error",
                    status_code=None,
                    response_time_ms=elapsed_ms,
                    server_id=server_id,
                    server_name=server_name,
                    event_type="sync_traffic",
                    description=f"Batch poll connection error on {server_name or base_url}: {str(e)}",
                    details={"error": str(e)}
                )
                return False, str(e), {}, {}, elapsed_ms, None

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
        base_url = XUIClient._clean_url(api_url)
        headers = XUIClient._get_headers(api_token)
        client_kwargs = get_httpx_proxy_config()
        safe_email = urllib.parse.quote(email.strip(), safe='')

        def extract_single_ip(item: Any) -> Optional[str]:
            if not item:
                return None
            candidate = None
            if isinstance(item, dict):
                candidate = item.get("ip") or item.get("address") or item.get("clientIp") or item.get("client_ip")
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
            # 1. Official OpenAPI: POST /panel/api/clients/ips/{email}
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
                logger.debug(f"3x-ui v3 POST /clients/ips error for {email}: {e}")

            # 2. Inbounds fallback: POST /panel/api/inbounds/clientIps/{email}
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
                logger.debug(f"3x-ui inbounds clientIps error for {email}: {e}")

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
                logger.debug(f"3x-ui /server/clientIps error for {email}: {e}")

        return []