import { internalAction } from "./_generated/server";
import { internal } from "./_generated/api";

const VINTED_BASE = "https://www.vinted.co.uk";

const STATUS_ID_MAP: Record<string, string> = {
  "6": "new with tags",
  "1": "new without tags",
  "2": "very good condition",
  "3": "good condition",
  "4": "satisfactory condition",
};

const USER_AGENT =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

interface VintedApiItem {
  id: number | string;
  path: string;
  title?: string;
  brand_title?: string;
  price?: { amount?: string | number; currency_code?: string };
  created_at_ts?: number;
  updated_at_ts?: number;
  size_title?: string;
  status?: string | number;
  description?: string;
  photo?: { url?: string; high_resolution?: { timestamp?: number } };
  view_count?: number;
  favourite_count?: number;
}

async function fetchSessionCookie(): Promise<string> {
  try {
    const resp = await fetch(VINTED_BASE, {
      headers: {
        "User-Agent": USER_AGENT,
        Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
      },
    });
    const raw = resp.headers.get("set-cookie") ?? "";
    const match = raw.match(/_vinted_fr_session=([^;,\s]+)/);
    return match ? `_vinted_fr_session=${match[1]}` : "";
  } catch {
    return "";
  }
}

async function fetchItems(cookie: string): Promise<VintedApiItem[]> {
  const params = new URLSearchParams({
    search_text: "patagonia",
    order: "newest_first",
    per_page: "96",
    page: "1",
  });
  params.append("catalog_ids[]", "5"); // men's
  for (const s of ["6", "1", "2", "3"]) params.append("status_ids[]", s);

  const url = `${VINTED_BASE}/api/v2/catalog/items?${params}`;
  try {
    const resp = await fetch(url, {
      headers: {
        Cookie: cookie,
        "User-Agent": USER_AGENT,
        Accept: "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        Referer: `${VINTED_BASE}/catalog`,
      },
    });
    if (!resp.ok) return [];
    const data = (await resp.json()) as { items?: VintedApiItem[] };
    return data.items ?? [];
  } catch {
    return [];
  }
}

function normalizeCondition(status: string | number | undefined): string | undefined {
  if (status === undefined || status === null) return undefined;
  const key = String(status);
  return STATUS_ID_MAP[key] ?? key.toLowerCase();
}

function toListingInput(item: VintedApiItem) {
  const publishedTs =
    item.created_at_ts ??
    item.updated_at_ts ??
    item.photo?.high_resolution?.timestamp;
  const publishedAt = publishedTs ? new Date(publishedTs * 1000).toISOString() : undefined;
  const amount = item.price?.amount;
  const priceMinor = amount !== undefined ? Math.round(Number(amount) * 100) : undefined;

  return {
    source: "vinted_uk",
    listingId: String(item.id),
    url: item.path?.startsWith("/") ? `${VINTED_BASE}${item.path}` : item.path,
    title: item.title ?? "(untitled)",
    brand: item.brand_title ?? undefined,
    priceMinor,
    currency: item.price?.currency_code ?? "GBP",
    size: item.size_title ?? undefined,
    condition: normalizeCondition(item.status),
    category: "clothes",
    publishedAt,
    fetchedAt: new Date().toISOString(),
    location: undefined as string | undefined,
    views: item.view_count,
    interested: item.favourite_count,
    description: item.description ?? undefined,
    imageUrl: item.photo?.url ?? undefined,
  };
}

export const scrapeAndIngest = internalAction({
  args: {},
  handler: async (ctx) => {
    const cookie = await fetchSessionCookie();
    const items = await fetchItems(cookie);
    if (items.length === 0) return { fetched: 0, inserted: 0, updated: 0, matched: 0, rejected: 0 };

    const listings = items.map(toListingInput);
    const result = await ctx.runMutation(internal.ingest.upsertListingsInternal, { listings });
    return { fetched: items.length, ...result };
  },
});
