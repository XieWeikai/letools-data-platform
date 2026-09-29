"""Install this one user service; never stop or overwrite unrelated units."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
source = root / "deploy/data-web-platform.service"
target = Path.home() / ".config/systemd/user/data-web-platform.service"
if target.exists() and "Isolated LeTools Tianji Data Web Platform" not in target.read_text():
    raise SystemExit("Refusing to overwrite an unrelated service with the same name")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(source.read_text())
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
subprocess.run(["systemctl", "--user", "enable", "--now", "data-web-platform.service"], check=True)
print("Installed user service: data-web-platform.service")
