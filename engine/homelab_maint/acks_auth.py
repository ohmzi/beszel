"""The runner side of the website's first-run login (SPEC6 section 7): the inbox kinds `auth_setup` and `auth_change`, and the ONE way
ack/auth.json is ever written. It was a verbatim copy of the old website's web/tools/ack_auth_handlers.py; that website has been decommissioned, so this module is now the sole implementation, and the parity test that compared them is gone with it. `register` also syncs the ack/ directory after each handled request. acks.process_inbox
calls register() before it reads the first request, so every process that serves the inbox (the tick, the ack-process job, `homelab-maint ack
process`) answers the setup screen. Stdlib only.

Wire format (both ride the normal signed inbox envelope of acks.register_inbox_handler: size, JSON, replay digest, +-skew and the HMAC
with ack/web.key are already checked when a handler runs):

  auth_setup  keys: rid pw totp recovery proof
    {"v":1,"kind":"auth_setup","source":"web","ts":int,"rid":"<8 hex>",
     "pw":{"alg":"pbkdf2-sha256","iter":600000,"salt":"<32 hex>","hash":"<64 hex>"},
     "totp":{"enc":"<64 hex>"}                  (optional; the base32 secret XOR HMAC-SHA256(bootstrap_key, "hm-totp-wrap-v1|"+rid))
     "recovery":["<32 hex>", ...]               (optional, only with totp; sha256(KDF+code)[:32])
     "proof":"<64 hex>","sig":"<64 hex>"}
    proof = HMAC-SHA256(bootstrap_key, canonical(request without "sig" and "proof")), bootstrap_key = sha256(b"homelab-maint web bootstrap v1\\0" + secret)
    where `secret` is the text of ack/bootstrap.secret (install.sh creates it, 0600 root; `homelab-maint web bootstrap` shows it once; strip()ped
    like the website does). The secret never reaches the inbox, and the authenticator secret is only ever in the file ENCRYPTED under it.
    Refused (and reported) when auth.json already exists (exists), the proof is wrong (bad_proof), there is no bootstrap.secret (no_bootstrap), the
    document is not what the website's parse_auth accepts (invalid), the file could not be made readable by the website's group (unreadable: it
    is NOT created, so the site stays in first-run setup) or the disk said no (write_failed). On success ack/auth.json is written (0640, group = the
    WEBSITE's group, see web_gid; atomic; verified readable). bootstrap.secret stays: setup is closed while auth.json exists.
  auth_change keys: rid op h
    {"v":1,"kind":"auth_change","source":"web","ts":int,"rid":"<8 hex>","op":"burn","h":"<32 hex>","sig":"..."}
    op "burn": remove recovery hash h from auth.json (a recovery code was used at login). Nothing else is ever changed this way.
  Both write ack/auth_result.json {"v":1,"results":{"<rid>":{"ok":bool,"reason":"ok|bad_proof|exists|invalid|no_bootstrap|no_auth|unknown_hash|unreadable|write_failed",
    "kind":"auth_setup|auth_change","at":ts}}} (0644, newest 20): the website shows the outcome of a setup attempt to the person who made it.
  auth.json {"v":1,"updated_at":ts,"pw":{...as above},"totp":{"secret","digits":6,"period":30}|absent,"recovery":["<32 hex>",...]}  (0640 root:<web gid>)

WHICH GROUP. The dashboard's ack-side reader runs as uid/gid 10001 with no supplementary groups, so auth.json must be group-readable by THAT gid.
web_gid() asks the files already shared with it: $HM_WEB_GID, else the group of ack/web.key, else of ack/inbox (ignoring root), else 10001. The
write verifies the result: a chgrp that was refused is reported (unreadable), never a silent success nobody can log in after.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

WEB_GID_DEFAULT = 10001
MIN_ITER = 600_000                                     # the website refuses weaker hashes (app.PBKDF2_ITER); tests lower both together
MAX_ITER = 10_000_000
BOOTSTRAP_KDF = b"homelab-maint web bootstrap v1\0"    # == app.BOOTSTRAP_KDF
WRAP_LABEL = b"hm-totp-wrap-v1|"                       # == app.TOTP_WRAP_LABEL
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX32 = re.compile(r"[0-9a-f]{32}")
RID = re.compile(r"[0-9a-f]{8}")
KEEP_RESULTS = 20


class Unreadable(OSError):
    """The file that was just written can not be read by the website's group (and the process could not change that)."""


