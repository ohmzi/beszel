"""notify_templates: the four renderings of every homelab-maint message (SMS, subject, plain text, HTML email).

One module so an alert, a recovery, a maintenance update, a digest, a weekly report and an incident all read
as one family, and the same family as the backup report (`/usr/local/sbin/backup_report_html.py`).

Look and feel
  * Palette and type come from the backup renderer. They are read from that file with `ast` (data only: the
    file is never imported or executed, so a root process never runs code it did not install) and checked
    against a strict pattern; anything missing or odd falls back to the embedded copy below, which a test
    keeps byte-identical to the reference. The block helpers (masthead, pill, stat tiles, section, kv list,
    bar, message card, footer, shell) are re-implemented here in the same markup: tables with inline styles,
    no flexbox/grid, no <style>, no webfonts, no external images (see backup_report_html's docstring for why).
  * Masthead "Omega Ohmz Maintenance", a 4px status-colour band on top of the panel, a status pill, the
    headline, summary card, stat tiles, facts, "what was done", "what to do", details, a dashboard link row
    and a mono footer. Colours stay real: green recovered/ok, amber-bright warning, red critical.
  * Phone-fluid: the 600px panel is `table-layout:fixed` and every text cell carries word-wrap/overflow-wrap/word-break, so an
    unbreakable token (path, unit name, hash, URL) wraps inside the card instead of widening or clipping it (measured in a 390px
    and a 320px headless Chrome viewport; tests/test_notify.py does the same when Chrome is installed).

Acknowledge block (SPEC5 S4 + S8)
  * An alert / incident_open / still-failing ack_expired email carries ONE call to action: "Understood and okay with it?" with a
    solid amber, table-based "bulletproof" button (dark text on amber, 8.2:1; 48px tall; the whole cell is the tap target; it wraps
    instead of overflowing on a 320px phone), the same promise in words, a note that the link only opens a confirmation page, the
    Issue ID (the 16-hex fingerprint, also what `homelab-maint ack add <id>` takes) and the bare URL as selectable text. The
    plain-text part has the line `Acknowledge (90 days): <url>`. The SMS never carries it.
  * The link is `<base>/ack?id=<fp>&t=<token>&d=<days>&s=<warn|crit>` (SPEC5 S8): the site binds id + severity to the token, so a
    stolen link cannot be re-pointed; `d` is one of 7/30/90/365 (snapped DOWN, never up). Opening it shows a review-and-confirm page;
    a GET never acknowledges anything.
  * This module only RENDERS: the caller (notify.py) issues the token and hands the finished URL in `prepare(ack=...)`, which
    re-validates it (`ack_url` / `_ACK_URL_RE`) so nothing but https://host[/path]/ack?id=..&t=..&d=..&s=.. can become an href.
    An offer with an `id` but no URL (the token could not be issued) still prints the Issue ID, as a facts row.

Safety
  * EVERY dynamic value goes through `esc()` (html.escape with quotes) before it reaches markup; the only
    unescaped strings are this file's own constants. The one `href` is built from the configured site URL
    (validated http/https) plus a fragment that must match a tight pattern, so `javascript:` cannot appear.
    tests/test_notify.py parses the output and rejects any tag outside a small allow-list and any `on*` attr.
  * All text is stripped of control characters (header injection) and passed through `scrub_secrets`
    (key=value secrets, bearer tokens, well-known token shapes) so a task summary that quotes a credential
    never leaves the host in a message.
  * The SMS is ASCII, one segment (<= 130 chars), with no URL, e-mail address or bare hostname: the carrier
    gateway silently drops such messages (measured by the Hermes team, see alert_transports.sms_body). The
    URL/TLD pattern below mirrors alert_transports.URL_RE; tests assert `sms_body(x) == x` when it is readable.

Pure functions: nothing here reads state, sends anything or touches the network.
"""
from __future__ import annotations

import ast
import html
import re
import socket
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- palette (verbatim from backup_report_html)
REFERENCE = "/usr/local/sbin/backup_report_html.py"
_DEFAULT_PALETTE = {
    "CANVAS": "#1a1917", "PANEL": "#211f1d", "RAISE": "#262421", "HOVER": "#2d2a26",
    "LINE": "#3a3733", "LINE_SOFT": "#302d2a",
    "TEXT": "#f0edea", "SECONDARY": "#cbc5be", "MUTED": "#8b857e",
    "AMBER": "#e0913f", "AMBER_BRIGHT": "#edaa5f", "ON_AMBER": "#241f18",
    "GREEN": "#86c06c", "RED": "#f87171",
    "RADIUS": "12px",
    "FONT": "'Space Grotesk',ui-sans-serif,system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif",
    "MONO": "'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace",
}
_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")
_CSSSAFE = re.compile(r"[A-Za-z0-9 ',._-]{1,200}\Z")          # no quotes-breaking, no ; : ( ) { } < >


def load_palette(path: str | Path = REFERENCE) -> dict:
    """Design tokens from the backup renderer when readable, else the embedded copy. Data only (ast), validated."""
    pal = dict(_DEFAULT_PALETTE)
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8")[:300_000])
    except (OSError, SyntaxError, ValueError):
        return pal
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        names = ([n.id for n in tgt.elts if isinstance(n, ast.Name)] if isinstance(tgt, ast.Tuple)
                 else [tgt.id] if isinstance(tgt, ast.Name) else [])
        try:
            val = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        vals = list(val) if (len(names) > 1 and isinstance(val, tuple)) else [val]
        if not names or len(names) != len(vals):
            continue
        for n, v in zip(names, vals):
            ok = isinstance(v, str) and (_CSSSAFE.match(v) if n in ("FONT", "MONO", "RADIUS") else _HEX.match(v))
            if n in pal and ok:
                pal[n] = v
    return pal


