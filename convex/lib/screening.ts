import type { FreshnessBucket, ListingInput, MatchResult } from "./types";

function envNumber(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return parsed;
}

const VINTED_MAX_PRICE_MINOR = envNumber("VINTED_MAX_PRICE_MINOR", 5000);
const VINTED_TARGET_MODEL_PATTERNS = [
  "r1",
  "torrentshell",
  "h2no",
  "shorts",
  "belted",
];
const FACEBOOK_TARGET_PATTERNS = [
  "captain's chair",
  "captains chair",
  "captain chair",
  "chesterfield captain chair",
];

const ALLOWED_CONDITION_PATTERNS = [
  "new with tags",
  "new without tags",
  "very good",
  "good condition",
  "good",
  "6", // Vinted: New with tags
  "1", // Vinted: New without tags
  "2", // Vinted: Very good condition
  "3", // Vinted: Good condition
];

const ALLOWED_CATEGORY_PATTERNS = [
  "fleece",
  "jumper",
  "sweatshirt",
  "hoodie",
  "jackets",
  "clothes",
  "tops",
  "men",
  "women",
  "activewear",
  "sports",
  "outdoor",
];

function includesAny(value: string | undefined, patterns: string[]) {
  if (!value) return false;
  const hay = value.toLowerCase();
  return patterns.some((p) => hay.includes(p));
}

function computeFreshness(ageMinutes: number, hotMinutes: number): FreshnessBucket {
  if (ageMinutes < hotMinutes) return "HOT";
  if (ageMinutes < 60) return "NEW";
  if (ageMinutes < 24 * 60) return "RECENT";
  return "STALE";
}

function screenVintedListing(
  listing: ListingInput & { publishedAtMs?: number },
  nowMs: number,
): MatchResult {
  const reasons: string[] = [];

  const brandOk =
    (listing.brand ?? "").toLowerCase().includes("patagonia") ||
    listing.title.toLowerCase().includes("patagonia");
  if (!brandOk) reasons.push("brand_not_patagonia");

  const targetBlob = `${listing.brand ?? ""} ${listing.title} ${listing.description ?? ""}`.toLowerCase();
  const modelOk = includesAny(targetBlob, VINTED_TARGET_MODEL_PATTERNS);
  if (!modelOk) reasons.push("missing_target_model");
  if (!modelOk && /\bregulator\b/.test(targetBlob)) {
    reasons.push("regulator_without_target_model");
  }

  const categoryOk = includesAny(listing.category, ALLOWED_CATEGORY_PATTERNS);
  if (!categoryOk) reasons.push("category_not_allowed");

  const conditionOk = includesAny(listing.condition, ALLOWED_CONDITION_PATTERNS);
  if (!conditionOk) reasons.push("condition_not_allowed");

  const hasRecentPublished = typeof listing.publishedAtMs === "number";
  let ageMinutes = 0;
  if (hasRecentPublished) {
    ageMinutes = Math.max(0, Math.floor((nowMs - listing.publishedAtMs!) / 60000));
  }

  const in24h = ageMinutes < 24 * 60;
  if (!in24h) reasons.push("outside_24h_window");

  const priceOk =
    (listing.currency ?? "").toUpperCase() === "GBP" &&
    typeof listing.priceMinor === "number" &&
    listing.priceMinor <= VINTED_MAX_PRICE_MINOR;
  if (!priceOk) reasons.push("price_or_currency_not_allowed");

  const freshnessBucket = computeFreshness(ageMinutes, 10);

  let score = 0;
  if (freshnessBucket === "HOT") score += 60;
  else if (freshnessBucket === "NEW") score += 40;
  else if (freshnessBucket === "RECENT") score += 20;

  if (typeof listing.priceMinor === "number") {
    const valueGain = Math.max(0, VINTED_MAX_PRICE_MINOR - listing.priceMinor);
    score += Math.min(30, Math.floor(valueGain / 100));
  }

  if (conditionOk) score += 10;

  const isMatch =
    brandOk && modelOk && categoryOk && conditionOk && in24h && priceOk;

  const eligibleForNotify =
    isMatch && (freshnessBucket === "HOT" || freshnessBucket === "NEW");

  return {
    isMatch,
    reasons,
    score,
    freshnessBucket,
    ageMinutes,
    eligibleForNotify,
  };
}

function screenFacebookListing(
  listing: ListingInput & { publishedAtMs?: number },
  nowMs: number,
): MatchResult {
  const reasons: string[] = [];
  const targetBlob = `${listing.title} ${listing.description ?? ""}`.toLowerCase();
  const hasTargetTerm = includesAny(targetBlob, FACEBOOK_TARGET_PATTERNS);
  if (!hasTargetTerm) reasons.push("missing_target_model");

  const hasRecentPublished = typeof listing.publishedAtMs === "number";
  let ageMinutes = 0;
  if (hasRecentPublished) {
    ageMinutes = Math.max(0, Math.floor((nowMs - listing.publishedAtMs!) / 60000));
  }

  const in24h = ageMinutes < 24 * 60;
  if (!in24h) reasons.push("outside_24h_window");

  // Facebook flow should prioritize very recent uploads.
  const withinNotifyWindow = ageMinutes <= 5;
  if (!withinNotifyWindow) reasons.push("outside_5m_notify_window");

  const freshnessBucket = computeFreshness(ageMinutes, 5);
  let score = 0;
  if (freshnessBucket === "HOT") score += 70;
  else if (freshnessBucket === "NEW") score += 40;
  else if (freshnessBucket === "RECENT") score += 20;

  const isMatch = hasTargetTerm && in24h;
  const eligibleForNotify = isMatch && withinNotifyWindow;

  return {
    isMatch,
    reasons,
    score,
    freshnessBucket,
    ageMinutes,
    eligibleForNotify,
  };
}

export function screenListing(
  listing: ListingInput & { publishedAtMs?: number },
  nowMs = Date.now(),
): MatchResult {
  if (listing.source === "facebook_marketplace_uk") {
    return screenFacebookListing(listing, nowMs);
  }
  return screenVintedListing(listing, nowMs);
}
