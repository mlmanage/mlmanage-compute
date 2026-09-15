"""
Alerts endpoint implementation for MLManage.

This module provides endpoints to fetch and manage GPU threshold alerts
(temperature, OOM, power usage) from Prometheus alert state.

Backend integration points:
1. Prometheus alert manager integration
2. Database storage for dismissed alerts
3. Real-time alert status queries

Alert Severity Mapping:
- critical: Values at or beyond critical threshold (temp > 85C, memory < 100MB, power > 250W)
- warning: Values approaching thresholds
- info: Informational alerts
"""

from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import Column, String, DateTime, Boolean
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, HTTPException
import requests
import logging

logger = logging.getLogger(__name__)

# Router setup
router = APIRouter(prefix="/alerts", tags=["alerts"])


# Database model for dismissed alerts
class DismissedAlert(Column):
    """Track dismissed alerts to avoid showing them repeatedly."""
    __tablename__ = "dismissed_alerts"
    id: str = Column(String, primary_key=True)
    alert_name: str = Column(String)
    gpu_uuid: Optional[str] = Column(String, nullable=True)
    dismissed_at: datetime = Column(DateTime, default=datetime.utcnow)
    dismissed_by: str = Column(String)


def query_prometheus_alerts(prometheus_url: str, alert_name: Optional[str] = None) -> list[dict]:
    """
    Query Prometheus for active alerts matching alert rules.

    Returns alerts in the format expected by the frontend:
    [
        {
            "id": "GPUHighTemperature-gpu-uuid",
            "alert_name": "GPUHighTemperature",
            "severity": "critical",
            "gpu_uuid": "gpu-uuid",
            "node": "node-name",
            "value": 87.5,
            "threshold": 85.0,
            "unit": "°C",
            "message": "GPU gpu-uuid temperature high (87.5°C)",
            "fired_at": "2024-09-15T10:30:00Z"
        }
    ]
    """
    alerts = []
    try:
        # Query Prometheus for active alerts
        query = 'ALERTS{alertstate="firing"}'
        resp = requests.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query},
            timeout=5
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            logger.debug(f"Prometheus query failed: {data}")
            return []

        # Map Prometheus alert format to our format
        for result in data.get("data", {}).get("result", []):
            metric = result.get("metric", {})
            alert_name_val = metric.get("alertname")
            severity_map = {
                "GPUHighTemperature": "critical",
                "GPUHighPower": "warning",
                "GPUOOM": "critical",
            }
            severity = severity_map.get(alert_name_val, "info")

            gpu_uuid = metric.get("gpu") or metric.get("UUID") or "unknown"
            node = metric.get("node") or metric.get("Hostname")

            # Create alert object
            alert_id = f"{alert_name_val}-{gpu_uuid}"

            # Map alert to specific messages
            unit = "°C" if "Temp" in alert_name_val else "MB" if "OOM" in alert_name_val else "W"
            message = metric.get("summary") or f"{alert_name_val} on GPU {gpu_uuid}"

            alerts.append({
                "id": alert_id,
                "alert_name": alert_name_val,
                "severity": severity,
                "gpu_uuid": gpu_uuid,
                "node": node,
                "value": float(result["value"][1]) if result.get("value") else None,
                "threshold": None,  # Could be extracted from alert annotations
                "unit": unit,
                "message": message,
                "fired_at": datetime.utcnow().isoformat(),
            })

        # Filter by alert name if specified
        if alert_name:
            alerts = [a for a in alerts if a["alert_name"] == alert_name]

    except Exception as exc:
        logger.error(f"Failed to query Prometheus alerts: {exc}")

    return alerts


