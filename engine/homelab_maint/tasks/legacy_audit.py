"""legacy_audit (C0, weekly): does anything on this host schedule itself that the migration inventory does not account for?

Read-only. Warns (alert=False, dashboard only) when a systemd timer, a crontab line or an /etc/cron.* file is in no
`etc/legacy-retirement.toml` item, so "one place that schedules everything" cannot silently rot. The logic is legacy.audit_result();
`homelab-maint migrate audit` prints the same list.
"""
from __future__ import annotations

from .. import legacy
from ..core import Ctx, Result, task


@task("legacy_audit", klass="C0", tier="weekly", title="Unaccounted schedulers", timeout=60)
def run(ctx: Ctx) -> Result:
    return legacy.audit_result()
