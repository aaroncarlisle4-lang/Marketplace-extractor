import { query } from "./_generated/server";
import { v } from "convex/values";

export const getRecentMatches = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const matches = await ctx.db
      .query("matches")
      .order("desc")
      .take(args.limit ?? 20);

    const result = [];
    for (const match of matches) {
      const listing = await ctx.db.get(match.listingRef);
      result.push({
        ...match,
        listing,
      });
    }
    return result;
  },
});

export const getRunStats = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    return await ctx.db.query("runs").order("desc").take(args.limit ?? 20);
  },
});

export const getPendingNotifications = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    return await ctx.db
      .query("matches")
      .withIndex("by_eligible", (q) => q.eq("eligibleForNotify", true))
      .order("desc")
      .take(args.limit ?? 20);
  },
});

export const getNotificationFailureSummary = query({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const rows = await ctx.db.query("notifications").order("desc").take(args.limit ?? 100);
    const failed = rows.filter((r) => r.status === "failed");
    const byError: Record<string, number> = {};
    for (const row of failed) {
      const raw = row.lastError ?? "unknown";
      const key = raw.replace(/[\u0000-\u001F\u007F]+/g, " ").slice(0, 180);
      byError[key] = (byError[key] ?? 0) + 1;
    }
    return {
      total: rows.length,
      failed: failed.length,
      byError,
    };
  },
});

export const getEligibleListingsTable = query({
  args: {
    limit: v.optional(v.number()),
    onlyEligibleForNotify: v.optional(v.boolean()),
  },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 50;
    const onlyEligibleForNotify = args.onlyEligibleForNotify ?? true;

    const matches = await ctx.db.query("matches").order("desc").take(limit * 5);
    const out = [];

    for (const match of matches) {
      if (!match.isMatch) continue;
      if (match.freshnessBucket === "STALE") continue;
      if (onlyEligibleForNotify && !match.eligibleForNotify) continue;

      const listing = await ctx.db.get(match.listingRef);
      if (!listing) continue;

      out.push({
        source: match.source,
        listingId: match.listingId,
        title: listing.title,
        brand: listing.brand,
        priceMinor: listing.priceMinor,
        currency: listing.currency,
        size: listing.size,
        condition: listing.condition,
        ageMinutes: match.ageMinutes,
        freshnessBucket: match.freshnessBucket,
        score: match.score,
        eligibleForNotify: match.eligibleForNotify,
        reasons: match.reasons,
        url: listing.url,
        publishedAt: listing.publishedAt,
      });

      if (out.length >= limit) break;
    }

    return out;
  },
});
