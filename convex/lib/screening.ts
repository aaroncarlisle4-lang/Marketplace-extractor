import type { FreshnessBucket, ListingInput, MatchResult } from "./types";

const MAX_PRICE_MINOR = 5000;
const RECENT_NOTIFY_MIN_SCORE = 35;
const TARGET_MODEL_PATTERNS = [
  "r1",
  "torrentshell",
  "h2no",
  "goretex",
  "gore-tex",
  "ski jacket",
];

const ALLOWED_CONDITION_PATTERNS = [
  "new with tags",
  "new without tags",
  "very good",
  "good condition",
  "good",
];

const ALLOWED_CATEGORY_PATTERNS = [
  "fleece",
  "jumper",
  "sweatshirt",
  "hoodie",
  "jackets",
  "clothes",
  "tops",
];

function includesAny(value: string | undefined, patterns: string[]) {
  if (!value) return false;
  const hay = value.toLowerCase();
  return patterns.some((p) => hay.includes(p));
}

function computeFreshness(ageMinutes: number): FreshnessBucket {
  if (ageMinutes < 10) return "HOT";
  if (ageMinutes < 60) return "NEW";
  if (ageMinutes < 24 * 60) return "RECENT";
  return "STALE";
}

export function screenListing(
  listing: ListingInput & { publishedAtMs?: number },
  nowMs = Date.now(),
): MatchResult {
  const reasons: string[] = [];

  const brandOk = (listing.brand ?? "").toLowerCase().includes("patagonia");
  if (!brandOk) reasons.push("brand_not_patagonia");

  const targetBlob = `${listing.brand ?? ""} ${listing.title} ${listing.description ?? ""}`.toLowerCase();
  const modelOk = includesAny(targetBlob, TARGET_MODEL_PATTERNS);
  if (!modelOk) reasons.push("missing_target_model");
  if (!modelOk && /\bregulator\b/.test(targetBlob)) {
    reasons.push("regulator_without_target_model");
  }

  const categoryOk = includesAny(listing.category, ALLOWED_CATEGORY_PATTERNS);
  if (!categoryOk) reasons.push("category_not_allowed");

  const conditionOk = includesAny(listing.condition, ALLOWED_CONDITION_PATTERNS);
  if (!conditionOk) reasons.push("condition_not_allowed");

  const hasRecentPublished = typeof listing.publishedAtMs === "number";
  let ageMinutes = Number.POSITIVE_INFINITY;
  if (hasRecentPublished) {
    ageMinutes = Math.max(0, Math.floor((nowMs - listing.publishedAtMs!) / 60000));
  } else {
    reasons.push("missing_published_at");
  }

  const in24h = ageMinutes < 24 * 60;
  if (!in24h) reasons.push("outside_24h_window");

  const priceOk =
    (listing.currency ?? "").toUpperCase() === "GBP" &&
    typeof listing.priceMinor === "number" &&
    listing.priceMinor <= MAX_PRICE_MINOR;
  if (!priceOk) reasons.push("price_or_currency_not_allowed");

  const freshnessBucket = computeFreshness(ageMinutes);

  let score = 0;
  if (freshnessBucket === "HOT") score += 60;
  else if (freshnessBucket === "NEW") score += 40;
  else if (freshnessBucket === "RECENT") score += 20;

  if (typeof listing.priceMinor === "number") {
    const valueGain = Math.max(0, MAX_PRICE_MINOR - listing.priceMinor);
    score += Math.min(30, Math.floor(valueGain / 100));
  }

  if (conditionOk) score += 10;

  const isMatch =
    brandOk && modelOk && categoryOk && conditionOk && in24h && priceOk;

  const eligibleForNotify =
    isMatch &&
    (freshnessBucket === "HOT" ||
      freshnessBucket === "NEW" ||
      (freshnessBucket === "RECENT" && score >= RECENT_NOTIFY_MIN_SCORE));

  return {
    isMatch,
    reasons,
    score,
    freshnessBucket,
    ageMinutes,
    eligibleForNotify,
  };
}