_P = load_palette()
CANVAS, PANEL, RAISE, HOVER = _P["CANVAS"], _P["PANEL"], _P["RAISE"], _P["HOVER"]
LINE, LINE_SOFT = _P["LINE"], _P["LINE_SOFT"]
TEXT, SECONDARY, MUTED = _P["TEXT"], _P["SECONDARY"], _P["MUTED"]
AMBER, AMBER_BRIGHT, ON_AMBER = _P["AMBER"], _P["AMBER_BRIGHT"], _P["ON_AMBER"]
GREEN, RED = _P["GREEN"], _P["RED"]
RADIUS, FONT, MONO = _P["RADIUS"], _P["FONT"], _P["MONO"]
_TABLE = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'
# Phone fluidity. An unbreakable token (a path, a unit or container name, a hash, a URL) in ANY cell must wrap, never widen the
# card: `overflow-wrap` alone does not shrink an auto-layout table's min-content width, `word-break:break-word` does, and the
# 600px panel is `table-layout:fixed` so its width never depends on its content. Every text cell carries this (clients that drop
# inheritance still get it) and the panel and <body> carry it too. tests/test_notify.py checks that no text cell is without it.
_WRAP = "word-wrap:break-word;overflow-wrap:break-word;word-break:break-word;"

KINDS = ("alert", "recovery", "maintenance", "digest_daily", "report_weekly", "incident_open",
         "incident_resolved", "ack_expired", "test")
SEVERITIES = ("ok", "info", "warn", "crit")
# Facts with a meaning for routing/rendering; they never show up as table rows.
RESERVED_FACTS = frozenset({"significant", "host", "link", "tiles", "confirmed", "was", "sev", "as", "notified", "sms",
                            "escalate", "ack", "ack_fp", "ack_mode", "ack_summary", "still_failing"})
_ACCENT = {"crit": RED, "warn": AMBER_BRIGHT, "ok": GREEN, "info": AMBER}

# --------------------------------------------------------------------------- text hygiene
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f  ‪-‮⁦-⁩]")   # incl. bidi overrides
# A secret is `<identifier containing a secret word><sep><value>`. The word is matched INSIDE the identifier (no \b: `_` is
# a word character, so \bPASS never matched SMTP_PASS, TWILIO_AUTH_TOKEN, CANCEL_SECRET, PGPASSWORD, access_token ...).
# The prefix before the word is not part of the match so it stays in the text; the suffix is bounded so a long token
# cannot make the regex quadratic. Bare `pass` / `pwd` must be a whole word ("passed: 5" and "bypass: on" stay readable).
_SECRET_WORD = (r"(?:pass(?:word|wd|phrase)|(?<![a-z])(?:pass|pwd)(?![a-z])|token|secret|api[_-]?key|private[_-]?key|"
                r"credential|authori[sz]ation|cookie|session[_-]?id)")
_SECRET_VALUE = r"(?:\"[^\"]*\"|'[^']*'|(?:(?:bearer|basic)[ \t]+)?[^\s,;\"']+)"
_SECRET_KV = re.compile(rf"(?i)({_SECRET_WORD}[\w.-]{{0,40}})([\"']?[ \t]*[=:][ \t]*){_SECRET_VALUE}")
# `--password hunter2` / `--token abc` (space separated: the flag is the key) and mysql's glued `-phunter2`.
_SECRET_FLAG = re.compile(rf"(?i)(--?{_SECRET_WORD}[\w-]{{0,20}}[ \t]+)(?!-)(?:\"[^\"]*\"|'[^']*'|\S+)")
_MYSQL_P = re.compile(r"(?i)\b(mysql\w*|mariadb[\w-]*)\b([^\n]*?[ \t])-p(?=\S)\S+")
# A private key block, even a truncated one (a log excerpt can end mid-block): to the end marker, else to the end.
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S)
_B64_LINE = re.compile(r"[A-Za-z0-9+/=]{40,}\Z")        # a key-body line whose BEGIN marker fell off the front of an excerpt
_SECRET_SHAPES = re.compile(
    r"(?i)\bbearer[ \t]+[\w.~+/=-]{8,}|\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}"
    r"|\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}|\bAC[0-9a-f]{32}\b")
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@")
# An acknowledgement link carries a bearer token: `/ack?id=<fp>&t=<token>&d=90&s=crit` (SPEC5 S8; the earlier `/ack/<token>` shape is
# scrubbed too). A summary or log excerpt that quotes one must not carry it out of the host.
_ACK_PATH = re.compile(r"(/ack/)[A-Za-z0-9_-]{16,}")
_ACK_QUERY = re.compile(r"(?i)(/ack\?[^\s\"'<>]*?\bt=)[A-Za-z0-9_-]{16,}")


def scrub_secrets(text: str) -> str:
    """Remove credentials a message could quote by accident. Keeps everything else (paths, hosts, numbers)."""
    text = _PEM.sub("<redacted private key>", text)
    text = _ACK_PATH.sub(r"\1<redacted>", text)
    text = _ACK_QUERY.sub(r"\1<redacted>", text)
    text = _URL_USERINFO.sub(r"\1<redacted>@", text)
    text = _SECRET_KV.sub(lambda m: m.group(1) + "=<redacted>", text)
    text = _SECRET_FLAG.sub(lambda m: m.group(1) + "<redacted>", text)
    text = _MYSQL_P.sub(lambda m: f"{m.group(1)}{m.group(2)}-p<redacted>", text)
    return _SECRET_SHAPES.sub("<redacted>", text)


def line(v, n: int = 200) -> str:
    """One clean line: control/bidi characters gone, whitespace collapsed, secrets scrubbed, clipped to n."""
    s = " ".join(_CTRL.sub(" ", "" if v is None else str(v)).split())
    s = scrub_secrets(s[:n * 4 + 400])                  # scrub only what can still be shown: a 20 KB line must stay cheap
    return s if len(s) <= n else s[:max(n - 3, 0)].rstrip() + "..."


def lines(v, items: int = 20, n: int = 240) -> list[str]:
    """str (one entry per non-empty line) or list/tuple of anything -> up to `items` clean lines (the FIRST ones)."""
    if v is None:
        return []
    raw = _PEM.sub("<redacted private key>", v).splitlines() if isinstance(v, str) else [x for x in v] if isinstance(v, (list, tuple)) else [v]
    out = [line(x, n) for x in raw]
    return [x for x in out if x][:items]


LOG_WINDOW = 64_000          # characters of a log looked at: its END is what matters, and a bound keeps scrubbing cheap


