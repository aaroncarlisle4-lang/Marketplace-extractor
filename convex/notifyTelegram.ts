import { internalAction } from "./_generated/server";
import { v } from "convex/values";

function getRequiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required env var: ${name}`);
  return value;
}

export const sendListingAlert = internalAction({
  args: {
    listing: v.object({
      source: v.string(),
      listingId: v.string(),
      url: v.string(),
      title: v.string(),
      priceMinor: v.optional(v.number()),
      currency: v.optional(v.string()),
      size: v.optional(v.string()),
      condition: v.optional(v.string()),
    }),
    match: v.object({
      freshnessBucket: v.union(
        v.literal("HOT"),
        v.literal("NEW"),
        v.literal("RECENT"),
        v.literal("STALE"),
      ),
      ageMinutes: v.number(),
      score: v.number(),
    }),
  },
  handler: async (_ctx, args) => {
    const token = getRequiredEnv("TELEGRAM_BOT_TOKEN");
    const chatId = getRequiredEnv("TELEGRAM_CHAT_ID");

    const price =
      typeof args.listing.priceMinor === "number"
        ? `${(args.listing.priceMinor / 100).toFixed(2)} ${args.listing.currency ?? "GBP"}`
        : "unknown";

    const message = [
      `[${args.match.freshnessBucket}] Patagonia R1`,
      args.listing.title,
      `Price: ${price}`,
      `Condition: ${args.listing.condition ?? "unknown"}`,
      `Size: ${args.listing.size ?? "unknown"}`,
      `Age: ${args.match.ageMinutes}m`,
      `Score: ${args.match.score}`,
      args.listing.url,
    ].join("\n");

    const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: message,
        disable_web_page_preview: false,
      }),
    });

    if (!response.ok) {
      const errorBody = await response.text();
      throw new Error(`Telegram send failed: ${response.status} ${errorBody}`);
    }

    return { ok: true };
  },
});
