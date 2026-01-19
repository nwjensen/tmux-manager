#!/bin/bash
#
# provision.sh - Provision the TMUX Fleet Dashboard inside the LXC container
# Run this script inside the container as root
#
# This script:
#   1. Installs system dependencies
#   2. Creates the tmux-dash service account
#   3. Sets up Python virtual environment
#   4. Installs Python dependencies
#   5. Generates SSH keypair for fleet access
#   6. Creates systemd service
#   7. Sets up directory structure
#

set -e

# Configuration
APP_DIR="/opt/tmux-dashboard"
APP_USER="tmux-dash"
APP_GROUP="tmux-dash"
VENV_DIR="$APP_DIR/venv"
SSH_DIR="$APP_DIR/.ssh"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step() { echo -e "${CYAN}[STEP]${NC} $1"; }

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    error "Please run as root"
fi

echo ""
echo "=============================================="
echo "  TMUX Fleet Dashboard - Provisioning"
echo "=============================================="
echo ""

# Step 1: Install system dependencies
step "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    openssh-client \
    curl \
    > /dev/null 2>&1
info "System dependencies installed"

# Step 2: Create application user
step "Creating application user: $APP_USER"
if id "$APP_USER" &>/dev/null; then
    info "User $APP_USER already exists"
else
    useradd --system --home-dir "$APP_DIR" --shell /bin/bash "$APP_USER"
    info "User $APP_USER created"
fi

# Step 3: Set up directory structure
step "Setting up directory structure..."
mkdir -p "$APP_DIR"/{backend,frontend,config,data}
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

# Copy application files if we're running from a different directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(dirname "$SCRIPT_DIR")"

