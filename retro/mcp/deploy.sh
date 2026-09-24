#!/usr/bin/env bash
# One-shot deploy of the retro MCP server to Cloudflare, then print the
# connector URL to paste into Claude ("Add custom connector").
#
#   ./deploy.sh
#
# Auth: uses `wrangler login` (browser) if you are not logged in, or
# CLOUDFLARE_API_TOKEN (+ CLOUDFLARE_ACCOUNT_ID) if set. Safe to re-run:
# the KV id and your key are created once and reused.
set -euo pipefail
cd "$(dirname "$0")"

WR="npx --yes wrangler"
[ -d node_modules ] || npm install --silent

if ! $WR whoami 2>/dev/null | grep -qi "associated with the email\|account id"; then
  if [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then
    echo "CLOUDFLARE_API_TOKEN is set but wrangler cannot authenticate with it." >&2; exit 1
  fi
  $WR login
fi

# KV namespace: create once, write its id into wrangler.toml.
if grep -q 'REPLACE_WITH_KV_ID' wrangler.toml; then
  out="$($WR kv namespace create RETRO 2>&1 || true)"
  id="$(printf '%s' "$out" | grep -oE '[0-9a-f]{32}' | head -1)"
  if [ -z "$id" ]; then # already exists -> find it in the list
    id="$($WR kv namespace list 2>/dev/null | node -e '
      let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
        const ns=JSON.parse(s.slice(s.indexOf("[")));
        const hit=ns.find(n=>/RETRO$/.test(n.title));
        process.stdout.write(hit?hit.id:"");});')"
  fi
  [ -n "$id" ] || { echo "$out" >&2; echo "could not get KV namespace id" >&2; exit 1; }
  sed -i.bak "s/REPLACE_WITH_KV_ID/$id/" wrangler.toml && rm -f wrangler.toml.bak
  echo "KV namespace: $id"
fi

deploy_out="$($WR deploy 2>&1)" || { echo "$deploy_out" >&2; exit 1; }
echo "$deploy_out" | tail -5
base="$(printf '%s' "$deploy_out" | grep -oE 'https://[A-Za-z0-9.-]+\.workers\.dev' | head -1)"

# Per-user secret key, kept locally (gitignored).
[ -f .retro-key ] || node -e 'process.stdout.write(require("crypto").randomBytes(24).toString("base64url"))' > .retro-key
key="$(cat .retro-key)"

# Smoke test against the live worker.
if [ -n "$base" ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$base/mcp/$key" \
    -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' || true)"
  echo "smoke test: HTTP $code"
fi

cat <<EOF

Connector URL (Claude > Settings > Connectors > Add custom connector):
  ${base:-https://retro-mcp.<subdomain>.workers.dev}/mcp/$key
Today's page:
  ${base:-https://retro-mcp.<subdomain>.workers.dev}/p/$key
Keep this URL private: the key in it is the only credential (PoC).
EOF
