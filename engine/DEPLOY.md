# OhmzMaintainer engine (moved here)

This is the homelab maintenance **engine** — the runner, checks, notify path, registry and the
`homelab-maint` CLI. It used to live in its own repo (`ohmzi/homelab-maint`); it now lives here,
next to the dashboard, so the two halves of OhmzMaintainer are developed and versioned together.
The old maintenance **website** (the `web/` folder in that repo) was decommissioned and is not part
of this tree.

## What runs where

The engine is installed, not run from this folder:

| Piece | Path |
|---|---|
| Python package | `/usr/local/lib/homelab-maint/homelab_maint/` |
| CLI wrapper | `/usr/local/sbin/homelab-maint` |
| Config (registry-driven) | `/etc/homelab-maint/` (`rules.d/` is the source of truth; `notify.toml`, `probes.toml`, ... are generated) |
| State + published JSON | `/var/lib/homelab-maint/` (`public/`, `ack/`) |
| Services | `homelab-maint-check|daily|weekly|live|metrics|selfhealth|tick|www`.service + their timers |

No systemd unit points at this folder, so nothing here is executed at runtime.

## Applying a change

- **Code** (`homelab_maint/**`): edit here, then copy the package into place and restart the units,
  or run `install.sh` again:
  `sudo cp -r homelab_maint /usr/local/lib/homelab-maint/ && sudo systemctl restart 'homelab-maint-*'`
- **Policy / config**: never edit `/etc/homelab-maint/*.toml` by hand — it is generated. Edit the
  rule in `/etc/homelab-maint/rules.d/` (the shipped defaults are in `etc/rules.d/` here), then run
  `sudo homelab-maint rules check && sudo homelab-maint rules sync`.
- **What the site shows**: `sudo homelab-maint publish` rewrites `/var/lib/homelab-maint/public/`,
  which the dashboard reads through the hub.

## The dashboard link

Acknowledgement links in alert e-mails are minted by the hub (the dashboard) and point at
`https://maintainer.ohmzhomelab.ca` — see `[ack] mint_url` in the generated `notify.toml`.
