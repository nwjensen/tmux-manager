"""
SQLite database for persisting alerts and metrics history
"""

import aiosqlite
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Alert, AlertType, AlertSeverity, Host

logger = logging.getLogger(__name__)


class Database:
    """SQLite database manager"""
    
    def __init__(self, db_path: str = "/opt/tmux-dashboard/data/dashboard.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: Optional[aiosqlite.Connection] = None
    
    async def connect(self):
        """Initialize database connection and create tables"""
        self._connection = await aiosqlite.connect(str(self.db_path))
        self._connection.row_factory = aiosqlite.Row
        await self._create_tables()
    
    async def close(self):
        """Close database connection"""
        if self._connection:
            await self._connection.close()
            self._connection = None
    
    async def _create_tables(self):
        """Create database tables if they don't exist"""
        await self._connection.executescript("""
            -- Alerts history
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                severity TEXT NOT NULL,
                host TEXT NOT NULL,
                session TEXT,
                message TEXT NOT NULL,
                created TEXT NOT NULL,
                acknowledged INTEGER DEFAULT 0,
                acknowledged_at TEXT,
                cleared_at TEXT
            );
            
            -- Metrics history (for graphs/trends)
            CREATE TABLE IF NOT EXISTS metrics_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                host TEXT NOT NULL,
                cpu_percent REAL,
                memory_percent REAL,
                gpu_temp INTEGER,
                gpu_util INTEGER,
                gpu_memory_percent REAL,
                session_count INTEGER
            );
            
            -- Create indexes
            CREATE INDEX IF NOT EXISTS idx_alerts_host ON alerts(host);
            CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created);
            CREATE INDEX IF NOT EXISTS idx_metrics_host_time ON metrics_history(host, timestamp);
            
            -- Cleanup old metrics (keep last 7 days)
            -- This would be run periodically
        """)
        await self._connection.commit()
    
    async def save_alert(self, alert: Alert):
        """Save or update an alert"""
        await self._connection.execute("""
            INSERT OR REPLACE INTO alerts 
            (id, type, severity, host, session, message, created, acknowledged, acknowledged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            alert.id,
            alert.type.value,
            alert.severity.value,
            alert.host,
            alert.session,
            alert.message,
            alert.created.isoformat(),
            1 if alert.acknowledged else 0,
            alert.acknowledged_at.isoformat() if alert.acknowledged_at else None
        ))
        await self._connection.commit()
    
    async def clear_alert(self, alert_id: str):
        """Mark an alert as cleared"""
        await self._connection.execute("""
            UPDATE alerts SET cleared_at = ? WHERE id = ?
        """, (datetime.utcnow().isoformat(), alert_id))
        await self._connection.commit()
    
    async def get_alert_history(self, host: Optional[str] = None, limit: int = 100) -> list[dict]:
        """Get historical alerts"""
        if host:
            cursor = await self._connection.execute("""
                SELECT * FROM alerts WHERE host = ? ORDER BY created DESC LIMIT ?
            """, (host, limit))
        else:
            cursor = await self._connection.execute("""
                SELECT * FROM alerts ORDER BY created DESC LIMIT ?
            """, (limit,))
        
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    
    async def save_metrics_snapshot(self, hosts: list[Host]):
        """Save current metrics for all hosts"""
        timestamp = datetime.utcnow().isoformat()
        
        for host in hosts:
            # Get GPU metrics if available
            gpu_temp = None
            gpu_util = None
            gpu_mem = None
            if host.gpus:
                gpu = host.gpus[0]  # Primary GPU
                gpu_temp = gpu.temperature_c
                gpu_util = gpu.utilization_percent
                gpu_mem = gpu.memory_percent
            
            await self._connection.execute("""
                INSERT INTO metrics_history 
                (timestamp, host, cpu_percent, memory_percent, gpu_temp, gpu_util, gpu_memory_percent, session_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp,
                host.hostname,
                host.cpu_percent,
                host.memory_percent,
                gpu_temp,
                gpu_util,
                gpu_mem,
                len(host.sessions)
            ))
        
        await self._connection.commit()
    
    async def get_metrics_history(
        self, 
        host: str, 
        hours: int = 24
    ) -> list[dict]:
        """Get metrics history for a host"""
        cursor = await self._connection.execute("""
            SELECT * FROM metrics_history 
            WHERE host = ? AND timestamp > datetime('now', ?)
            ORDER BY timestamp ASC
        """, (host, f'-{hours} hours'))
        
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    
    async def cleanup_old_data(self, days: int = 7):
        """Remove data older than specified days"""
        await self._connection.execute("""
            DELETE FROM metrics_history 
            WHERE timestamp < datetime('now', ?)
        """, (f'-{days} days',))
        
        await self._connection.execute("""
            DELETE FROM alerts 
            WHERE cleared_at IS NOT NULL 
            AND cleared_at < datetime('now', ?)
        """, (f'-{days} days',))
        
        await self._connection.commit()
        logger.info(f"Cleaned up data older than {days} days")