def log_lines(v, items: int = 40, n: int = 240) -> list[str]:
    """A log excerpt: the LAST `items` non-empty lines, never the first (the failure is at the end of a log). The tail is
    cut BEFORE any per-line work; when that cut falls inside a line the fragment is dropped; private-key blocks are
    scrubbed on the whole text (they span lines); a line that is only base64 (a key body whose BEGIN marker fell off the
    front of the window) is replaced."""
    if v is None:
        return []
    text = v if isinstance(v, str) else "\n".join("" if x is None else str(x) for x in v[-4 * items:]) if isinstance(v, (list, tuple)) else str(v)
    cut = len(text) > LOG_WINDOW
    if cut:
        text = text[-LOG_WINDOW:]
    raw = _PEM.sub("<redacted private key>", text).splitlines()
    if cut and len(raw) > 1:
        raw = raw[1:]                                    # the first line starts mid-line (a window that is ONE line keeps it: a fragment beats nothing)
    raw = [x for x in raw if x.strip()][-(items + 8):]   # a few spare: scrubbing can empty a line
    out = ["<redacted blob>" if _B64_LINE.match(x.strip()) else line(x, n) for x in raw]
    return [x for x in out if x][-items:]


def truthy(v) -> bool:
    """A fact flag that survives the outbox round trip and a `--fact ack=false` hook: True, a non-zero number, or yes/true/on/1."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def esc(v) -> str:
    """HTML-escape (quotes included) anything; None is empty. The single escape every block uses."""
    return html.escape("" if v is None else str(v))


def dur(sec) -> str:
    sec = max(0, int(sec or 0))
    if sec >= 86400:
        return f"{sec // 86400}d{(sec % 86400) // 3600:02d}h"
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


def _val(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, (list, tuple)):
        return ", ".join(line(x, 60) for x in v[:8])
    return line(v, 200)


# --------------------------------------------------------------------------- prepared model
@dataclass
class Model:
    kind: str                       # event kind as sent
    template: str                   # layout used (== kind, except `test` mimics another kind)
    sev: str
    host: str
    pill: str
    accent: str
    title: str
    summary: str
    task: str = ""
    when: str = ""
    facts: list = field(default_factory=list)       # [(label, value)]
    tiles: list = field(default_factory=list)       # [(value, label, accent|None)]
    done: list = field(default_factory=list)
    todo: list = field(default_factory=list)
    paras: list = field(default_factory=list)
    timeline: list = field(default_factory=list)    # [(when, text)]
    log: list = field(default_factory=list)
    sections: list = field(default_factory=list)    # [{"title", "lines", "kv", "checks"}]
    url: str = ""
    test: bool = False
    notified: int = 0
    sms: str = ""                   # caller-provided SMS wording (replaces the summary part), still sanitised
    # the Acknowledge offer (empty ack_url = none): the URL is a bearer token, so it lives ONLY in the email bodies built from this
    ack_url: str = ""
    ack_id: str = ""                # the issue fingerprint ("Issue ID"): not a secret, shown even when no link could be made
    ack_days: int = 0               # what the button promises ("Acknowledge for 90 days")
    ack_ttl_days: int = 0           # how long the link itself is valid
    ack_text: str = ""              # button label (validated, escaped when rendered)
    ack_escalates: bool = True      # wording only: "if it gets worse you are told again"
    ack_demo: bool = False          # a preview/test link: it is shown but is not a real token


_SEV_WORD = {"critical": "crit", "error": "crit", "err": "crit", "fatal": "crit", "warning": "warn", "degraded": "warn",
             "good": "ok", "success": "ok", "fine": "ok", "skipped": "info", "notice": "info"}


def _sev_word(x) -> str:
    s = str(x or "").strip().lower()
    return _SEV_WORD.get(s, s)


def norm_severity(sev, status=None, kind: str = "") -> str:
    """Severity word for an event. `sev` unset -> derived from `status`; both set -> the MORE severe one (a caller that
    says status="crit" and forgets severity, or the reverse, must never be demoted to an info email). An explicit but
    unknown `sev` is not guessed from the status."""
    a, b = _sev_word(sev), _sev_word(status)
    if a in SEVERITIES and b in SEVERITIES:
        return a if SEVERITIES.index(a) >= SEVERITIES.index(b) else b       # SEVERITIES is ordered ok < info < warn < crit
    s = a or b
    if s in SEVERITIES:
        return s
    return "ok" if kind in ("recovery", "incident_resolved") else "info"


def site_url(base: str, fragment) -> str:
    """Dashboard link: http(s) base from config + an optional tight fragment ("#/incidents"). Else ""."""
    base = str(base or "").strip()
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/-]*)?", base):
        return ""
    frag = str(fragment or "").strip()
    if frag and not re.fullmatch(r"#?/?[A-Za-z0-9][A-Za-z0-9/_.-]{0,60}", frag):
        frag = ""
    if frag and ".." in frag:
        frag = ""
    if not frag:
        return base
    return base.rstrip("/") + "/" + (frag if frag.startswith("#") else "#/" + frag.lstrip("/"))


# --------------------------------------------------------------------------- acknowledge links (SPEC5 S4, S8)
ACK_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")               # secrets.token_urlsafe(32) is exactly 43 characters of this alphabet
ACK_FP_RE = re.compile(r"[0-9a-f]{16}\Z")                        # the issue fingerprint: sha1(...)[:16]
ACK_DAYS_ALLOWED = (7, 30, 90, 365)                              # what the site accepts for `d`
_ACK_URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/-]*)?/ack\?id=([0-9a-f]{16})&t=([A-Za-z0-9_-]{43})"
                         r"&d=(7|30|90|365)&s=(warn|crit)\Z")
# What a preview or a TEST shows instead of a token. It has the real shape (43 characters) so the layout is the real one, and it can
# never be a token the site knows (the site only honours the SHA-256 of a token it issued).
PREVIEW_TOKEN = "PREVIEW-this-link-does-nothing-000000000000"
PREVIEW_ID = "0123456789abcdef"
ACK_PROMISE = "You will stop receiving this exact alert; it stays listed as an acknowledged issue on the maintenance site."
ACK_DEFAULT_TEXT = "Acknowledge for {days} days"


def snap_days(days) -> int:
    """The longest allowed duration that is not longer than asked (a configured 60 means 30, never 90); below the shortest, the
    shortest. The link and the words around the button both use this, so the promise is what the site will do."""
    d = _to_int(days, 90)
    return max((x for x in ACK_DAYS_ALLOWED if x <= d), default=ACK_DAYS_ALLOWED[0])


def ack_url(base, token, fp, days=90, sev="warn") -> str:
    """`<base>/ack?id=<fp>&t=<token>&d=<days>&s=<sev>` from the configured http(s) base, a 43-character token, a 16-hex fingerprint
    and a severity (warn|crit); else "". The only way an acknowledge link is ever built, so a hostile base or token (quotes,
    spaces, `javascript:`, a second `&`) yields no link at all."""
    base = site_url(base, "")
    tok, fid, s = str(token or ""), str(fp or "").strip().lower(), str(sev or "").strip().lower()
    if not base or s not in ("warn", "crit") or not ACK_FP_RE.match(fid) or not ACK_TOKEN_RE.match(tok):
        return ""
    url = f"{base.rstrip('/')}/ack?id={fid}&t={tok}&d={snap_days(days)}&s={s}"
    return url if _ACK_URL_RE.match(url) else ""


def button_label(text, days: int) -> str:
    """Config `button_text` with its one placeholder {days} (a plain replace: no str.format, so nothing in the text is evaluated)."""
    label = line(str(text or ACK_DEFAULT_TEXT).replace("{days}", str(int(days))), 60)
    return label or ACK_DEFAULT_TEXT.replace("{days}", str(int(days)))


def _ack_texts(m: "Model") -> tuple[str, str, str]:
    """(label, what it does, fine print): the words around the button, shared by the HTML card and the plain-text block."""
    d = f"{m.ack_days} days"
    if m.template == "ack_expired":
        label = "Still okay with it?"
        intro = f"If you are still fine with this exact error, acknowledge it again for {d}. {ACK_PROMISE}"
    else:
        label = "Understood and okay with it?"
        intro = ACK_PROMISE
    intro += (" If it gets worse, or anything else fails, you are told as usual." if m.ack_escalates
              else " Any other problem still alerts you as usual.")
    fine = ("The button opens a confirmation page: nothing changes until you confirm there. "
            f"The link works once and expires in {m.ack_ttl_days} days.")
    if m.ack_demo:
        fine += " PREVIEW: this link is not valid."
    return label, intro, fine


def _pill_word(template: str, sev: str, sev_label: str) -> str:
    if template == "alert":
        return {"crit": "Critical", "warn": "Warning"}.get(sev, "Notice")
    if template == "recovery":
        return "Recovered"
    if template == "maintenance":
        return "Maintenance warning" if sev in ("warn", "crit") else "Maintenance done"
    if template == "digest_daily":
        return "Daily digest"
    if template == "report_weekly":
        return "Weekly report"
    if template == "incident_open":
        return f"Incident {sev_label}".strip() if sev_label else "Incident opened"
    if template == "incident_resolved":
        return "Incident resolved"
    if template == "ack_expired":
        return "Acknowledgement expired"
    return "Notice"


def _timeline(v) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for it in (v if isinstance(v, (list, tuple)) else lines(v)):
        if isinstance(it, dict):
            out.append((line(it.get("when") or it.get("t") or it.get("time"), 30), line(it.get("text") or it.get("what"), 160)))
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            out.append((line(it[0], 30), line(it[1], 160)))
        else:
            w, sep, rest = str(it).partition(": ")
            out.append((line(w, 30), line(rest, 160)) if sep else ("", line(it, 160)))
    return [t for t in out if t[0] or t[1]][:20]


def _sections(v) -> list[dict]:
    out = []
    for s in (v if isinstance(v, (list, tuple)) else [])[:8]:
        if not isinstance(s, dict):
            continue
        sec = {"title": line(s.get("title"), 60), "lines": lines(s.get("lines") or s.get("text"), 30, 240),
               "kv": [(line(p[0], 60), _val(p[1])) for p in (s.get("kv") or []) if isinstance(p, (list, tuple)) and len(p) >= 2][:20],
               "checks": [(line(c[0], 80), c[1] if isinstance(c[1], bool) or c[1] is None else bool(c[1]),
                           line(c[2] if len(c) > 2 else "", 60))
                          for c in (s.get("checks") or []) if isinstance(c, (list, tuple)) and len(c) >= 2][:20]}
        if sec["title"] and (sec["lines"] or sec["kv"] or sec["checks"]):
            out.append(sec)
    return out


def clean_facts(facts) -> dict:
    """Event.facts -> a small JSON-safe dict of already-clean values (what prepare() would show), for storage in the notify
    outbox. Reserved facts keep their meaning (tiles stay tiles, flags stay truthy); every other value becomes its display string."""
    try:
        src = facts if isinstance(facts, dict) else dict(facts or []) if isinstance(facts, (list, tuple)) else {}
    except (TypeError, ValueError):
        return {}
    out: dict = {}
    for k, v in list(src.items())[:40]:
        key = line(k, 40)
        if not key or v is None:
            continue
        if key == "tiles":
            out[key] = [[line(t[0], 14), line(t[1], 20)] + ([str(t[2]).lower()[:8]] if len(t) > 2 else [])
                        for t in (v if isinstance(v, (list, tuple)) else [])[:4] if isinstance(t, (list, tuple)) and len(t) >= 2]
        elif key in ("significant", "escalate", "confirmed", "ack", "still_failing"):
            out[key] = truthy(v)
        elif key == "ack_fp":
            out[key] = line(v, 16)
        elif key == "ack_mode":
            out[key] = line(v, 12)
        elif key == "ack_summary":
            out[key] = line(v, 200)
        elif key == "notified":
            out[key] = int(v) if str(v).isdigit() else 0
        else:
            out[key] = _val(v)
    return out


def clean_details(d) -> dict:
    """Event.details (str | list | dict) -> the clean, bounded, JSON-safe dict prepare() would build its sections from; an
    entry that is empty is left out. Used to keep a failed critical page in the notify outbox without a raw log or a credential."""
    det = d if isinstance(d, dict) else {"text": d}
    parts = (("todo", lines(det.get("todo") or det.get("what_to_do") or det.get("playbook"), 12, 300)),
             ("done", lines(det.get("done") or det.get("what_was_done"), 20, 200)),
             ("text", lines(det.get("text"), 20, 300)),
             ("timeline", [list(t) for t in _timeline(det.get("timeline"))]),
             ("log", log_lines(det.get("log"), 30, 200)),
             ("sections", [{**s_, "kv": [list(p) for p in s_["kv"]], "checks": [list(c) for c in s_["checks"]]}
                           for s_ in _sections(det.get("sections"))]))
    return {k: v for k, v in parts if v}


def _to_int(v, default: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def prepare(ev, *, site: dict | None = None, todo_default=None, now: float | None = None, ack: dict | None = None) -> Model:
    """Event (duck-typed: kind/severity/title/summary/details/facts/status/task) -> clean, bounded Model.
    `ack` = {"url", "days", "ttl_days", "text", "escalates", "demo"} offers the Acknowledge button; the URL is validated again here
    (ack_url's shape or nothing), so a bad one simply means no button."""
    site = site or {}
    now = time.time() if now is None else now
    kind = ev.kind if ev.kind in KINDS else "alert"
    facts = ev.facts if isinstance(ev.facts, dict) else dict(ev.facts or []) if isinstance(ev.facts, (list, tuple)) else {}
    template = kind
    if kind == "test":
        template = facts.get("as") if facts.get("as") in KINDS and facts.get("as") != "test" else "alert"
    sev = norm_severity(ev.severity, ev.status, template)
    sev_label = line(facts.get("sev"), 12)
    accent = {"recovery": GREEN, "incident_resolved": GREEN}.get(template) or _ACCENT[sev]
    if kind == "test":
        accent = AMBER
    d = ev.details
    det = d if isinstance(d, dict) else {"text": d}
    task = line(ev.task, 60)
    todo = lines(det.get("todo") or det.get("what_to_do") or det.get("playbook"), 12, 300)
    if not todo and todo_default and template in ("alert", "incident_open"):
        todo = lines(todo_default, 12, 300)
    tiles = []
    for t in (facts.get("tiles") or [])[:4]:
        if isinstance(t, (list, tuple)) and len(t) >= 2:
            tiles.append((line(t[0], 14), line(t[1], 20), _ACCENT.get(str(t[2]).lower()) if len(t) > 2 else None))
    rows = [(line(k, 40), _val(v)) for k, v in facts.items() if k not in RESERVED_FACTS and v is not None][:24]
    offer: dict = {}
    if isinstance(ack, dict):
        um = _ACK_URL_RE.match(str(ack.get("url") or ""))
        fid = um[1] if um else str(ack.get("id") or "").strip().lower()
        if um:               # id and days come from the validated URL itself: the words around the button always match what the link does
            days = int(um[3])
            offer = {"ack_url": str(ack["url"]), "ack_id": fid, "ack_days": days,
                     "ack_ttl_days": int(min(max(_to_int(ack.get("ttl_days"), 30), 1), 365)),
                     "ack_text": button_label(ack.get("text"), days), "ack_escalates": ack.get("escalates", True) is not False,
                     "ack_demo": bool(ack.get("demo"))}
        elif ACK_FP_RE.match(fid):                                   # no link could be made: the Issue ID is still useful
            offer = {"ack_id": fid}
            if not any(k == "Issue ID" for k, _v in rows):
                rows.append(("Issue ID", fid))
    return Model(
        kind=kind, template=template, sev=sev, host=line(facts.get("host") or site.get("host_label") or socket.gethostname(), 60),
        pill=_pill_word(template, sev, sev_label), accent=accent, title=line(ev.title, 100) or "(no title)",
        summary=line(ev.summary, 600), task=task,
        when=time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(now)),
        facts=rows, tiles=tiles, done=lines(det.get("done") or det.get("what_was_done"), 20, 200), todo=todo,
        paras=lines(det.get("text"), 20, 300), timeline=_timeline(det.get("timeline")),
        log=log_lines(det.get("log"), 40, 240), sections=_sections(det.get("sections")),
        url=site_url(site.get("url"), facts.get("link")), test=(kind == "test"), sms=line(facts.get("sms"), 200),
        notified=int(facts["notified"]) if str(facts.get("notified", "")).isdigit() else 0, **offer)


