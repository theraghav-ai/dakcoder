// Generated from api/contract.json by scripts/gen-contract.mjs. Do not edit.
// Run `make contract` at the repository root and commit the result.

/**
 * The runtime API this build speaks. A mismatch with `/v1/health` is refused
 * at connect time.
 */
export const API_VERSION = '1.1';

/**
 * A hash of the contract this build was generated from. `/v1/health` reports
 * the runtime's. When only this differs, the runtime speaks the same version
 * with additions this build does not know about. That is legal under C2, so it
 * is logged, not refused.
 */
export const CONTRACT_HASH = '431a75cd3ee1d6dc';

/**
 * Every event type the runtime can emit (C2). A lower bound: a newer runtime
 * may send types not listed here, and they must be ignored.
 */
export type EventType =
  | 'assistant'
  | 'assistant_delta'
  | 'end'
  | 'error'
  | 'finish'
  | 'gate'
  | 'heartbeat'
  | 'metrics'
  | 'plan'
  | 'quota'
  | 'steer'
  | 'tool_call'
  | 'tool_pending'
  | 'tool_result'
  | 'turn_start'
  | 'usage'
  | 'user';
