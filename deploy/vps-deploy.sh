#!/usr/bin/env bash
set -euo pipefail

target_ref="${1:-origin/main}"
notify_channel_id="${2:-1385304293845766366}"

if [ ! -f ".env" ]; then
  echo ".env is missing in $(pwd)" >&2
  exit 1
fi

git fetch origin main
if git rev-parse --verify "$target_ref^{commit}" >/dev/null 2>&1; then
  git reset --hard "$target_ref"
else
  git reset --hard origin/main
fi

docker compose up -d --build
docker compose ps

short_sha="$(git rev-parse --short HEAD)"

python3 - "$notify_channel_id" "$short_sha" <<'PY'
import json
import sys
import urllib.request
from pathlib import Path

channel_id, short_sha = sys.argv[1], sys.argv[2]
env = {}
for raw in Path(".env").read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    env[key] = value

token = env.get("DISCORD_TOKEN")
if not token:
    raise SystemExit("DISCORD_TOKEN is missing from .env")

content = f"gifgoblin updated on VPS to `{short_sha}` and restarted."
payload = json.dumps({"content": content}).encode("utf-8")
request = urllib.request.Request(
    f"https://discord.com/api/v10/channels/{channel_id}/messages",
    data=payload,
    headers={
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "gifgoblin-deploy",
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=20) as response:
    print(f"sent deploy notification to {channel_id}: HTTP {response.status}")
PY
