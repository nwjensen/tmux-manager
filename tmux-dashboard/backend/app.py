"""
TMUX Fleet Dashboard - Main Application
FastAPI server with WebSocket support for real-time updates
"""

import asyncio
import logging
import json
import yaml
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from .models import (
    Config, Host, Session, SessionStatus, Alert,
    HostsResponse, SessionsResponse, AlertsResponse, StatusResponse,
    HostConfig, SSHConfig, AlertConfig
)
from .collector import FleetCollector
from .alerts import AlertManager
from .database import Database

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Global state
class AppState:
    def __init__(self):
        self.config: Optional[Config] = None
        self.hosts: list[Host] = []
        self.collector: Optional[FleetCollector] = None
        self.alert_manager: Optional[AlertManager] = None
        self.database: Optional[Database] = None
        self.websockets: list[WebSocket] = []
        self.last_poll: Optional[datetime] = None
        self.start_time: datetime = datetime.utcnow()
        self.polling_task: Optional[asyncio.Task] = None

state = AppState()


def load_config(config_path: str = "/opt/tmux-dashboard/config/hosts.yml") -> Config:
    """Load configuration from YAML file"""
    path = Path(config_path)
    
    if not path.exists():
        logger.warning(f"Config file not found at {path}, using defaults")
        return Config()
    
    with open(path) as f:
        data = yaml.safe_load(f)
    
    # Parse hosts
    hosts = []
    for h in data.get('hosts', []):
        hosts.append(HostConfig(
            name=h.get('name', ''),
            address=h.get('address', ''),
            has_gpu=h.get('has_gpu', False),
            tags=h.get('tags', [])
        ))
    
    # Parse SSH config
    ssh_data = data.get('ssh', {})
    ssh_config = SSHConfig(
        user=ssh_data.get('user', 'tmux-dash'),
        key_path=ssh_data.get('key_path', '/opt/tmux-dashboard/.ssh/id_ed25519'),
        timeout=ssh_data.get('timeout', 10),
        known_hosts_policy=ssh_data.get('known_hosts_policy', 'accept')
    )
    
    # Parse alert config
    alert_data = data.get('alerts', {})
    alert_config = AlertConfig(
        enabled=alert_data.get('enabled', True),
        session_cpu_warning=alert_data.get('session_cpu_warning', 80),
        session_memory_mb_warning=alert_data.get('session_memory_mb_warning', 2048),
        host_cpu_warning=alert_data.get('host_cpu_warning', 90),
        host_memory_warning=alert_data.get('host_memory_warning', 90),
        gpu_temp_warning=alert_data.get('gpu_temp_warning', 80),
        gpu_temp_critical=alert_data.get('gpu_temp_critical', 90),
        gpu_memory_warning=alert_data.get('gpu_memory_warning', 90),
        gpu_util_info=alert_data.get('gpu_util_info', 95)
    )
    
    return Config(
        polling_interval_seconds=data.get('polling_interval_seconds', 30),
        legacy_threshold_hours=data.get('legacy_threshold_hours', 72),
        alerts=alert_config,
        ssh=ssh_config,
        hosts=hosts
    )


async def broadcast(event: str, data: dict):
    """Broadcast message to all connected WebSocket clients"""
    if not state.websockets:
        return
    
    message = json.dumps({
        "event": event, 
        "data": data, 
        "timestamp": datetime.utcnow().isoformat()
    }, default=str)
    
    disconnected = []
    for ws in state.websockets:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.append(ws)
    
    for ws in disconnected:
        state.websockets.remove(ws)


async def poll_hosts():
    """Single polling iteration"""
    if not state.collector:
        return
    
    logger.debug("Starting host poll")
    state.hosts = await state.collector.collect_all()
    state.last_poll = datetime.utcnow()
    
    # Evaluate alerts
    if state.alert_manager:
        state.alert_manager.evaluate_hosts(state.hosts)
    
    # Save metrics to database
    if state.database:
        await state.database.save_metrics_snapshot(state.hosts)
    
    # Broadcast update
    await broadcast("hosts_update", {
        "hosts": [h.model_dump() for h in state.hosts],
        "alerts": [a.model_dump() for a in state.alert_manager.get_alerts()] if state.alert_manager else []
    })
    
    logger.debug(f"Poll complete: {len(state.hosts)} hosts, {sum(len(h.sessions) for h in state.hosts)} sessions")


