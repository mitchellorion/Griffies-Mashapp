# Griffies desktop builds

Griffies ships as a native window around the same local server: `desktop/app.py` starts `server.py` on a free `127.0.0.1` port and opens it with pywebview (WKWebView on macOS, WebView2 on Windows).

## Build in CI (recommended)

macOS apps have to be built on a Mac and Windows apps on Windows, so the GitHub Actions workflow in `.github/workflows/desktop.yml` builds all three:

- `Griffies-mac-arm64.dmg` (Apple Silicon)
- `Griffies-mac-x64.dmg` (Intel)
- `Griffies-windows-x64.exe`

Run it from the Actions tab (**Desktop builds → Run workflow**) and download the artifacts, or push a tag like `v0.1.0` to attach them to a GitHub release.

## Build locally

```bash
pip install -r desktop/requirements.txt
pip install wreq            # optional, only needed for Grindr sign-in
pyinstaller desktop/griffies.spec --noconfirm
```

Output lands in `dist/`. Run a build with `--browser` to open your default browser instead of a native window.

## What's not bundled

- **Map key.** `local-config.json` is git-ignored and never packaged. To give shared builds the Amazon map, add the key as a repository secret named `GRIFFIES_AWS_MAP_KEY` (Settings → Secrets and variables → Actions). CI writes it into the app at build time, and it is never committed. Without the secret, builds use a free dark map. A user's own `~/.config/sniffies-messages/app.json` or `GRIFFIES_AWS_MAP_KEY` overrides the bundled key.
- **AI keys.** Each user enters their own key under **AI Reply**. Shared builds default to the **OpenAI-compatible** provider (OpenAI, OpenRouter, Groq, a local server, anything with `/chat/completions`). Bedrock and the Cloudflare Worker are still available. AI Reply starts in **Drafts** mode.
- **Sessions and settings.** These live per user in `~/.config/sniffies-messages/`.

## Signing (optional, avoids warnings)

Unsigned builds work but show a warning the first time they open:

- **macOS:** "unidentified developer". Users right-click the app and choose Open. To remove the warning you need an Apple Developer ID ($99/yr), then `codesign` and `notarytool` in the workflow.
- **Windows:** SmartScreen "unrecognized app". Users choose More info, then Run anyway. To remove it you need a code-signing certificate.
