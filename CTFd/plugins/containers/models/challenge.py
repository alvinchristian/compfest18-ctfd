"""
Container Challenge Model
"""
from CTFd.models import Challenges, db


ALLOWED_CONTAINER_CAPABILITIES = frozenset(
    {
        "AUDIT_CONTROL",
        "AUDIT_READ",
        "AUDIT_WRITE",
        "BLOCK_SUSPEND",
        "CHOWN",
        "DAC_OVERRIDE",
        "DAC_READ_SEARCH",
        "FOWNER",
        "FSETID",
        "IPC_LOCK",
        "IPC_OWNER",
        "KILL",
        "LEASE",
        "LINUX_IMMUTABLE",
        "MAC_ADMIN",
        "MAC_OVERRIDE",
        "MKNOD",
        "NET_ADMIN",
        "NET_BIND_SERVICE",
        "NET_BROADCAST",
        "NET_RAW",
        "PERFMON",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
        "SYS_ADMIN",
        "SYS_BOOT",
        "SYS_CHROOT",
        "SYS_MODULE",
        "SYS_NICE",
        "SYS_PACCT",
        "SYS_PTRACE",
        "SYS_RAWIO",
        "SYS_RESOURCE",
        "SYS_TIME",
        "SYS_TTY_CONFIG",
        "SYSLOG",
        "WAKE_ALARM",
    }
)


class ContainerChallenge(Challenges):
    """
    Container challenge type - spawns Docker container for each team/user
    """

    __mapper_args__ = {"polymorphic_identity": "container"}
    __table_args__ = {"extend_existing": True}

    id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True
    )

    # Docker configuration
    image = db.Column(db.String(255), nullable=False)
    internal_port = db.Column(db.Integer, nullable=False, default=22)
    command = db.Column(db.Text, default="")
    capabilities = db.Column(db.JSON, nullable=True, default=list)
    no_new_privileges = db.Column(db.Boolean, nullable=False, default=True)
    flag_delivery_mode = db.Column(db.String(20), nullable=False, default="environment")
    flag_file_path = db.Column(db.String(512), nullable=True, default="/home/ctf/flag.txt")
    flag_file_owner = db.Column(db.String(64), nullable=True, default="target")
    flag_file_group = db.Column(db.String(64), nullable=True, default="target")
    flag_file_mode = db.Column(db.String(4), nullable=True, default="440")

    # Connection info for users
    container_connection_type = db.Column(
        db.String(20), default="ssh", name="connection_type"
    )  # ssh, http, nc, custom
    container_connection_info = db.Column(
        db.Text, default="", name="connection_info"
    )  # Extra info to display
    auth_required = db.Column(db.Boolean, nullable=False, default=True)

    # Resource limits (deprecated - use global config)
    # Kept for backward compatibility, but values are ignored
    memory_limit = db.Column(db.String(20), nullable=True)
    cpu_limit = db.Column(db.Float, nullable=True)
    pids_limit = db.Column(db.Integer, default=100)

    # Container lifecycle (deprecated - use global config)
    # Kept for backward compatibility, but values are ignored
    timeout_minutes = db.Column(db.Integer, nullable=True)
    max_renewals = db.Column(db.Integer, nullable=True)

    def get_timeout_minutes(self):
        """Get per-challenge timeout, falling back to legacy global config."""
        from ..models.config import ContainerConfig

        if self.timeout_minutes is not None:
            return int(self.timeout_minutes)
        return int(ContainerConfig.get("default_timeout", "60"))

    def get_max_renewals(self):
        """Get max renewals from global config"""
        from ..models.config import ContainerConfig

        return int(ContainerConfig.get("max_renewals", "3"))

    def get_memory_limit(self):
        """Get memory limit from global config"""
        from ..models.config import ContainerConfig

        return ContainerConfig.get("max_memory", "512m")

    def get_cpu_limit(self):
        """Get CPU limit from global config"""
        from ..models.config import ContainerConfig

        return float(ContainerConfig.get("max_cpu", "0.5"))

    def get_capabilities(self):
        """Return the configured additional Linux capabilities."""
        return list(self.capabilities or [])

    def get_no_new_privileges(self):
        """Whether Docker should prevent privilege escalation in the container."""
        return bool(self.no_new_privileges)

    def get_flag_file_config(self):
        """Return validated defaults for file-based flag delivery."""
        return {
            "path": self.flag_file_path or "/home/ctf/flag.txt",
            "owner": self.flag_file_owner or "target",
            "group": self.flag_file_group or "target",
            "mode": self.flag_file_mode or "440",
        }

    # Flag configuration
    flag_mode = db.Column(db.String(20), default="random")  # "random" or "static"
    flag_prefix = db.Column(db.String(70), default="CTF{")
    flag_suffix = db.Column(db.String(50), default="}")
    random_flag_length = db.Column(db.Integer, default=16)

    # Dynamic scoring (like CTFd dynamic challenges)
    container_initial = db.Column(db.Integer, default=500, name="initial")
    container_minimum = db.Column(db.Integer, default=100, name="minimum")
    container_decay = db.Column(db.Integer, default=20, name="decay")
    decay_function = db.Column(
        db.String(32), default="logarithmic"
    )  # linear or logarithmic

    def __init__(self, *args, **kwargs):
        super(ContainerChallenge, self).__init__(**kwargs)
        # Set initial value
        if "container_initial" in kwargs:
            self.value = kwargs["container_initial"]
        elif "initial" in kwargs:
            self.value = kwargs["initial"]
