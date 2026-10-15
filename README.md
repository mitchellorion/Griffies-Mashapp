# Griffies Mashapp

Sniffies and Grindr in one grid, so you do not have to go back and forth.

This version is Gridified Sniffies. Nearby people, chats, places, and community, in one local app, for your own Sniffies account. Grindr is the next build. More features are coming.

Griffies is an unofficial client. It is not affiliated with Sniffies or Grindr.

## What you can do now

- Browse a nearby grid with photo, age, distance, and an online marker.
- Filter by photo, online, age (18–25, 26–35, 36–45, 46+), and distance (1, 5, 10, or 20 miles).
- Open a profile, then message that person.
- Read and reply to chats, including one photo on a reply.
- Browse places, open a place, and talk in its chat.
- Read and post in community, with an optional photo.
- Sign in up to two Sniffies accounts and switch between them.
- Turn on desktop notifications for new messages. A Voice Monkey token and speaker are optional, and they stay in a private file on this machine.

## Run it

Python 3.11 or newer. The server uses the standard library only.

```bash
python3 server.py
```

Then open http://127.0.0.1:8765 and sign in with the email and password on your Sniffies account.

The server listens on `127.0.0.1` only. Your session is stored under `~/.config/sniffies-messages/` and is not part of this repository. Notification settings live in that same folder.

Set `SNIFFIES_PORT` if you need a port other than `8765`.

The desktop shortcut named Sniffies does the same thing: it starts the server if port 8765 is closed, then opens the page.

## What is coming

Grindr in this same grid. Until that lands, this release is the Sniffies app.
