import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();

crons.interval(
  "trigger github vinted cycle",
  { minutes: 10 },
  internal.githubTrigger.triggerVintedCycle,
  {},
);

crons.interval(
  "run pending notification pass",
  { minutes: 1 },
  internal.jobs.runNotificationPass,
  { limit: 100 },
);

crons.interval(
  "prune old listings",
  { hours: 1 },
  internal.admin.pruneOldListings,
  {},
);

export default crons;
