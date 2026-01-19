# TMUX Fleet Dashboard

A web-based dashboard for monitoring tmux sessions across a fleet of Linux hosts. Displays session status, system resources, and NVIDIA GPU metrics with real-time updates and alerting.

![Dashboard Preview](docs/preview.png)

## Features

- **Session Monitoring**: Track all tmux sessions across your fleet
- **Active vs Legacy**: Sessions detached >72 hours flagged as "legacy"
- **System Metrics**: CPU, memory, load average per host
- **GPU Metrics**: Power draw, VRAM usage, temperature, utilization (NVIDIA)
- **Alerts**: Configurable thresholds for sessions, hosts, and GPUs
- **Real-time Updates**: WebSocket-powered live dashboard
- **Quick Actions**: Attach links and kill buttons for sessions

## Quick Start

### 1. Create LXC Container (on pve1)

```bash
# SSH to pve1 as root
scp -r tmux-dashboard root@pve1:/tmp/
ssh root@pve1

cd /tmp/tmux-dashboard
chmod +x scripts/*.sh
./scripts/create-lxc.sh 200

# Note the IP address printed at the end
```

### 2. Provision the Container

```bash
# SSH into the new container
ssh root@<container-ip>

# Copy files (or git clone)
mkdir -p /opt/tmux-dashboard
# ... copy files ...

cd /opt/tmux-dashboard
./scripts/provision.sh

# Save the public key that's printed
```

### 3. Deploy SSH Key to Fleet

```bash
# From the container, deploy to each host
./scripts/deploy-key.sh /opt/tmux-dashboard/.ssh/id_ed25519.pub tmux-dash \
    192.168.1.10 \
    192.168.1.11 \
    192.168.1.50
```

### 4. Configure

```bash
cp config/hosts.yml.example config/hosts.yml
nano config/hosts.yml
# Edit with your actual hosts
```

### 5. Start

```bash
systemctl start tmux-dashboard
systemctl enable tmux-dashboard

# Check status
systemctl status tmux-dashboard
journalctl -u tmux-dashboard -f
```

### 6. Access

Open `http://<container-ip>:8080` in your browser.

## Configuration

See `config/hosts.yml.example` for all options:

```yaml
polling_interval_seconds: 30
legacy_threshold_hours: 72

alerts:
  enabled: true
  gpu_temp_warning: 80
  gpu_temp_critical: 90
  # ... more thresholds

hosts:
  - name: pve1
    address: 192.168.1.10
    has_gpu: true
  - name: pi-01
    address: 192.168.1.50
    has_gpu: false
```

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/hosts` | GET | All hosts with metrics |
| `/api/sessions` | GET | All sessions (?status=active\|legacy) |
| `/api/alerts` | GET | Active alerts |
| `/api/alerts/{id}/ack` | POST | Acknowledge alert |
| `/api/sessions/{id}/kill` | POST | Kill session |
| `/ws` | WS | Real-time updates |

## Architecture

```
┌─────────────────────────────────────────┐
│         tmux-dashboard container        │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  │
│  │ FastAPI │  │Collector│  │Frontend │  │
│  │   API   │←→│  (SSH)  │  │ (HTML)  │  │
│  └─────────┘  └─────────┘  └─────────┘  │
│       ↓            ↓                    │
│  ┌─────────────────────────────────┐    │
│  │           SQLite DB             │    │
│  └─────────────────────────────────┘    │
└─────────────────────────────────────────┘
            │ SSH
    ┌───────┴───────┬───────────┐
    ▼               ▼           ▼
  [pve1]         [pve2]      [pi-01]
```

## Requirements

- Proxmox VE 7+ (for LXC container)
- Python 3.11+
- NVIDIA drivers (on hosts with GPUs, for nvidia-smi)
- tmux (on monitored hosts)

## License

MIT
