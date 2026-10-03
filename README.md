# mail-mcp

Local MCP server for mailbox cleanup over IMAP: search, top senders, labels,
archive, trash, spam, read state, unsubscribe. Gmail gets the X-GM-* extensions
(Gmail search syntax, labels); any other IMAP server works with folders.
Nothing deletes permanently; the only outgoing mail is a mailto: unsubscribe.

## Setup

1. `python3 -m venv .venv && .venv/bin/pip install "mcp[cli]"`
2. Accounts in `~/.config/mail-mcp/accounts.json`:
   ```json
   { "perso": { "email": "me@gmail.com", "provider": "gmail" } }
   ```
   Non-Gmail: add `imap_host`, `imap_port`, `smtp_host`, `smtp_port`.
3. Password in the macOS keychain (Gmail: an app password, needs 2FA):
   `security add-generic-password -s mail-mcp -a me@gmail.com -w`
4. `claude mcp add mail --scope user -- $PWD/.venv/bin/python $PWD/server.py`
