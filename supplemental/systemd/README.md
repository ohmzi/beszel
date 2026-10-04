# Running the Ohmz fork as system services

Built binaries (`make build`) run directly, no Docker required. `install.sh`-style steps:

```
sudo install -m0644 supplemental/systemd/beszel-hub.service  /etc/systemd/system/
sudo install -m0644 supplemental/systemd/beszel-agent.service /etc/systemd/system/
sudo mkdir -p /var/lib/beszel            # hub data (users, systems, history)
sudo touch /etc/beszel-agent.env && sudo chmod 0600 /etc/beszel-agent.env
# after first-run login in the UI: copy the universal token and the hub key into the env file
sudo tee /etc/beszel-agent.env >/dev/null <<'ENV'
HUB_URL=ws://127.0.0.1:8088
TOKEN=<universal token>
KEY=<ssh-ed25519 … hub key>
MAINTENANCE_PUBLIC_DIR=/var/lib/homelab-maint/public
MAINTENANCE_ACK_DIR=/var/lib/homelab-maint/ack
DISABLE_SSH=true
ENV
sudo systemctl daemon-reload && sudo systemctl enable --now beszel-hub beszel-agent
```

The agent runs as **root** so it can read the engine's published state and write its inbox
(the acknowledge bridge). It never runs the engine's scripts.
