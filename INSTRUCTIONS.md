# TMUX Fleet Dashboard - Build Instructions

## Project Overview

Build a web dashboard that monitors tmux sessions across a fleet of Linux hosts, displaying session status, system resources, and GPU metrics. The dashboard categorizes sessions as "active" or "legacy" (detached >72 hours) and provides alerts for various conditions.

## Target Deployment

- **Host**: Proxmox node `pve1`
- **Container**: LXC (Debian 12)
- **Network**: DHCP for IP assignment
- **Access**: SSH enabled by default

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                 tmux-dashboard (LXC container)                   │
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────────┐  │
│  │ FastAPI     │  │ Collector   │  │ Static Frontend          │  │
│  │ - REST API  │  │ - SSH poll  │  │ - Session grid           │  │
│  │ - WebSocket │  │ - Metrics   │  │ - Resource gauges        │  │
│  │             │  │ - Alerts    │  │ - Alert banner           │  │
│  └─────────────┘  └─────────────┘  └──────────────────────────┘  │
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐                                │
│  │ SQLite      │  │ Config      │                                │
│  │ - Sessions  │  │ - hosts.yml │                                │
│  │ - Metrics   │  │ - alerts    │                                │
│  │ - History   │  │ - thresholds│                                │
│  └─────────────┘  └─────────────┘                                │
└──────────────────────────────────────────────────────────────────┘
          │
          │ SSH (dedicated service account + key)
          ▼
    ┌─────────┬─────────┬─────────┬─────────┐
    │  pve1   │  pve2   │  pi-01  │  ...    │
    └─────────┴─────────┴─────────┴─────────┘
```

---

## Part 1: LXC Container Setup Script

Create `scripts/create-lxc.sh` that runs on pve1 to create the container.

### Requirements:
- Container ID: 200 (or next available)
- Template: Debian 12 (download if needed)
- Hostname: `tmux-dashboard`
- Resources: 1 CPU core, 1GB RAM, 8GB disk
- Network: DHCP on vmbr0
- SSH: Enabled and accessible after creation
- Start on boot: Yes

### Script should:
1. Check if running on Proxmox
2. Download Debian 12 template if not present
3. Create the container with specified settings
4. Start the container
5. Wait for DHCP lease
6. Output the assigned IP address
7. Enable SSH password auth initially (for setup)

```bash
#!/bin/bash
# scripts/create-lxc.sh
# Run this on pve1 as root

set -e

CTID=${1:-200}
HOSTNAME="tmux-dashboard"
TEMPLATE="debian-12-standard"
STORAGE="local-lvm"  # Adjust if needed
MEMORY=1024
CORES=1
DISK=8
BRIDGE="vmbr0"

# ... implement the full script
```

---

## Part 2: Container Provisioning Script

Create `scripts/provision.sh` that runs INSIDE the container after creation.

### This script should:
1. Update apt and install dependencies:
   - python3, python3-pip, python3-venv
   - openssh-client
   - git (optional)
2. Create application user: `tmux-dash`
3. Create directory structure: `/opt/tmux-dashboard/`
4. Set up Python virtual environment
5. Install Python dependencies (see requirements.txt)
6. Generate SSH keypair for fleet access: `/opt/tmux-dashboard/.ssh/id_ed25519`
7. Create systemd service file
8. Enable and start the service
9. Output the public key for fleet deployment

---

## Part 3: Fleet Key Deployment Script

Create `scripts/deploy-key.sh` for deploying the dashboard's public key to fleet hosts.

### Usage:
```bash
./deploy-key.sh <public_key_file> <user> <host1> [host2] [host3] ...
```

### Script should:
1. Read the public key
2. SSH to each host (will prompt for password)
3. Create `tmux-dash` user if needed
4. Add public key to `~tmux-dash/.ssh/authorized_keys`
5. Set correct permissions
6. Verify key-based access works

---

## Part 4: Backend Application

Create `backend/app.py` - the main FastAPI application.

### Dependencies (requirements.txt):
```
fastapi==0.109.0
uvicorn[standard]==0.27.0
asyncssh==2.14.2
pyyaml==6.0.1
aiosqlite==0.19.0
pydantic==2.5.3
python-dateutil==2.8.2
```

### Data Models:

```python
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
from enum import Enum

class HostStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"

class SessionStatus(str, Enum):
    ACTIVE = "active"
    LEGACY = "legacy"

class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

class AlertType(str, Enum):
    LEGACY_SESSION = "legacy_session"
    HIGH_CPU = "high_cpu"
    HIGH_MEMORY = "high_memory"
    HOST_OFFLINE = "host_offline"
    GPU_TEMP_WARNING = "gpu_temp_warning"
    GPU_TEMP_CRITICAL = "gpu_temp_critical"
    GPU_MEMORY_HIGH = "gpu_memory_high"

class GPUProcess(BaseModel):
    pid: int
    name: str
    memory_mb: int

class GPU(BaseModel):
    index: int
    name: str
    power_draw_watts: float
    power_limit_watts: float
    memory_used_mb: int
    memory_total_mb: int
    utilization_percent: int
    temperature_c: int
    processes: list[GPUProcess] = []

