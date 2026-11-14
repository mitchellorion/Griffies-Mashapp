# Griffies Mashapp

Phase 1 beta.

Sign in to Sniffies, Grindr, or both. The grid shows both, together. More features are coming.

Griffies is an unofficial client. It is not affiliated with Sniffies or Grindr.

## Run it

Python 3.11 or newer. Sniffies calls use the standard library. Grindr calls use the `wreq` package so the connection matches the Android app. Install it for the same Python that runs the server:

```bash
uv pip install --python "$(command -v python3)" --target "$(python3 -c 'import site; print(site.getusersitepackages())')" wreq
```

```bash
python3 server.py
```

Then open http://127.0.0.1:8765 and sign in to Sniffies, Grindr, or both.

The server listens on `127.0.0.1` only. Your session is stored under `~/.config/sniffies-messages/` and is not part of this repository. Notification settings live in that same folder.

Set `SNIFFIES_PORT` if you need a port other than `8765`.

## AI auto-responder (optional)

The web UI has an **AI Reply** button that can auto-respond to new unread Sniffies messages through a Cloudflare Worker running Workers AI.

1. Deploy the Worker in `ai-worker/` (see `ai-worker/README.md`).
2. Set the Worker's `AUTH_TOKEN` secret.
3. In the web UI, paste the Worker URL, the secret key, and optionally a custom prompt.
4. Check the risk-acceptance box and save.
5. Use the **AI** toggle on each chat in the Chats list to choose which conversations the AI should watch.

The first check records the current state of your inbox and stays quiet. After that, every new incoming message in an existing Sniffies conversation triggers a reply (respecting the configured per-conversation gap and hourly limit). The responder waits 15 seconds after a message arrives before sending its reply.

Enable **Send a saved photo when asked** and upload a few pool photos, and the responder will attach one when the other person asks for pics.

**Caveats:**
- Only existing Sniffies conversations are auto-replied to; it will not start new conversations.
- Grindr chats are ignored.
- Message text is sent to Cloudflare for generation.
- Automated replies may violate Sniffies Terms of Service. Use at your own risk.

## Chat list labels

The Chats tab shows distance plus city/borough under each Sniffies conversation when the information is available from the other person's profile.
