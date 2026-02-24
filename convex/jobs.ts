import { v } from "convex/values";
import { internal } from "./_generated/api";
import { internalAction, internalMutation, internalQuery } from "./_generated/server";

const NOTIFY_MAX_ATTEMPTS = 5;

function keyFor(source: string, listingId: string): string {
  return `${source}:${listingId}`;
}

export const runNotificationPass = internalAction({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const disableNotify = (process.env.DISABLE_NOTIFY ?? "0").toLowerCase();
    if (["1", "true", "yes", "on"].includes(disableNotify)) {
      return { sent: 0, failed: 0, skipped: 0, pending: 0, disabled: true };
    }

    const startedAtMs = Date.now();
    const runId = await ctx.runMutation(internal.jobs.startRun, {
      kind: "notify",
      startedAtMs,
    });

    let sent = 0;
    let failed = 0;
    let skipped = 0;

    try {
      const pending = await ctx.runQuery(internal.jobs.getPendingMatches, {
        limit: args.limit ?? 100,
      });

      for (const item of pending) {
        const idempotencyKey = keyFor(item.source, item.listingId);

        const existing = await ctx.runQuery(internal.jobs.getNotificationByKey, {
          idempotencyKey,
        });
        if (existing?.status === "sent") {
          skipped += 1;
          continue;
        }
        if (existing?.status === "failed" && existing.attempts >= NOTIFY_MAX_ATTEMPTS) {
          skipped += 1;
          continue;
        }

        try {
          await ctx.runAction(internal.notifyTelegram.sendListingAlert, {
            listing: {
              source: item.source,
              listingId: item.listingId,
              url: item.url,
              title: item.title,
              priceMinor: item.priceMinor,
              currency: item.currency,
              size: item.size,
              condition: item.condition,
            },
            match: {
              freshnessBucket: item.freshnessBucket,
              ageMinutes: item.ageMinutes,
              score: item.score,
            },
          });

          sent += 1;
          await ctx.runMutation(internal.jobs.recordNotificationResult, {
            idempotencyKey,
            source: item.source,
            listingId: item.listingId,
            status: "sent",
            lastError: undefined,
          });
        } catch (err) {
          failed += 1;
          await ctx.runMutation(internal.jobs.recordNotificationResult, {
            idempotencyKey,
            source: item.source,
            listingId: item.listingId,
            status: "failed",
            lastError: err instanceof Error ? err.message : "unknown_error",
          });
        }
      }

      await ctx.runMutation(internal.jobs.finishRun, {
        runId,
        endedAtMs: Date.now(),
        scraped: 0,
        inserted: 0,
        updated: 0,
        rejected: 0,
        matched: pending.length,
        sent,
        failed,
        skipped,
      });

      return { sent, failed, skipped, pending: pending.length };
    } catch (err) {
      await ctx.runMutation(internal.jobs.finishRun, {
        runId,
        endedAtMs: Date.now(),
        scraped: 0,
        inserted: 0,
        updated: 0,
        rejected: 0,
        matched: 0,
        sent,
        failed,
        skipped,
        error: err instanceof Error ? err.message : "unknown_run_error",
      });
      throw err;
    }
  },
});

export const startRun = internalMutation({
  args: { kind: v.string(), startedAtMs: v.number() },
  handler: async (ctx, args) => {
    return await ctx.db.insert("runs", {
      kind: args.kind,
      startedAtMs: args.startedAtMs,
      scraped: 0,
      inserted: 0,
      updated: 0,
      rejected: 0,
      matched: 0,
      sent: 0,
      failed: 0,
      skipped: 0,
    });
  },
});

export const finishRun = internalMutation({
  args: {
    runId: v.id("runs"),
    endedAtMs: v.number(),
    scraped: v.optional(v.number()),
    inserted: v.number(),
    updated: v.number(),
    rejected: v.optional(v.number()),
    matched: v.number(),
    sent: v.number(),
    failed: v.number(),
    skipped: v.optional(v.number()),
    error: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    await ctx.db.patch(args.runId, {
      endedAtMs: args.endedAtMs,
      scraped: args.scraped,
      inserted: args.inserted,
      updated: args.updated,
      rejected: args.rejected,
      matched: args.matched,
      sent: args.sent,
      failed: args.failed,
      skipped: args.skipped,
      error: args.error,
    });
  },
});

export const getPendingMatches = internalQuery({
  args: { limit: v.number() },
  handler: async (ctx, args) => {
    const matches = await ctx.db
      .query("matches")
      .withIndex("by_eligible", (q) => q.eq("eligibleForNotify", true))
      .order("desc")
      .take(args.limit * 12);

    const out: Array<{
      source: string;
      listingId: string;
      url: string;
      title: string;
      priceMinor?: number;
      currency?: string;
      size?: string;
      condition?: string;
      freshnessBucket: "HOT" | "NEW" | "RECENT" | "STALE";
      ageMinutes: number;
      score: number;
    }> = [];

    for (const match of matches) {
      const listing = await ctx.db.get(match.listingRef);
      if (!listing || !match.isMatch || !match.eligibleForNotify) continue;
      if (match.freshnessBucket === "STALE") continue;

      out.push({
        source: match.source,
        listingId: match.listingId,
        url: listing.url,
        title: listing.title,
        priceMinor: listing.priceMinor,
        currency: listing.currency,
        size: listing.size,
        condition: listing.condition,
        freshnessBucket: match.freshnessBucket,
        ageMinutes: match.ageMinutes,
        score: match.score,
      });

      if (out.length >= args.limit) break;
    }

    return out;
  },
});

export const getNotificationByKey = internalQuery({
  args: { idempotencyKey: v.string() },
  handler: async (ctx, args) => {
    return await ctx.db
      .query("notifications")
      .withIndex("by_idempotencyKey", (q) => q.eq("idempotencyKey", args.idempotencyKey))
      .unique();
  },
});

export const recordNotificationResult = internalMutation({
  args: {
    idempotencyKey: v.string(),
    source: v.string(),
    listingId: v.string(),
    status: v.union(v.literal("sent"), v.literal("failed")),
    lastError: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const now = Date.now();
    const existing = await ctx.db
      .query("notifications")
      .withIndex("by_idempotencyKey", (q) => q.eq("idempotencyKey", args.idempotencyKey))
      .unique();

    if (existing) {
      const sameFailure =
        args.status === "failed" &&
        existing.status === "failed" &&
        (existing.lastError ?? "") === (args.lastError ?? "");
      await ctx.db.patch(existing._id, {
        status: args.status,
        attempts: sameFailure ? existing.attempts : existing.attempts + 1,
        sentAtMs: args.status === "sent" ? now : existing.sentAtMs,
        lastError: args.lastError,
        updatedAtMs: now,
      });
      return;
    }

    await ctx.db.insert("notifications", {
      idempotencyKey: args.idempotencyKey,
      source: args.source,
      listingId: args.listingId,
      status: args.status,
      attempts: 1,
      sentAtMs: args.status === "sent" ? now : undefined,
      lastError: args.lastError,
      createdAtMs: now,
      updatedAtMs: now,
    });
  },
});
