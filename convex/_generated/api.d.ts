/* eslint-disable */
/**
 * Generated `api` utility.
 *
 * THIS CODE IS AUTOMATICALLY GENERATED.
 *
 * To regenerate, run `npx convex dev`.
 * @module
 */

import type * as admin from "../admin.js";
import type * as crons from "../crons.js";
import type * as http from "../http.js";
import type * as ingest from "../ingest.js";
import type * as jobs from "../jobs.js";
import type * as lib_normalize from "../lib/normalize.js";
import type * as lib_screening from "../lib/screening.js";
import type * as lib_types from "../lib/types.js";
import type * as notifyTelegram from "../notifyTelegram.js";
import type * as queries from "../queries.js";
import type * as screen from "../screen.js";

import type {
  ApiFromModules,
  FilterApi,
  FunctionReference,
} from "convex/server";

declare const fullApi: ApiFromModules<{
  admin: typeof admin;
  crons: typeof crons;
  http: typeof http;
  ingest: typeof ingest;
  jobs: typeof jobs;
  "lib/normalize": typeof lib_normalize;
  "lib/screening": typeof lib_screening;
  "lib/types": typeof lib_types;
  notifyTelegram: typeof notifyTelegram;
  queries: typeof queries;
  screen: typeof screen;
}>;

/**
 * A utility for referencing Convex functions in your app's public API.
 *
 * Usage:
 * ```js
 * const myFunctionReference = api.myModule.myFunction;
 * ```
 */
export declare const api: FilterApi<
  typeof fullApi,
  FunctionReference<any, "public">
>;

/**
 * A utility for referencing Convex functions in your app's internal API.
 *
 * Usage:
 * ```js
 * const myFunctionReference = internal.myModule.myFunction;
 * ```
 */
export declare const internal: FilterApi<
  typeof fullApi,
  FunctionReference<any, "internal">
>;

export declare const components: {};