# --------------------------------------------------------------------------- the website's group
def detect_web_gid(ack_dir, env=None, stat=os.lstat) -> int | None:
    """The gid the dashboard reads ack files as, from what is already shared with it, or None when nothing says."""
    env = os.environ if env is None else env
    raw = (env.get("HM_WEB_GID") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    for name in ("web.key", "inbox"):                    # both are root:<web gid> in a real install; a root-owned one says nothing
        try:
            gid = stat(os.path.join(os.fspath(ack_dir), name)).st_gid
        except OSError:
            continue
        if gid != 0:
            return gid
    return None


def web_gid(ack_dir, env=None, stat=os.lstat) -> int:
    gid = detect_web_gid(ack_dir, env, stat)
    return gid if gid is not None else WEB_GID_DEFAULT


def readable_by(st, gid: int) -> bool:
    """Could a process whose ONLY group is `gid` (and whose uid owns nothing here) read the file with this stat result?"""
    return bool(st.st_mode & 0o004) or (st.st_gid == gid and bool(st.st_mode & 0o040))


# --------------------------------------------------------------------------- bytes on disk
def write_atomic(path, data: bytes, mode: int, gid: int | None = None, *, verify: bool = False) -> None:
    """tmp file in the same directory (dot-name), group set BEFORE the content is written, fsync, rename. With `verify` the result must be
    readable by `gid` (raises Unreadable and leaves `path` exactly as it was): a chgrp that was refused is otherwise only a silent failure."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(3)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            if gid is not None:
                try:
                    os.fchown(f.fileno(), -1, gid)
                except PermissionError:
                    pass                                 # not root and not in that group: the verification below says whether it matters
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            st = os.fstat(f.fileno())
        if verify and gid is not None and not readable_by(st, gid):
            raise Unreadable(f"{path.name} would be group {st.st_gid} mode {st.st_mode & 0o777:o}, unreadable for gid {gid}")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_auth_file(ack_dir, doc: dict, gid: int | None) -> Path:
    """THE way auth.json is written (runner handler and host tool). gid None = leave the group as created (a dev sandbox that is not root);
    otherwise chgrp to it and VERIFY the web group can read the result (Unreadable when not)."""
    dest = Path(ack_dir) / "auth.json"
    write_atomic(dest, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode(), 0o640, gid, verify=gid is not None)
    return dest


@contextlib.contextmanager
def _locked(ack_dir):
    """Serialises the read-modify-write of auth.json between two handler runs (best effort: a directory we cannot create the lock in just runs)."""
    f = None
    try:
        f = open(os.path.join(os.fspath(ack_dir), ".auth.lock"), "a")
        fcntl.flock(f, fcntl.LOCK_EX)
    except OSError:
        pass
    try:
        yield
    finally:
        if f is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


# --------------------------------------------------------------------------- shared formats (equal to web/app.py: tests compare them)
def canonical(req: dict) -> bytes:
    """The bytes that are signed / proven: the request WITHOUT "sig", keys sorted, no spaces, ASCII (== acks.canonical == app.canonical)."""
    return json.dumps({k: v for k, v in req.items() if k != "sig"}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def bootstrap_key(secret: str) -> bytes:
    return hashlib.sha256(BOOTSTRAP_KDF + secret.encode("utf-8")).digest()


def totp_unwrap(enc, secret: str, rid: str) -> str | None:
    """The base32 authenticator secret from `totp.enc`, or None when it is not what the website sends."""
    if not (isinstance(enc, str) and re.fullmatch(r"(?:[0-9a-f]{2}){16,32}", enc) and isinstance(rid, str) and RID.fullmatch(rid)):
        return None
    pad = hmac.new(bootstrap_key(secret), WRAP_LABEL + rid.encode("ascii"), hashlib.sha256).digest()
    raw = bytes(a ^ b for a, b in zip(bytes.fromhex(enc), pad))
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return text if re.fullmatch(r"[A-Z2-7]{26,32}", text) else None


def _b32(text: str) -> bytes | None:
    try:
        raw = base64.b32decode(text.upper() + "=" * (-len(text) % 8), casefold=True)
    except (binascii.Error, ValueError):
        return None
    return raw or None


def validate_auth(obj) -> bool:
    """True when the website's parse_auth would accept this auth.json document (same rules, same limits)."""
    if not isinstance(obj, dict) or obj.get("v") != 1 or isinstance(obj.get("v"), bool):
        return False
    pw = obj.get("pw")
    if not isinstance(pw, dict) or pw.get("alg") != "pbkdf2-sha256":
        return False
    n, salt, digest = pw.get("iter"), pw.get("salt"), pw.get("hash")
    if not (isinstance(n, int) and not isinstance(n, bool) and MIN_ITER <= n <= MAX_ITER):
        return False
    if not (isinstance(salt, str) and re.fullmatch(r"(?:[0-9a-f]{2}){8,64}", salt) and isinstance(digest, str) and HEX64.fullmatch(digest)):
        return False
    totp = obj.get("totp")
    if totp is not None:
        if not isinstance(totp, dict) or not isinstance(totp.get("secret"), str) or totp.get("digits", 6) != 6 or totp.get("period", 30) != 30:
            return False
        secret = _b32(totp["secret"])
        if secret is None or not 10 <= len(secret) <= 64:
            return False
    rec = obj.get("recovery", [])
    return isinstance(rec, list) and len(rec) <= 32 and all(isinstance(h, str) and HEX32.fullmatch(h) for h in rec)


# --------------------------------------------------------------------------- quarantine hygiene
_B32_RUN = re.compile(rb"[A-Z2-7]{26,}")
_HEX_RUN = re.compile(rb"[0-9a-fA-F]{32,}")
SECRET_KEYS = ("pw", "totp", "recovery", "proof", "h")


def scrub(data: bytes) -> bytes:
    """What acks._quarantine should keep of a refused inbox file: for an auth_* request every secret-bearing value is replaced (valid JSON), and
    anything that does not parse loses its base32 and long hex runs. Other kinds are returned as they are (acks redacts 64-hex runs itself)."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        obj = None
    if isinstance(obj, dict):
        if str(obj.get("kind", "")).startswith("auth_"):
            for k in SECRET_KEYS:
                if k in obj:
                    obj[k] = "<redacted>"
            return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
        return data
    return _HEX_RUN.sub(lambda m: m.group(0)[:8] + b"..", _B32_RUN.sub(b"<redacted>", data))


# --------------------------------------------------------------------------- the handlers
def make_auth_handlers(ack_dir):
    """(auth_setup_handler, auth_change_handler): fn(request_without_sig, now) -> (ok, reason), for acks.register_inbox_handler."""
    ack_dir = Path(ack_dir)

    def report(req: dict, ok: bool, reason: str, now: float) -> tuple[bool, str]:
        rid = req.get("rid")
        if isinstance(rid, str) and RID.fullmatch(rid):
            path, results = ack_dir / "auth_result.json", {}
            try:
                old = json.loads(path.read_text())
                results = old["results"] if isinstance(old.get("results"), dict) else {}
            except (OSError, ValueError, KeyError, AttributeError):
                pass
            results[rid] = {"ok": ok, "reason": reason, "kind": req.get("kind", "auth_setup"), "at": now}
            for k in sorted(results, key=lambda k: results[k].get("at", 0) if isinstance(results[k], dict) else 0)[:-KEEP_RESULTS]:
                del results[k]
            try:
                write_atomic(path, json.dumps({"v": 1, "results": results}, sort_keys=True, separators=(",", ":")).encode(), 0o644)
            except OSError:
                pass                                     # the outcome is still the return value (acks.jsonl); only the site's hint is lost
        return ok, reason

    def put(doc: dict) -> str:
        """"" = written and readable by the website, else the reason code."""
        try:
            write_auth_file(ack_dir, doc, web_gid(ack_dir))
        except Unreadable:
            return "unreadable"
        except OSError:
            return "write_failed"
        return ""

    def setup(req: dict, now: float) -> tuple[bool, str]:
        with _locked(ack_dir):
            if os.path.lexists(ack_dir / "auth.json"):
                return report(req, False, "exists", now)
            try:
                secret = (ack_dir / "bootstrap.secret").read_text().strip()
            except (OSError, UnicodeDecodeError):
                return report(req, False, "no_bootstrap", now)
            if not secret:
                return report(req, False, "no_bootstrap", now)
            proof = hmac.new(bootstrap_key(secret), canonical({k: v for k, v in req.items() if k != "proof"}), hashlib.sha256).hexdigest()
            if not isinstance(req.get("proof"), str) or not hmac.compare_digest(proof, req["proof"]):
                return report(req, False, "bad_proof", now)
            doc = {"v": 1, "updated_at": int(now), "pw": req.get("pw")}
            if "totp" in req:
                t = req["totp"]
                secret_b32 = totp_unwrap(t.get("enc") if isinstance(t, dict) and set(t) == {"enc"} else None, secret, req.get("rid"))
                if secret_b32 is None:
                    return report(req, False, "invalid", now)
                doc["totp"] = {"secret": secret_b32, "digits": 6, "period": 30}
                doc["recovery"] = req.get("recovery", [])
            elif "recovery" in req:
                return report(req, False, "invalid", now)
            if not validate_auth(doc):
                return report(req, False, "invalid", now)
            why = put(doc)
            return report(req, not why, why or "ok", now)

    def change(req: dict, now: float) -> tuple[bool, str]:
        if req.get("op") != "burn" or not isinstance(req.get("h"), str) or not HEX32.fullmatch(req["h"]):
            return report(req, False, "invalid", now)
        with _locked(ack_dir):
            try:
                doc = json.loads((ack_dir / "auth.json").read_text())
            except (OSError, ValueError):
                return report(req, False, "no_auth", now)
            if not isinstance(doc, dict) or not isinstance(doc.get("recovery", []), list):
                return report(req, False, "no_auth", now)
            left = [h for h in doc.get("recovery", []) if h != req["h"]]
            if len(left) == len(doc.get("recovery", [])):
                return report(req, False, "unknown_hash", now)
            doc["recovery"], doc["updated_at"] = left, int(now)
            why = put(doc)
            return report(req, not why, why or "ok", now)

    return setup, change


def fsync_dir(path) -> None:
    """Flush a DIRECTORY entry to disk. write_atomic fsyncs the file, but the rename that publishes it only survives a power cut once the
    directory is synced too: without this a burned recovery code (or a finished setup) could come back after a crash. Best effort."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def durable(fn, ack_dir):
    """Wrap a handler so the directory is synced once it has written (also after a refusal: auth_result.json is written then too)."""
    def run(req: dict, now: float) -> tuple[bool, str]:
        try:
            return fn(req, now)
        finally:
            fsync_dir(ack_dir)
    return run


def register(acks_mod=None, ack_dir=None, kinds=("auth_setup", "auth_change")) -> None:
    """Register the handlers for `kinds` with the acks module that is READING the inbox (idempotent). Pass that module: under `python3 -m
    homelab_maint.acks` it is `__main__`, and a second import of the package module would register into a different table. ack_dir
    defaults to STATE_DIR/ack at call time."""
    from . import core
    if acks_mod is None:
        from . import acks as acks_mod
    ack_dir = ack_dir if ack_dir is not None else Path(core.STATE_DIR) / "ack"
    setup, change = (durable(f, ack_dir) for f in make_auth_handlers(ack_dir))   # (the handlers themselves stay verbatim the tested reference)
    setup._acks_auth = change._acks_auth = True          # ours: process_inbox may replace them (a test's or plugin's own handler is left alone)
    table = {"auth_setup": (setup, ("rid", "pw", "totp", "recovery", "proof")), "auth_change": (change, ("rid", "op", "h"))}
    for kind in kinds:
        acks_mod.register_inbox_handler(kind, table[kind][0], allowed_keys=table[kind][1])
