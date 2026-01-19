"""
SSH-based data collector for fleet hosts
Collects system metrics, tmux sessions, and GPU stats
"""

import asyncio
import asyncssh
import logging
from datetime import datetime
from typing import Optional
from pathlib import Path

from .models import (
    Host, HostStatus, Session, SessionStatus,
    GPU, GPUProcess, HostConfig, SSHConfig
)

logger = logging.getLogger(__name__)


class SSHCollector:
    """Collects data from remote hosts via SSH"""
    
    def __init__(self, ssh_config: SSHConfig, legacy_threshold_hours: int = 72):
        self.ssh_config = ssh_config
        self.legacy_threshold_hours = legacy_threshold_hours
        self.legacy_threshold_seconds = legacy_threshold_hours * 3600
    
    async def collect_host(self, host_config: HostConfig) -> Host:
        """Collect all data from a single host"""
        host = Host(
            hostname=host_config.name,
            address=host_config.address,
            has_gpu=host_config.has_gpu,
            tags=host_config.tags,
        )
        
        try:
            async with await self._connect(host_config.address) as conn:
                host.status = HostStatus.ONLINE
                host.last_seen = datetime.utcnow()
                
                # Collect system metrics
                await self._collect_system_metrics(conn, host)
                
                # Collect tmux sessions
                await self._collect_sessions(conn, host)
                
                # Collect GPU metrics if applicable
                if host_config.has_gpu:
                    await self._collect_gpu_metrics(conn, host)
                
        except asyncssh.Error as e:
            logger.warning(f"SSH error connecting to {host_config.name}: {e}")
            host.status = HostStatus.OFFLINE
            host.error_message = str(e)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout connecting to {host_config.name}")
            host.status = HostStatus.OFFLINE
            host.error_message = "Connection timeout"
        except Exception as e:
            logger.error(f"Unexpected error collecting from {host_config.name}: {e}")
            host.status = HostStatus.DEGRADED
            host.error_message = str(e)
        
        return host
    
    async def _connect(self, address: str) -> asyncssh.SSHClientConnection:
        """Create SSH connection to host"""
        key_path = Path(self.ssh_config.key_path)
        
        connect_options = {
            'host': address,
            'username': self.ssh_config.user,
            'client_keys': [str(key_path)] if key_path.exists() else None,
            'known_hosts': None,  # Accept any host key (configure properly in production)
            'connect_timeout': self.ssh_config.timeout,
        }
        
        return await asyncssh.connect(**connect_options)
    
    async def _run_command(self, conn: asyncssh.SSHClientConnection, cmd: str) -> str:
        """Run a command and return stdout"""
        try:
            result = await conn.run(cmd, check=False, timeout=10)
            return result.stdout.strip() if result.stdout else ""
        except Exception as e:
            logger.debug(f"Command failed: {cmd}: {e}")
            return ""
    
    async def _collect_system_metrics(self, conn: asyncssh.SSHClientConnection, host: Host):
        """Collect CPU, memory, and load average"""
        # CPU usage (using /proc/stat for more reliable results)
        cpu_cmd = "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | cut -d'%' -f1"
        cpu_output = await self._run_command(conn, cpu_cmd)
        try:
            host.cpu_percent = float(cpu_output) if cpu_output else 0.0
        except ValueError:
            # Fallback: try alternative parsing
            cpu_cmd_alt = "grep 'cpu ' /proc/stat | awk '{usage=($2+$4)*100/($2+$4+$5)} END {print usage}'"
            cpu_output = await self._run_command(conn, cpu_cmd_alt)
            try:
                host.cpu_percent = float(cpu_output) if cpu_output else 0.0
            except ValueError:
                host.cpu_percent = 0.0
        
        # Memory usage
        mem_cmd = "free -m | awk 'NR==2{printf \"%d %d %.1f\", $3, $2, $3*100/$2}'"
        mem_output = await self._run_command(conn, mem_cmd)
        try:
            parts = mem_output.split()
            if len(parts) >= 3:
                host.memory_used_mb = int(parts[0])
                host.memory_total_mb = int(parts[1])
                host.memory_percent = float(parts[2])
        except (ValueError, IndexError):
            pass
        
        # Load average
        load_cmd = "cat /proc/loadavg | awk '{print $1, $2, $3}'"
        load_output = await self._run_command(conn, load_cmd)
        try:
            parts = load_output.split()
            if len(parts) >= 3:
                host.load_avg = (float(parts[0]), float(parts[1]), float(parts[2]))
        except (ValueError, IndexError):
            pass
    
    async def _collect_sessions(self, conn: asyncssh.SSHClientConnection, host: Host):
        """Collect tmux sessions"""
        # List sessions with metadata
        # Format: name|created_timestamp|attached(0/1)|window_count|last_activity
        sessions_cmd = (
            "tmux list-sessions -F "
            "'#{session_name}|#{session_created}|#{session_attached}|#{session_windows}|#{session_activity}' "
            "2>/dev/null || echo ''"
        )
        sessions_output = await self._run_command(conn, sessions_cmd)
        
        if not sessions_output:
            return
        
        now = datetime.utcnow()
        
        for line in sessions_output.split('\n'):
            if not line or '|' not in line:
                continue
            
            try:
                parts = line.split('|')
                if len(parts) < 5:
                    continue
                
                name = parts[0]
                created_ts = int(parts[1]) if parts[1] else 0
                attached = parts[2] == '1'
                window_count = int(parts[3]) if parts[3] else 1
                activity_ts = int(parts[4]) if parts[4] else 0
                
                created = datetime.utcfromtimestamp(created_ts) if created_ts else None
                last_activity = datetime.utcfromtimestamp(activity_ts) if activity_ts else None
                
                # Determine session status
                status = SessionStatus.ACTIVE
                if not attached and last_activity:
                    detached_seconds = (now - last_activity).total_seconds()
                    if detached_seconds > self.legacy_threshold_seconds:
                        status = SessionStatus.LEGACY
                
                session = Session(
                    id=f"{host.hostname}:{name}",
                    host=host.hostname,
                    name=name,
                    created=created,
                    last_activity=last_activity,
                    attached=attached,
                    window_count=window_count,
                    status=status,
                )
                
                host.sessions.append(session)
                
            except Exception as e:
                logger.debug(f"Failed to parse session line '{line}': {e}")
                continue
    
    async def _collect_gpu_metrics(self, conn: asyncssh.SSHClientConnection, host: Host):
        """Collect NVIDIA GPU metrics"""
        # GPU info
        gpu_cmd = (
            "nvidia-smi --query-gpu=index,name,power.draw,power.limit,"
            "memory.used,memory.total,utilization.gpu,temperature.gpu "
            "--format=csv,noheader,nounits 2>/dev/null || echo ''"
        )
        gpu_output = await self._run_command(conn, gpu_cmd)
        
        if not gpu_output:
            return
        
        # GPU processes
        proc_cmd = (
            "nvidia-smi --query-compute-apps=pid,process_name,used_memory "
            "--format=csv,noheader,nounits 2>/dev/null || echo ''"
        )
        proc_output = await self._run_command(conn, proc_cmd)
        
        # Parse processes first
        processes_by_gpu: dict[int, list[GPUProcess]] = {}
        if proc_output:
            for line in proc_output.split('\n'):
                if not line or ',' not in line:
                    continue
                try:
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) >= 3:
                        proc = GPUProcess(
                            pid=int(parts[0]),
                            name=parts[1],
                            memory_mb=int(parts[2])
                        )
                        # For simplicity, associate with GPU 0 (enhance later if needed)
                        processes_by_gpu.setdefault(0, []).append(proc)
                except (ValueError, IndexError):
                    continue
        
        # Parse GPU info
        for line in gpu_output.split('\n'):
            if not line or ',' not in line:
                continue
            
            try:
                parts = [p.strip() for p in line.split(',')]
                if len(parts) < 8:
                    continue
                
                gpu_index = int(parts[0])
                gpu = GPU(
                    index=gpu_index,
                    name=parts[1],
                    power_draw_watts=float(parts[2]) if parts[2] else 0.0,
                    power_limit_watts=float(parts[3]) if parts[3] else 0.0,
                    memory_used_mb=int(float(parts[4])) if parts[4] else 0,
                    memory_total_mb=int(float(parts[5])) if parts[5] else 0,
                    utilization_percent=int(parts[6]) if parts[6] else 0,
                    temperature_c=int(parts[7]) if parts[7] else 0,
                    processes=processes_by_gpu.get(gpu_index, [])
                )
                
                host.gpus.append(gpu)
                
            except Exception as e:
                logger.debug(f"Failed to parse GPU line '{line}': {e}")
                continue


class FleetCollector:
    """Manages collection across all fleet hosts"""
    
    def __init__(self, ssh_config: SSHConfig, hosts: list[HostConfig], legacy_threshold_hours: int = 72):
        self.ssh_collector = SSHCollector(ssh_config, legacy_threshold_hours)
        self.hosts = hosts
    
    async def collect_all(self) -> list[Host]:
        """Collect data from all hosts in parallel"""
        tasks = [
            self.ssh_collector.collect_host(host_config)
            for host_config in self.hosts
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        hosts = []
        for result, host_config in zip(results, self.hosts):
            if isinstance(result, Exception):
                logger.error(f"Collection failed for {host_config.name}: {result}")
                hosts.append(Host(
                    hostname=host_config.name,
                    address=host_config.address,
                    has_gpu=host_config.has_gpu,
                    tags=host_config.tags,
                    status=HostStatus.OFFLINE,
                    error_message=str(result)
                ))
            else:
                hosts.append(result)
        
        return hosts
