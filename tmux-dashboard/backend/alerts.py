"""
Alert evaluation and management
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from .models import (
    Host, HostStatus, Session, SessionStatus,
    Alert, AlertType, AlertSeverity, AlertConfig
)

logger = logging.getLogger(__name__)


class AlertManager:
    """Evaluates conditions and manages alerts"""
    
    def __init__(self, config: AlertConfig, legacy_threshold_hours: int = 72):
        self.config = config
        self.legacy_threshold_hours = legacy_threshold_hours
        self.ancient_threshold_hours = legacy_threshold_hours * 2  # 144 hours = 6 days
        self.alerts: dict[str, Alert] = {}  # keyed by alert ID
        self._alert_keys: dict[str, str] = {}  # Maps unique condition key to alert ID
    
    def _make_key(self, alert_type: AlertType, host: str, session: Optional[str] = None) -> str:
        """Create a unique key for an alert condition"""
        if session:
            return f"{alert_type}:{host}:{session}"
        return f"{alert_type}:{host}"
    
    def _create_alert(
        self,
        alert_type: AlertType,
        severity: AlertSeverity,
        host: str,
        message: str,
        session: Optional[str] = None
    ) -> Alert:
        """Create a new alert"""
        alert_id = str(uuid.uuid4())[:8]
        return Alert(
            id=alert_id,
            type=alert_type,
            severity=severity,
            host=host,
            session=session,
            message=message,
            created=datetime.utcnow()
        )
    
    def _set_alert(self, key: str, alert: Alert):
        """Set or update an alert by condition key"""
        if key in self._alert_keys:
            # Update existing alert (keep same ID)
            old_id = self._alert_keys[key]
            old_alert = self.alerts.get(old_id)
            if old_alert:
                alert.id = old_id
                alert.created = old_alert.created
                alert.acknowledged = old_alert.acknowledged
                alert.acknowledged_at = old_alert.acknowledged_at
        
        self._alert_keys[key] = alert.id
        self.alerts[alert.id] = alert
    
    def _clear_alert(self, key: str):
        """Clear an alert by condition key"""
        if key in self._alert_keys:
            alert_id = self._alert_keys.pop(key)
            self.alerts.pop(alert_id, None)
    
    def evaluate_hosts(self, hosts: list[Host]) -> list[Alert]:
        """Evaluate all hosts and return current alerts"""
        if not self.config.enabled:
            return []
        
        active_keys = set()
        
        for host in hosts:
            # Check host offline
            key = self._make_key(AlertType.HOST_OFFLINE, host.hostname)
            if host.status == HostStatus.OFFLINE:
                active_keys.add(key)
                if key not in self._alert_keys:
                    alert = self._create_alert(
                        AlertType.HOST_OFFLINE,
                        AlertSeverity.CRITICAL,
                        host.hostname,
                        f"Host {host.hostname} is offline"
                    )
                    self._set_alert(key, alert)
            else:
                self._clear_alert(key)
            
            # Skip other checks if host is offline
            if host.status == HostStatus.OFFLINE:
                continue
            
            # Check host CPU
            key = self._make_key(AlertType.HOST_HIGH_CPU, host.hostname)
            if host.cpu_percent >= self.config.host_cpu_warning:
                active_keys.add(key)
                if key not in self._alert_keys:
                    alert = self._create_alert(
                        AlertType.HOST_HIGH_CPU,
                        AlertSeverity.WARNING,
                        host.hostname,
                        f"Host {host.hostname} CPU at {host.cpu_percent:.1f}%"
                    )
                    self._set_alert(key, alert)
            else:
                self._clear_alert(key)
            
            # Check host memory
            key = self._make_key(AlertType.HOST_HIGH_MEMORY, host.hostname)
            if host.memory_percent >= self.config.host_memory_warning:
                active_keys.add(key)
                if key not in self._alert_keys:
                    alert = self._create_alert(
                        AlertType.HOST_HIGH_MEMORY,
                        AlertSeverity.WARNING,
                        host.hostname,
                        f"Host {host.hostname} memory at {host.memory_percent:.1f}%"
                    )
                    self._set_alert(key, alert)
            else:
                self._clear_alert(key)
            
            # Check GPUs
            for gpu in host.gpus:
                # GPU temperature critical
                key = self._make_key(AlertType.GPU_TEMP_CRITICAL, host.hostname, f"gpu{gpu.index}")
                if gpu.temperature_c >= self.config.gpu_temp_critical:
                    active_keys.add(key)
                    if key not in self._alert_keys:
                        alert = self._create_alert(
                            AlertType.GPU_TEMP_CRITICAL,
                            AlertSeverity.CRITICAL,
                            host.hostname,
                            f"GPU {gpu.index} ({gpu.name}) temperature critical: {gpu.temperature_c}°C",
                            f"gpu{gpu.index}"
                        )
                        self._set_alert(key, alert)
                else:
                    self._clear_alert(key)
                
                # GPU temperature warning (only if not critical)
                key = self._make_key(AlertType.GPU_TEMP_WARNING, host.hostname, f"gpu{gpu.index}")
                if (gpu.temperature_c >= self.config.gpu_temp_warning and 
                    gpu.temperature_c < self.config.gpu_temp_critical):
                    active_keys.add(key)
                    if key not in self._alert_keys:
                        alert = self._create_alert(
                            AlertType.GPU_TEMP_WARNING,
                            AlertSeverity.WARNING,
                            host.hostname,
                            f"GPU {gpu.index} ({gpu.name}) temperature high: {gpu.temperature_c}°C",
                            f"gpu{gpu.index}"
                        )
                        self._set_alert(key, alert)
                else:
                    self._clear_alert(key)
                
                # GPU memory
                key = self._make_key(AlertType.GPU_MEMORY_HIGH, host.hostname, f"gpu{gpu.index}")
                if gpu.memory_percent >= self.config.gpu_memory_warning:
                    active_keys.add(key)
                    if key not in self._alert_keys:
                        alert = self._create_alert(
                            AlertType.GPU_MEMORY_HIGH,
                            AlertSeverity.WARNING,
                            host.hostname,
                            f"GPU {gpu.index} VRAM at {gpu.memory_percent:.1f}%",
                            f"gpu{gpu.index}"
                        )
                        self._set_alert(key, alert)
                else:
                    self._clear_alert(key)
            
            # Check sessions
            for session in host.sessions:
                # Legacy session (>72h detached)
                key = self._make_key(AlertType.LEGACY_SESSION, host.hostname, session.name)
                if session.status == SessionStatus.LEGACY:
                    active_keys.add(key)
                    if key not in self._alert_keys:
                        detached_hours = (session.detached_seconds or 0) / 3600
                        alert = self._create_alert(
                            AlertType.LEGACY_SESSION,
                            AlertSeverity.WARNING,
                            host.hostname,
                            f"Session '{session.name}' detached for {detached_hours:.0f} hours",
                            session.name
                        )
                        self._set_alert(key, alert)
                else:
                    self._clear_alert(key)
                
                # Ancient session (>144h detached) - escalate to critical
                key = self._make_key(AlertType.ANCIENT_SESSION, host.hostname, session.name)
                detached_seconds = session.detached_seconds or 0
                if detached_seconds > self.ancient_threshold_hours * 3600:
                    active_keys.add(key)
                    if key not in self._alert_keys:
                        detached_hours = detached_seconds / 3600
                        alert = self._create_alert(
                            AlertType.ANCIENT_SESSION,
                            AlertSeverity.CRITICAL,
                            host.hostname,
                            f"Session '{session.name}' detached for {detached_hours:.0f} hours (very old)",
                            session.name
                        )
                        self._set_alert(key, alert)
                else:
                    self._clear_alert(key)
        
        # Return all active alerts (sorted by severity, then creation time)
        severity_order = {AlertSeverity.CRITICAL: 0, AlertSeverity.WARNING: 1, AlertSeverity.INFO: 2}
        return sorted(
            self.alerts.values(),
            key=lambda a: (severity_order.get(a.severity, 3), a.created)
        )
    
    def acknowledge(self, alert_id: str) -> bool:
        """Acknowledge an alert by ID"""
        if alert_id in self.alerts:
            self.alerts[alert_id].acknowledged = True
            self.alerts[alert_id].acknowledged_at = datetime.utcnow()
            return True
        return False
    
    def get_alerts(self, include_acknowledged: bool = True) -> list[Alert]:
        """Get all alerts, optionally filtering acknowledged ones"""
        alerts = list(self.alerts.values())
        if not include_acknowledged:
            alerts = [a for a in alerts if not a.acknowledged]
        
        severity_order = {AlertSeverity.CRITICAL: 0, AlertSeverity.WARNING: 1, AlertSeverity.INFO: 2}
        return sorted(alerts, key=lambda a: (severity_order.get(a.severity, 3), a.created))
    
    def get_alert(self, alert_id: str) -> Optional[Alert]:
        """Get a specific alert by ID"""
        return self.alerts.get(alert_id)
    
    @property
    def unacknowledged_count(self) -> int:
        """Count of unacknowledged alerts"""
        return sum(1 for a in self.alerts.values() if not a.acknowledged)
    
    @property
    def critical_count(self) -> int:
        """Count of critical alerts"""
        return sum(1 for a in self.alerts.values() if a.severity == AlertSeverity.CRITICAL and not a.acknowledged)
