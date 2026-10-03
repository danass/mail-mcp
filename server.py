"""mail-mcp — provider-agnostic mailbox cleanup over IMAP.

Accounts live in ~/.config/mail-mcp/accounts.json, passwords in the macOS
keychain (service "mail-mcp", account = email). Gmail accounts get the X-GM-*
extensions (Gmail search syntax, labels); any other IMAP server falls back to
folders and raw IMAP SEARCH criteria.

Scope is cleanup: search, triage stats, label, archive, trash, spam, read
state, unsubscribe. The only outgoing mail is a mailto: unsubscribe request.
"""

from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import json
import re
import smtplib
import ssl
import subprocess
import urllib.parse
import urllib.request
from collections import Counter
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

CONFIG_PATH = Path.home() / ".config" / "mail-mcp" / "accounts.json"
KEYCHAIN_SERVICE = "mail-mcp"
CHUNK = 500
MAX_BULK = 5000

mcp = MCPServer(
    name="mail",
    instructions=(
        "Mailbox cleanup tools. Start with list_accounts, then top_senders or search "
        "to find noise. For Gmail accounts, `query` uses Gmail search syntax "
        "(from:, older_than:, category:promotions, has:attachment, larger:5M...). "
        "Bulk tools take either explicit uids (from search) or a query; run search "
        "first and tell the user how many messages a query matches before trashing. "
        "trash is recoverable for 30 days; nothing here deletes permanently."
    ),
)

RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


# --- config & credentials -------------------------------------------------

def _accounts() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"No config at {CONFIG_PATH}. See README.")
    return json.loads(CONFIG_PATH.read_text())


def _account(name: str) -> dict:
    accounts = _accounts()
    if name in accounts:
        return {"name": name, **accounts[name]}
    for key, acc in accounts.items():
        if acc.get("email") == name:
            return {"name": key, **acc}
    raise ValueError(f"Unknown account {name!r}. Known: {', '.join(accounts)}")


def _password(addr: str) -> str:
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", addr, "-w"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"No keychain entry for {addr}. Add it with: "
            f"security add-generic-password -s {KEYCHAIN_SERVICE} -a {addr} -w"
        )
    return out.stdout.strip()


def _is_gmail(acc: dict) -> bool:
    return acc.get("provider") == "gmail" or acc.get("imap_host") == "imap.gmail.com"


# --- modified UTF-7 (RFC 3501 mailbox names) -------------------------------

