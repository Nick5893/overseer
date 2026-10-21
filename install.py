#!/usr/bin/env python3
"""Install Overseer using Python's standard library only."""
import argparse
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parent


def install(prefix, *, force=False, legacy_alias=True):
    destination = prefix / "share/frontier-foundry"
    binaries = prefix / "bin"
    files = ["spark_workers.py", "worker_harnesses.py", "spark_proxy.py", "make_model_catalog.py",
             "templates/SKILL.md", "templates/agents/openai.yaml", "assets/overseer.jpg"]
    links = {"overseer": "spark_workers.py", "overseer-adapter": "spark_proxy.py",
             "overseer-catalog": "make_model_catalog.py",
             "foundry": "spark_workers.py", "spark-workers": "spark_workers.py",
             "foundry-adapter": "spark_proxy.py", "foundry-catalog": "make_model_catalog.py"}
    if not legacy_alias:
        links.pop("spark-workers")
    for name, target in links.items():
        link = binaries / name
        if (link.exists() or link.is_symlink()) and link.resolve() != destination / target:
            if not force:
                raise RuntimeError(f"{link} belongs to another installation. Use a different --prefix or explicitly pass --force.")
    if destination.resolve() == SOURCE:
        raise RuntimeError("Installation destination cannot be the source checkout.")
    for relative in files:
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / relative, path)
        path.chmod(0o755 if path.suffix == ".py" else 0o644)
    binaries.mkdir(parents=True, exist_ok=True)
    for name, target in links.items():
        link = binaries / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(destination / target)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    parser.add_argument("--force", action="store_true", help="Explicitly replace conflicting command symlinks")
    parser.add_argument("--no-legacy-alias", action="store_true", help="Install Overseer without replacing an existing spark-workers command")
    args = parser.parse_args()
    if sys.version_info < (3, 11) or not sys.platform.startswith("linux"):
        parser.error("Overseer currently supports Linux with Python 3.11 or newer.")
    try:
        destination = install(args.prefix.expanduser().resolve(), force=args.force, legacy_alias=not args.no_legacy_alias)
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"Install failed: {exc}\n")
    print(f"Installed Overseer at {destination}")
    names = "overseer,overseer-adapter,overseer-catalog,foundry,foundry-adapter,foundry-catalog"
    if not args.no_legacy_alias:
        names += ",spark-workers"
    print(f"Commands: {args.prefix.expanduser() / 'bin'}/{{{names}}}")
    print("Put that bin directory on PATH. Next: overseer setup (interactive) or overseer configure --help")


if __name__ == "__main__":
    main()