async def polling_loop():
    """Background task that polls hosts periodically"""
    while True:
        try:
            await poll_hosts()
        except Exception as e:
            logger.error(f"Polling error: {e}")
        
        await asyncio.sleep(state.config.polling_interval_seconds if state.config else 30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    logger.info("Starting TMUX Fleet Dashboard")
    
    # Load configuration
    state.config = load_config()
    logger.info(f"Loaded config with {len(state.config.hosts)} hosts")
    
    # Initialize components
    state.collector = FleetCollector(
        state.config.ssh,
        state.config.hosts,
        state.config.legacy_threshold_hours
    )
    
    state.alert_manager = AlertManager(
        state.config.alerts,
        state.config.legacy_threshold_hours
    )
    
    state.database = Database()
    await state.database.connect()
    
    # Start polling
    state.polling_task = asyncio.create_task(polling_loop())
    logger.info("Polling started")
    
    yield
    
    # Shutdown
    logger.info("Shutting down")
    if state.polling_task:
        state.polling_task.cancel()
        try:
            await state.polling_task
        except asyncio.CancelledError:
            pass
    
    if state.database:
        await state.database.close()


# Create FastAPI app
app = FastAPI(
    title="TMUX Fleet Dashboard",
    version="1.0.0",
    lifespan=lifespan
)


# API Routes
@app.get("/api/status", response_model=StatusResponse)
async def get_status():
    """Get dashboard status"""
    uptime = (datetime.utcnow() - state.start_time).total_seconds()
    online_hosts = sum(1 for h in state.hosts if h.status.value == "online")
    total_sessions = sum(len(h.sessions) for h in state.hosts)
    
    return StatusResponse(
        status="running",
        version="1.0.0",
        uptime_seconds=int(uptime),
        hosts_online=online_hosts,
        hosts_total=len(state.hosts),
        sessions_total=total_sessions,
        alerts_active=state.alert_manager.unacknowledged_count if state.alert_manager else 0,
        last_poll=state.last_poll
    )


@app.get("/api/hosts")
async def get_hosts():
    """Get all hosts with current data"""
    return {
        "hosts": [h.model_dump() for h in state.hosts],
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/api/hosts/{hostname}")
async def get_host(hostname: str):
    """Get a specific host"""
    for host in state.hosts:
        if host.hostname == hostname:
            return host.model_dump()
    raise HTTPException(status_code=404, detail=f"Host '{hostname}' not found")


@app.get("/api/sessions")
async def get_sessions(status: Optional[str] = Query(None, enum=["active", "legacy"])):
    """Get all sessions, optionally filtered by status"""
    sessions = []
    for host in state.hosts:
        for session in host.sessions:
            if status is None or session.status.value == status:
                sessions.append(session.model_dump())
    
    active_count = sum(1 for s in sessions if s.get("status") == "active")
    legacy_count = sum(1 for s in sessions if s.get("status") == "legacy")
    
    return {
        "sessions": sessions,
        "total": len(sessions),
        "active": active_count,
        "legacy": legacy_count,
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/api/sessions/{session_id:path}")
async def get_session(session_id: str):
    """Get a specific session by ID (format: hostname:session_name)"""
    for host in state.hosts:
        for session in host.sessions:
            if session.id == session_id:
                return session.model_dump()
    raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")


@app.get("/api/alerts")
async def get_alerts(acknowledged: Optional[bool] = None):
    """Get all alerts"""
    if not state.alert_manager:
        return {"alerts": [], "total": 0, "unacknowledged": 0}
    
    if acknowledged is None:
        alerts = state.alert_manager.get_alerts(include_acknowledged=True)
    else:
        alerts = state.alert_manager.get_alerts(include_acknowledged=acknowledged)
    
    return {
        "alerts": [a.model_dump() for a in alerts],
        "total": len(alerts),
        "unacknowledged": state.alert_manager.unacknowledged_count,
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/api/alerts/{alert_id}/ack")
async def acknowledge_alert(alert_id: str):
    """Acknowledge an alert"""
    if not state.alert_manager:
        raise HTTPException(status_code=500, detail="Alert manager not initialized")
    
    if state.alert_manager.acknowledge(alert_id):
        alert = state.alert_manager.get_alert(alert_id)
        if alert and state.database:
            await state.database.save_alert(alert)
        
        await broadcast("alert_acknowledged", {"id": alert_id})
        return {"success": True, "id": alert_id}
    
    raise HTTPException(status_code=404, detail=f"Alert '{alert_id}' not found")


@app.post("/api/sessions/{session_id:path}/kill")
async def kill_session(session_id: str, confirm: bool = Query(False)):
    """Kill a tmux session (requires confirm=true)"""
    if not confirm:
        raise HTTPException(
            status_code=400, 
            detail="Add ?confirm=true to confirm session termination"
        )
    
    # Find the session
    target_host = None
    target_session = None
    for host in state.hosts:
        for session in host.sessions:
            if session.id == session_id:
                target_host = host
                target_session = session
                break
        if target_session:
            break
    
    if not target_session or not target_host:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    
    # Kill via SSH
    try:
        import asyncssh
        
        key_path = Path(state.config.ssh.key_path)
        async with await asyncssh.connect(
            target_host.address,
            username=state.config.ssh.user,
            client_keys=[str(key_path)] if key_path.exists() else None,
            known_hosts=None,
            connect_timeout=state.config.ssh.timeout
        ) as conn:
            result = await conn.run(f"tmux kill-session -t {target_session.name}", check=False)
            
            if result.returncode == 0:
                # Trigger immediate refresh
                await poll_hosts()
                return {"success": True, "id": session_id, "message": f"Session '{target_session.name}' killed"}
            else:
                raise HTTPException(
                    status_code=500, 
                    detail=f"Failed to kill session: {result.stderr}"
                )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SSH error: {str(e)}")


@app.get("/api/config")
async def get_config():
    """Get current configuration (sanitized)"""
    if not state.config:
        return {}
    
    return {
        "polling_interval_seconds": state.config.polling_interval_seconds,
        "legacy_threshold_hours": state.config.legacy_threshold_hours,
        "alerts": state.config.alerts.model_dump(),
        "hosts": [{"name": h.name, "has_gpu": h.has_gpu, "tags": h.tags} for h in state.config.hosts]
    }


@app.post("/api/refresh")
async def trigger_refresh():
    """Manually trigger a host refresh"""
    await poll_hosts()
    return {"success": True, "timestamp": datetime.utcnow().isoformat()}


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time updates"""
    await websocket.accept()
    state.websockets.append(websocket)
    logger.info(f"WebSocket connected ({len(state.websockets)} total)")
    
    try:
        # Send initial state
        await websocket.send_text(json.dumps({
            "event": "connected",
            "data": {
                "hosts": [h.model_dump() for h in state.hosts],
                "alerts": [a.model_dump() for a in state.alert_manager.get_alerts()] if state.alert_manager else []
            },
            "timestamp": datetime.utcnow().isoformat()
        }, default=str))
        
        # Keep connection alive and listen for messages
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                # Handle ping/pong or other client messages if needed
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Send periodic ping to keep connection alive
                try:
                    await websocket.send_text(json.dumps({"event": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in state.websockets:
            state.websockets.remove(websocket)
        logger.info(f"WebSocket disconnected ({len(state.websockets)} remaining)")


# Serve frontend
frontend_path = Path(__file__).parent.parent / "frontend"

@app.get("/")
async def serve_index():
    """Serve the dashboard HTML"""
    index_path = frontend_path / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse({"error": "Frontend not found"}, status_code=404)


# Mount static files if there are any additional assets
if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")