# --------------------------------------------------------------------------- SMS
_TLD = (r"(?:com|net|org|edu|gov|mil|int|info|biz|name|pro|mobi|asia|io|co|ai|app|dev|page|site|"
        r"online|shop|store|cloud|tech|blog|news|live|life|world|today|space|website|link|click|"
        r"media|video|studio|design|art|music|games?|fun|xyz|top|icu|vip|cc|ws|me|tv|ly|to|sh|gg|"
        r"fm|am|nu|bz|ca|uk|us|de|fr|jp|cn|au|nz|in|br|mx|es|it|nl|se|no|fi|dk|pl|ru|ch|at|be|pt|"
        r"gr|ie|il|za|kr|sg|hk|tw|th|my|ph|id|vn|tr|ua|cz|hu|ro|bg|hr|rs|sk|si|lt|lv|ee|is|lu|ar|"
        r"cl|pe|eu)")
_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
URL_RE = re.compile(rf"{_EMAIL}|https?://\S+|\bwww\.[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?"
                    rf"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.{_TLD}\b(?:/\S*)?", re.I)
_FOLD = {"—": "-", "–": "-", "…": "...", "‘": "'", "’": "'", "“": '"', "”": '"',
         " ": " ", "·": "-", "•": "-", "€": "EUR", "£": "GBP", "¥": "JPY", "→": "->",
         "✓": "ok", "✗": "x"}
