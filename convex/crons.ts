import { cronJobs } from "convex/server";

// Convex is retired: Supabase pg_cron dispatches the workflows and the
// worker sends alerts via Supabase. No Convex crons may run, otherwise
// Convex sends its own Telegram alerts and duplicates every listing.
const crons = cronJobs();

export default crons;
