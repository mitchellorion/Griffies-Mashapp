# Sniffies AI Responder (Cloudflare Worker)

A tiny Cloudflare Worker that uses Workers AI to draft replies for the `sniffies-messages` auto-responder.

## What it does

Exposes one endpoint:

```
POST https://<worker-host>/respond
Authorization: Bearer <AUTH_TOKEN>
Content-Type: application/json

{
  "messages": [
    {"role": "assistant", "content": "hey, how's it going?"},
    {"role": "user",       "content": "not bad, you?"}
  ],
  "prompt": "Optional custom system prompt",
  "model": "@cf/meta/llama-3.1-8b-instruct-fast"
}
```

Response:

```json
{"reply": "pretty good here too", "model": "@cf/meta/llama-3.1-8b-instruct"}
```

## Deploy

From this directory, with a Cloudflare API token that has **Cloudflare Workers:Edit** and **Workers AI:Edit** permissions:

```bash
export CLOUDFLARE_API_TOKEN="your-token"
npx wrangler deploy
```

Then set a strong shared secret:

```bash
echo -n 'your-random-key' | npx wrangler secret put AUTH_TOKEN
```

The local server will send this key in the `Authorization` header.

## Incoming webhook (e.g. PentestLLM)

The Worker also has a webhook receiver. Set a secret for it (keep it different from `AUTH_TOKEN`):

```bash
openssl rand -hex 24 | tee /dev/stderr | npx wrangler secret put WEBHOOK_SECRET
```

Then paste this URL into the other service's webhook field:

```
https://<worker-host>/webhook?key=<WEBHOOK_SECRET>
```

If the service has a separate "secret" or "header" field, you can instead use the plain URL `https://<worker-host>/webhook` and send the secret as `Authorization: Bearer <secret>` or `X-Webhook-Secret: <secret>`.

Each event is logged. Watch them live with `npx wrangler tail`, or open the Cloudflare dashboard → Workers → `sniffies-ai-responder` → Logs. Payloads are logged only and never sent to the AI model.

## Files

- `wrangler.toml` — Worker name, account ID, and Workers AI binding.
- `src/index.ts` — Request handler and AI call.
