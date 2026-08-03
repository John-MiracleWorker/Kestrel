/**
 * Shared Flock qualification/activation primitives (Adaptive Flock plan,
 * Task 18).  These mirror the server contract in
 * ``src/nested_memvid_agent/server_flock_routes.py``; money is always decimal
 * text and digests are 64 lowercase hex characters.
 */

/** Risk gate: ``high``/``critical`` scopes are deterministic-only forever. */
export type FlockRiskLevel = "low" | "medium" | "high" | "critical";

export type FlockEvidenceKind = "synthetic" | "real_project";

export type FlockPrivacyClass =
  | "local_required"
  | "local_preferred"
  | "approved_cloud"
  | "any";

export type FlockRunStatus =
  | "draft"
  | "ready"
  | "running"
  | "pausing"
  | "paused"
  | "cancelled"
  | "failed"
  | "completed";

export type FlockTerminalRunStatus = "cancelled" | "failed" | "completed";

/** Per-scope qualification outcome; never inferred from the run status. */
export type FlockScopeQualificationState =
  | "qualified"
  | "abstained"
  | "deterministic_only";

/** Grant lifecycle status from the latest transition; never implies effective. */
export type FlockGrantStatus = "inactive" | "active" | "suspended" | "revoked";

export type FlockTransitionType =
  | "activated"
  | "resumed"
  | "suspended"
  | "revoked";

/** Decimal USD text ("37.25"); never a JS float, end to end. */
export type FlockUsdText = string;

/** 64 lowercase hex canonical digest. */
export type FlockDigest = string;
