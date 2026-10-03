"""Inventory a mailbox by sender domain, to decide which sets to build.

  python inventory.py perso                 # writes inventory-perso.tsv
  python inventory.py perso --min 3 --out report.tsv

One line per sender domain, biggest first: message count, % read, how many
are still in the inbox, whether you ever wrote to that domain (P = a person or
someone you correspond with, keep those out of bulk rules), whether it offers
List-Unsubscribe (U), and three sample subjects. Read-only.
"""

import argparse
import re
from collections import defaultdict
from email.utils import getaddresses

import server

TWO_LEVEL_TLDS = {"co", "com", "gouv", "org", "net", "ac"}


def domain(addr: str) -> str:
    host = addr.split("@")[-1]
    parts = host.split(".")
    if len(parts) > 2 and parts[-2] in TWO_LEVEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("account")
    p.add_argument("--min", type=int, default=1, help="only domains with at least this many messages")
    p.add_argument("--out")
    a = p.parse_args()

    acc = server._account(a.account)
    gmail = server._is_gmail(acc)
    with server._imap(acc) as conn:
        server._select(conn, acc, None, readonly=True)
        received = server._search(conn, acc, "-in:sent -in:drafts -in:chats" if gmail else "ALL")
        rows = server._fetch_headers(conn, acc, received, "FROM SUBJECT LIST-UNSUBSCRIBE")
        contacted = set()
        if gmail:
            sent = server._search(conn, acc, "in:sent")
            for r in server._fetch_headers(conn, acc, sent, "TO CC"):
                for _, addr in getaddresses([str(r["headers"].get("To", "")), str(r["headers"].get("Cc", ""))]):
                    contacted.add(domain(addr.lower()))

    agg = defaultdict(lambda: {"n": 0, "read": 0, "inbox": 0, "unsub": 0, "subjects": []})
    for r in rows:
        addr = server._sender(r["headers"])
        if "@" not in addr:
            continue
        g = agg[domain(addr)]
        g["n"] += 1
        g["read"] += "\\Seen" in r["flags"]
        g["inbox"] += "\\Inbox" in (r["labels"] or [])
        g["unsub"] += bool(r["headers"].get("List-Unsubscribe"))
        if len(g["subjects"]) < 3:
            g["subjects"].append(re.sub(r"\s+", " ", str(r["headers"].get("Subject", "")))[:60])

    lines = ["count\tread%\tinbox\tflags\tdomain\tsample subjects"]
    for d, g in sorted(agg.items(), key=lambda x: -x[1]["n"]):
        if g["n"] < a.min:
            continue
        flags = ("P" if d in contacted else "-") + ("U" if g["unsub"] else "-")
        lines.append(f'{g["n"]}\t{g["read"] * 100 // g["n"]}\t{g["inbox"]}\t{flags}\t{d}\t{" / ".join(g["subjects"])}')

    out = a.out or f"inventory-{a.account}.tsv"
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{len(rows)} messages, {len(agg)} domains -> {out}")


if __name__ == "__main__":
    main()
