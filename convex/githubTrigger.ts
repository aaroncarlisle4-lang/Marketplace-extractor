import { action, internalAction } from "./_generated/server";
import { internal } from "./_generated/api";

export const testTrigger = action({
  args: {},
  handler: async (ctx) => {
    return await ctx.runAction(internal.githubTrigger.triggerVintedCycle, {});
  },
});

export const triggerVintedCycle = internalAction({
  args: {},
  handler: async () => {
    const token = process.env.GITHUB_TOKEN;
    if (!token) {
      console.error("Missing GITHUB_TOKEN environment variable");
      return;
    }

    const repo = "aaroncarlisle4-lang/Marketplace-extractor";
    const workflowId = "vinted-cycle.yml";
    const url = `https://api.github.com/repos/${repo}/actions/workflows/${workflowId}/dispatches`;

    const response = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Convex-Trigger",
      },
      body: JSON.stringify({
        ref: "main",
      }),
    });

    if (!response.ok) {
      const text = await response.text();
      console.error(`Failed to trigger GitHub Action: ${response.status} ${text}`);
    } else {
      console.log("Successfully triggered GitHub Vinted Cycle");
    }
  },
});
