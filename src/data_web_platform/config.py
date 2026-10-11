"""Resolve the private runtime and construct child-process environments.

Configuration is per service/venv. It must not inherit arbitrary source-module
overrides from an operator's shell or replace the user's global provider file.
The internal token is solely for the loopback Viewer bridge, not browser login.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Paths and service settings; construction alone does not create state."""
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    letools_root: Path = field(default_factory=lambda: Path.home() / "projects/letools-tianji")
    state_dir: Path | None = None
    python: Path | None = None
    allowed_roots: list[Path] = field(default_factory=lambda: [Path.home(), Path("/media/user/BAG_STORAGE")])
    bun: Path = field(default_factory=lambda: Path.home() / ".bun/bin/bun")
    viewer_port: int = 3010
    host: str = "0.0.0.0"
    port: int = 8095
    default_source: str = "/media/user/BAG_STORAGE/recorded_bags"
    internal_token: str = ""

    def __post_init__(self):
        self.project_root = Path(self.project_root).resolve()
        self.letools_root = Path(self.letools_root).resolve()
        self.state_dir = Path(self.state_dir or self.project_root / ".state").resolve()
        self.python = Path(self.python or self.letools_root / ".venv/bin/python")
        self.allowed_roots = [Path(p).expanduser().resolve() for p in self.allowed_roots]

    @classmethod
    def from_env(cls):
        values = {}
        for name in ("project_root", "letools_root", "state_dir", "python", "bun"):
            value = os.environ.get("DWP_" + name.upper())
            if value:
                values[name] = Path(value).expanduser()
        if os.environ.get("DWP_ALLOWED_ROOTS"):
            values["allowed_roots"] = [Path(p) for p in os.environ["DWP_ALLOWED_ROOTS"].split(os.pathsep)]
        obj = cls(**values)
        obj.host = os.environ.get("DWP_HOST", obj.host)
        obj.port = int(os.environ.get("DWP_PORT", obj.port))
        obj.viewer_port = int(os.environ.get("DWP_VIEWER_PORT", obj.viewer_port))
        obj.default_source = os.environ.get("DWP_DEFAULT_SOURCE", obj.default_source)
        return obj

    def initialize(self):
        """Create only owned state/config files and restrict their permissions.

        Existing provider settings and the bridge token are reused. Permission
        repair is restricted to managed state directories, never source data.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir(mode=...) and the service umask do not repair directories that
        # were created by a deployment tool before the first service start.
        # Change only our own state directories, never user dataset contents.
        self.state_dir.chmod(0o700)
        for name in ("jobs", "config", "outputs", "exports"):
            directory = self.state_dir / name
            directory.mkdir(exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        provider_file = self.state_dir / "config/providers.toml"
        if not provider_file.exists():
            provider_file.write_text("# This file is private to Data Web Platform.\n[providers]\n")
        provider_file.chmod(0o600)
        # Only the loopback bridge needs a secret. Browser access has no login.
        path = self.state_dir / "internal-token"
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32) + "\n")
        path.chmod(0o600)
        if not self.internal_token:
            self.internal_token = path.read_text().strip()

    def environment(self):
        """Build a fresh subprocess environment with private plugin discovery.

        Removing PYTHONPATH and LEtools module overrides prevents the service
        from accidentally importing a different checkout than its interpreter.
        These changes apply only to this child, not the shell or global config.
        """
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("LETOOLS_PROVIDER_MODULES", None)
        env["LETOOLS_PROVIDERS_FILE"] = str(self.state_dir / "config/providers.toml")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["OPENBLAS_NUM_THREADS"] = "2"
        env["OMP_NUM_THREADS"] = "2"
        env["DWP_INTERNAL_TOKEN"] = self.internal_token
        return env
