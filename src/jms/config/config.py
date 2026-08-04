"""Configuration management for jms-cli.

Supports multiple JumpServer instances, default server selection,
and encrypted credential storage. Config lives in a single YAML file
located via platformdirs (XDG-compliant on Linux, Application Support
on macOS). There is no ``.env`` support and no ``./config.yaml``
auto-discovery — an explicit path may be passed via ``--config``.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from platformdirs import user_config_dir

from jms.config.crypto import decrypt, encrypt, is_encrypted
from jms.exceptions import ConfigError

# Application name used by platformdirs to resolve the config directory.
APP_NAME: str = "jms"


def config_dir() -> Path:
    """Return the jms config directory (platformdirs-resolved)."""
    return Path(user_config_dir(APP_NAME))


def config_file_path() -> Path:
    """Return the canonical config file path."""
    return config_dir() / "config.yaml"


@dataclass(frozen=True)
class ServerConfig:
    """Configuration for a single JumpServer instance.

    Attributes:
        name: Alias for this server.
        host: JumpServer hostname or URL.
        username: Login username.
        password: Login password (plaintext after decryption).
        otp_secret: TOTP secret (base32, plaintext after decryption).
    """

    name: str
    host: str
    username: str
    password: str
    otp_secret: str = ""

    @property
    def base_url(self) -> str:
        """HTTP(S) base URL derived from host."""
        if self.host.startswith(("http://", "https://")):
            return self.host.rstrip("/")
        return f"https://{self.host}"


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration.

    Attributes:
        version: Config file schema version.
        default: Default server alias (empty = first server).
        servers: Mapping of alias to ServerConfig.
    """

    version: float = 1.0
    default: str = ""
    servers: dict[str, ServerConfig] = field(default_factory=dict)

    @property
    def default_server(self) -> ServerConfig:
        """Return the default server.

        Raises:
            ConfigError: If no servers configured.
        """
        if not self.servers:
            raise ConfigError(
                "No servers configured. Run: jms config add"
            )
        if self.default and self.default in self.servers:
            return self.servers[self.default]
        return next(iter(self.servers.values()))

    def get_server(self, name: str) -> ServerConfig:
        """Get a server by alias.

        Raises:
            ConfigError: If not found.
        """
        if name not in self.servers:
            available = ", ".join(self.servers.keys()) or "(none)"
            raise ConfigError(
                f"Server '{name}' not found. Available: {available}"
            )
        return self.servers[name]


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load and validate configuration.

    Args:
        config_path: Explicit path, or None for platformdirs default.

    Returns:
        Parsed AppConfig with decrypted credentials.

    Raises:
        ConfigError: If missing or invalid.
    """
    path = Path(config_path) if config_path else config_file_path()

    if not path.exists():
        raise ConfigError(
            f"Config not found at {path}\n"
            f"Run: jms config add <alias>"
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"Failed to read config: {e}") from e

    if raw is None:
        raise ConfigError(f"Config file is empty: {path}")
    if not isinstance(raw, dict):
        raise ConfigError(f"Config must be a YAML mapping, got {type(raw).__name__}.")

    return parse_config(raw)


def parse_config(raw: dict) -> AppConfig:
    """Parse raw config dict, decrypting credentials.

    Args:
        raw: Dict from config.yaml.

    Returns:
        AppConfig with plaintext credentials.

    Raises:
        ConfigError: If required fields are missing.
    """
    version = raw.get("version", 1.0)
    default = raw.get("default", "")
    servers_raw = raw.get("servers")

    if not servers_raw or not isinstance(servers_raw, dict):
        raise ConfigError(
            "Config must contain a 'servers' mapping. "
            "Run: jms config add <alias>"
        )

    servers: dict[str, ServerConfig] = {}
    for name, srv in servers_raw.items():
        if not isinstance(srv, dict):
            raise ConfigError(f"Server '{name}' must be a mapping.")

        host = srv.get("host", "")
        username = srv.get("username", "")
        password_raw = srv.get("password", "")
        otp_raw = srv.get("otp_secret") or ""  # YAML null → ""

        if not host:
            raise ConfigError(f"Server '{name}' missing 'host'.")
        if not username:
            raise ConfigError(f"Server '{name}' missing 'username'.")
        if not password_raw:
            raise ConfigError(f"Server '{name}' missing 'password'.")

        # Decrypt if encrypted
        try:
            password = (
                decrypt(password_raw, host, username)
                if is_encrypted(password_raw) else password_raw
            )
            otp_secret = (
                decrypt(otp_raw, host, username)
                if is_encrypted(otp_raw) else otp_raw
            )
        except ValueError as e:
            raise ConfigError(
                f"Server '{name}': credential decryption failed: {e}"
            ) from e

        servers[name] = ServerConfig(
            name=name, host=host, username=username,
            password=password, otp_secret=otp_secret,
        )

    return AppConfig(version=version, default=default, servers=servers)


def save_config(cfg: AppConfig, config_path: Optional[str] = None) -> Path:
    """Save config to disk with credentials encrypted.

    Args:
        cfg: AppConfig to save.
        config_path: Explicit path, or None for platformdirs default.

    Returns:
        Path where config was saved.
    """
    path = Path(config_path) if config_path else config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    raw: dict = {
        "version": cfg.version,
        "default": cfg.default,
        "servers": {},
    }
    for name, srv in cfg.servers.items():
        raw["servers"][name] = {
            "host": srv.host,
            "username": srv.username,
            "password": encrypt(srv.password, srv.host, srv.username),
            "otp_secret": (
                encrypt(srv.otp_secret, srv.host, srv.username)
                if srv.otp_secret else ""
            ),
        }

    # Write with 0600 from the start — no window where creds are group/world-readable
    data = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # mode only applies at creation; enforce 0600 for pre-existing files too.
    # POSIX-only: Windows has no permission bits (ACLs govern access) and
    # os.fchmod is not always available there.
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    return path


def add_server(
    name: str, host: str, username: str, password: str,
    otp_secret: str = "",
    set_default: bool = False,
    config_path: Optional[str] = None,
) -> Path:
    """Add a server to the config (or update if exists).

    Args:
        name: Server alias.
        host: JumpServer host.
        username: Login username.
        password: Login password (plaintext).
        otp_secret: TOTP secret (plaintext).
        set_default: Whether to make this the default server.
        config_path: Explicit config path.

    Returns:
        Path where config was saved.
    """
    path = Path(config_path) if config_path else config_file_path()

    # Load existing or create new. If the file exists but is broken,
    # let ConfigError propagate — never silently overwrite credentials.
    cfg = load_config(str(path)) if path.exists() else AppConfig()

    # Add/update server
    servers = dict(cfg.servers)
    servers[name] = ServerConfig(
        name=name, host=host, username=username,
        password=password, otp_secret=otp_secret,
    )

    default = name if set_default else (cfg.default or name)

    new_cfg = AppConfig(version=cfg.version, default=default, servers=servers)
    return save_config(new_cfg, str(path))


def remove_server(name: str, config_path: Optional[str] = None) -> Path:
    """Remove a server from the config.

    Args:
        name: Server alias to remove.
        config_path: Explicit config path.

    Returns:
        Path where config was saved.

    Raises:
        ConfigError: If server not found.
    """
    cfg = load_config(config_path)
    if name not in cfg.servers:
        raise ConfigError(f"Server '{name}' not found.")

    servers = {k: v for k, v in cfg.servers.items() if k != name}
    default = cfg.default if cfg.default != name else ""
    if not default and servers:
        default = next(iter(servers.keys()))

    new_cfg = AppConfig(version=cfg.version, default=default, servers=servers)
    path = Path(config_path) if config_path else config_file_path()
    return save_config(new_cfg, str(path))


def set_default_server(name: str, config_path: Optional[str] = None) -> Path:
    """Set the default server.

    Args:
        name: Server alias.
        config_path: Explicit config path.

    Returns:
        Path where config was saved.

    Raises:
        ConfigError: If server not found.
    """
    cfg = load_config(config_path)
    if name not in cfg.servers:
        raise ConfigError(f"Server '{name}' not found.")

    new_cfg = AppConfig(version=cfg.version, default=name, servers=cfg.servers)
    path = Path(config_path) if config_path else config_file_path()
    return save_config(new_cfg, str(path))