SMS_LIMIT = 130
_SMS_WORD = {"alert": {"crit": "CRIT", "warn": "WARN"}, "recovery": "OK", "maintenance": "DONE",
             "digest_daily": "DIGEST", "report_weekly": "REPORT", "incident_open": "INCIDENT",
             "incident_resolved": "RESOLVED", "ack_expired": "ACK ENDED"}


def ascii_fold(text: str) -> str:
    for bad, good in _FOLD.items():
        text = text.replace(bad, good)
    return text.encode("ascii", "ignore").decode("ascii")


def sms_clean(text) -> str:
    """ASCII, one line, no URL/e-mail/bare hostname. A dotted name keeps its words ("smart-alert.sh" ->
    "smart-alert sh") instead of vanishing, because the gateway would drop the whole text for the dot."""
    s = ascii_fold(line(text, 1000))

    def _sub(m: re.Match) -> str:
        t = m.group(0)
        return "" if ("@" in t or t.lower().startswith(("http", "www."))) else t.replace(".", " ")
    s = URL_RE.sub(_sub, s)
    return " ".join(s.split()).strip(" -")


def build_sms(m: Model, prefix: str = "homelab", limit: int = SMS_LIMIT) -> str:
    """`homelab: CRIT disk / 4% free`. Severity word and title always survive; the summary is trimmed first.
    `facts["sms"]` replaces the summary part when the caller has a better one-liner (still sanitised)."""
    w = _SMS_WORD.get(m.template, "INFO")
    word = w.get(m.sev, "INFO") if isinstance(w, dict) else w
    if m.test:
        word = f"TEST {word}"
    head = f"{sms_clean(prefix) or 'homelab'}: {word} "
    title = sms_clean(m.title) or "notice"
    summary = sms_clean(m.sms) or sms_clean(m.summary) or ("recovered" if m.template in ("recovery", "incident_resolved") else "")
    if m.test:
        summary = "ignore, notification test"
    out = head + title
    if len(out) > limit:
        out = out[:limit - 3].rstrip() + "..."
    elif summary:
        sep = " - " if summary.startswith("/") else " / "          # "Disk space / / is 4% free" reads badly
        room = limit - len(out) - len(sep)
        if room >= 6:
            out += sep + (summary if len(summary) <= room else summary[:room - 3].rstrip() + "...")
    return out.rstrip(" -")


