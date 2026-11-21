export interface Env {
  AI: any;
  AUTH_TOKEN: string;
  WEBHOOK_SECRET?: string;
  MODEL?: string;
}

const MAX_WEBHOOK_BYTES = 64 * 1024;

// Incoming webhook receiver, e.g. for PentestLLM.
// URL to give the sender: https://<worker-host>/webhook?key=<WEBHOOK_SECRET>
// The secret can also be sent as "Authorization: Bearer <secret>" or "X-Webhook-Secret: <secret>".
// Each event is logged; view them with `npx wrangler tail` or in the Cloudflare dashboard (Workers > Logs).
// The payload is treated as data only. It is never passed to the AI model.
async function handleWebhook(request: Request, env: Env, url: URL): Promise<Response> {
  const provided =
    url.searchParams.get("key") ||
    request.headers.get("X-Webhook-Secret") ||
    (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  if (!env.WEBHOOK_SECRET || provided !== env.WEBHOOK_SECRET) {
    return error("Unauthorized.", 401);
  }

  // Some services send a GET first to check the URL works.
  if (request.method === "GET") {
    return json({ ok: true, message: "Webhook endpoint is live." });
  }

  const raw = await request.text();
  if (raw.length > MAX_WEBHOOK_BYTES) {
    return error("Payload too large.", 413);
  }
  let payload: unknown = raw;
  try {
    payload = JSON.parse(raw);
  } catch {
    // Not JSON; keep the raw text.
  }

  console.log(
    JSON.stringify({
      webhook: "incoming",
      received_at: new Date().toISOString(),
      content_type: request.headers.get("Content-Type"),
      user_agent: request.headers.get("User-Agent"),
      payload,
    }),
  );

  return json({ ok: true });
}

interface Message {
  role: "system" | "user" | "assistant";
  content: string;
}

const DEFAULT_PROMPT = `You are writing a short reply on behalf of a user in a casual gay cruising/hookup chat app. The message history below is from the user's perspective: assistant = messages the user has already sent, user = messages from the other person.

Keep the reply natural, friendly, and concise (under 200 characters). Match the casual tone of the app but avoid explicit sexual content, contact details, promises to meet, or anything that sounds robotic or generic. If the last incoming message is only a photo, react to it briefly.`;

const MAX_CONTENT = 6000;
const MAX_MESSAGES = 20;
const MAX_PROMPT = 2000;

function json(body: object, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

function error(message: string, status = 400): Response {
  return json({ error: message }, status);
}

function sanitizeMessages(input: unknown): Message[] {
  if (!Array.isArray(input)) {
    throw new Error("messages must be an array");
  }
  const out: Message[] = [];
  for (const raw of input) {
    if (!raw || typeof raw !== "object") continue;
    const role = String(raw.role || "").toLowerCase();
    const content = String(raw.content || "").slice(0, MAX_CONTENT);
    if (!content) continue;
    if (role === "user" || role === "assistant" || role === "system") {
      out.push({ role, content });
    }
  }
  if (out.length === 0) {
    throw new Error("messages array is empty");
  }
  return out.slice(-MAX_MESSAGES);
}

function trimReply(text: string): string {
  let reply = text.replace(/^\s+|\s+$/g, "");
  // Remove any quoted role prefixes sometimes emitted by models.
  reply = reply.replace(/^\s*(user|assistant)\s*[:\-]\s*/i, "");
  if (reply.length > 1000) reply = reply.slice(0, 999).replace(/\s+\S*$/, "") + "…";
  return reply;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Webhook-Secret",
        },
      });
    }

    const url = new URL(request.url);
    if (url.pathname.replace(/\/$/, "") === "/webhook") {
      return handleWebhook(request, env, url);
    }

    if (request.method !== "POST") {
      return error("Only POST is supported.", 405);
    }

    if (url.pathname.replace(/\/$/, "") !== "/respond") {
      return error("Not found.", 404);
    }

    const auth = request.headers.get("Authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "");
    if (!env.AUTH_TOKEN || token !== env.AUTH_TOKEN) {
      return error("Unauthorized.", 401);
    }

    let body: any;
    try {
      body = await request.json();
    } catch {
      return error("Invalid JSON.", 400);
    }

    let messages: Message[];
    try {
      messages = sanitizeMessages(body.messages);
    } catch (e: any) {
      return error(e.message, 400);
    }

    const customPrompt =
      typeof body.prompt === "string" && body.prompt.trim().length
        ? body.prompt.trim().slice(0, MAX_PROMPT)
        : DEFAULT_PROMPT;

    const model =
      typeof body.model === "string" && body.model.trim().length
        ? body.model.trim()
        : env.MODEL || "@cf/meta/llama-3.1-8b-instruct";

    const chatMessages: Message[] = [{ role: "system", content: customPrompt }, ...messages];

    let result: any;
    try {
      result = await env.AI.run(model, {
        messages: chatMessages,
        max_tokens: 256,
        temperature: 0.75,
      });
    } catch (e: any) {
      return error(`Workers AI error: ${e.message || "unknown"}`, 502);
    }

    let reply = "";
    if (result && typeof result === "object") {
      if (typeof result.response === "string") reply = result.response;
      else if (typeof result.content === "string") reply = result.content;
      else if (Array.isArray(result.choices) && result.choices[0]?.message?.content) {
        reply = String(result.choices[0].message.content);
      }
    }

    reply = trimReply(reply);
    if (!reply) {
      return error("The model returned an empty reply.", 502);
    }

    return json({ reply, model });
  },
};
