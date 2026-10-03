"""Apply ~/.config/mail-mcp/sets.json: label each set's senders, take them out
of the inbox, and write a Gmail filters export so new mail lands in the same
folders.

  python classify.py perso            # label + archive existing mail
  python classify.py perso --dry-run  # counts only
  python classify.py perso --xml out.xml   # also write filters to import in Gmail
"""

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

import server

SETS_PATH = Path.home() / ".config" / "mail-mcp" / "sets.json"
BATCH = 25  # domains per Gmail query, keeps X-GM-RAW short


def batches(domains):
    for i in range(0, len(domains), BATCH):
        yield domains[i:i + BATCH]


def query(domains):
    return "from:(" + " OR ".join(domains) + ") -in:sent -is:starred"


def apply(account, sets, dry_run):
    for label, domains in sets.items():
        total = 0
        for chunk in batches(domains):
            q = query(chunk)
            if dry_run:
                total += server.search(account, q, limit=1)["total"]
                continue
            total += server.label(account, add=[label], query=q, limit=server.MAX_BULK)["updated"]
            server.archive(account, query=q + " in:inbox", limit=server.MAX_BULK)
        print(f"{label:22} {total:5} messages")


def filters_xml(email, sets):
    entries = []
    for label, domains in sets.items():
        for chunk in batches(domains):
            props = {
                "from": " OR ".join(chunk),
                "label": label,
                "shouldArchive": "true",
            }
            body = "".join(
                f"<apps:property name='{k}' value='{escape(v, {chr(39): '&apos;'})}'/>" for k, v in props.items()
            )
            entries.append(
                "<entry><category term='filter'></category><title>Mail Filter</title>"
                f"<content></content>{body}</entry>"
            )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<feed xmlns='http://www.w3.org/2005/Atom' xmlns:apps='http://schemas.google.com/apps/2006'>"
        f"<title>Mail Filters</title><author><name>mail-mcp</name><email>{email}</email></author>"
        + "".join(entries) + "</feed>\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("account")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--xml")
    p.add_argument("--only", help="comma-separated set names")
    a = p.parse_args()
    sets = json.loads(SETS_PATH.read_text())[a.account]
    if a.only:
        sets = {k: v for k, v in sets.items() if k in a.only.split(",")}
    apply(a.account, sets, a.dry_run)
    if a.xml:
        Path(a.xml).write_text(filters_xml(server._account(a.account)["email"], sets))
        print("filters written to", a.xml)