if [ "$SOURCE_DIR" != "$APP_DIR" ]; then
    info "Copying application files from $SOURCE_DIR to $APP_DIR"
    cp -r "$SOURCE_DIR"/backend/* "$APP_DIR/backend/" 2>/dev/null || true
    cp -r "$SOURCE_DIR"/frontend/* "$APP_DIR/frontend/" 2>/dev/null || true
    cp "$SOURCE_DIR"/requirements.txt "$APP_DIR/" 2>/dev/null || true
    cp "$SOURCE_DIR"/config/*.example "$APP_DIR/config/" 2>/dev/null || true
    cp "$SOURCE_DIR"/scripts/*.sh "$APP_DIR/scripts/" 2>/dev/null || mkdir -p "$APP_DIR/scripts"
fi

info "Directory structure created"

# Step 4: Generate SSH keypair
step "Generating SSH keypair for fleet access..."
SSH_KEY="$SSH_DIR/id_ed25519"

if [ -f "$SSH_KEY" ]; then
    warn "SSH key already exists at $SSH_KEY"
    read -p "Regenerate? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -f "$SSH_KEY" "$SSH_KEY.pub"
        ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "tmux-dashboard@$(hostname)"
        info "New SSH keypair generated"
    fi
else
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "tmux-dashboard@$(hostname)"
    info "SSH keypair generated"
fi

# Step 5: Set up Python virtual environment
step "Setting up Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

# Upgrade pip
pip install --upgrade pip -q

# Install dependencies
if [ -f "$APP_DIR/requirements.txt" ]; then
    pip install -r "$APP_DIR/requirements.txt" -q
    info "Python dependencies installed"
else
    warn "requirements.txt not found - install dependencies manually"
fi

deactivate

# Step 6: Create example config if needed
step "Setting up configuration..."
CONFIG_FILE="$APP_DIR/config/hosts.yml"
EXAMPLE_CONFIG="$APP_DIR/config/hosts.yml.example"

if [ ! -f "$EXAMPLE_CONFIG" ]; then
    cat > "$EXAMPLE_CONFIG" << 'EOF'
# TMUX Fleet Dashboard Configuration

polling_interval_seconds: 30
legacy_threshold_hours: 72

alerts:
  enabled: true
  # Session thresholds
  session_cpu_warning: 80
  session_memory_mb_warning: 2048
  # Host thresholds  
  host_cpu_warning: 90
  host_memory_warning: 90
  # GPU thresholds
  gpu_temp_warning: 80
  gpu_temp_critical: 90
  gpu_memory_warning: 90
  gpu_util_info: 95

ssh:
  user: tmux-dash
  key_path: /opt/tmux-dashboard/.ssh/id_ed25519
  timeout: 10

hosts:
  # Example hosts - update with your actual hosts
  - name: pve1
    address: 192.168.1.10
    has_gpu: true
    
  - name: pve2
    address: 192.168.1.11
    has_gpu: true
    
  - name: pi-01
    address: 192.168.1.50
    has_gpu: false
    tags:
      - raspberry-pi
      - edge
EOF
    info "Example configuration created"
fi

if [ ! -f "$CONFIG_FILE" ]; then
    cp "$EXAMPLE_CONFIG" "$CONFIG_FILE"
    warn "Created hosts.yml from example - edit this with your actual hosts!"
fi

# Step 7: Create systemd service
step "Creating systemd service..."
cat > /etc/systemd/system/tmux-dashboard.service << EOF
[Unit]
Description=TMUX Fleet Dashboard
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
Environment="PATH=$VENV_DIR/bin:/usr/bin"
ExecStart=$VENV_DIR/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
info "Systemd service created"

# Step 8: Set ownership
step "Setting file ownership..."
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
chmod 600 "$SSH_KEY"
chmod 644 "$SSH_KEY.pub"
info "Ownership configured"

# Step 9: Create helper scripts
step "Creating helper scripts..."

# Script to deploy SSH key
cat > "$APP_DIR/scripts/deploy-key-to-host.sh" << 'DEPLOY_EOF'
#!/bin/bash
# Deploy the dashboard's SSH key to a remote host
# Usage: ./deploy-key-to-host.sh <host> [user]

HOST=$1
USER=${2:-tmux-dash}
KEY_FILE="/opt/tmux-dashboard/.ssh/id_ed25519.pub"

if [ -z "$HOST" ]; then
    echo "Usage: $0 <host> [user]"
    exit 1
fi

if [ ! -f "$KEY_FILE" ]; then
    echo "Error: Public key not found at $KEY_FILE"
    exit 1
fi

echo "Deploying key to $USER@$HOST..."
echo "You may be prompted for the root password of the remote host."
echo ""

# Read the public key
PUBKEY=$(cat "$KEY_FILE")

# SSH to remote and set up user + key
ssh -o StrictHostKeyChecking=accept-new "root@$HOST" bash << REMOTE_EOF
    # Create user if doesn't exist
    if ! id "$USER" &>/dev/null; then
        useradd --system --create-home --shell /bin/bash "$USER"
        echo "Created user $USER"
    fi
    
    # Set up SSH directory
    USER_HOME=\$(eval echo ~$USER)
    mkdir -p "\$USER_HOME/.ssh"
    chmod 700 "\$USER_HOME/.ssh"
    
    # Add public key
    echo "$PUBKEY" >> "\$USER_HOME/.ssh/authorized_keys"
    chmod 600 "\$USER_HOME/.ssh/authorized_keys"
    chown -R "$USER:$USER" "\$USER_HOME/.ssh"
    
    echo "Key deployed successfully"
REMOTE_EOF

echo ""
echo "Testing connection..."
sudo -u tmux-dash ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes "$USER@$HOST" "echo 'Connection successful!'" && \
    echo "✓ Key-based authentication working" || \
    echo "✗ Connection test failed"
DEPLOY_EOF

chmod +x "$APP_DIR/scripts/deploy-key-to-host.sh"
chown "$APP_USER:$APP_GROUP" "$APP_DIR/scripts/deploy-key-to-host.sh"

info "Helper scripts created"

# Summary
echo ""
echo "=============================================="
echo -e "${GREEN}Provisioning complete!${NC}"
echo "=============================================="
echo ""
echo "Application directory: $APP_DIR"
echo "Service user:          $APP_USER"
echo "Python venv:           $VENV_DIR"
echo ""
echo -e "${CYAN}SSH Public Key (deploy this to your fleet):${NC}"
echo "----------------------------------------------"
cat "$SSH_KEY.pub"
echo "----------------------------------------------"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo ""
echo "1. Deploy the SSH key to each host in your fleet:"
echo "   $APP_DIR/scripts/deploy-key-to-host.sh <hostname>"
echo ""
echo "2. Edit the configuration file:"
echo "   nano $APP_DIR/config/hosts.yml"
echo ""
echo "3. Start the dashboard:"
echo "   systemctl start tmux-dashboard"
echo "   systemctl enable tmux-dashboard"
echo ""
echo "4. Check the logs:"
echo "   journalctl -u tmux-dashboard -f"
echo ""
echo "5. Access the dashboard:"
echo "   http://$(hostname -I | awk '{print $1}'):8080"
echo ""
