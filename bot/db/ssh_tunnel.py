"""SSH tunnel for bastion-host database access using paramiko."""

import socket
import threading
from typing import Any

import paramiko

from ..config import get_config
from ..logging import get_logger

logger = get_logger(__name__)


class SSHTunnel:
    """SSH port-forwarding tunnel for database access through a bastion host."""

    def __init__(
        self,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        ssh_key_path: str,
        remote_host: str,
        remote_port: int,
        local_port: int = 0,
    ):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.ssh_key_path = ssh_key_path
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port = local_port

        self._client: paramiko.SSHClient | None = None
        self._transport: paramiko.Transport | None = None
        self._server_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> int:
        """Start the SSH tunnel and return the local port."""
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        logger.info(f"Connecting to SSH host {self.ssh_host}:{self.ssh_port}")
        self._client.connect(
            hostname=self.ssh_host,
            port=self.ssh_port,
            username=self.ssh_user,
            key_filename=self.ssh_key_path,
        )

        self._transport = self._client.get_transport()
        if self._transport is None:
            raise RuntimeError("Failed to get SSH transport")

        # Create local server socket
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(("127.0.0.1", self.local_port))
        self._server_socket.listen(5)

        self.local_port = self._server_socket.getsockname()[1]
        logger.info(f"SSH tunnel listening on localhost:{self.local_port}")

        self._running = True
        self._thread = threading.Thread(target=self._forward_loop, daemon=True)
        self._thread.start()

        return self.local_port

    def _forward_loop(self) -> None:
        """Accept connections and forward them through the SSH tunnel."""
        while self._running and self._server_socket:
            try:
                self._server_socket.settimeout(1.0)
                try:
                    client_socket, addr = self._server_socket.accept()
                except TimeoutError:
                    continue

                logger.debug(f"Forwarding connection from {addr}")
                channel = self._transport.open_channel(
                    "direct-tcpip",
                    (self.remote_host, self.remote_port),
                    addr,
                )
                if channel is None:
                    client_socket.close()
                    continue

                # Start bidirectional forwarding
                threading.Thread(
                    target=self._forward_data,
                    args=(client_socket, channel),
                    daemon=True,
                ).start()

            except Exception as e:
                if self._running:
                    logger.error(f"Error in tunnel forward loop: {e}")

    def _forward_data(self, client_socket: socket.socket, channel: Any) -> None:
        """Forward data between client socket and SSH channel."""
        try:
            while True:
                # Check for data from client
                if client_socket.fileno() == -1:
                    break
                try:
                    client_socket.settimeout(0.1)
                    data = client_socket.recv(4096)
                    if not data:
                        break
                    channel.send(data)
                except TimeoutError:
                    pass

                # Check for data from channel
                if channel.recv_ready():
                    data = channel.recv(4096)
                    if not data:
                        break
                    client_socket.send(data)

        except Exception as e:
            logger.debug(f"Forward connection closed: {e}")
        finally:
            client_socket.close()
            channel.close()

    def stop(self) -> None:
        """Stop the SSH tunnel."""
        self._running = False

        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None

        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

        logger.info("SSH tunnel stopped")


def create_tunnel_if_configured() -> SSHTunnel | None:
    """Create an SSH tunnel if SSH_HOST is configured."""
    config = get_config()

    if not config.ssh_host:
        return None

    if not config.ssh_user or not config.ssh_key_path:
        raise ValueError("SSH_USER and SSH_KEY_PATH required when SSH_HOST is set")

    # Parse remote host/port from DB_SYNC_URL
    # Expected format: postgresql://user:pass@host:port/dbname
    import urllib.parse

    parsed = urllib.parse.urlparse(config.db_sync_url)
    remote_host = parsed.hostname or "localhost"
    remote_port = parsed.port or 5432

    return SSHTunnel(
        ssh_host=config.ssh_host,
        ssh_port=config.ssh_port,
        ssh_user=config.ssh_user,
        ssh_key_path=str(config.ssh_key_path),
        remote_host=remote_host,
        remote_port=remote_port,
    )
