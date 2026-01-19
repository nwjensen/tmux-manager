#!/bin/bash
#
# create-lxc.sh - Create LXC container for TMUX Fleet Dashboard
# Run this script on pve1 as root
#
# Usage: ./create-lxc.sh [CTID]
#        CTID defaults to 200 if not specified
#

set -e

# Configuration
CTID=${1:-200}
HOSTNAME="tmux-dashboard"
TEMPLATE_NAME="debian-12-standard"
STORAGE="local-lvm"          # Change if your storage is different
TEMPLATE_STORAGE="local"     # Where templates are stored
MEMORY=1024                  # MB
SWAP=512                     # MB
CORES=1
DISK=8                       # GB
BRIDGE="vmbr0"
ROOT_PASSWORD="changeme"     # Change this or use SSH keys

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Check if running on Proxmox
if ! command -v pct &> /dev/null; then
    error "This script must be run on a Proxmox VE host"
fi

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    error "Please run as root"
fi

# Check if CTID is already in use
if pct status $CTID &> /dev/null; then
    error "Container ID $CTID already exists. Choose a different ID or remove the existing container."
fi

info "Creating TMUX Fleet Dashboard container (CTID: $CTID)"

# Find the Debian 12 template
info "Looking for Debian 12 template..."
TEMPLATE=$(pveam list $TEMPLATE_STORAGE 2>/dev/null | grep -i "$TEMPLATE_NAME" | tail -1 | awk '{print $1}')

if [ -z "$TEMPLATE" ]; then
    info "Template not found locally. Downloading..."
    
    # Update template list
    pveam update
    
    # Find available Debian 12 template
    AVAILABLE_TEMPLATE=$(pveam available | grep -i "debian-12-standard" | tail -1 | awk '{print $2}')
    
    if [ -z "$AVAILABLE_TEMPLATE" ]; then
        error "Could not find Debian 12 template. Please download manually."
    fi
    
    info "Downloading template: $AVAILABLE_TEMPLATE"
    pveam download $TEMPLATE_STORAGE $AVAILABLE_TEMPLATE
    
    TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${AVAILABLE_TEMPLATE}"
else
    info "Found template: $TEMPLATE"
fi

# Create the container
info "Creating container..."
pct create $CTID "$TEMPLATE" \
    --hostname $HOSTNAME \
    --memory $MEMORY \
    --swap $SWAP \
    --cores $CORES \
    --rootfs ${STORAGE}:${DISK} \
    --net0 name=eth0,bridge=$BRIDGE,ip=dhcp \
    --onboot 1 \
    --start 0 \
    --unprivileged 1 \
    --features nesting=1 \
    --password "$ROOT_PASSWORD"

info "Container created successfully"

# Start the container
info "Starting container..."
pct start $CTID

# Wait for container to fully start
info "Waiting for container to initialize..."
sleep 5

# Wait for network (DHCP)
info "Waiting for DHCP lease..."
MAX_WAIT=60
WAITED=0
IP_ADDR=""

while [ $WAITED -lt $MAX_WAIT ]; do
    # Try to get IP from container
    IP_ADDR=$(pct exec $CTID -- ip -4 addr show eth0 2>/dev/null | grep -oP 'inet \K[\d.]+' || true)
    
    if [ -n "$IP_ADDR" ]; then
        break
    fi
    
    sleep 2
    WAITED=$((WAITED + 2))
    echo -n "."
done
echo ""

if [ -z "$IP_ADDR" ]; then
    warn "Could not determine IP address. Container may still be acquiring DHCP lease."
    warn "Check manually with: pct exec $CTID -- ip addr"
else
    info "Container IP address: $IP_ADDR"
fi

# Enable and configure SSH inside container
info "Configuring SSH access..."
pct exec $CTID -- bash -c "
    apt-get update -qq
    apt-get install -y -qq openssh-server > /dev/null 2>&1
    
    # Enable root login with password (for initial setup only)
    sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
    sed -i 's/PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
    
    # Enable password authentication
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
    sed -i 's/PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
    
    systemctl enable ssh
    systemctl restart ssh
"

info "SSH configured and enabled"

# Summary
echo ""
echo "=============================================="
echo -e "${GREEN}Container created successfully!${NC}"
echo "=============================================="
echo ""
echo "Container ID:   $CTID"
echo "Hostname:       $HOSTNAME"
echo "IP Address:     ${IP_ADDR:-'Pending DHCP'}"
echo "Root Password:  $ROOT_PASSWORD"
echo ""
echo "Next steps:"
echo "  1. SSH into the container:"
echo "     ssh root@${IP_ADDR:-<container-ip>}"
echo ""
echo "  2. Copy the tmux-dashboard files to /opt/tmux-dashboard/"
echo ""
echo "  3. Run the provisioning script:"
echo "     cd /opt/tmux-dashboard && ./scripts/provision.sh"
echo ""
if [ "$ROOT_PASSWORD" = "changeme" ]; then
    warn "Remember to change the root password!"
fi
