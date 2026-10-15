# Gridified Sniffies

A grid for Sniffies. Nearby people, chats, places, and community, in one local app, for your own account.

This is the first public version. More features are coming. The app stays Sniffies-only here. A separate project, [Gridified](https://github.com/mitchellorion/gridified), will add Grindr beside Sniffies. That line is its own repository, not a fork of this one, so this release can stay a Sniffies app.

Gridified Sniffies is an unofficial client. It is not affiliated with Sniffies.

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

## What is coming

More on the Sniffies side in this repo. Grindr, in the same grid as Sniffies, will land in [Gridified](https://github.com/mitchellorion/gridified) rather than here.