@router.get("/alerts")
async def get_alerts(
    dismissed: bool = False,
    db: Session = Depends(get_database)
) -> dict:
    """
    Get active GPU alerts.

    Query Parameters:
    - dismissed: If True, return dismissed alerts; if False, return active alerts

    Returns:
    {
        "alerts": [
            {
                "id": "GPUHighTemperature-uuid",
                "alert_name": "GPUHighTemperature",
                "severity": "critical",
                "gpu_uuid": "gpu-uuid",
                "node": "node-name",
                "value": 87.5,
                "threshold": 85,
                "unit": "°C",
                "message": "GPU temperature high (87.5°C)",
                "fired_at": "2024-09-15T10:30:00Z"
            }
        ]
    }
    """
    try:
        prometheus_url = os.getenv(
            "PROMETHEUS_URL",
            "http://monitoring-kube-prometheus-prometheus.monitoring:9090"
        )

        if dismissed:
            # Return dismissed alerts from the last 24 hours
            since = datetime.utcnow() - timedelta(hours=24)
            dismissed_alerts = db.query(DismissedAlert).filter(
                DismissedAlert.dismissed_at >= since
            ).all()
            return {
                "alerts": [
                    {
                        "id": a.id,
                        "alert_name": a.alert_name,
                        "gpu_uuid": a.gpu_uuid,
                        "dismissed_at": a.dismissed_at.isoformat(),
                    }
                    for a in dismissed_alerts
                ]
            }
        else:
            # Get active alerts and filter out dismissed ones
            active_alerts = query_prometheus_alerts(prometheus_url)
            dismissed_ids = {
                a.id for a in db.query(DismissedAlert).filter(
                    DismissedAlert.dismissed_at >= datetime.utcnow() - timedelta(hours=24)
                ).all()
            }

            # Filter dismissed alerts
            active_alerts = [a for a in active_alerts if a["id"] not in dismissed_ids]

            return {"alerts": active_alerts}

    except Exception as exc:
        logger.error(f"Failed to get alerts: {exc}")
        return {"alerts": []}


@router.post("/alerts/{alert_id}/dismiss")
async def dismiss_alert(
    alert_id: str,
    db: Session = Depends(get_database),
    current_user: UserDB = Depends(get_current_user)
) -> dict:
    """
    Dismiss an alert for the current user.

    The alert will not appear in active alerts for 24 hours.

    Parameters:
    - alert_id: The alert ID to dismiss (e.g., "GPUHighTemperature-uuid")

    Returns:
    {
        "message": "Alert dismissed"
    }
    """
    try:
        # Check if already dismissed
        existing = db.query(DismissedAlert).filter(
            DismissedAlert.id == alert_id
        ).first()

        if not existing:
            # Store dismissed alert
            dismissed = DismissedAlert(
                id=alert_id,
                alert_name=alert_id.split("-")[0] if "-" in alert_id else alert_id,
                gpu_uuid="-".join(alert_id.split("-")[1:]) if "-" in alert_id else None,
                dismissed_by=current_user.username,
            )
            db.add(dismissed)
            db.commit()

        return {"message": "Alert dismissed"}

    except Exception as exc:
        logger.error(f"Failed to dismiss alert: {exc}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to dismiss alert")


@router.get("/alerts/stats")
async def get_alert_stats(
    db: Session = Depends(get_database)
) -> dict:
    """
    Get alert statistics.

    Returns:
    {
        "total_active": 5,
        "critical": 2,
        "warning": 3,
        "by_alert_type": {
            "GPUHighTemperature": 2,
            "GPUOOM": 3
        },
        "affected_gpus": ["gpu-1", "gpu-2"]
    }
    """
    try:
        prometheus_url = os.getenv(
            "PROMETHEUS_URL",
            "http://monitoring-kube-prometheus-prometheus.monitoring:9090"
        )
        alerts = query_prometheus_alerts(prometheus_url)

        stats = {
            "total_active": len(alerts),
            "critical": len([a for a in alerts if a["severity"] == "critical"]),
            "warning": len([a for a in alerts if a["severity"] == "warning"]),
            "by_alert_type": {},
            "affected_gpus": list(set(a.get("gpu_uuid") for a in alerts if a.get("gpu_uuid")))
        }

        for alert in alerts:
            alert_name = alert["alert_name"]
            stats["by_alert_type"][alert_name] = stats["by_alert_type"].get(alert_name, 0) + 1

        return stats

    except Exception as exc:
        logger.error(f"Failed to get alert stats: {exc}")
        return {
            "total_active": 0,
            "critical": 0,
            "warning": 0,
            "by_alert_type": {},
            "affected_gpus": []
        }
