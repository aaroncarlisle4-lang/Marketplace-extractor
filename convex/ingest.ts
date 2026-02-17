import { v } from "convex/values";
import { mutation, internalMutation } from "./_generated/server";
import { internal } from "./_generated/api";
import type { ListingInput } from "./lib/types";
import { normalizeListingInput } from "./lib/normalize";
import { screenListing } from "./lib/screening";

const listingInputValidator = v.object({
  source: v.string(),
  listingId: v.string(),
  url: v.string(),
  title: v.string(),
  brand: v.optional(v.string()),
  priceMinor: v.optional(v.number()),
  currency: v.optional(v.string()),
  size: v.optional(v.string()),
  condition: v.optional(v.string()),
  category: v.optional(v.string()),
  publishedAt: v.optional(v.string()),
  fetchedAt: v.string(),
  location: v.optional(v.string()),
  views: v.optional(v.number()),
  interested: v.optional(v.number()),
  description: v.optional(v.string()),
  imageUrl: v.optional(v.string()),
});

export const upsertListings = mutation({
  args: { listings: v.array(listingInputValidator) },
  handler: async (ctx, args) => {
    return await ctx.runMutation(internal.ingest.upsertListingsInternal, args);
  },
});

export const upsertListingsInternal = internalMutation({
  args: { listings: v.array(listingInputValidator) },
  handler: async (ctx, args) => {
    let inserted = 0;
    let updated = 0;
    let matched = 0;
    let rejected = 0;

    for (const rawListing of args.listings as ListingInput[]) {
      const listing = normalizeListingInput(rawListing);
      if (!listing.source || !listing.listingId || !listing.title || !listing.url) {
        rejected += 1;
        continue;
      }

      const existing = await ctx.db
        .query("listingSnapshots")
        .withIndex("by_source_listingId", (q) =>
          q.eq("source", listing.source).eq("listingId", listing.listingId),
        )
        .unique();

      const payload = {
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
        publishedAtMs: listing.publishedAtMs,
        fetchedAt: listing.fetchedAt,
        fetchedAtMs: listing.fetchedAtMs,
        location: listing.location,
        views: listing.views,
        interested: listing.interested,
        description: listing.description,
        imageUrl: listing.imageUrl,
      };

      const listingRef = existing
        ? await ctx.db.patch(existing._id, payload).then(() => existing._id)
        : await ctx.db.insert("listingSnapshots", payload);

      if (existing) updated += 1;
      else inserted += 1;

      const screen = screenListing({ ...listing, publishedAtMs: listing.publishedAtMs });
      if (screen.isMatch) matched += 1;

      const existingMatch = await ctx.db
        .query("matches")
        .withIndex("by_source_listingId", (q) =>
          q.eq("source", listing.source).eq("listingId", listing.listingId),
        )
        .unique();

      const matchPayload = {
        source: listing.source,
        listingId: listing.listingId,
        listingRef,
        isMatch: screen.isMatch,
        reasons: screen.reasons,
        score: screen.score,
        freshnessBucket: screen.freshnessBucket,
        ageMinutes: screen.ageMinutes,
        eligibleForNotify: screen.eligibleForNotify,
        updatedAtMs: Date.now(),
      } as const;

      if (existingMatch) {
        await ctx.db.patch(existingMatch._id, matchPayload);
      } else {
        await ctx.db.insert("matches", matchPayload);
      }
    }

    return { inserted, updated, matched, rejected };
  },
});
