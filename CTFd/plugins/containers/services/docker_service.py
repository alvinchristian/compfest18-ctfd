"""
Docker Service - Manage Docker containers
"""
import logging
import io
import posixpath
import tarfile
import time
from typing import Any, Dict, Optional

import docker

from ..models.challenge import ALLOWED_CONTAINER_CAPABILITIES

logger = logging.getLogger(__name__)

AUTH_PROXY_SCRIPT = r'''
import base64
import json
import os
import selectors
import select
import socket
import threading
import html
import urllib.parse
import urllib.request

VALIDATE_URL = os.environ["VALIDATE_URL"]
PROXY_SECRET = os.environ["PROXY_SECRET"]
PORT_RANGE_START = int(os.environ["PORT_RANGE_START"])
PORT_RANGE_END = int(os.environ["PORT_RANGE_END"])


def call_ctfd(public_port, token=None):
    payload = {
        "secret": PROXY_SECRET,
        "port": public_port,
    }
    if token:
        payload["token"] = token.strip()

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        VALIDATE_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception:
        return {"allowed": False}


def lookup_route(public_port):
    data = call_ctfd(public_port)
    # An unused or expired public port is returned without a connection type.
    # Do not fall back to TCP here: every port in the configured range has a
    # listener, but only ports belonging to running instances should prompt
    # for credentials.
    if not data.get("type"):
        return None
    return data["type"]


def validate_token(public_port, token):
    if not token:
        return None
    payload = json.dumps(
        {
            "secret": PROXY_SECRET,
            "port": public_port,
            "token": token.strip(),
        }
    ).encode()
    req = urllib.request.Request(
        VALIDATE_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            if data.get("allowed") is True:
                return data
    except Exception:
        pass
    return None


def safe_send(sock, data):
    try:
        sock.sendall(data)
    except OSError:
        pass


def read_line(sock, limit=4096):
    data = bytearray()
    while len(data) < limit:
        chunk = sock.recv(1)
        if not chunk:
            break
        data.extend(chunk)
        if chunk == b"\n":
            break
    return bytes(data)


def read_until(sock, marker, limit=65536):
    data = bytearray()
    while len(data) < limit and marker not in data:
        chunk = sock.recv(min(4096, limit - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def read_http_request(sock):
    request_data = read_until(sock, b"\r\n\r\n")
    if not request_data:
        return b""

    head, separator, body = request_data.partition(b"\r\n\r\n")
    content_length = 0
    for line in head.decode("iso-8859-1", errors="replace").split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                content_length = 0
            break

    while len(body) < content_length and len(request_data) < 1048576:
        chunk = sock.recv(min(65536, content_length - len(body)))
        if not chunk:
            break
        body += chunk
        request_data += chunk

    return head + separator + body


def extract_http_token(request_data):
    try:
        head = request_data.decode("iso-8859-1", errors="replace")
        lines = head.split("\r\n")
        request_line = lines[0] if lines else ""
        for line in lines[1:]:
            if not line.lower().startswith("authorization:"):
                continue
            value = line.split(":", 1)[1].strip()
            if value.lower().startswith("bearer "):
                return value[7:].strip()
            if value.lower().startswith("basic "):
                raw = base64.b64decode(value[6:].strip()).decode(
                    "utf-8", errors="ignore"
                )
                if ":" in raw:
                    username, password = raw.split(":", 1)
                    return password or username
                return raw
        if "?" in request_line:
            target = request_line.split(" ", 2)[1]
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
            values = query.get("access_token") or query.get("token")
            if values:
                return values[0]
        for line in lines[1:]:
            if not line.lower().startswith("cookie:"):
                continue
            cookies = line.split(":", 1)[1].split(";")
            for cookie in cookies:
                if "=" not in cookie:
                    continue
                key, value = cookie.strip().split("=", 1)
                if key == "ctfd_proxy_token":
                    return urllib.parse.unquote(value)
    except Exception:
        pass
    return None


def get_http_target(request_data):
    try:
        request_line = request_data.decode("iso-8859-1", errors="replace").split(
            "\r\n", 1
        )[0]
        parts = request_line.split(" ", 2)
        if len(parts) >= 2:
            return parts[1]
    except Exception:
        pass
    return "/"


def is_auth_form_submission(request_data):
    try:
        request_line = request_data.decode("iso-8859-1", errors="replace").split(
            "\r\n", 1
        )[0]
        parts = request_line.split(" ", 2)
        return len(parts) >= 2 and parts[0] == "POST" and parts[1] == "/__ctfd_auth"
    except Exception:
        return False


def extract_form_auth(request_data):
    try:
        _, _, body = request_data.partition(b"\r\n\r\n")
        values = urllib.parse.parse_qs(body.decode("utf-8", errors="ignore"))
        token = values.get("access_token", [""])[0]
        next_url = values.get("next", ["/"])[0] or "/"
        if not next_url.startswith("/"):
            next_url = "/"
        return token, next_url
    except Exception:
        return "", "/"


def send_auth_form(client, next_url="/", error=None):
    escaped_next = html.escape(next_url, quote=True)
    error_html = ""
    if error:
        error_html = (
            '<p style="color:#b00020;margin:0 0 12px">Invalid access token.</p>'
        )
    body = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CTFd Access Token</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f6f7f9; color: #111827; }}
    main {{ width: min(420px, calc(100vw - 32px)); background: white; border: 1px solid #d1d5db; border-radius: 8px; padding: 24px; box-shadow: 0 8px 30px rgba(0,0,0,.08); }}
    label {{ display: block; font-weight: 600; margin-bottom: 8px; }}
    input {{ box-sizing: border-box; width: 100%; padding: 10px 12px; border: 1px solid #9ca3af; border-radius: 6px; font: inherit; }}
    button {{ margin-top: 14px; width: 100%; padding: 10px 12px; border: 0; border-radius: 6px; background: #1f2937; color: white; font: inherit; font-weight: 600; cursor: pointer; }}
    p {{ color: #4b5563; line-height: 1.4; }}
  </style>
</head>
<body>
  <main>
    <h1 style="font-size:20px;margin:0 0 8px">Challenge Access</h1>
    <p>Enter your CTFd access token to open this instance.</p>
    {error_html}
    <form method="post" action="/__ctfd_auth" autocomplete="off">
      <input type="hidden" name="next" value="{escaped_next}">
      <label for="access_token">Access token</label>
      <input id="access_token" name="access_token" type="password" autofocus required>
      <button type="submit">Continue</button>
    </form>
  </main>
</body>
</html>
""".encode("utf-8")
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Cache-Control: no-store\r\n"
        b"Connection: close\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )
    safe_send(client, response)


def send_auth_redirect(client, token, next_url):
    cookie = urllib.parse.quote(token, safe="")
    response = (
        b"HTTP/1.1 303 See Other\r\n"
        + f"Location: {next_url}\r\n".encode("iso-8859-1")
        + f"Set-Cookie: ctfd_proxy_token={cookie}; Path=/; HttpOnly; SameSite=Lax\r\n".encode(
            "ascii"
        )
        + b"Cache-Control: no-store\r\n"
        b"Content-Length: 0\r\n"
        b"Connection: close\r\n\r\n"
    )
    safe_send(client, response)


def strip_token_query(request_line):
    parts = request_line.split(" ", 2)
    if len(parts) < 2 or "?" not in parts[1]:
        return request_line
    url = urllib.parse.urlsplit(parts[1])
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(url.query, keep_blank_values=True)
        if key not in ("access_token", "token")
    ]
    parts[1] = urllib.parse.urlunsplit(
        (url.scheme, url.netloc, url.path, urllib.parse.urlencode(query), url.fragment)
    )
    return " ".join(parts)


def strip_http_token(request_data):
    try:
        head, separator, body = request_data.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
        if not lines:
            return request_data
        lines[0] = strip_token_query(lines[0])
        sanitized = [lines[0]]
        for line in lines[1:]:
            if line.lower().startswith("authorization:"):
                continue
            if line.lower().startswith("cookie:"):
                cookie_header = strip_proxy_cookie(line)
                if cookie_header:
                    sanitized.append(cookie_header)
                continue
            sanitized.append(line)
        return "\r\n".join(sanitized).encode("iso-8859-1") + separator + body
    except Exception:
        return request_data


def strip_proxy_cookie(line):
    try:
        cookies = line.split(":", 1)[1].split(";")
        kept = []
        for cookie in cookies:
            cookie = cookie.strip()
            if not cookie:
                continue
            if cookie.split("=", 1)[0] == "ctfd_proxy_token":
                continue
            kept.append(cookie)
        if not kept:
            return None
        return "Cookie: " + "; ".join(kept)
    except Exception:
        return line


def authenticate_tcp(client, public_port):
    safe_send(client, b"CTFd access token: ")
    token = read_line(client).decode("utf-8", errors="ignore").strip()
    route = validate_token(public_port, token)
    if route:
        return route, b""
    safe_send(client, b"Access denied.\n")
    return None, b""


def authenticate_http(client, public_port):
    request_data = read_http_request(client)
    if not request_data:
        return None, b""

    if is_auth_form_submission(request_data):
        token, next_url = extract_form_auth(request_data)
        route = validate_token(public_port, token)
        if route:
            send_auth_redirect(client, token, next_url)
            return None, b""
        send_auth_form(client, next_url=next_url, error=True)
        return None, b""

    token = extract_http_token(request_data)
    route = validate_token(public_port, token)
    if route:
        return route, strip_http_token(request_data)
    send_auth_form(client, next_url=get_http_target(request_data))
    return None, b""


def relay(client, upstream):
    client.setblocking(False)
    upstream.setblocking(False)
    sockets = [client, upstream]

    def send_all(sock, data):
        view = memoryview(data)
        while view:
            try:
                sent = sock.send(view)
            except (BlockingIOError, InterruptedError):
                try:
                    _, writable, _ = select.select([], [sock], [], 1)
                except (OSError, ValueError):
                    return False
                if not writable:
                    continue
                continue
            except OSError:
                return False
            if sent <= 0:
                return False
            view = view[sent:]
        return True

    while True:
        try:
            readable, _, exceptional = select.select(sockets, [], sockets, 1)
        except (OSError, ValueError):
            return
        if exceptional:
            return
        for source in readable:
            target = upstream if source is client else client
            try:
                data = source.recv(65536)
                if not data:
                    return
                if not send_all(target, data):
                    return
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return


def handle(client, public_port):
    with client:
        client.settimeout(30)
        proxy_type = lookup_route(public_port)
        if proxy_type is None:
            return
        if proxy_type == "http":
            route, initial_data = authenticate_http(client, public_port)
        else:
            route, initial_data = authenticate_tcp(client, public_port)
        if not route:
            return
        try:
            upstream = socket.create_connection(
                (route["target_host"], int(route["target_port"])), timeout=10
            )
        except OSError:
            safe_send(client, b"Upstream challenge is unavailable.\n")
            return
        with upstream:
            if initial_data:
                upstream.sendall(initial_data)
            relay(client, upstream)


def create_listener(public_port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", public_port))
    listener.listen(512)
    listener.setblocking(False)
    return listener


selector = selectors.DefaultSelector()
for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
    try:
        selector.register(create_listener(port), selectors.EVENT_READ, port)
    except OSError:
        pass

while True:
    for key, _ in selector.select(timeout=1):
        listener = key.fileobj
        public_port = key.data
        try:
            client, _ = listener.accept()
        except OSError:
            continue
        threading.Thread(
            target=handle, args=(client, public_port), daemon=True
        ).start()
'''


