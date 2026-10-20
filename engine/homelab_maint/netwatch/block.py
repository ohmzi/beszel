"""netwatch.block: owner-confirmed blocks. Signed `net_block` / `net_unblock` requests from the logged-in dashboard arrive in the acks
inbox; this module validates them, checks the protection rules (hard ranges always, sign-ins only with a reason), and applies them.

Two targets:
  host        an element of a timeout set in the dedicated `inet netwatch` nftables table, with a drop rule per input/forward chain.
              ufw's rules are never touched.
  cloudflare  an IP access rule (mode block) on the zone that owns the hostname; its note carries the block id and the expiry date.

Blocks never happen automatically. Every row lands in the `blocks` table and every decision in the audit log.
"""
import hashlib
import ipaddress
import re
from datetime import datetime, timezone

from homelab_maint import core
from homelab_maint.netwatch import entities, protect, store
from homelab_maint.netwatch.cloudflare import CF, CFUnavailable

TABLE = "netwatch"
SETS = {4: "block4", 6: "block6"}
CHAINS = ("input", "forward")
TARGETS = ("host", "cloudflare")
MAX_DAYS = 365
REASON_MAX = 200
DAY_S = 86400
NET_BLOCK_KEYS = ("finding", "ip", "target", "zone", "days", "permanent", "override_reason")
NET_UNBLOCK_KEYS = ("block_id",)
_HEX16 = re.compile(r"[0-9a-f]{16}")
_UNTIL = re.compile(r" until (\d{4}-\d{2}-\d{2})$")


class NftError(Exception):
    """An nft command the table depends on failed."""


class _Refuse(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def block_id(ip: str, target: str, zone: str) -> str:
    """The same address, target and zone always give the same id, so a repeated block is recognised."""
    return hashlib.sha1(f"{ip}|{target}|{zone}".encode()).hexdigest()[:16]


def _date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def _norm_ip(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if getattr(addr, "scope_id", None):           # a scoped (zone-qualified) IPv6 address is not a firewall address
        return None
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr)


def _validate(req: dict) -> dict:
    """The checked fields of a net_block request, or _Refuse with a short reason. Nothing is applied before this passes."""
    ip = _norm_ip(req.get("ip"))
    if ip is None:
        raise _Refuse("bad-ip")
    target = req.get("target")
    if target not in TARGETS:
        raise _Refuse("bad-target")
    zone = req.get("zone", "")
    if not isinstance(zone, str) or (target == "cloudflare" and not zone) or (target == "host" and zone):
        raise _Refuse("bad-zone")          # a host block is keyed by its address alone
    days = req.get("days")
    if type(days) is not int or not 1 <= days <= MAX_DAYS:      # type() so that a bool is not an int here
        raise _Refuse("bad-days")
    permanent = req.get("permanent")
    if type(permanent) is not bool:
        raise _Refuse("bad-permanent")
    finding = req.get("finding")
    if not isinstance(finding, str) or not _HEX16.fullmatch(finding):
        raise _Refuse("bad-finding")
    reason = req.get("override_reason", "")
    if not isinstance(reason, str) or len(reason) > REASON_MAX or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in reason):
        raise _Refuse("bad-reason")
    return {"ip": ip, "target": target, "zone": zone, "days": days, "permanent": permanent, "reason": reason.strip()}


def _nft(sh, argv: list[str]):
    res = sh(argv)
    if res.returncode != 0:
        raise NftError(argv[1])
    return res


def ensure_table(sh=core.sh) -> None:
    """Create the table, the two timeout sets and, per chain, a drop rule for each set. Safe to call again: no rule is added twice."""
    _nft(sh, ["nft", "add", "table", "inet", TABLE])
    _nft(sh, ["nft", "add", "set", "inet", TABLE, SETS[4], "{ type ipv4_addr; flags timeout; }"])
    _nft(sh, ["nft", "add", "set", "inet", TABLE, SETS[6], "{ type ipv6_addr; flags timeout; }"])
    for name in CHAINS:
        _nft(sh, ["nft", "add", "chain", "inet", TABLE, name, f"{{ type filter hook {name} priority -10; policy accept; }}"])
        listing = _nft(sh, ["nft", "list", "chain", "inet", TABLE, name]).stdout or ""
        if "@block4" not in listing:
            _nft(sh, ["nft", "add", "rule", "inet", TABLE, name, "ip", "saddr", "@block4", "drop"])
        if "@block6" not in listing:
            _nft(sh, ["nft", "add", "rule", "inet", TABLE, name, "ip6", "saddr", "@block6", "drop"])


