# Griffies Mashapp

Phase 1 beta of Gridified Sniffies.

This is the Sniffies version, for people who do not want the mashup. More features are coming.

Griffies is an unofficial client. It is not affiliated with Sniffies or Grindr.

## Run it

Python 3.11 or newer. The server uses the standard library only.

```bash
python3 server.py
```

Then open http://127.0.0.1:8765 and sign in with the email and password on your Sniffies account.

The server listens on `127.0.0.1` only. Your session is stored under `~/.config/sniffies-messages/` and is not part of this repository. Notification settings live in that same folder.

Set `SNIFFIES_PORT` if you need a port other than `8765`.
