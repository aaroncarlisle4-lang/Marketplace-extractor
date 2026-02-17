import { v } from "convex/values";
import { query } from "./_generated/server";
import { screenListing } from "./lib/screening";

export const previewScreen = query({
  args: {
    listing: v.object({
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
    }),
  },
  handler: async (_ctx, args) => {
    const publishedAtMs = args.listing.publishedAt
      ? Date.parse(args.listing.publishedAt)
      : undefined;
    return screenListing({ ...args.listing, publishedAtMs });
  },
});
