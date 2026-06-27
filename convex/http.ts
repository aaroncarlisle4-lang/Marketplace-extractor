import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";

type JsonValue = null | boolean | number | string | JsonValue[] | { [k: string]: JsonValue };

function getSharedSecret(): string {
  const value = process.env.INGEST_SHARED_SECRET;
  if (!value) throw new Error("Missing INGEST_SHARED_SECRET env var");
  return value;
}

function unauthorized() {
  return new Response(JSON.stringify({ error: "unauthorized" }), {
    status: 401,
    headers: { "content-type": "application/json" },
  });
}

async function parseBody(req: Request): Promise<JsonValue> {
  try {
    return (await req.json()) as JsonValue;
  } catch {
    return {};
  }
}

const http = httpRouter();

http.route({
  path: "/ingest/listings",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    const secret = req.headers.get("x-ingest-secret");
    if (!secret || secret !== getSharedSecret()) return unauthorized();

    const body = (await parseBody(req)) as { listings?: unknown };
    const listings = Array.isArray(body.listings) ? body.listings : [];

    const startedAtMs = Date.now();
    const runId = await ctx.runMutation(internal.jobs.startRun, {
      kind: "ingest",
      startedAtMs,
    });

    try {
      const result = await ctx.runMutation(internal.ingest.upsertListingsInternal, {
        listings: listings as any,
      });

      await ctx.runMutation(internal.jobs.finishRun, {
        runId,
        endedAtMs: Date.now(),
        scraped: listings.length,
        inserted: result.inserted,
        updated: result.updated,
        rejected: result.rejected ?? 0,
        matched: result.matched,
        sent: 0,
        failed: 0,
        skipped: 0,
      });

      let notify: unknown = null;
      try {
        notify = await ctx.runAction(internal.jobs.runNotificationPass, {
          limit: 100,
        });
      } catch (notifyErr) {
        notify = {
          error:
            notifyErr instanceof Error ? notifyErr.message : "notify_after_ingest_failed",
        };
      }

      return new Response(JSON.stringify({ ...result, notify }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : "ingest_error";
      try {
        await ctx.runMutation(internal.jobs.finishRun, {
          runId,
          endedAtMs: Date.now(),
          scraped: listings.length,
          inserted: 0,
          updated: 0,
          rejected: 0,
          matched: 0,
          sent: 0,
          failed: 0,
          skipped: 0,
          error: errorMsg,
        });
      } catch {
        // best-effort; don't mask the original error
      }
      return new Response(JSON.stringify({ error: errorMsg }), {
        status: 500,
        headers: { "content-type": "application/json" },
      });
    }
  }),
});

http.route({
  path: "/jobs/run-notifications",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    const secret = req.headers.get("x-ingest-secret");
    if (!secret || secret !== getSharedSecret()) return unauthorized();

    const body = (await parseBody(req)) as { limit?: unknown };
    const limit = typeof body.limit === "number" ? body.limit : undefined;

    const result = await ctx.runAction(internal.jobs.runNotificationPass, {
      limit,
    });

    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }),
});

export default http;
