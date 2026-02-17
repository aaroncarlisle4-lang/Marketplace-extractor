import type { ListingInput } from "./types";

const MAX_FUTURE_SKEW_MS = 5 * 60 * 1000;

function parseTimestamp(input?: string): { iso?: string; ms?: number } {
  if (!input) return {};
  const ms = Date.parse(input);
  if (!Number.isFinite(ms)) return {};
  if (ms > Date.now() + MAX_FUTURE_SKEW_MS) return {};
  return { iso: new Date(ms).toISOString(), ms };
}

function cleanString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const out = value.trim();
  return out.length > 0 ? out : undefined;
}

function cleanNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const n = Number(value);
    if (Number.isFinite(n)) return n;
  }
  return undefined;
}

export function normalizeListingInput(input: ListingInput): ListingInput & {
  publishedAtMs?: number;
  fetchedAtMs: number;
} {
  const published = parseTimestamp(input.publishedAt);
  const fetched = parseTimestamp(input.fetchedAt);
  const fetchedAtMs = fetched.ms ?? Date.now();

  return {
    source: input.source.trim(),
    listingId: input.listingId.trim(),
    url: input.url,
    title: input.title.trim(),
    brand: cleanString(input.brand),
    priceMinor: cleanNumber(input.priceMinor),
    currency: cleanString(input.currency)?.toUpperCase(),
    size: cleanString(input.size),
    condition: cleanString(input.condition),
    category: cleanString(input.category),
    publishedAt: published.iso,
    publishedAtMs: published.ms,
    fetchedAt: new Date(fetchedAtMs).toISOString(),
    fetchedAtMs,
    location: cleanString(input.location),
    views: cleanNumber(input.views),
    interested: cleanNumber(input.interested),
    description: cleanString(input.description),
    imageUrl: cleanString(input.imageUrl),
  };
}
