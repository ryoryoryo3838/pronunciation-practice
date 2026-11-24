#!/usr/bin/env bash
# One-time Secure MCP Tunnel setup: writes a tunnel-client profile, stores the runtime key outside the
# repo (mode 600), and starts a systemd user service. Usage: deploy/tunnel-setup.sh tunnel_<id>
set -euo pipefail

TUNNEL_ID="${1:?usage: $0 tunnel_<32 chars>}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$HOME/.config/pronunciation-practice"
UNIT="$HOME/.config/systemd/user/pron-tunnel.service"
HEALTH="127.0.0.1:8791"  # 8080 (tunnel-client's default) is often taken

command -v tunnel-client >/dev/null || { echo "tunnel-client not on PATH" >&2; exit 1; }
mkdir -p "$CONF/tunnel" "$(dirname "$UNIT")"

if [[ ! -s "$CONF/tunnel.env" ]]; then
  read -rsp "Runtime API key (CONTROL_PLANE_API_KEY, input hidden): " KEY; echo
  [[ "$KEY" == sk-* ]] || { echo "that does not look like an API key" >&2; exit 1; }
  (umask 077; printf 'CONTROL_PLANE_API_KEY=%s\n' "$KEY" > "$CONF/tunnel.env")
fi

tunnel-client init --force --sample sample_mcp_stdio_local --profile pron --profile-dir "$CONF/tunnel" \
  --tunnel-id "$TUNNEL_ID" --health-listen-addr "$HEALTH" \
  --mcp-command "$(command -v uv) run --directory $REPO python server.py --stdio"

set -a; source "$CONF/tunnel.env"; set +a
tunnel-client doctor --profile pron --profile-dir "$CONF/tunnel" --explain

sed -e "s|@CONF@|$CONF|g" -e "s|@TC@|$(command -v tunnel-client)|g" "$REPO/deploy/pron-tunnel.service" > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now pron-tunnel.service
sleep 3
curl -fsS "http://$HEALTH/readyz" && echo && echo "ready: register the tunnel in ChatGPT now"
