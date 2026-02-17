export type FreshnessBucket = "HOT" | "NEW" | "RECENT" | "STALE";

export type ListingInput = {
  source: string;
  listingId: string;
  url: string;
  title: string;
  brand?: string;
  priceMinor?: number;
  currency?: string;
  size?: string;
  condition?: string;
  category?: string;
  publishedAt?: string;
  fetchedAt: string;
  location?: string;
  views?: number;
  interested?: number;
  description?: string;
  imageUrl?: string;
};

export type MatchResult = {
  isMatch: boolean;
  reasons: string[];
  score: number;
  freshnessBucket: FreshnessBucket;
  ageMinutes: number;
  eligibleForNotify: boolean;
};
