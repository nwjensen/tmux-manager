#!/bin/bash
#
# deploy-key.sh - Deploy SSH public key to multiple fleet hosts
# 
# Usage: ./deploy-key.sh <public_key_file> <remote_user> <host1> [host2] [host3] ...
#
# Example:
#   ./deploy-key.sh /opt/tmux-dashboard/.ssh/id_ed25519.pub tmux-dash pve1 pve2 pi-01
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
fail() { echo -e "${RED}[✗]${NC} $1"; }

# Check arguments
if [ $# -lt 3 ]; then
    echo "Usage: $0 <public_key_file> <remote_user> <host1> [host2] [host3] ..."
    echo ""
    echo "Arguments:"
    echo "  public_key_file  Path to the SSH public key to deploy"
    echo "  remote_user      Username to create/use on remote hosts"
    echo "  host1, host2...  Hostnames or IP addresses of fleet hosts"
    echo ""
    echo "Example:"
    echo "  $0 /opt/tmux-dashboard/.ssh/id_ed25519.pub tmux-dash 192.168.1.10 192.168.1.11"
    exit 1
fi

KEY_FILE="$1"
REMOTE_USER="$2"
shift 2
HOSTS=("$@")

# Validate key file
if [ ! -f "$KEY_FILE" ]; then
    fail "Public key file not found: $KEY_FILE"
    exit 1
fi

PUBKEY=$(cat "$KEY_FILE")
echo ""
echo "=============================================="
echo "  SSH Key Deployment to Fleet"
echo "=============================================="
echo ""
echo "Key file:    $KEY_FILE"
echo "Remote user: $REMOTE_USER"
echo "Hosts:       ${HOSTS[*]}"
echo ""
echo "This will:"
echo "  1. Create user '$REMOTE_USER' on each host (if needed)"
echo "  2. Add the public key to authorized_keys"
echo "  3. Test key-based authentication"
echo ""
echo "You will be prompted for the ROOT password of each host."
echo ""
read -p "Continue? (y/N) " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

echo ""

# Track results
SUCCEEDED=()
FAILED=()

for HOST in "${HOSTS[@]}"; do
    echo "----------------------------------------------"
    echo "Processing: $HOST"
    echo "----------------------------------------------"
    
    # Deploy key via SSH
    if ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$HOST" bash << REMOTE_EOF
        set -e
        
        # Create user if doesn't exist
        if ! id "$REMOTE_USER" &>/dev/null; then
            useradd --system --create-home --shell /bin/bash "$REMOTE_USER"
            echo "Created user $REMOTE_USER"
        else
            echo "User $REMOTE_USER already exists"
        fi
        
        # Get user's home directory
        USER_HOME=\$(eval echo ~$REMOTE_USER)
        
        # Set up SSH directory
        mkdir -p "\$USER_HOME/.ssh"
        chmod 700 "\$USER_HOME/.ssh"
        
        # Check if key already exists
        if [ -f "\$USER_HOME/.ssh/authorized_keys" ] && grep -q "$PUBKEY" "\$USER_HOME/.ssh/authorized_keys" 2>/dev/null; then
            echo "Key already present in authorized_keys"
        else
            echo "$PUBKEY" >> "\$USER_HOME/.ssh/authorized_keys"
            echo "Key added to authorized_keys"
        fi
        
        # Set permissions
        chmod 600 "\$USER_HOME/.ssh/authorized_keys"
        chown -R "$REMOTE_USER:$REMOTE_USER" "\$USER_HOME/.ssh"
        
        echo "SSH configuration complete"
REMOTE_EOF
    then
        info "Key deployed to $HOST"
        
        # Test key-based authentication
        echo "Testing key-based authentication..."
        
        # Get the private key path (remove .pub extension)
        PRIVATE_KEY="${KEY_FILE%.pub}"
        
        if ssh -o StrictHostKeyChecking=accept-new \
               -o BatchMode=yes \
               -o ConnectTimeout=5 \
               -i "$PRIVATE_KEY" \
               "$REMOTE_USER@$HOST" "echo 'Authentication successful'" 2>/dev/null; then
            info "Key-based auth working for $HOST"
            SUCCEEDED+=("$HOST")
        else
            warn "Key deployed but auth test failed for $HOST"
            warn "The dashboard service user may need to run the test"
            SUCCEEDED+=("$HOST (needs verification)")
        fi
    else
        fail "Failed to deploy key to $HOST"
        FAILED+=("$HOST")
    fi
    
    echo ""
done

# Summary
echo "=============================================="
echo "  Deployment Summary"
echo "=============================================="
echo ""

if [ ${#SUCCEEDED[@]} -gt 0 ]; then
    echo -e "${GREEN}Succeeded:${NC}"
    for host in "${SUCCEEDED[@]}"; do
        echo "  ✓ $host"
    done
    echo ""
fi

if [ ${#FAILED[@]} -gt 0 ]; then
    echo -e "${RED}Failed:${NC}"
    for host in "${FAILED[@]}"; do
        echo "  ✗ $host"
    done
    echo ""
fi

echo "Total: ${#SUCCEEDED[@]} succeeded, ${#FAILED[@]} failed"
echo ""

if [ ${#FAILED[@]} -gt 0 ]; then
    exit 1
fi