def _add_element(sh, ip: str, timeout: str | None) -> None:
    family = ipaddress.ip_address(ip).version
    body = f"{{ {ip} timeout {timeout} }}" if timeout else f"{{ {ip} }}"
    _nft(sh, ["nft", "add", "element", "inet", TABLE, SETS[family], body])


def _delete_element(sh, ip: str) -> None:
    family = ipaddress.ip_address(ip).version
    res = sh(["nft", "delete", "element", "inet", TABLE, SETS[family], f"{{ {ip} }}"])
    if res.returncode != 0:
        text = f"{res.stdout or ''} {res.stderr or ''}".lower()
        if "no such file or directory" in text or "does not exist" in text:
            return          # the element is already gone (its timeout ran out): the block is off
        raise NftError("delete")


def _cf(cf, cfg: dict):
    if cf is None:
        return CF(account_id=cfg.get("cf_account_id") or None)
    return cf


def _open(db):
    return (store.open_db(), True) if db is None else (db, False)


def apply_block(req: dict, now: float, *, cf=None, sh=core.sh, db=None) -> tuple[bool, str]:
    """Validate and apply one owner-confirmed block. Returns (ok, reason); nothing is written unless ok is True.
    `db` defaults to the Network Watch database (opened and closed here)."""
    db, own = _open(db)
    try:
        return _apply_block(req, now, cf, sh, db)
    except _Refuse as exc:
        return False, exc.reason
    except Exception:  # noqa: BLE001  (the inbox never sees a traceback)
        return False, "error"
    finally:
        if own:
            db.close()


def _apply_block(req: dict, now: float, cf, sh, db) -> tuple[bool, str]:
    r = _validate(req)
    ip, target, zone, reason = r["ip"], r["target"], r["zone"], r["reason"]
    bid = block_id(ip, target, zone)
    if db.execute("SELECT 1 FROM blocks WHERE id = ? AND removed IS NULL", (bid,)).fetchone():
        return True, "already-blocked"
    cfg = core.load_config().get("netwatch", {})
    protection = protect.protection(ip, db, entities.load_topology(sh), now, cfg)
    if protection and protection[0] == "hard":
        raise _Refuse(f"hard:{protection[1]}")
    signin = bool(protection and protection[0] == "signin")
    if signin and not reason:
        raise _Refuse("signin-protected")
    until = None if r["permanent"] else now + r["days"] * DAY_S
    rule_id = None
    if target == "host":
        try:
            ensure_table(sh)
            _add_element(sh, ip, None if r["permanent"] else f"{r['days']}d")
        except NftError:
            raise _Refuse("nft:add") from None
    else:
        when = "permanent" if r["permanent"] else f"until {_date(until)}"
        try:
            rule_id = _cf(cf, cfg).create_access_rule(zone, ip, f"netwatch:{bid} {when}")
        except CFUnavailable as exc:
            raise _Refuse(f"cloudflare:{exc.reason}") from None
    db.execute(
        "INSERT OR REPLACE INTO blocks (id, ip, target, zone, created, until, permanent, reason, cf_rule_id, removed)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (bid, ip, target, zone, now, until, int(r["permanent"]), reason, rule_id),
    )
    db.commit()
    core.audit("netwatch", "block", ip, 0, f"ok:{target}" + (f" override:{reason}" if reason and signin else ""))
    return True, "ok"


def apply_unblock(req: dict, now: float, *, cf=None, sh=core.sh, db=None) -> tuple[bool, str]:
    """Reverse an open block. An expired or unknown block is already off, so it is a success too."""
    db, own = _open(db)
    try:
        return _apply_unblock(req, now, cf, sh, db)
    except _Refuse as exc:
        return False, exc.reason
    except Exception:  # noqa: BLE001
        return False, "error"
    finally:
        if own:
            db.close()


def _apply_unblock(req: dict, now: float, cf, sh, db) -> tuple[bool, str]:
    bid = req.get("block_id")
    if not isinstance(bid, str) or not _HEX16.fullmatch(bid):
        raise _Refuse("bad-block-id")
    row = db.execute("SELECT ip, target, zone, cf_rule_id FROM blocks WHERE id = ? AND removed IS NULL", (bid,)).fetchone()
    if row is None:
        return True, "not-blocked"
    ip, target, zone, rule_id = row
    if target == "host":
        try:
            _delete_element(sh, ip)
        except NftError:
            raise _Refuse("nft:delete") from None
    elif rule_id:
        try:
            _cf(cf, core.load_config().get("netwatch", {})).delete_access_rule(zone, rule_id)
        except CFUnavailable as exc:
            if exc.reason != "http-404":
                raise _Refuse(f"cloudflare:{exc.reason}") from None
    if target == "host":                  # one open row per address: close every one of them, not only this id
        db.execute("UPDATE blocks SET removed = ? WHERE target = 'host' AND ip = ? AND removed IS NULL", (now, ip))
    else:
        db.execute("UPDATE blocks SET removed = ? WHERE id = ?", (now, bid))
    db.commit()
    core.audit("netwatch", "unblock", ip, 0, f"ok:{target}")
    return True, "ok"


