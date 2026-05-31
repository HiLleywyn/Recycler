# Validators  -  Active PoS

Player validators are the active infrastructure layer of Discoin. They stake tokens,
produce blocks, process transactions, and earn **90% of all gas fees** from blocks they
validate. The remaining 10% goes to the protocol treasury.

---

## Validators vs Protocol Nodes

|                     | **Validators (Active)**             | **Protocol Nodes (Passive)**         |
|---------------------|-------------------------------------|--------------------------------------|
| Who runs it         | You (your user ID is the validator) | The protocol (config-seeded NPCs)    |
| Staking requirement | Min 100 tokens, 24h lock            | Any amount, no lock                  |
| Revenue             | 90% of gas fees from blocks         | Fixed APY from protocol reserves     |
| Risk                | Slashing: 5% stake burn per offense | Zero principal risk to delegator     |
| Commands            | `.stake validator register`         | `.stake <validator> <amount>`        |

---

## The 90/10 Gas Split

When your validator produces a block, gas fees from all transactions in that block are
split:

- **90%** → you (the validator operator)  -  further split with your delegators by your commission rate
- **10%** → protocol treasury → funds savings floor rates and node yields

This creates the economic loop: validators earn gas → treasury accumulates → treasury
funds protocol node yields → yields attract delegators → more liquidity → more
transactions → more gas.

Use `.gas` to see current gas fees and mempool depth.

---

## How to Register

```
.stake validator register <network> <amount>
```

- **Minimum stake:** 100 tokens (network's native coin  -  ARC for Arcadia, DSC for Discoin)
- **Lock period:** 24 hours from registration
- **Networks:** `arc`, `dsc` (Sun Network is PoW only  -  no PoS validators)

After registering, you are eligible for selection in the next block cycle (~120 seconds).

---

## Block Production

Every 120 seconds, one validator per network is selected to produce a block:

1. All active validators for the network are fetched
2. Selection probability = `your_stake / total_network_stake`
3. Back-to-back dampener: if you produced the last block, your weight is multiplied by
   0.1 to prevent concentration
4. The selected validator processes up to 50 pending mempool actions (highest gas fee first)
5. Gas fees are distributed; block is confirmed

If no active validators exist on a network, no block is produced and mempool actions wait.

---

## Slashing

Validators can be slashed for bad behavior:

| Offense | Slash Rate | Applied To |
|---------|-----------|------------|
| Submitting a rejected transaction | 1% of stake | Validator + delegators proportionally |
| Micro-swap exploit (< $5 swap) | Configurable | Validator only |

- **5 slashes = auto-deactivation.** Your stake is not returned immediately  -  you must
  unregister after the lock period expires.
- **Slash decay:** After 7 days without a slash, your slash count decreases by 1.
  Recovery is possible.
- **Delegators on deactivation:** All delegations are automatically refunded to delegators'
  wallets. Both you and each delegator receive a DM.

---

## Delegation

Players can delegate tokens to your validator to earn a share of your gas rewards.

- **Your commission rate:** 30 - 90% (you keep this percentage of your gas reward)
- **Delegator share:** The remaining percentage, split proportionally by delegation amount
- **Lock period:** 24 hours
- **Early unstake penalty:** 5% burn if undelegating within 48 hours of locking

As validator, you set your commission rate at registration. A lower commission attracts
more delegators; a higher commission maximizes your own reward.

---

## MEV Protections

To prevent front-running and mempool manipulation:

- Transactions are ordered by gas fee (descending), then by submission time (FIFO within tier)
- Validators cannot insert their own transactions during block production
- Micro-swaps (< $5 USD equivalent) are blocked for active validators
- Maximum 2 swaps per user per block

---

## When to Run a Validator vs Delegate to a Protocol Node

**Run a validator if:**

- You have ≥ 100 tokens to stake and can accept slash risk
- You want variable, activity-driven income (grows with server transaction volume)
- You want to be part of the active infrastructure

**Delegate to a Protocol Node if:**

- You want zero principal risk
- You prefer predictable fixed APY regardless of transaction volume
- You have less than 100 tokens to commit
