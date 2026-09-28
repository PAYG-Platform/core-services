# traffic-service/app/services/isp_matcher.py
import os
import json
import re
import ipaddress
import logging
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)

# Resolves path relative to: traffic-service/app/services/isp_matcher.py -> config/isp_subnets.json
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config",
    "isp_subnets.json"
)


def extract_clean_ip(raw: Any) -> Optional[str]:
    """
    Safely extracts and validates a clean IPv4 or IPv6 string from:
    - dicts: {"ip": "5.112.1.2", ...} or {"address": "5.112.1.2", ...}
    - strings: "5.112.1.2 (2026-09-19 14:00:00)" or "5.112.1.2:54321" or "[2001:db8::1]:54321"
    """
    if not raw:
        return None

    candidate = None
    if isinstance(raw, dict):
        candidate = raw.get("ip") or raw.get("address") or raw.get("clientIp") or raw.get("client_ip")
        if not candidate:
            for k in raw.keys():
                extracted = extract_clean_ip(k)
                if extracted:
                    return extracted
    elif isinstance(raw, str):
        candidate = raw

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
        # Regex fallback in case of stringified JSON residue
        match_v4 = re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', str(raw))
        if match_v4:
            try:
                ip_obj = ipaddress.ip_address(match_v4.group(0))
                return str(ip_obj)
            except ValueError:
                pass
        return None


class ISPMatcher:
    _loaded = False
    _networks: List[Tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, Dict[str, Any]]] = []
    _provider_colors: Dict[str, str] = {}

    @classmethod
    def load(cls):
        """Loads and pre-parses all CIDR subnets and colors from the JSON configuration into memory."""
        if cls._loaded:
            return

        cls._networks = []
        cls._provider_colors = {}

        if not os.path.exists(CONFIG_PATH):
            logger.warning(f"ISP configuration file not found at {CONFIG_PATH}. Using fallback lookup.")
            cls._loaded = True
            return

        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                providers = data.get("providers", [])

                for prov in providers:
                    p_name = prov.get("name", "Other")
                    country = prov.get("country", "Unknown")
                    color = prov.get("color", "#64748b")
                    p_type = prov.get("type", "Broadband")

                    cls._provider_colors[p_name] = color

                    for subnet_str in prov.get("subnets", []):
                        try:
                            net = ipaddress.ip_network(subnet_str.strip(), strict=False)
                            cls._networks.append((net, {
                                "name": p_name,
                                "country": country,
                                "type": p_type,
                                "color": color
                            }))
                        except ValueError as err:
                            logger.error(f"Invalid subnet format '{subnet_str}' for provider '{p_name}': {err}")

            logger.info(f"ISPMatcher successfully compiled {len(cls._networks)} subnets across {len(providers)} providers.")
        except Exception as e:
            logger.error(f"Error parsing ISP subnets JSON configuration: {e}")

        cls._loaded = True

    @classmethod
    def get_provider_color(cls, provider_name: str) -> str:
        """Retrieves the color configured for a given provider directly from the JSON dataset."""
        if not cls._loaded:
            cls.load()
        return cls._provider_colors.get(provider_name, "#64748b")

    @classmethod
    def lookup(cls, ip_str: Optional[str]) -> Dict[str, Any]:
        """
        Matches a given client IP address (IPv4 or IPv6) string against compiled CIDR subnets.
        Returns a dictionary containing provider name, country, type, and chart color.
        """
        fallback_result = {
            "name": "Other / Direct", 
            "country": "INT", 
            "type": "Other", 
            "color": "#64748b"
        }

        clean_ip = extract_clean_ip(ip_str)
        if not clean_ip:
            return fallback_result

        if not cls._loaded:
            cls.load()

        try:
            ip_obj = ipaddress.ip_address(clean_ip)
            for net, info in cls._networks:
                if ip_obj in net:
                    return info
        except ValueError:
            pass

        return fallback_result