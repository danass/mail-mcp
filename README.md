# mail-mcp

Clean up and organize a mailbox from Claude Code (or any MCP client), over plain
IMAP. Works with any provider; Gmail gets its extensions (Gmail search syntax,
labels, thread lookup from web links).

What it does:

- **Triage**: who fills the mailbox, what share you actually read, who offers unsubscribe.
- **Clean**: unsubscribe (RFC 8058 one-click, else mailto), trash, spam, mark read, in bulk.
- **Organize**: put senders into labels ("sets"), take them out of the inbox, and
  export matching Gmail filters so new mail lands in the same place.

Nothing deletes permanently: trash is recoverable for 30 days on Gmail. The only
mail it ever sends is a mailto: unsubscribe request.

## Contents

| File | Role |
|---|---|
| `server.py` | MCP server (stdio). Tools listed below. |
| `inventory.py` | Read-only report of the mailbox by sender domain, to design sets. |
| `classify.py` | Applies a sets file: label + archive, and writes Gmail filters XML. |
| `examples/` | Example `accounts.json` and `sets.json`. |

## Setup (macOS)

1. **Install**
   ```sh
   git clone https://github.com/danass/mail-mcp && cd mail-mcp
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
2. **Gmail app password.** Turn on 2-Step Verification on the Google account,
   then create an app password at <https://myaccount.google.com/apppasswords>.
   Your normal Google password is refused by IMAP ("Application-specific
   password required").
3. **Store it in the keychain** (prompts for it, nothing lands in a file):
   ```sh
   security add-generic-password -U -s mail-mcp -a you@gmail.com -w
   ```
4. **Declare the account** in `~/.config/mail-mcp/accounts.json`
   (see `examples/accounts.example.json`; non-Gmail servers need `imap_host` etc.):
   ```json
   { "perso": { "email": "you@gmail.com", "provider": "gmail" } }
   ```
5. **Register with Claude Code** (user scope, so it works from any folder), then restart Claude Code:
   ```sh
   claude mcp add mail --scope user -- "$PWD/.venv/bin/python" "$PWD/server.py"
   ```

Check the connection without Claude:
```sh
.venv/bin/python -c "import server; print(server.list_labels('perso'))"
```

## MCP tools

| Tool | What it does |
|---|---|
| `list_accounts`, `list_labels` | Configured accounts; folders/labels with special-use flags. |
| `top_senders` | Newest N messages of a query grouped by sender: count, unread, unsubscribe support. |
| `search`, `read_message` | Find messages (Gmail syntax on Gmail), read one without marking it read. |
| `resolve_gmail_link` | Paste a Gmail web link (`#inbox/FMfcgz…`), get that thread's messages. |
| `label`, `archive`, `move_to_folder` | Add/remove labels, leave the inbox, move (generic IMAP). |
| `trash`, `mark_spam`, `mark_read` | Bulk actions. |
| `unsubscribe` | Uses the message's List-Unsubscribe header. `folder` lets it act on mail already in Trash. |

Every bulk tool takes either `uids` (from `search`) or a `query`, and refuses a
query that matches more than `limit` messages (default 1000, max 5000).

## Reproducing a full cleanup on a new inbox

The order matters: unsubscribe before trashing (the tool needs a message to find
the link), and keep people you correspond with out of bulk rules.

1. **Inventory**
   ```sh
   .venv/bin/python inventory.py perso --min 3
   ```
   Open `inventory-perso.tsv`. `P` marks domains you have written to (people,
   your bank, your landlord): never bulk-trash those. `read%` near 0 with `U`
   means a list you never read.

2. **Never-read lists.** In Claude Code: *"with the mail tools, find senders
   with at least 3 messages and none read, that I never wrote to; unsubscribe
   and trash them, keep banks and account notices"*. A second pass with a
   looser rule (under 10% read, at least 5 messages) catches lists you opened once.

3. **Ephemeral mail.** One-time codes, sign-in alerts, parcel tracking, terms of
   service updates: search, eyeball a sample, then trash with an age guard
   (`older_than:3d`) so a code you are about to use survives. Example query:
   `older_than:3d -is:starred subject:("verification code" OR "code de vérification" OR "new sign-in" OR "security alert" OR "votre colis" OR shipped)`.

4. **Sets.** Copy `examples/sets.example.json` to `~/.config/mail-mcp/sets.json`
   and fill each label with sender domains taken from the inventory. Then:
   ```sh
   .venv/bin/python classify.py perso --dry-run                 # counts per label
   .venv/bin/python classify.py perso --xml ~/Downloads/filters.xml
   ```
   This labels every matching message, removes it from the inbox (it stays
   under its label, unread state untouched), and writes Gmail filters.

5. **Import the filters** so new mail is sorted without this tool running:
   Gmail → Settings → See all settings → Filters and Blocked Addresses →
   Import filters → choose the XML → Create filters (leave "apply to existing"
   unchecked, step 4 already did it). Each filter applies the label and skips the inbox.

To change a set later: edit `sets.json`, rerun `classify.py`, re-import the XML
(delete the previous mail-mcp filters first to avoid duplicates).

## Notes and gotchas

- Gmail answers `System Error` on some bulk operations; the server retries the
  batch, then splits it, before failing.
- Gmail's `from:` also matches the display name, so a domain can catch a few
  extra senders. `classify.py --dry-run` gives counts; check them before applying.
- Labels are case-insensitive on Gmail.
- Unsubscribe outcomes: `one_click_post` and `mailto_sent` are done; `manual`
  returns a link you have to open yourself.
- UIDs are per folder: a uid from `search` in All Mail is not the same message in Trash.
