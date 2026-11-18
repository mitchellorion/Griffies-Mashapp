# Griffies

Phase 1 beta.

Sign in to Sniffies, Grindr, or both. The grid shows both, together. More features are coming.

Griffies is an unofficial client. It is not affiliated with Sniffies or Grindr.

## Install

Download the latest version from **[Releases](../../releases/latest)**.

### Mac (macOS 11 Big Sur or newer)

| Your Mac | Download |
|---|---|
| Apple chip (M1, M2, M3, M4…) | `Griffies-mac-apple-silicon.dmg` |
| Intel | `Griffies-mac-intel.dmg` |

Not sure which you have? Apple menu → **About This Mac**. It says "Chip: Apple M…" or "Processor: Intel".

1. Open the `.dmg` and drag **Griffies** into **Applications**.
2. The first time only, **right-click** Griffies in Applications, choose **Open**, then **Open** again. The app isn't signed with a paid Apple developer certificate yet, so a normal double-click is blocked the first time.
3. If macOS says the app "is damaged and can't be opened", run this once in Terminal, then open it again:
   ```bash
   xattr -dr com.apple.quarantine /Applications/Griffies.app
   ```

### Windows (10 or 11)

1. Download `Griffies-windows-x64.exe` and run it.
2. If SmartScreen says "Windows protected your PC", click **More info**, then **Run anyway**.

### Linux, or from source

Python 3.11 or newer:

```bash
git clone https://github.com/mitchellorion/Griffies-Mashapp.git
cd Griffies-Mashapp
python3 server.py
```

Then open http://127.0.0.1:8765. For a native window instead of a browser tab, run `pip install pywebview` and then `python3 desktop/app.py`.

### Updates

Griffies checks for a newer release when it opens. When there is one, a banner offers the download. Install it the same way, replacing the old app.

### Your data

Accounts, settings and AI keys stay on your computer, in `~/.config/sniffies-messages/`. Nothing is sent anywhere except to Sniffies/Grindr and the AI provider you choose.

## Run from source (details)

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

The **AI Reply** menu writes replies to Sniffies messages with Amazon Bedrock, any OpenAI-compatible API (OpenAI, OpenRouter, Groq, a local model server), or a Cloudflare Worker running Workers AI.

It starts in **Drafts** mode: suggested replies appear in the chat for you to edit and send, and **Suggest** in any chat asks for one on demand. **Auto-send** is opt-in.

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

## Building the desktop apps

See [`desktop/README.md`](desktop/README.md). In short: `bash desktop/release.sh 0.1.1` tags a version, and GitHub Actions builds the Mac and Windows apps and publishes them as a release.