def _utf7_encode(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=")
            out.append("&" + b.replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _utf7_decode(s: str) -> str:
    def repl(m):
        body = m.group(1)
        if not body:
            return "&"
        b = body.replace(",", "/")
        b += "=" * (-len(b) % 4)
        return base64.b64decode(b).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", repl, s)


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- IMAP plumbing ---------------------------------------------------------

@contextmanager
def _imap(acc: dict):
    conn = imaplib.IMAP4_SSL(acc.get("imap_host", "imap.gmail.com"), acc.get("imap_port", 993))
    try:
        conn.login(acc["email"], _password(acc["email"]))
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _check(typ, data, what):
    if typ != "OK":
        raise RuntimeError(f"IMAP {what} failed: {data!r}")
    return data


_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')


def _folders(conn) -> list[dict]:
    typ, data = conn.list()
    _check(typ, data, "LIST")
    out = []
    for line in data:
        if not isinstance(line, bytes):
            continue
        m = _LIST_RE.match(line)
        if not m:
            continue
        name = m.group("name").decode()
        if name.startswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        flags = m.group("flags").decode().split()
        out.append({"raw": name, "name": _utf7_decode(name), "flags": flags})
    return out


def _special(conn, flag: str) -> str:
    for f in _folders(conn):
        if flag in f["flags"]:
            return f["raw"]
    raise RuntimeError(f"No folder with special-use flag {flag}")


def _select(conn, acc: dict, folder: str | None, readonly: bool) -> str:
    if folder:
        raw = _utf7_encode(folder)
    elif _is_gmail(acc):
        raw = _special(conn, "\\All")
    else:
        raw = "INBOX"
    _check(*conn.select(_quote(raw), readonly=readonly), f"SELECT {folder or raw}")
    return raw


def _search(conn, acc: dict, query: str) -> list[int]:
    if _is_gmail(acc):
        conn.literal = query.encode()
        typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
    else:
        typ, data = conn.uid("SEARCH", *(query.split() if query else ["ALL"]))
    _check(typ, data, "SEARCH")
    return sorted(int(x) for x in (data[0] or b"").split())


def _chunks(uids: list[int]):
    for i in range(0, len(uids), CHUNK):
        yield ",".join(str(u) for u in uids[i:i + CHUNK])


_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([^\s()]+)')


def _parse_list(s: str) -> list[str]:
    return [bare or q.replace('\\"', '"').replace("\\\\", "\\") for q, bare in _TOKEN_RE.findall(s)]


def _fetch_headers(conn, acc: dict, uids: list[int], fields: str) -> list[dict]:
    items = "(UID FLAGS BODY.PEEK[HEADER.FIELDS (" + fields + ")]"
    items += " X-GM-LABELS)" if _is_gmail(acc) else ")"
    out = []
    for chunk in _chunks(uids):
        typ, data = conn.uid("FETCH", chunk, items)
        _check(typ, data, "FETCH")
        for part in data:
            if not isinstance(part, tuple):
                continue
            meta = part[0].decode(errors="replace")
            uid = re.search(r"UID (\d+)", meta)
            if not uid:
                continue
            flags = re.search(r"FLAGS \(([^)]*)\)", meta)
            labels = re.search(r"X-GM-LABELS \(((?:[^()\"]|\"(?:[^\"\\]|\\.)*\")*)\)", meta)
            msg = email.message_from_bytes(part[1], policy=email.policy.default)
            out.append({
                "uid": int(uid.group(1)),
                "flags": flags.group(1).split() if flags else [],
                "labels": [_utf7_decode(l) for l in _parse_list(labels.group(1))] if labels else None,
                "headers": msg,
            })
    out.sort(key=lambda r: r["uid"], reverse=True)
    return out


def _resolve(conn, acc: dict, uids: list[int] | None, query: str | None, limit: int) -> list[int]:
    if uids:
        return sorted(set(uids))
    if not query:
        raise ValueError("Pass either uids or query.")
    found = _search(conn, acc, query)
    if len(found) > limit:
        raise ValueError(
            f"Query matches {len(found)} messages, above limit={limit}. "
            f"Narrow the query or raise limit (max {MAX_BULK})."
        )
    return found


def _sender(h) -> str:
    return parseaddr(str(h.get("From", "")))[1].lower()


# --- tools -----------------------------------------------------------------

@mcp.tool(annotations=RO)
def list_accounts() -> list[dict]:
    """Configured mail accounts (name, email, provider)."""
    return [
        {"name": k, "email": v["email"], "provider": "gmail" if _is_gmail(v) else "imap"}
        for k, v in _accounts().items()
    ]


@mcp.tool(annotations=RO)
def list_labels(account: str) -> list[dict]:
    """Folders / Gmail labels, with special-use flags (\\Trash, \\Junk, \\All...)."""
    acc = _account(account)
    with _imap(acc) as conn:
        return [
            {"name": f["name"], "special": [x for x in f["flags"] if x not in ("\\HasNoChildren", "\\HasChildren")]}
            for f in _folders(conn)
        ]


@mcp.tool(annotations=RO)
def search(account: str, query: str, limit: int = 50, folder: str | None = None) -> dict:
    """Find messages, newest first.

    Gmail: Gmail search syntax, searched across All Mail (e.g. "category:promotions older_than:1y").
    Other IMAP: raw SEARCH criteria in `folder` (default INBOX), e.g. 'FROM "news@x.com" BEFORE 1-Jan-2025'.
    Returns the total match count and the newest `limit` messages with their uids.
    """
    acc = _account(account)
    with _imap(acc) as conn:
        _select(conn, acc, folder, readonly=True)
        found = _search(conn, acc, query)
        rows = _fetch_headers(conn, acc, found[-limit:], "FROM SUBJECT DATE LIST-UNSUBSCRIBE")
    return {
        "total": len(found),
        "shown": len(rows),
        "messages": [
            {
                "uid": r["uid"],
                "date": str(r["headers"].get("Date", "")),
                "from": str(r["headers"].get("From", "")),
                "subject": str(r["headers"].get("Subject", "")),
                "unread": "\\Seen" not in r["flags"],
                "labels": r["labels"],
                "can_unsubscribe": bool(r["headers"].get("List-Unsubscribe")),
            }
            for r in rows
        ],
    }


@mcp.tool(annotations=RO)
def top_senders(account: str, query: str = "in:inbox", sample: int = 1000, top: int = 40) -> dict:
    """Group the newest `sample` messages matching `query` by sender address.

    The main triage view: who fills the mailbox, how much is unread, and whether
    each sender offers unsubscribe. `example_uid` is a message usable with unsubscribe.
    For non-Gmail accounts pass IMAP criteria, e.g. query="ALL".
    """
    acc = _account(account)
    with _imap(acc) as conn:
        _select(conn, acc, None, readonly=True)
        found = _search(conn, acc, query)
        rows = _fetch_headers(conn, acc, found[-sample:], "FROM LIST-UNSUBSCRIBE")
    count, unread, unsub, name, example = Counter(), Counter(), {}, {}, {}
    for r in rows:
        addr = _sender(r["headers"])
        count[addr] += 1
        if "\\Seen" not in r["flags"]:
            unread[addr] += 1
        if r["headers"].get("List-Unsubscribe") and addr not in unsub:
            unsub[addr] = r["uid"]
        name.setdefault(addr, parseaddr(str(r["headers"].get("From", "")))[0])
        example.setdefault(addr, r["uid"])
    return {
        "matched": len(found),
        "sampled": len(rows),
        "senders": [
            {
                "address": a, "name": name[a], "count": c, "unread": unread[a],
                "can_unsubscribe": a in unsub, "example_uid": unsub.get(a, example[a]),
            }
            for a, c in count.most_common(top)
        ],
    }


@mcp.tool(annotations=RO)
def read_message(account: str, uid: int, folder: str | None = None, max_chars: int = 4000) -> dict:
    """Headers and plain-text body of one message (does not mark it read)."""
    acc = _account(account)
    with _imap(acc) as conn:
        _select(conn, acc, folder, readonly=True)
        typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        _check(typ, data, "FETCH")
    raw = next((p[1] for p in data if isinstance(p, tuple)), None)
    if raw is None:
        raise ValueError(f"No message with uid {uid}")
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body is not None and body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", text))
        text = re.sub(r"\s+", " ", text)
    return {
        "uid": uid,
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "date": str(msg.get("Date", "")),
        "subject": str(msg.get("Subject", "")),
        "list_unsubscribe": str(msg.get("List-Unsubscribe", "")) or None,
        "attachments": [p.get_filename() for p in msg.iter_attachments()],
        "body": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


@mcp.tool(annotations=WRITE)
def label(
    account: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    uids: list[int] | None = None,
    query: str | None = None,
    limit: int = 1000,
    create_missing: bool = True,
) -> dict:
    """Add/remove Gmail labels on messages (by uids from search, or by query).

    Use "\\\\Inbox" to (un)inbox, "\\\\Starred", "\\\\Important". Gmail only; for
    other IMAP servers use move_to_folder.
    """
    acc = _account(account)
    if not _is_gmail(acc):
        raise ValueError("Labels are Gmail-only; use move_to_folder.")
    with _imap(acc) as conn:
        if create_missing and add:
            existing = {f["name"] for f in _folders(conn)}
            for name in add:
                if not name.startswith("\\") and name not in existing:
                    _check(*conn.create(_quote(_utf7_encode(name))), f"CREATE {name}")
        _select(conn, acc, None, readonly=False)
        targets = _resolve(conn, acc, uids, query, limit)

        def fmt(names):
            return "(" + " ".join(n if n.startswith("\\") else _quote(_utf7_encode(n)) for n in names) + ")"

        for chunk in _chunks(targets):
            if add:
                _check(*conn.uid("STORE", chunk, "+X-GM-LABELS", fmt(add)), "STORE +labels")
            if remove:
                _check(*conn.uid("STORE", chunk, "-X-GM-LABELS", fmt(remove)), "STORE -labels")
    return {"updated": len(targets), "added": add or [], "removed": remove or []}


@mcp.tool(annotations=WRITE)
def archive(account: str, uids: list[int] | None = None, query: str | None = None, limit: int = 1000) -> dict:
    """Take messages out of the inbox without deleting them.

    Gmail: removes the \\Inbox label. Other IMAP: moves INBOX messages to the
    folder flagged \\Archive (uids are then INBOX uids).
    """
    acc = _account(account)
    if _is_gmail(acc):
        res = label(account, remove=["\\Inbox"], uids=uids, query=query, limit=limit)
        return {"archived": res["updated"]}
    with _imap(acc) as conn:
        dest = _special(conn, "\\Archive")
        _select(conn, acc, "INBOX", readonly=False)
        targets = _resolve(conn, acc, uids, query, limit)
        for chunk in _chunks(targets):
            _check(*conn.uid("MOVE", chunk, _quote(dest)), "MOVE")
    return {"archived": len(targets)}


def _move_special(account, flag, uids, query, limit, folder=None):
    acc = _account(account)
    with _imap(acc) as conn:
        dest = _special(conn, flag)
        _select(conn, acc, folder, readonly=False)
        targets = _resolve(conn, acc, uids, query, limit)
        for chunk in _chunks(targets):
            _check(*conn.uid("MOVE", chunk, _quote(dest)), "MOVE")
    return len(targets), _utf7_decode(dest)


@mcp.tool(annotations=DESTRUCTIVE)
def trash(account: str, uids: list[int] | None = None, query: str | None = None, limit: int = 1000) -> dict:
    """Move messages to Trash (Gmail empties it after 30 days; recoverable until then)."""
    n, dest = _move_special(account, "\\Trash", uids, query, limit)
    return {"trashed": n, "folder": dest}


@mcp.tool(annotations=DESTRUCTIVE)
def mark_spam(account: str, uids: list[int] | None = None, query: str | None = None, limit: int = 1000) -> dict:
    """Move messages to Spam/Junk (also trains the provider's filter)."""
    n, dest = _move_special(account, "\\Junk", uids, query, limit)
    return {"moved_to_spam": n, "folder": dest}


@mcp.tool(annotations=WRITE)
def move_to_folder(
    account: str, folder: str, uids: list[int] | None = None, query: str | None = None,
    source: str | None = None, limit: int = 1000,
) -> dict:
    """Move messages to a folder (generic IMAP). On Gmail prefer label + archive."""
    acc = _account(account)
    with _imap(acc) as conn:
        _select(conn, acc, source, readonly=False)
        targets = _resolve(conn, acc, uids, query, limit)
        for chunk in _chunks(targets):
            _check(*conn.uid("MOVE", chunk, _quote(_utf7_encode(folder))), "MOVE")
    return {"moved": len(targets), "folder": folder}


@mcp.tool(annotations=WRITE)
def mark_read(
    account: str, read: bool = True, uids: list[int] | None = None, query: str | None = None, limit: int = MAX_BULK,
) -> dict:
    """Mark messages read (or unread with read=false)."""
    acc = _account(account)
    with _imap(acc) as conn:
        _select(conn, acc, None, readonly=False)
        targets = _resolve(conn, acc, uids, query, limit)
        for chunk in _chunks(targets):
            _check(*conn.uid("STORE", chunk, "+FLAGS" if read else "-FLAGS", "(\\Seen)"), "STORE flags")
    return {"updated": len(targets), "read": read}


def _unsub_targets(header: str) -> list[str]:
    return [t.strip() for t in re.findall(r"<([^>]+)>", header)]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
def unsubscribe(account: str, uid: int, dry_run: bool = False, allow_mailto: bool = True) -> dict:
    """Unsubscribe from the list that sent message `uid` using its List-Unsubscribe header.

    Order of preference: RFC 8058 one-click HTTPS POST, then a mailto: request
    sent from the account over SMTP, else the link is returned for the user to
    open by hand. dry_run=true only reports what would happen.
    """
    acc = _account(account)
    with _imap(acc) as conn:
        _select(conn, acc, None, readonly=True)
        rows = _fetch_headers(conn, acc, [uid], "FROM LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST")
    if not rows:
        raise ValueError(f"No message with uid {uid}")
    h = rows[0]["headers"]
    header = str(h.get("List-Unsubscribe", ""))
    if not header:
        return {"sender": _sender(h), "status": "no_header", "detail": "Message has no List-Unsubscribe header."}
    targets = _unsub_targets(header)
    https = [t for t in targets if t.lower().startswith("https://")]
    mailto = [t for t in targets if t.lower().startswith("mailto:")]
    one_click = "one-click" in str(h.get("List-Unsubscribe-Post", "")).lower()
    sender = _sender(h)

    if https and one_click:
        if dry_run:
            return {"sender": sender, "would": "one_click_post", "url": https[0]}
        req = urllib.request.Request(
            https[0], data=b"List-Unsubscribe=One-Click", method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "mail-mcp"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
                return {"sender": sender, "status": "one_click_post", "http_status": resp.status}
        except Exception as e:  # fall through to mailto if any
            if not mailto:
                return {"sender": sender, "status": "failed", "detail": str(e), "url": https[0]}

    if mailto and allow_mailto:
        parsed = urllib.parse.urlparse(mailto[0])
        params = urllib.parse.parse_qs(parsed.query)
        to = urllib.parse.unquote(parsed.path)
        subject = params.get("subject", ["unsubscribe"])[0]
        if dry_run:
            return {"sender": sender, "would": "mailto", "to": to, "subject": subject}
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = acc["email"], to, subject
        msg.set_content(params.get("body", ["unsubscribe"])[0])
        with smtplib.SMTP_SSL(acc.get("smtp_host", "smtp.gmail.com"), acc.get("smtp_port", 465)) as s:
            s.login(acc["email"], _password(acc["email"]))
            s.send_message(msg)
        return {"sender": sender, "status": "mailto_sent", "to": to}

    return {
        "sender": sender, "status": "manual",
        "detail": "No one-click endpoint; open this link to finish.",
        "url": https[0] if https else (targets[0] if targets else None),
    }


if __name__ == "__main__":
    mcp.run("stdio")
