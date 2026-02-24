import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();

crons.interval(
  "run pending notification pass",
  { minutes: 1 },
  internal.jobs.runNotificationPass,
  { limit: 100 },
);

export default crons;