def restore(db, now: float, sh=core.sh) -> str:
    """After a reboot the nft sets are empty: re-add every open, unexpired host block with its remaining time, unless the address
    is protected now (a hard range): such a row is closed and audited instead. Returns 'ok' or 'stale:nft'."""
    try:
        ensure_table(sh)
    except Exception:  # noqa: BLE001
        return "stale:nft"
    status = "ok"
    rows = db.execute("SELECT id, ip, until, permanent FROM blocks WHERE target = 'host' AND removed IS NULL").fetchall()
    topo, cfg = None, None
    for bid, ip, until, permanent in rows:
        left = None if permanent else int(until - now)
        if left is not None and left <= 0:
            db.execute("UPDATE blocks SET removed = ? WHERE id = ?", (now, bid))
            continue
        timeout = None if permanent else f"{left}s"
        try:
            if topo is None:
                topo = entities.load_topology(sh)
                cfg = core.load_config().get("netwatch", {})
            protection = protect.protection(ip, db, topo, now, cfg)
            if protection and protection[0] == "hard":
                db.execute("UPDATE blocks SET removed = ? WHERE id = ?", (now, bid))
                core.audit("netwatch", "unblock", ip, 0, "unblock:now-protected")
                continue
            _add_element(sh, ip, timeout)
        except Exception:  # noqa: BLE001
            status = "stale:nft"
    db.commit()
    return status


def sweep(db, now: float, cf=None) -> str:
    """Retire expired blocks. Host elements expire by themselves, so their rows are only closed. Cloudflare rules are deleted, and
    `netwatch:` rules whose date has passed are deleted even when no open row remembers them. Never raises.
    Returns 'ok', 'skipped' (no Cloudflare client: its rules are left alone) or 'stale:<reason>'."""
    status = "ok"
    try:
        db.execute("UPDATE blocks SET removed = ? WHERE target = 'host' AND removed IS NULL AND permanent = 0 AND until < ?", (now, now))
        db.commit()
        expired = db.execute(
            "SELECT id, zone, cf_rule_id FROM blocks WHERE target = 'cloudflare' AND removed IS NULL AND permanent = 0 AND until < ?",
            (now,),
        ).fetchall()
        zones = sorted({z for (z,) in db.execute("SELECT DISTINCT zone FROM blocks WHERE target = 'cloudflare'")})
        if cf is None:
            return "skipped" if (expired or zones) else "ok"
        for bid, zone, rule_id in expired:
            try:
                if rule_id:
                    cf.delete_access_rule(zone, rule_id)
                db.execute("UPDATE blocks SET removed = ? WHERE id = ?", (now, bid))
            except CFUnavailable as exc:
                if exc.reason == "http-404":
                    db.execute("UPDATE blocks SET removed = ? WHERE id = ?", (now, bid))
                else:
                    status = f"stale:{exc.reason}"
        db.commit()
        today = _date(now)
        for zone in zones:
            try:
                rules = cf.list_access_rules(zone)
            except CFUnavailable as exc:
                status = f"stale:{exc.reason}"
                continue
            for rule in rules:
                note = rule.get("notes") if isinstance(rule, dict) else None
                if not isinstance(note, str) or not note.startswith("netwatch:"):
                    continue
                m = _UNTIL.search(note)
                if not m or m.group(1) >= today:        # permanent, or the date has not passed yet
                    continue
                try:
                    cf.delete_access_rule(zone, rule["id"])
                except CFUnavailable as exc:
                    if exc.reason != "http-404":
                        status = f"stale:{exc.reason}"
    except Exception as exc:  # noqa: BLE001
        status = f"stale:{type(exc).__name__}"
    return status


def register(acks_mod) -> None:
    """Register both request kinds with the acks module that reads the inbox (idempotent)."""
    acks_mod.register_inbox_handler("net_block", apply_block, allowed_keys=NET_BLOCK_KEYS)
    acks_mod.register_inbox_handler("net_unblock", apply_unblock, allowed_keys=NET_UNBLOCK_KEYS)