# --------------------------------------------------------------------------- subject
def build_subject(m: Model, prefix: str = "[homelab] ") -> str:
    """`[homelab] CRIT: Disk space - / is 4% free ...` (one ASCII line <= 110 chars; EmailMessage rejects CR/LF)."""
    w = _SMS_WORD.get(m.template, "INFO")
    head = {"maintenance": "Maintenance", "digest_daily": "Daily digest", "report_weekly": "Weekly report",
            "ack_expired": "Ack expired"}.get(
        m.template) or (w.get(m.sev, "INFO") if isinstance(w, dict) else w)
    if m.test:
        head = f"TEST {m.template.replace('_', ' ')}"
    s = f"{prefix}{head}: {m.title}" + (f" - {m.summary}" if m.summary else "")
    return ascii_fold(line(s, 110)).strip()


# --------------------------------------------------------------------------- plain text
def build_plain(m: Model, footer_name: str = "homelab-maint") -> str:
    out: list[str] = []
    if m.test:
        out += ["*** TEST MESSAGE: nothing is wrong and nothing needs doing. ***", ""]
    out += [f"[{m.pill.upper()}] {m.title}"]
    if m.summary:
        out += [m.summary]
    if m.ack_url:
        _label, intro, fine = _ack_texts(m)
        out += ["", f"Acknowledge ({m.ack_days} days): {m.ack_url}", f"  Issue ID: {m.ack_id}"]
        out += textwrap.wrap(intro, 76, initial_indent="  ", subsequent_indent="  ") + textwrap.wrap(fine, 76, initial_indent="  ", subsequent_indent="  ")
    elif m.ack_id and not any(k == "Issue ID" for k, _v in m.facts):      # no link (none could be made, or a copy for a transport that keeps bodies)
        out += ["", f"Issue ID: {m.ack_id}"]
    out += ["", f"Host: {m.host}", f"Time: {m.when}"]
    if m.task:
        out += [f"Check: {m.task}"]
    if m.notified > 1:
        out += [f"Notified: {m.notified} times (still unresolved)"]
    if m.tiles:
        out += [""] + [f"  {v} {lab}" for v, lab, _a in m.tiles]
    if m.facts:
        out += ["", "Facts"] + [f"  {k}: {v}" for k, v in m.facts]
    if m.done:
        out += ["", "What was done"] + [f"  - {x}" for x in m.done]
    if m.todo:
        out += ["", "What to do"] + [f"  {i}. {x}" for i, x in enumerate(m.todo, 1)]
    if m.timeline:
        out += ["", "Timeline"] + [f"  {w}  {t}".rstrip() for w, t in m.timeline]
    if m.paras:
        out += ["", "Highlights" if m.template in ("digest_daily", "report_weekly") else "Details"] + [f"  {x}" for x in m.paras]
    for s in m.sections:
        out += ["", s["title"]] + [f"  {x}" for x in s["lines"]] + [f"  {k}: {v}" for k, v in s["kv"]] + \
               [f"  [{'ok' if ok else '--' if ok is None else 'FAIL'}] {n} {note}".rstrip() for n, ok, note in s["checks"]]
    if m.log:
        out += ["", "Log excerpt"] + [f"  {x}" for x in m.log]
    out += [""]
    if m.url:
        out += [f"Dashboard: {m.url}"]
    out += [f"Sent by {footer_name} on {m.host}."]
    return "\n".join(out)[:30000]


# --------------------------------------------------------------------------- HTML blocks
def _strip(colour: str) -> str:
    return f'<tr><td height="4" style="height:4px;background:{colour};font-size:0;line-height:0;">&nbsp;</td></tr>'


def _masthead(word: str, right: str) -> str:
    return f"""
      <tr><td style="padding:22px 28px;border-bottom:1px solid {LINE_SOFT};">
        <table {_TABLE} width="100%">
          <tr>
            <td align="left" style="font:600 15px/1 {FONT};color:{TEXT};letter-spacing:-.01em;{_WRAP}">
              <span style="color:{AMBER};font-size:17px;">&#937;</span>&nbsp; Ohmz
              <span style="color:{AMBER};">{esc(word)}</span>
            </td>
            <td align="right" style="font:500 12px/1.3 {MONO};color:{MUTED};{_WRAP}">{esc(right)}</td>
          </tr></table></td></tr>"""


def _pill(text: str, colour: str) -> str:
    return f"""
        <table {_TABLE}>
          <tr><td style="background:{colour};border-radius:999px;padding:5px 13px;
                         font:600 11px/1 {FONT};letter-spacing:.1em;text-transform:uppercase;
                         color:{ON_AMBER};white-space:nowrap;">{esc(text)}</td></tr>
        </table>"""


def _title(pill: str, title: str, sub: str) -> str:
    return f"""
      <tr><td style="padding:30px 28px 0 28px;">{pill}
        <div style="font:600 27px/1.2 {FONT};color:{TEXT};letter-spacing:-.02em;
                    padding:16px 0 0 0;{_WRAP}">{esc(title)}</div>
        <div style="font:400 13px/1.5 {FONT};color:{SECONDARY};padding-top:6px;{_WRAP}">{sub}</div></td></tr>"""