class DockerService:
    """
    Service to interact with Docker daemon
    """

    def __init__(self, base_url="unix://var/run/docker.sock"):
        """
        Initialize Docker client

        Args:
            base_url: Docker daemon URL
                     - Unix socket: 'unix://var/run/docker.sock' (default)
                     - TCP: 'tcp://192.168.1.100:2376'
                     - SSH: 'ssh://user@host:port' or 'ssh://user@host' (default port 22)
        """
        self.base_url = base_url
        self.client = None
        self._connect()

    def _connect(self):
        """Connect to Docker daemon - Don't raise exception, just log warning"""
        try:
            # Handle SSH connection
            if self.base_url.startswith("ssh://"):
                logger.info(f"Attempting SSH connection to Docker:" f"{self.base_url}")
                # docker-py supports ssh:// URLs directly
                # Format: ssh://user@host:port or ssh://user@host (default port 22)
                # SSH keys will be used from ~/.ssh/ or SSH agent
                self.client = docker.DockerClient(base_url=self.base_url, timeout=30)
            else:
                # Regular connection (Unix socket or TCP)
                self.client = docker.DockerClient(base_url=self.base_url, timeout=10)

            self.client.ping()
            logger.info(f"Connected to Docker daemon at {self.base_url}")
        except Exception as e:
            logger.warning(f"Failed to connect to Docker: {e}")
            logger.warning(
                "Docker connection will be retried when needed. Configure in plugin settings."
            )
            self.client = None
            # Don't raise - allow plugin to load even if Docker is unavailable

    def is_connected(self) -> bool:
        """Check if Docker is connected"""
        if not self.client:
            return False
        try:
            self.client.ping()
            return True
        except Exception as e:
            logger.error(f"Failed to ping to Docker: {e}")
            return False

    def create_container(
        self,
        image: str,
        internal_port: int,
        host_port: int = None,
        host_ip: str = None,
        command: str = None,
        environment: Dict[str, str] = None,
        memory_limit: str = "512m",
        cpu_limit: float = 0.5,
        pids_limit: int = 100,
        capabilities: list = None,
        no_new_privileges: bool = True,
        flag_file: Dict[str, Any] = None,
        labels: Dict[str, str] = None,
        name: str = None,
        network: str = None,
    ) -> Dict[str, Any]:
        """
        Create and start a container

        Args:
            image: Docker image name
            internal_port: Port inside container
            host_port: Port on host to expose
            host_ip: Optional host interface to bind the exposed port
            command: Command to run
            environment: Environment variables
            memory_limit: Memory limit (e.g., "512m", "1g")
            cpu_limit: CPU limit (0.5 = 50% of one core)
            pids_limit: Max number of processes
            capabilities: Additional allowlisted Linux capabilities
            labels: Labels for the container
            name: Container name (optional)

        Returns:
            {
                'container_id': str,
                'status': str,
                'port': int
            }
        """
        if not self.is_connected():
            raise Exception("Docker is not connected")

        container = None
        try:
            # CPU quota calculation
            cpu_period = 100000  # Docker default
            cpu_quota = int(cpu_limit * cpu_period)

            # Labels for management
            container_labels = labels or {}
            container_labels.update(
                {"ctfd.managed": "true", "ctfd.plugin": "containers"}
            )

            ports = None
            if host_port:
                port_binding = host_port
                if host_ip:
                    port_binding = (host_ip, host_port)
                ports = {f"{internal_port}/tcp": port_binding}

            kwargs = {}
            if network:
                kwargs["network"] = network
            else:
                kwargs["network_mode"] = "bridge"

            requested_capabilities = [str(capability).upper() for capability in (capabilities or [])]
            invalid_capabilities = [
                capability
                for capability in requested_capabilities
                if capability not in ALLOWED_CONTAINER_CAPABILITIES
            ]
            if invalid_capabilities:
                raise ValueError(
                    "Unsupported container capabilities: {}".format(
                        ", ".join(invalid_capabilities)
                    )
                )

            cap_add = list(dict.fromkeys(
                ["CHOWN", "SETUID", "SETGID"] + requested_capabilities
            ))

            container = self.client.containers.create(
                image=image,
                name=name,
                command=command,
                # Keep stopped containers until the lifecycle service has
                # captured their final logs and removed them explicitly.
                auto_remove=False,
                ports=ports,
                environment=environment or {},
                mem_limit=memory_limit,
                cpu_quota=cpu_quota,
                cpu_period=cpu_period,
                pids_limit=pids_limit,
                labels=container_labels,
                # Security options
                cap_drop=["ALL"],  # Drop all capabilities
                cap_add=cap_add,
                security_opt=["no-new-privileges"] if no_new_privileges else [],
                **kwargs,
            )

            if flag_file:
                self._inject_flag_file(container, flag_file)

            container.start()
            container.reload()

            logger.info(f"Created container {container.id[:12]} from image {image}")

            return {
                "container_id": container.id,
                "status": container.status,
                "port": host_port,
            }

        except docker.errors.ImageNotFound:
            logger.error(f"Docker image not found: {image}")
            raise Exception(f"Docker image '{image}' not found")
        except docker.errors.APIError as e:
            logger.error(f"Docker API error: {e}")
            raise Exception(f"Failed to create container: {e}")
        except Exception as e:
            logger.error(f"Unexpected error creating container: {e}")
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    logger.warning("Failed to remove partially-created container")
            raise

    def _inject_flag_file(self, container, flag_file):
        """Inject a flag file into a stopped container with image-user ownership."""
        path = str(flag_file.get("path", ""))
        normalized_path = posixpath.normpath(path)
        path_parts = normalized_path.split("/")
        if not normalized_path.startswith("/") or ".." in path_parts:
            raise ValueError("Flag file path must be an absolute path without '..'")

        parent = posixpath.dirname(normalized_path) or "/"
        filename = posixpath.basename(normalized_path)
        if not filename:
            raise ValueError("Flag file path must name a file")

        owner = str(flag_file.get("owner", "root"))
        group = str(flag_file.get("group", owner))
        uid, _ = self._resolve_container_identity(container, "/etc/passwd", owner, "user")
        gid, _ = self._resolve_container_identity(container, "/etc/group", group, "group")

        try:
            mode = int(str(flag_file.get("mode", "440")), 8)
        except ValueError:
            raise ValueError("Flag file mode must be an octal value such as 440")
        if mode < 0 or mode > 0o777:
            raise ValueError("Flag file mode must be between 000 and 777")

        content = str(flag_file.get("content", "")).encode("utf-8")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            entry = tarfile.TarInfo(name=filename)
            entry.size = len(content)
            entry.mode = mode
            entry.uid = uid
            entry.gid = gid
            entry.uname = owner
            entry.gname = group
            tar.addfile(entry, io.BytesIO(content))

        container.put_archive(parent, archive.getvalue())
        logger.info(
            "Injected dynamic flag file %s into container %s as %s:%s mode %s",
            normalized_path,
            container.id[:12],
            owner,
            group,
            format(mode, "o"),
        )

    def _resolve_container_identity(self, container, path, name, kind):
        """Resolve a username/group name or numeric ID from a stopped image."""
        if str(name).isdigit():
            numeric_id = int(name)
            return numeric_id, str(name)

        try:
            stream, _ = container.get_archive(path)
            archive_bytes = b"".join(stream)
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
                member = next((item for item in tar.getmembers() if item.isfile()), None)
                if not member:
                    raise ValueError("empty identity file")
                raw = tar.extractfile(member).read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise ValueError(
                "Unable to resolve {} '{}' from {}: {}".format(kind, name, path, exc)
            )

        for line in raw.splitlines():
            fields = line.split(":")
            if len(fields) >= 3 and fields[0] == name:
                return int(fields[2]), name
        raise ValueError("{} '{}' does not exist in the container image".format(kind, name))

    def create_network(self, name: str, labels: Dict[str, str] = None):
        """Create a Docker bridge network for one challenge instance."""
        if not self.is_connected():
            raise Exception("Docker is not connected")

        network_labels = labels or {}
        network_labels.update({"ctfd.managed": "true", "ctfd.plugin": "containers"})

        try:
            return self.client.networks.create(
                name=name,
                driver="bridge",
                labels=network_labels,
            )
        except docker.errors.APIError as e:
            logger.error(f"Docker API error creating network: {e}")
            raise Exception(f"Failed to create network: {e}")

    def remove_network(self, name: str) -> bool:
        """Remove a Docker network if it exists."""
        if not self.is_connected():
            return False

        try:
            network = self.client.networks.get(name)
            network.remove()
            logger.info(f"Removed network {name}")
            return True
        except docker.errors.NotFound:
            return True
        except Exception as e:
            logger.error(f"Error removing network {name}: {e}")
            return False

    def ensure_auth_proxy_daemon(
        self,
        image: str,
        environment: Dict[str, str],
        labels: Dict[str, str] = None,
        name: str = "ctfd-container-auth-proxy",
    ) -> Dict[str, Any]:
        """Ensure a single host-network auth proxy daemon is running."""
        if not self.is_connected():
            raise Exception("Docker is not connected")

        try:
            try:
                existing = self.client.containers.get(name)
                if existing.status == "running":
                    existing_labels = existing.labels or {}
                    expected_labels = labels or {}
                    if all(
                        existing_labels.get(key) == str(value)
                        for key, value in expected_labels.items()
                    ):
                        return {
                            "container_id": existing.id,
                            "status": existing.status,
                        }
                existing.remove(force=True)
            except docker.errors.NotFound:
                pass

            container_labels = labels or {}
            container_labels.update(
                {
                    "ctfd.managed": "true",
                    "ctfd.plugin": "containers",
                    "ctfd.role": "auth-proxy-daemon",
                }
            )

            container = self.client.containers.run(
                image=image,
                name=name,
                command=["python", "-u", "-c", AUTH_PROXY_SCRIPT],
                detach=True,
                restart_policy={"Name": "unless-stopped"},
                environment=environment,
                labels=container_labels,
                network_mode="host",
                user="65534:65534",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                read_only=True,
                tmpfs={"/tmp": "rw,noexec,nosuid,size=16m"},
            )

            logger.info(f"Created auth proxy {container.id[:12]} from image {image}")

            return {
                "container_id": container.id,
                "status": container.status,
            }

        except docker.errors.ImageNotFound:
            logger.error(f"Docker image not found: {image}")
            raise Exception(f"Docker image '{image}' not found")
        except docker.errors.APIError as e:
            logger.error(f"Docker API error: {e}")
            raise Exception(f"Failed to create auth proxy: {e}")
        except Exception as e:
            logger.error(f"Unexpected error creating auth proxy daemon: {e}")
            raise

    def get_container_ip(
        self,
        container_id: str,
        network: str,
        retries: int = 10,
        retry_delay: float = 0.5,
    ) -> Optional[str]:
        """Return a container IPv4 address on a Docker network.

        Remote Docker daemons can briefly report a container before its
        network endpoint has been populated. Retry the lookup for a bounded
        period so provisioning does not fail on that transient state.
        """
        last_error = None

        for attempt in range(max(1, retries)):
            try:
                if not self.is_connected():
                    last_error = "Docker daemon is not connected"
                else:
                    container = self.client.containers.get(container_id)
                    container.reload()
                    networks = container.attrs.get("NetworkSettings", {}).get(
                        "Networks", {}
                    )
                    network_data = networks.get(network)
                    address = network_data.get("IPAddress") if network_data else None
                    if address:
                        return address

                    last_error = (
                        f"network '{network}' has no IP yet; "
                        f"available networks: {', '.join(networks) or 'none'}"
                    )
            except Exception as exc:
                last_error = str(exc)

            if attempt < max(1, retries) - 1:
                time.sleep(max(0.0, retry_delay))

        logger.error(
            "Unable to determine IP for container %s on network %s after %d attempts: %s",
            container_id[:12],
            network,
            max(1, retries),
            last_error,
        )
        return None

    def stop_container(self, container_id: str) -> bool:
        """
        Stop and remove a container

        Args:
            container_id: Container ID

        Returns:
            True if successful, False otherwise
        """
        if not self.is_connected():
            logger.warning("Docker not connected, cannot stop container")
            return False

        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=10)
            container.remove()
            logger.info(f"Stopped and removed container {container_id[:12]}")
            return True
        except docker.errors.NotFound:
            logger.info(f"Container {container_id[:12]} not found (already removed)")
            return True
        except Exception as e:
            logger.error(f"Error stopping container {container_id[:12]}: {e}")
            return False

    def get_container_status(self, container_id: str) -> Optional[str]:
        """
        Get container status

        Returns:
            Status string ('running', 'exited', etc.) or None if not found
        """
        if not self.is_connected():
            return None

        try:
            container = self.client.containers.get(container_id)
            return container.status
        except docker.errors.NotFound:
            return None
        except Exception as e:
            logger.error(f"Error getting container status: {e}")
            return None

    def is_container_running(self, container_id: str) -> bool:
        """Check if container is running"""
        status = self.get_container_status(container_id)
        return status == "running"

    def list_managed_containers(self):
        """
        List all containers managed by this plugin

        Returns:
            List of container objects
        """
        if not self.is_connected():
            return []

        try:
            return self.client.containers.list(
                all=True, filters={"label": "ctfd.managed=true"}
            )
        except Exception as e:
            logger.error(f"Error listing containers: {e}")
            return []

    def list_images(self):
        """
        List all available Docker images

        Returns:
            List of image objects
        """
        if not self.is_connected():
            raise Exception("Docker is not connected")

        try:
            images = self.client.images.list()
            return images
        except Exception as e:
            logger.error(f"Failed to list images: {e}")
            raise Exception(f"Failed to list Docker images: {e}")

    def get_container_logs(
        self, container_id: str, tail: Optional[int] = 100, timestamps: bool = False
    ) -> Optional[str]:
        """
        Get container logs

        Args:
            container_id: Container ID
            tail: Number of lines to return. Use ``None`` to return the
                  complete log history for the container.

        Returns:
            Logs as string or None
        """
        if not self.is_connected():
            return None

        try:
            container = self.client.containers.get(container_id)
            logs = container.logs(
                tail=tail, timestamps=timestamps
            ).decode("utf-8", errors="ignore")
            return logs
        except Exception as e:
            logger.error(f"Error getting container logs: {e}")
            return None

    def cleanup_expired_containers(self, instance_uuids: list):
        """
        Cleanup containers không còn trong database

        Args:
            instance_uuids: List of valid instance UUIDs from database
        """
        if not self.is_connected():
            return

        try:
            containers = self.list_managed_containers()
            for container in containers:
                instance_uuid = container.labels.get("ctfd.instance_uuid")
                if instance_uuid and instance_uuid not in instance_uuids:
                    logger.info(f"Cleaning up orphaned container {container.id[:12]}")
                    try:
                        container.stop(timeout=5)
                        container.remove()
                    except (Exception):
                        pass
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
