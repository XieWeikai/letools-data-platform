"""Install per-venv provider discovery defaults; never change the user's config.

Run with letools-tianji/.venv/bin/python. Explicit LeTools configuration still
takes precedence. The generated .pth file is owned solely by this installer.
"""
from pathlib import Path
import sys


def main():
    prefix = Path(sys.prefix).resolve()
    if sys.prefix == sys.base_prefix or not (prefix / "pyvenv.cfg").is_file():
        raise SystemExit("Refusing to install outside a virtual environment")
    config = prefix / "letools-config/providers.toml"
    config.parent.mkdir(exist_ok=True)
    if not config.exists():
        config.write_text("# Isolated environment: only installed provider entry points.\n[providers]\n")
    site = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages" / "data_web_platform_isolation.pth"
    marker = "# Managed by data-web-platform: isolated LeTools providers"
    if site.exists() and not site.read_text().startswith(marker):
        raise SystemExit(f"Refusing to replace an unrelated {site}")
    site.write_text(marker + "\n" +
                    f"import os; os.environ.setdefault('LETOOLS_PROVIDERS_FILE', {str(config)!r})\n")
    print(f"Isolated provider defaults installed in {prefix}")


if __name__ == "__main__":
    main()
