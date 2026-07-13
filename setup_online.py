"""Provision PartyPad's private desktop-to-service authentication token."""

import argparse
import secrets
import subprocess
import sys
from pathlib import Path

from online_transport import host_token_path


def save_token(path: Path, token: str):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(token + "\n")
    path.chmod(0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(description="configure PartyPad online authentication")
    parser.add_argument("--rotate", action="store_true", help="replace an existing host token")
    parser.add_argument(
        "--worker-dir",
        type=Path,
        default=Path(__file__).parent / "cloudflare",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    path = host_token_path()
    if path.exists() and not args.rotate:
        token = path.read_text().strip()
        if not token:
            parser.error(f"existing token file is empty: {path}")
    else:
        token = secrets.token_hex(32)
        save_token(path, token)

    wrangler = args.worker_dir / "node_modules" / ".bin" / "wrangler"
    if not wrangler.exists():
        parser.error(f"Wrangler is not installed; run `npm install` in {args.worker_dir}")
    result = subprocess.run(
        [str(wrangler), "secret", "put", "HOST_TOKEN"],
        cwd=args.worker_dir,
        input=token + "\n",
        text=True,
        check=False,
    )
    if result.returncode:
        sys.exit(f"Wrangler could not save HOST_TOKEN (status {result.returncode})")
    print(f"PartyPad host authentication configured; private token saved at {path}")


if __name__ == "__main__":
    main()
