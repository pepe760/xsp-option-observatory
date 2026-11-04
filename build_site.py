"""Copy the fixed public files after verifying the status document."""
from pathlib import Path
import shutil
import sys

from observe import validate_public


def main():
    target = Path(sys.argv[1])
    if target.exists():
        raise SystemExit("build destination already exists")
    source = Path(__file__).resolve().parent / "site"
    files = ("index.html", "style.css", "app.js", "data/status.json")
    for name in files:
        path = source / name
        if path.is_symlink() or not path.is_file() or source not in path.resolve().parents:
            raise SystemExit(f"public asset must be an ordinary site file: {name}")
    validate_public(source / "data/status.json")
    for name in files:
        destination = target / name
        destination.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(source / name, destination)
    (target / ".nojekyll").touch()


if __name__ == "__main__":
    main()