def _card(label: str, text: str, colour: str) -> str:
    body = (f"""<div style="font:400 13px/1.55 {FONT};color:{SECONDARY};padding-top:4px;
                 {_WRAP}">{esc(text)}</div>""" if text else "")
    return f"""
      <tr><td style="padding:22px 28px 0 28px;">
        <table {_TABLE} width="100%" style="width:100%;background:{RAISE};border-radius:9px;border-left:3px solid {colour};">
          <tr><td style="padding:11px 13px;">
            <span style="font:600 10px/1.5 {FONT};letter-spacing:.1em;color:{colour};text-transform:uppercase;">{esc(label)}</span>
            {body}</td></tr></table></td></tr>"""


def _tiles(tiles) -> str:
    if not tiles:
        return ""
    w = 100 // len(tiles)
    cells = "".join(f"""
      <td width="{w}%" valign="top" style="width:{w}%;padding:0 4px;">
        <table {_TABLE} width="100%" style="width:100%;background:{RAISE};border:1px solid {LINE_SOFT};border-radius:10px;">
          <tr><td style="padding:14px 6px;text-align:center;">
            <div style="font:600 {18 if len(v) > 7 else 20}px/1.15 {FONT};color:{acc or TEXT};letter-spacing:-.01em;
                        {_WRAP}">{esc(v)}</div>
            <div style="font:500 10px/1.2 {FONT};letter-spacing:.08em;text-transform:uppercase;color:{MUTED};padding-top:7px;{_WRAP}">{esc(lab)}</div>
          </td></tr></table></td>""" for v, lab, acc in tiles)
    return f"""
      <tr><td style="padding:24px 22px 0 22px;">
        <table {_TABLE} width="100%" style="width:100%;table-layout:fixed;"><tr>{cells}</tr></table></td></tr>"""


def _section(title: str, inner: str) -> str:
    return f"""
      <tr><td style="padding:28px 28px 0 28px;">
        <div style="font:600 11px/1 {FONT};letter-spacing:.14em;text-transform:uppercase;
                    color:{MUTED};padding-bottom:14px;{_WRAP}">{esc(title)}</div>
        {inner}
      </td></tr>"""


def _kv(pairs) -> str:
    rows = "".join(f"""
          <tr>
            <td valign="top" style="padding:0 0 9px 0;font:400 13px/1.4 {FONT};color:{SECONDARY};{_WRAP}">{esc(k)}</td>
            <td align="right" valign="top" style="padding:0 0 9px 12px;font:500 12px/1.4 {MONO};color:{TEXT};
                       {_WRAP}">{esc(v)}</td>
          </tr>""" for k, v in pairs)
    return f'<table {_TABLE} width="100%" style="width:100%;">{rows}</table>'


def _timeline_rows(items) -> str:
    """(when, text) rows: the time in mono at the left, the event text left-aligned beside it."""
    rows = "".join(f"""
          <tr>
            <td width="74" valign="top" style="width:74px;padding:0 10px 9px 0;font:500 12px/1.5 {MONO};color:{AMBER_BRIGHT};
                       white-space:nowrap;">{esc(w)}</td>
            <td valign="top" style="padding:0 0 9px 0;font:400 13px/1.5 {FONT};color:{SECONDARY};
                       {_WRAP}">{esc(t)}</td>
          </tr>""" for w, t in items)
    return f'<table {_TABLE} width="100%" style="width:100%;">{rows}</table>'


def _checks(items) -> str:
    """(name, ok, note) rows: green tick / red cross / muted dash. Same hairlines as the backup phase list."""
    rows = ""
    for i, (name, ok, note) in enumerate(items):
        mark, col = (("&#10003;", GREEN) if ok else ("&#10007;", RED)) if ok is not None else ("&ndash;", MUTED)
        top = f"border-top:1px solid {LINE_SOFT};" if i else ""
        rows += f"""
          <tr>
            <td width="18" valign="top" style="width:18px;{top}padding:8px 0;font:600 13px/1.4 {FONT};color:{col};">{mark}</td>
            <td valign="top" style="{top}padding:8px 8px 8px 0;font:400 13px/1.4 {FONT};color:{SECONDARY};
                       {_WRAP}">{esc(name)}</td>
            <td align="right" valign="top" style="{top}padding:8px 0 8px 12px;font:500 12px/1.4 {MONO};
                       color:{RED if ok is False else MUTED};{_WRAP}">{esc(note)}</td>
          </tr>"""
    return f'<table {_TABLE} width="100%" style="width:100%;">{rows}</table>'


def _steps(items) -> str:
    rows = "".join(f"""
          <tr>
            <td width="24" valign="top" style="width:24px;padding:0 0 10px 0;font:600 12px/1.5 {MONO};color:{AMBER};">{i}.</td>
            <td valign="top" style="padding:0 0 10px 0;font:400 13px/1.55 {FONT};color:{SECONDARY};
                       {_WRAP}">{esc(x)}</td>
          </tr>""" for i, x in enumerate(items, 1))
    return f'<table {_TABLE} width="100%" style="width:100%;">{rows}</table>'


def _paras(items) -> str:
    return "".join(f'<div style="font:400 13px/1.6 {FONT};color:{SECONDARY};padding-bottom:8px;'
                   f'{_WRAP}">{esc(x)}</div>' for x in items)


def _pre(items) -> str:
    body = "<br>".join(esc(x) if x else "&nbsp;" for x in items)
    return f"""
      <table {_TABLE} width="100%" style="width:100%;background:{RAISE};border:1px solid {LINE_SOFT};border-radius:9px;">
        <tr><td style="padding:12px 13px;font:400 11px/1.65 {MONO};color:{SECONDARY};
                       {_WRAP}">{body}</td></tr></table>"""


def _button(url: str, text: str) -> str:
    return f"""
      <tr><td style="padding:28px 28px 0 28px;">
        <table {_TABLE}>
          <tr><td style="border:1px solid {AMBER};border-radius:999px;padding:9px 18px;">
            <a href="{esc(url)}" style="font:600 12px/1 {FONT};letter-spacing:.06em;color:{AMBER_BRIGHT};
               text-decoration:none;white-space:nowrap;">{esc(text)} &rarr;</a></td></tr></table></td></tr>"""


