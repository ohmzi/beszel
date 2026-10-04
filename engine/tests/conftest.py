"""Shared pytest setup. Importing this module first points homelab_maint at throw-away dirs."""
import os, sys, tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="hm-tests-"))
for k, d in (("STATE", "state"), ("LOG", "log"), ("RUN", "run"), ("CONF", "conf")):
    p = _TMP / d
    p.mkdir(parents=True, exist_ok=True)
    os.environ[f"HOMELAB_MAINT_{k}"] = str(p)
sys.path.insert(0, str(_ROOT))

# No test writes to the host's journal: core.audit forks `logger` for every real attempt, and tests that build a real audit row (test_publish
# issuing ack tokens, test_native running the smart hook in a subprocess) left lines in syslog on every run. core.sh turns a `logger` argv into a
# no-op while this is set (subprocesses inherit it); a test that stubs core.sh sees its own stub as before.
os.environ["HOMELAB_MAINT_NO_SYSLOG"] = "1"
