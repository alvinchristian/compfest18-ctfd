"""
Port Manager - Manage port allocation
"""
import logging
import random
import time

from CTFd.models import db

from ..models.config import ContainerConfig

logger = logging.getLogger(__name__)


class NoAvailablePort(Exception):
    pass


class PortManager:
    """
    Manage port pool

    Ports are configured in config (e.g., 30000-31000)
    This service tracks which ports are in use
    """

    def __init__(
        self,
        port_range_start=30000,
        port_range_end=31000,
        start_config_key="port_range_start",
        end_config_key="port_range_end",
    ):
        """
        Initialize port manager

        Args:
            port_range_start: Start of port range
            port_range_end: End of port range
        """
        self.port_range_start = port_range_start
        self.port_range_end = port_range_end
        self.start_config_key = start_config_key
        self.end_config_key = end_config_key

        # Load from config
        config_start = ContainerConfig.get(self.start_config_key)
        config_end = ContainerConfig.get(self.end_config_key)

        if config_start:
            self.port_range_start = int(config_start)
        if config_end:
            self.port_range_end = int(config_end)

    def allocate_port(self, retry_seconds=0) -> int:
        """
        Allocate next available port

        Returns:
            Port number

        Raises:
            Exception if no ports available
        """
        from ..models.instance import ContainerInstance

        # Configuration can be changed from the admin dashboard while the
        # plugin process is running. Refresh the range before every
        # allocation so newly-created instances never use a stale range.
        self._refresh_from_config()
        deadline = time.monotonic() + max(0, retry_seconds)
        while True:
            used_ports = db.session.query(ContainerInstance.connection_port).filter(
                ContainerInstance.status.in_(["running", "provisioning", "stopping"]),
                ContainerInstance.connection_port.isnot(None),
            ).all()
            used_ports_set = {port[0] for port in used_ports if port[0]}
            available = [p for p in range(self.port_range_start, self.port_range_end + 1) if p not in used_ports_set]
            if available:
                port = random.choice(available)
                logger.info(f"Allocated port {port}")
                return port
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        raise NoAvailablePort(
            f"No available ports in range {self.port_range_start}-{self.port_range_end}"
        )

    def _refresh_from_config(self):
        """Refresh the configured port range without replacing the service."""
        config_start = ContainerConfig.get(self.start_config_key)
        config_end = ContainerConfig.get(self.end_config_key)
        try:
            if config_start is not None:
                self.port_range_start = int(config_start)
            if config_end is not None:
                self.port_range_end = int(config_end)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid port range configuration; retaining %s-%s",
                self.port_range_start,
                self.port_range_end,
            )

    def release_port(self, port: int):
        """
        Release a port (currently this is implicit when instance status changes)

        Args:
            port: Port to release
        """
        logger.info(f"Released port {port}")
        # Port is automatically released when instance status changes
        # No explicit action needed

    def get_available_count(self) -> int:
        """Get number of available ports"""
        from ..models.instance import ContainerInstance

        self._refresh_from_config()
        used_count = (
            db.session.query(ContainerInstance)
            .filter(
                ContainerInstance.status.in_(["running", "provisioning", "stopping"]),
                ContainerInstance.connection_port.isnot(None),
            )
            .count()
        )

        total_ports = self.port_range_end - self.port_range_start + 1
        return total_ports - used_count
