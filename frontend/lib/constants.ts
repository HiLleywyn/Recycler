/**
 * Server-authoritative constants fetched from GET /api/v2/constants.
 *
 * Usage:
 *   import { useConstants } from "@/lib/constants";
 *   const { data } = useConstants();
 *   // data?.validators.max_slash_count
 */

"use client";

import { useApi } from "@/hooks/useApi";

export interface ServerConstants {
  validators: {
    max_slash_count: number;
    min_stake: number;
    stake_lock_secs: number;
    delegation_lock_secs: number;
    min_delegation: number;
    max_delegations: number;
    gas_tiers: Record<string, number>;
    validator_reward_pct: number;
    treasury_cut_pct: number;
  };
  trading: {
    default_swap_fee: number;
    slippage_warn: number;
    min_trade_usd: number;
    usd_precision: number;
    token_precision: number;
  };
  games: {
    mines_total_tiles: number;
    mines_default_bombs: number;
    mines_min_bombs: number;
    mines_max_bombs: number;
  };
  economy: {
    chain_switch_cooldown: number;
    ws_heartbeat_interval: number;
  };
}

export function useConstants() {
  return useApi<ServerConstants>("/constants");
}
