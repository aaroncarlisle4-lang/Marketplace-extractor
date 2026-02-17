import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  listingSnapshots: defineTable({
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
    publishedAtMs: v.optional(v.number()),
    fetchedAt: v.string(),
    fetchedAtMs: v.number(),
    location: v.optional(v.string()),
    views: v.optional(v.number()),
    interested: v.optional(v.number()),
    description: v.optional(v.string()),
    imageUrl: v.optional(v.string()),
  })
    .index("by_source_listingId", ["source", "listingId"])
    .index("by_publishedAtMs", ["publishedAtMs"]),

  matches: defineTable({
    source: v.string(),
    listingId: v.string(),
    listingRef: v.id("listingSnapshots"),
    isMatch: v.boolean(),
    reasons: v.array(v.string()),
    score: v.number(),
    freshnessBucket: v.union(
      v.literal("HOT"),
      v.literal("NEW"),
      v.literal("RECENT"),
      v.literal("STALE"),
    ),
    ageMinutes: v.number(),
    eligibleForNotify: v.boolean(),
    updatedAtMs: v.number(),
  })
    .index("by_source_listingId", ["source", "listingId"])
    .index("by_eligible", ["eligibleForNotify"])
    .index("by_freshness", ["freshnessBucket"]),

  notifications: defineTable({
    idempotencyKey: v.string(),
    source: v.string(),
    listingId: v.string(),
    status: v.union(v.literal("sent"), v.literal("failed")),
    attempts: v.number(),
    sentAtMs: v.optional(v.number()),
    lastError: v.optional(v.string()),
    createdAtMs: v.number(),
    updatedAtMs: v.number(),
  })
    .index("by_idempotencyKey", ["idempotencyKey"])
    .index("by_source_listingId", ["source", "listingId"]),

  runs: defineTable({
    kind: v.string(),
    startedAtMs: v.number(),
    endedAtMs: v.optional(v.number()),
    scraped: v.optional(v.number()),
    inserted: v.number(),
    updated: v.number(),
    rejected: v.optional(v.number()),
    matched: v.number(),
    sent: v.number(),
    failed: v.number(),
    skipped: v.optional(v.number()),
    error: v.optional(v.string()),
  }).index("by_kind", ["kind"]),
});
