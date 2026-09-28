# backend/traffic-service/app/api/traffic.py
import uuid
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import DailyTrafficUsage, UserIspTrafficUsage
from app.services.isp_matcher import ISPMatcher
from vpn_shared.jwt_auth import get_current_user_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/traffic", tags=["Network Telemetry & Metering"])


@router.post("/internal/policy-sync")
def sync_traffic_policy_internal(
    xui_request_timeout: float = Query(15.0),
    db: Session = Depends(get_db)
):
    """Internal HTTP sync endpoint for 3x-ui timeout."""
    from app.models import TrafficSettings
    from app.xui_client import XUIClient

    timeout_val = max(1.0, float(xui_request_timeout))
    XUIClient.set_timeout(timeout_val)

    row = db.query(TrafficSettings).filter(TrafficSettings.key == "xui_request_timeout").first()
    if not row:
        db.add(TrafficSettings(key="xui_request_timeout", value=str(timeout_val)))
    else:
        row.value = str(timeout_val)
    db.commit()

    return {"status": "success", "xui_request_timeout": timeout_val}


NUMBER_OF_PAST_DAYS = 30

# =========================================================================
# CLIENT TELEMETRY ENDPOINTS
# =========================================================================

@router.get("/client/provider-stats")
def get_client_provider_traffic_stats(
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    try:
        user_id = uuid.UUID(str(user_payload["sub"]))
    except (KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user identification token."
        )

    records = (
        db.query(UserIspTrafficUsage)
        .filter(UserIspTrafficUsage.user_id == user_id)
        .order_by(UserIspTrafficUsage.total_bytes.desc())
        .all()
    )

    total_bytes = sum(r.total_bytes for r in records)

    if total_bytes == 0:
        return {
            "total_bytes": 0,
            "providers": []
        }

    providers_list = []
    for r in records:
        percentage = round((r.total_bytes / total_bytes) * 100, 1)
        if percentage <= 0:
            continue

        color = ISPMatcher.get_provider_color(r.isp_name)

        providers_list.append({
            "name": r.isp_name,
            "country": r.country,
            "ip_address": getattr(r, "ip_address", None) or getattr(r, "last_ip", None) or "Unknown",
            "last_ip": getattr(r, "last_ip", None) or getattr(r, "ip_address", None) or "Unknown",
            "bytes": r.total_bytes,
            "percentage": percentage,
            "color": color
        })

    return {
        "total_bytes": total_bytes,
        "providers": providers_list
    }


@router.get("/client/daily")
def get_client_daily_traffic(
    days: int = Query(10, ge=1, le=90, description="Number of past days to query"),
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    try:
        user_id = uuid.UUID(str(user_payload["sub"]))
    except (KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user identification token."
        )

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days - 1)

    records = (
        db.query(DailyTrafficUsage)
        .filter(
            DailyTrafficUsage.user_id == user_id,
            DailyTrafficUsage.date >= start_date
        )
        .all()
    )

    usage_map: Dict[str, int] = {}
    for r in records:
        d_str = r.date.isoformat() if hasattr(r.date, "isoformat") else str(r.date).split()[0]
        usage_map[d_str] = usage_map.get(d_str, 0) + (r.total_bytes or 0)

    results = []
    for i in range(days):
        current_day = start_date + timedelta(days=i)
        d_str = current_day.isoformat()
        b_count = usage_map.get(d_str, 0)
        results.append({
            "date": current_day.strftime("%b %d"),
            "date_iso": d_str,
            "bytes": b_count,
            "mb": round(b_count / (1024 ** 2), 2),
            "gb": round(b_count / (1024 ** 3), 4)
        })

    return results


# =========================================================================
# ADMIN TELEMETRY & AGGREGATION ENDPOINTS
# =========================================================================

@router.get("/admin/users/{target_user_id}/provider-stats")
def get_admin_user_provider_traffic_stats(
    target_user_id: uuid.UUID,
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    """
    Admin-only: Returns ISP provider percentage share, connected device IP addresses,
    and byte counts for a specific monitored user.
    """
    user_role = str(user_payload.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required."
        )

    records = (
        db.query(UserIspTrafficUsage)
        .filter(UserIspTrafficUsage.user_id == target_user_id)
        .order_by(UserIspTrafficUsage.total_bytes.desc())
        .all()
    )

    total_bytes = sum(r.total_bytes for r in records)

    if total_bytes == 0:
        return {
            "total_bytes": 0,
            "providers": []
        }

    providers_list = []
    for r in records:
        percentage = round((r.total_bytes / total_bytes) * 100, 1)
        if percentage <= 0:
            continue

        color = ISPMatcher.get_provider_color(r.isp_name)

        providers_list.append({
            "name": r.isp_name,
            "country": r.country,
            "ip_address": getattr(r, "ip_address", None) or getattr(r, "last_ip", None) or "Unknown",
            "last_ip": getattr(r, "last_ip", None) or getattr(r, "ip_address", None) or "Unknown",
            "bytes": r.total_bytes,
            "percentage": percentage,
            "color": color
        })

    return {
        "total_bytes": total_bytes,
        "providers": providers_list
    }


@router.get("/admin/daily-trends")
def get_admin_daily_bandwidth_trends(
    days: int = Query(NUMBER_OF_PAST_DAYS, ge=7, le=90, description="Number of past days to aggregate"),
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    user_role = str(user_payload.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to access platform traffic trends."
        )

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days - 1)

    records = (
        db.query(
            DailyTrafficUsage.date,
            func.sum(DailyTrafficUsage.total_bytes).label("total_bytes")
        )
        .filter(DailyTrafficUsage.date >= start_date)
        .group_by(DailyTrafficUsage.date)
        .all()
    )

    traffic_map: Dict[str, int] = {
        (r.date.isoformat() if hasattr(r.date, "isoformat") else str(r.date)): int(r.total_bytes or 0)
        for r in records
    }

    results = []
    for i in range(days):
        current_day = start_date + timedelta(days=i)
        d_str = current_day.isoformat()
        b_count = traffic_map.get(d_str, 0)

        results.append({
            "day": current_day.strftime("%b %d"),
            "date_iso": d_str,
            "data_bytes": b_count,
            "data_mb": round(b_count / (1024 ** 2), 2),
            "data_gb": round(b_count / (1024 ** 3), 4)
        })

    return results


@router.get("/admin/users/{target_user_id}/daily")
def get_user_daily_traffic_admin(
    target_user_id: uuid.UUID,
    days: int = Query(NUMBER_OF_PAST_DAYS, ge=1, le=90),
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    user_role = str(user_payload.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required."
        )

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days - 1)

    records = (
        db.query(DailyTrafficUsage)
        .filter(
            DailyTrafficUsage.user_id == target_user_id,
            DailyTrafficUsage.date >= start_date
        )
        .all()
    )

    usage_map: Dict[str, int] = {}
    for r in records:
        d_str = r.date.isoformat() if hasattr(r.date, "isoformat") else str(r.date).split()[0]
        usage_map[d_str] = usage_map.get(d_str, 0) + (r.total_bytes or 0)

    results = []
    for i in range(days):
        current_day = start_date + timedelta(days=i)
        d_str = current_day.isoformat()
        b_count = usage_map.get(d_str, 0)
        results.append({
            "date": current_day.strftime("%b %d"),
            "date_iso": d_str,
            "bytes": b_count,
            "mb": round(b_count / (1024 ** 2), 2),
            "gb": round(b_count / (1024 ** 3), 4)
        })

    return results


@router.get("/admin/summary")
def get_traffic_summary(
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    user_role = str(user_payload.get("role", "")).capitalize()
    if user_role != "Admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required."
        )

    today = datetime.now(timezone.utc).date()

    all_time_bytes = db.query(func.sum(DailyTrafficUsage.total_bytes)).scalar() or 0
    today_bytes = (
        db.query(func.sum(DailyTrafficUsage.total_bytes))
        .filter(DailyTrafficUsage.date == today)
        .scalar() or 0
    )
    active_users_count = (
        db.query(func.count(func.distinct(DailyTrafficUsage.user_id)))
        .filter(DailyTrafficUsage.date >= (today - timedelta(days=30)))
        .scalar() or 0
    )

    return {
        "all_time_bytes": int(all_time_bytes),
        "all_time_gb": round(all_time_bytes / (1024 ** 3), 2),
        "today_bytes": int(today_bytes),
        "today_mb": round(today_bytes / (1024 ** 2), 2),
        "active_users_past_30_days": int(active_users_count)
    }