class Session(BaseModel):
    id: str  # host:session_name
    host: str
    name: str
    created: Optional[datetime]
    last_activity: Optional[datetime]
    attached: bool
    window_count: int
    status: SessionStatus
    pids: list[int] = []
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    gpu_memory_mb: Optional[float] = None

class Host(BaseModel):
    hostname: str
    address: str
    last_seen: Optional[datetime]
    status: HostStatus
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_used_mb: int = 0
    memory_total_mb: int = 0
    load_avg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gpus: list[GPU] = []
    sessions: list[Session] = []
    has_gpu: bool = False

class Alert(BaseModel):
    id: str
    type: AlertType
    severity: AlertSeverity
    host: str
    session: Optional[str]
    message: str
    created: datetime
    acknowledged: bool = False
```

### Configuration (config/hosts.yml):

```yaml
polling_interval_seconds: 30
legacy_threshold_hours: 72

alerts:
  enabled: true
  # Sessions
  session_cpu_warning: 80
  session_memory_mb_warning: 2048
  # Hosts
  host_cpu_warning: 90
  host_memory_warning: 90
  # GPU
  gpu_temp_warning: 80
  gpu_temp_critical: 90
  gpu_memory_warning: 90

ssh:
  user: tmux-dash
  key_path: /opt/tmux-dashboard/.ssh/id_ed25519
  timeout: 10
  known_hosts: null  # Accept any for initial setup

hosts:
  - name: pve1
    address: 192.168.1.10
    has_gpu: true
    
  - name: pve2
    address: 192.168.1.11
    has_gpu: true
```

### SSH Commands for Data Collection:

**System metrics:**
```bash
# CPU usage (returns single percentage)
top -bn1 | grep "Cpu(s)" | awk '{print $2}'

# Memory (used_mb total_mb percent)
free -m | awk 'NR==2{printf "%d %d %.1f", $3, $2, $3*100/$2}'

# Load average
cat /proc/loadavg | awk '{print $1, $2, $3}'
```

**tmux sessions:**
```bash
# List sessions with details
# Format: name, created timestamp, attached (0/1), windows count
tmux list-sessions -F '#{session_name}|#{session_created}|#{session_attached}|#{session_windows}|#{session_activity}' 2>/dev/null || echo ""
```

**GPU metrics (nvidia-smi):**
```bash
# GPU info (run only if has_gpu: true)
nvidia-smi --query-gpu=index,name,power.draw,power.limit,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null || echo ""

# GPU processes
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits 2>/dev/null || echo ""
```

**Session PIDs (for resource correlation):**
```bash
# Get all PIDs in a tmux session
tmux list-panes -t SESSION_NAME -F '#{pane_pid}' 2>/dev/null | while read pid; do
  pstree -p $pid | grep -oP '\(\K[0-9]+(?=\))'
done | sort -u
```

### API Endpoints:

```
GET  /api/hosts              - List all hosts with current data
GET  /api/hosts/{hostname}   - Get single host details
GET  /api/sessions           - List all sessions (optional ?status=active|legacy)
GET  /api/sessions/{id}      - Get single session details
GET  /api/alerts             - List active alerts (optional ?acknowledged=false)
POST /api/alerts/{id}/ack    - Acknowledge an alert
POST /api/sessions/{id}/kill - Kill a session (requires confirmation token)
GET  /api/config             - Get current config (sanitized, no keys)
WS   /ws                     - WebSocket for real-time updates
```

### WebSocket Events:

```json
{"event": "hosts_update", "data": [...]}
{"event": "alert_new", "data": {...}}
{"event": "alert_cleared", "data": {"id": "..."}}
```

### Core Logic:

1. **Collector Loop**: 
   - Runs every `polling_interval_seconds`
   - Connects to each host via SSH (async, parallel)
   - Runs collection commands
   - Parses output
   - Updates in-memory state
   - Stores to SQLite for history
   - Evaluates alert conditions
   - Broadcasts updates via WebSocket

2. **Alert Evaluation**:
   - Check each session: if detached > 72h, create/update legacy alert
   - Check host CPU/memory against thresholds
   - Check GPU temp and memory against thresholds
   - Clear alerts when conditions resolve
   - Persist alert state to SQLite

3. **Session Status Calculation**:
   - `active`: attached OR detached < 72 hours
   - `legacy`: detached >= 72 hours

---

## Part 5: Frontend

Create `frontend/index.html` - a single-file dashboard.

### Design Direction:
- **Aesthetic**: Industrial/utilitarian with a dark theme
- **Feel**: Server room monitoring, serious but not boring
- **Colors**: Dark background (#0d1117), green accents for healthy (#238636), amber for warnings (#d29922), red for critical (#da3633)
- **Typography**: Monospace for data (JetBrains Mono or similar), clean sans for labels
- **Layout**: Grid of host cards, collapsible alert banner at top

### Features:
1. **Alert Banner** (top, collapsible)
   - Shows active alerts with severity icons
   - Acknowledge button per alert
   - Badge count in header

2. **Tab Navigation**
   - Active Sessions
   - Legacy Sessions  
   - All Hosts

3. **Host Cards** (grid layout, responsive)
   - Host name and status indicator
   - CPU/Memory bars
   - GPU panel (if has_gpu):
     - GPU name
     - Power: current/limit with bar
     - VRAM: used/total with bar
     - Temperature with color coding
     - Utilization percentage
     - Active processes list
   - Session list:
     - Session name
     - Window count
     - Age (human readable: "3h", "2d", etc.)
     - Resource usage
     - Attach link (ssh:// URI)
     - Kill button (legacy only, with confirmation)

4. **Auto-refresh**
   - WebSocket connection for real-time updates
   - Fallback to polling if WS disconnects
   - Visual indicator showing connection status and last update time

5. **Session Attach Links**
   - Format: `ssh://tmux-dash@HOST?command=tmux attach -t SESSION`
   - Or display as copyable command: `ssh tmux-dash@HOST -t 'tmux attach -t SESSION'`