def _ack_button(url: str, text: str) -> str:
    """A bulletproof button. A table cell with a solid background (bgcolor too, for clients that drop CSS) holds ONE block link
    whose line box fills the cell, so the whole button is the tap target (Outlook, which ignores padding on a link, still gets
    the 48px cell). Dark text on bright amber is 8.2:1 (AAA). Long labels wrap instead of overflowing a 320px phone."""
    return f"""
        <table {_TABLE} width="100%" style="width:100%;max-width:340px;">
          <tr><td align="center" valign="middle" height="48" bgcolor="{AMBER_BRIGHT}"
                  style="height:48px;background:{AMBER_BRIGHT};border-radius:999px;color:{ON_AMBER};{_WRAP}">
            <a href="{esc(url)}" style="display:block;padding:14px 18px;font:700 15px/20px {FONT};color:{ON_AMBER};
               text-decoration:none;text-align:center;{_WRAP}">{esc(text)} &rarr;</a></td></tr></table>"""


def _ack_block(m: Model) -> str:
    """The acknowledge call to action: a RAISE card with an amber rule (the family's card), the promise in words, the button,
    what the click really does, the Issue ID, and the bare URL as selectable text for clients that hide or mangle buttons.
    SECONDARY text on RAISE is 9:1; MUTED would be 4.2:1 there, so it is not used for anything that matters."""
    label, intro, fine = _ack_texts(m)
    return f"""
      <tr><td style="padding:22px 28px 0 28px;">
        <table {_TABLE} width="100%" style="width:100%;background:{RAISE};border-radius:9px;border-left:3px solid {AMBER_BRIGHT};">
          <tr><td style="padding:16px 16px 16px 16px;">
            <div style="font:600 10px/1.5 {FONT};letter-spacing:.1em;color:{AMBER_BRIGHT};text-transform:uppercase;">{esc(label)}</div>
            <div style="font:400 13px/1.55 {FONT};color:{SECONDARY};padding:4px 0 14px 0;{_WRAP}">{esc(intro)}</div>
            {_ack_button(m.ack_url, m.ack_text)}
            <div style="font:400 12px/1.55 {FONT};color:{SECONDARY};padding-top:12px;{_WRAP}">{esc(fine)}</div>
            <div style="font:400 11px/1.6 {MONO};color:{SECONDARY};padding-top:8px;{_WRAP}">Issue ID: <span style="color:{TEXT};">{esc(m.ack_id)}</span></div>
            <div style="font:400 11px/1.6 {MONO};color:{SECONDARY};padding-top:4px;{_WRAP}">Button not working? Open this link:<br>
              <a href="{esc(m.ack_url)}" style="font-size:10px;color:{AMBER_BRIGHT};text-decoration:underline;{_WRAP}">{esc(m.ack_url)}</a></div>
          </td></tr></table></td></tr>"""


def _footer(lines_: list[str]) -> str:
    body = "<br>".join(esc(x) for x in lines_ if x)
    return f"""
      <tr><td style="padding:26px 28px 24px 28px;">
        <div style="border-top:1px solid {LINE_SOFT};padding-top:16px;font:400 11px/1.7 {MONO};color:{MUTED};
                    {_WRAP}">{body}</div></td></tr>"""


def _shell(title: str, preheader: str, rows: str, footnote: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark">
<title>{esc(title)}</title></head>
<body style="margin:0;padding:0;background:{CANVAS};{_WRAP}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;font-size:1px;line-height:1px;color:{CANVAS};">{esc(preheader)}</div>
<table {_TABLE} width="100%" bgcolor="{CANVAS}" style="background:{CANVAS};margin:0;padding:0;">
  <tr><td align="center" style="padding:20px 10px;">
    <table {_TABLE} width="600" bgcolor="{PANEL}" style="table-layout:fixed;width:100%;max-width:600px;{_WRAP}background:{PANEL};
           border:1px solid {LINE_SOFT};border-radius:{RADIUS};overflow:hidden;">
      {rows}
    </table>
    <div style="font:400 11px/1.6 {FONT};color:{MUTED};padding-top:14px;">{esc(footnote)}</div>
  </td></tr></table></body></html>"""


_SUMMARY_LABEL = {"alert": "What happened", "recovery": "Back to normal", "maintenance": "Summary",
                  "digest_daily": "Overview", "report_weekly": "Overview", "incident_open": "What happened",
                  "incident_resolved": "Resolved", "ack_expired": "What changed"}


def build_html(m: Model, masthead: str = "Maintenance", footer_name: str = "homelab-maint") -> str:
    """The email body. Every argument is escaped on the way in; see the module docstring."""
    rows = [_strip(m.accent), _masthead(masthead, f"{m.host} · {m.when}")]
    sub = esc(m.host) + (f' &nbsp;&middot;&nbsp; <span style="font-family:{MONO};font-size:12px;">{esc(m.task)}</span>' if m.task else "")
    rows.append(_title(_pill(("Test: " if m.test else "") + m.pill, m.accent), m.title, sub))
    if m.test:
        rows.append(_card("Test message", f"This is a test of the notification path (layout: {m.template.replace('_', ' ')}). "
                          "Nothing is wrong and nothing needs doing.", AMBER))
    if m.summary:
        rows.append(_card(_SUMMARY_LABEL.get(m.template, "Summary"), m.summary, m.accent))
    rows.append(_tiles(m.tiles))
    if m.notified > 1:
        rows.append(_section("Still open", _paras([f"Notified {m.notified} times for this problem; it has not cleared."])))
    if m.ack_url:
        rows.append(_ack_block(m))
    if m.facts:
        rows.append(_section("Facts", _kv(m.facts)))
    if m.done:
        rows.append(_section("What was done", _checks([(x, True, "") for x in m.done])))
    if m.todo:
        rows.append(_section("What to do", _steps(m.todo)))
    if m.timeline:
        rows.append(_section("Timeline", _timeline_rows(m.timeline)))
    if m.paras:
        rows.append(_section("Highlights" if m.template in ("digest_daily", "report_weekly") else "Details", _paras(m.paras)))
    for s in m.sections:
        inner = _paras(s["lines"]) + (_kv(s["kv"]) if s["kv"] else "") + (_checks(s["checks"]) if s["checks"] else "")
        rows.append(_section(s["title"], inner))
    if m.log:
        rows.append(_section("Log excerpt", _pre(m.log)))
    if m.url:
        rows.append(_button(m.url, "Open the dashboard"))
    rows.append(_footer([m.url, f"Sent by {footer_name} on {m.host} at {m.when}",
                         "TEST message from `homelab-maint notify-test`" if m.test else ""]))
    return _shell(f"{m.pill}: {m.title}", m.summary[:110] or m.title, "".join(r for r in rows if r),
                  "Ohmz Cloud · automated maintenance report")
