import { mutation, internalMutation } from "./_generated/server";
import { v } from "convex/values";
import { screenListing } from "./lib/screening";

export const deleteListingBySourceAndId = mutation({
  args: {
    source: v.string(),
    listingId: v.string(),
  },
  handler: async (ctx, args) => {
    const listing = await ctx.db
      .query("listingSnapshots")
      .withIndex("by_source_listingId", (q) =>
        q.eq("source", args.source).eq("listingId", args.listingId),
      )
      .unique();

    if (listing) {
      await ctx.db.delete(listing._id);
    }

    const match = await ctx.db
      .query("matches")
      .withIndex("by_source_listingId", (q) =>
        q.eq("source", args.source).eq("listingId", args.listingId),
      )
      .unique();

    if (match) {
      await ctx.db.delete(match._id);
    }

    const notifications = await ctx.db
      .query("notifications")
      .withIndex("by_source_listingId", (q) =>
        q.eq("source", args.source).eq("listingId", args.listingId),
      )
      .collect();

    for (const notification of notifications) {
      await ctx.db.delete(notification._id);
    }

    return {
      deletedListing: !!listing,
      deletedMatch: !!match,
      deletedNotifications: notifications.length,
    };
  },
});

// Deletes listingSnapshots + their matches older than `olderThanHours` (default 48).
// Processes up to 200 per call so it stays well within Convex's transaction limits.
// Intended to be called from a cron job hourly.
export const pruneOldListings = internalMutation({
  args: { olderThanHours: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const hours = args.olderThanHours ?? 120;
    const cutoffMs = Date.now() - hours * 60 * 60 * 1000;

    const old = await ctx.db
      .query("listingSnapshots")
      .withIndex("by_publishedAtMs", (q) => q.lt("publishedAtMs", cutoffMs))
      .take(200);

    let deleted = 0;
    for (const listing of old) {
      const match = await ctx.db
        .query("matches")
        .withIndex("by_source_listingId", (q) =>
          q.eq("source", listing.source).eq("listingId", listing.listingId),
        )
        .unique();
      if (match) await ctx.db.delete(match._id);
      await ctx.db.delete(listing._id);
      deleted += 1;
    }

    return { deleted };
  },
});

export const rescoreMatches = mutation({
  args: { limit: v.optional(v.number()) },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 500;
    const matches = await ctx.db.query("matches").order("desc").take(limit);
    let updated = 0;
    for (const match of matches) {
      const listing = await ctx.db.get(match.listingRef);
      if (!listing) continue;
      const screen = screenListing(
        {
          source: listing.source,
          listingId: listing.listingId,
          url: listing.url,
          title: listing.title,
          brand: listing.brand,
          priceMinor: listing.priceMinor,
          currency: listing.currency,
          size: listing.size,
          condition: listing.condition,
          category: listing.category,
          publishedAt: listing.publishedAt,
          fetchedAt: listing.fetchedAt,
          location: listing.location,
          views: listing.views,
          interested: listing.interested,
          description: listing.description,
          imageUrl: listing.imageUrl,
          publishedAtMs: listing.publishedAtMs,
        },
        Date.now(),
      );
      await ctx.db.patch(match._id, {
        isMatch: screen.isMatch,
        reasons: screen.reasons,
        score: screen.score,
        freshnessBucket: screen.freshnessBucket,
        ageMinutes: screen.ageMinutes,
        eligibleForNotify: screen.eligibleForNotify,
        updatedAtMs: Date.now(),
      });
      updated += 1;
    }
    return { updated };
  },
});