### Frontend Structure:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TMUX Fleet Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        /* CSS variables, reset, layout, components */
    </style>
</head>
<body>
    <div id="app">
        <!-- Header with title, alert badge, connection status -->
        <!-- Alert banner (collapsible) -->
        <!-- Tab navigation -->
        <!-- Host grid -->
    </div>
    <script>
        // State management
        // WebSocket connection
        // API calls
        // UI rendering
        // Event handlers
    </script>
</body>
</html>
```

---

## Part 6: Systemd Service

Create `config/tmux-dashboard.service`:

```ini
[Unit]
Description=TMUX Fleet Dashboard
After=network.target

[Service]
Type=simple
User=tmux-dash
Group=tmux-dash
WorkingDirectory=/opt/tmux-dashboard
Environment="PATH=/opt/tmux-dashboard/venv/bin:/usr/bin"
ExecStart=/opt/tmux-dashboard/venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Part 7: Project File Structure

```
tmux-dashboard/
├── README.md                    # Project overview and quick start
├── INSTRUCTIONS.md              # This file
├── requirements.txt             # Python dependencies
├── scripts/
│   ├── create-lxc.sh           # Run on pve1 to create container
│   ├── provision.sh            # Run inside container to set up app
│   └── deploy-key.sh           # Deploy SSH key to fleet hosts
├── config/
│   ├── hosts.yml.example       # Example configuration
│   └── tmux-dashboard.service  # Systemd unit file
├── backend/
│   ├── __init__.py
│   ├── app.py                  # FastAPI application
│   ├── collector.py            # SSH data collection
│   ├── models.py               # Pydantic models
│   ├── alerts.py               # Alert evaluation logic
│   └── database.py             # SQLite operations
└── frontend/
    └── index.html              # Single-file dashboard
```

---

## Deployment Steps (Summary)

1. **On pve1** (as root):
   ```bash
   ./scripts/create-lxc.sh 200
   # Note the IP address output
   ```

2. **SSH into the new container**:
   ```bash
   ssh root@<container-ip>
   ```

3. **Copy project files** to container (scp or git clone)

4. **Run provisioning**:
   ```bash
   cd /path/to/tmux-dashboard
   ./scripts/provision.sh
   # Note the public key output
   ```

5. **Deploy SSH key to fleet**:
   ```bash
   ./scripts/deploy-key.sh /opt/tmux-dashboard/.ssh/id_ed25519.pub tmux-dash pve1 pve2 pi-01 pi-02
   ```

6. **Configure hosts.yml**:
   ```bash
   cp config/hosts.yml.example /opt/tmux-dashboard/config/hosts.yml
   nano /opt/tmux-dashboard/config/hosts.yml
   # Add your hosts
   ```

7. **Start the service**:
   ```bash
   systemctl start tmux-dashboard
   systemctl status tmux-dashboard
   ```

8. **Access the dashboard**:
   ```
   http://<container-ip>:8080
   ```

---

## Testing Checklist

- [ ] LXC container creates successfully on pve1
- [ ] Container gets DHCP address
- [ ] SSH into container works
- [ ] Provisioning script completes without errors
- [ ] SSH keypair generated
- [ ] Key deployed to at least one fleet host
- [ ] Dashboard service starts
- [ ] Web UI loads at http://<ip>:8080
- [ ] Hosts show as online (after key deployment)
- [ ] tmux sessions are detected
- [ ] GPU metrics display (on hosts with GPUs)
- [ ] Alerts trigger for legacy sessions
- [ ] WebSocket updates work
- [ ] Session attach command works
- [ ] Session kill works (with confirmation)

---

## Notes for Claude Code

- Use Python 3.11+ features (typing, async)
- Handle SSH connection failures gracefully (host offline, not host error)
- Parse nvidia-smi output carefully - it may not exist or may fail
- All times should be UTC internally, display in local time on frontend
- The frontend should work without JavaScript frameworks - vanilla JS only
- Make the UI responsive (works on mobile for quick checks)
- Include loading states and error handling in the UI
- Add console logging in frontend for debugging
- Backend should log to stdout (systemd will capture)
