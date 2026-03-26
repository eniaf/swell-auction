# Swell Fee Flow Dutch Auction - Atomic Arbitrage Bot

## Overview

Atomic arbitrage bot for the [Swell Fee Flow](https://app.swellnetwork.io/fee-flow) Dutch auction. The auction periodically sells protocol-accumulated LST tokens (swETH, rswETH) in exchange for SWELL tokens (which get burned).

**The arb loop (single atomic transaction):**
```
WETH → buy SWELL (via Odos) → bid on auction → receive swETH + rswETH → sell back to WETH (via Odos)
```

If you end up with more WETH than you started, profit. If not, the entire transaction reverts and you only lose gas.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  Python Bot (bot/main.py)                 │
│                                                          │
│  1. Monitor auction contract for active epochs           │
│  2. Price the full loop via Odos API                     │
│  3. If profitable: build tx + submit via Flashbots       │
└──────────────────────┬──────────────────────────────────┘
                       │ calls execute()
                       ▼
┌─────────────────────────────────────────────────────────┐
│            SwellFeeFlowExecutor.sol (on-chain)           │
│                                                          │
│  execute() — single atomic transaction:                  │
│    1. Pull WETH from owner                               │
│    2. Swap WETH → SWELL (Odos router calldata)           │
│    3. Approve SWELL → call auction buy()                 │
│    4. Swap swETH → WETH (Odos router calldata)           │
│    5. Swap rswETH → WETH (Odos router calldata)          │
│    6. require(weth_out >= weth_in + minProfit)            │
│    7. Return all WETH to owner                           │
│                                                          │
│  ⚡ If ANY step fails → entire tx reverts                │
│  ⚡ If profit < minProfit → entire tx reverts            │
└─────────────────────────────────────────────────────────┘
```

## Frontrunning Risk Assessment

| Risk Factor | Level | Notes |
|---|---|---|
| **Public mempool exposure** | **ELIMINATED** | Using Flashbots Protect RPC |
| **Generalized frontrunners** | **Low** | Complex multi-step flow is hard to replicate |
| **Specialized auction bots** | **Medium** | Other bots may monitor the same auction |
| **Block builder collusion** | **Low** | Flashbots distributes to multiple builders |

### Why the example bidder wasn't frontrun (multiple txs)
The example bidder did it across separate transactions and wasn't frontrun because:
1. The auction opportunity window is narrow
2. You need SWELL tokens first (a searcher would need to acquire them)
3. The flow is multi-step and non-trivial to replicate atomically
4. Each auction epoch can only be bought once

### Our additional protections
- **Flashbots Protect**: Tx never enters public mempool → invisible to sandwich bots
- **Atomic execution**: All-or-nothing in one tx → no exposure between steps
- **On-chain profit check**: Contract reverts if not profitable → zero-risk aside from gas
- **Fast mode**: `rpc.flashbots.net/fast` sends to all builders with higher validator payment

## Setup

### 1. Prerequisites
```bash
pip install web3 requests python-dotenv eth-account
```

### 2. Deploy the Executor Contract
Deploy `contracts/SwellFeeFlowExecutor.sol` using Foundry, Hardhat, or Remix.

**Foundry example:**
```bash
forge create contracts/SwellFeeFlowExecutor.sol:SwellFeeFlowExecutor \
  --constructor-args 0x0D05a7D3448512B78fa8A9e46c4872C88C4a0D05 0xf17b581496bc2669ce0931FAcAA1ADe35029E85D \
  --rpc-url $RPC_URL \
  --private-key $PRIVATE_KEY \
  --etherscan-api-key $ETHERSCAN_KEY \
  --verify
```

### 3. Approve WETH to Executor
Your wallet needs to approve the executor contract to spend WETH:
```python
from web3 import Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
weth = w3.eth.contract(address=WETH, abi=ERC20_ABI)
tx = weth.functions.approve(EXECUTOR_ADDRESS, 2**256 - 1).transact()
```

### 4. Configure & Run
```bash
cp .env.example .env
# Edit .env with your values

# Test with simulation first:
python bot/main.py simulate

# Run in dry-run mode (monitors but doesn't submit):
python bot/main.py

# Go live (set DRY_RUN=false in .env):
python bot/main.py
```

## Key Addresses

| Contract | Address |
|---|---|
| SWELL Token | `0x0a6E7Ba5042B38349e437ec6Db6214AEC7B35676` |
| swETH | `0xf951E335afb289353dc249e82926178EaC7DEd78` |
| rswETH | `0xFAe103DC9cf190eD75350761e95403b7b8aFa6c0` |
| Fee Flow Auction | `0xf17b581496bc2669ce0931FAcAA1ADe35029E85D` |
| WETH | `0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2` |
| Odos Router V3 | `0x0D05a7D3448512B78fa8A9e46c4872C88C4a0D05` |

## Important Notes

### Auction ABI
The bot includes a **guessed ABI** based on the example transaction. The actual contract may have slightly different function signatures. Before going live:

1. Check if the contract is verified on Etherscan
2. If not, use the function selector `0x99d5ce49` (from the example tx) to confirm the `buy()` signature
3. Read the contract's view functions to understand epoch mechanics
4. The `getEpochInfo`, `getCurrentPrice`, `getAvailableAssets` functions may have different names — adjust the ABI accordingly

### Odos API Considerations
- **Free tier**: 3 bps protocol fee on volume, 1 RPS, 1000 req/day — fine for this use case
- The `userAddr` in Odos quotes must be the **executor contract** address (it's the one doing the swaps)
- Quotes are valid for 60 seconds — the bot fetches fresh quotes right before submitting
- For the executor contract, SWELL/swETH/rswETH must be approved to the Odos router

### Pre-approvals Needed
The executor contract handles all internal approvals dynamically via `forceApprove` + `revoke` — no persistent allowances.

Your EOA needs:
- WETH → Executor contract (one-time infinite approve)

### Gas Costs
From the example tx: ~135k gas at ~1.5 gwei = ~$0.45. The atomic version with 4 Odos swaps will use more gas (~500-800k), costing ~$1-3 at current gas prices. This is factored into the profit calculation.

## Testing: 5 Levels

Each level builds on the previous. Run them in order.

### Level 1: Solidity Unit Tests (no network, no fork)

Tests pure contract logic: access control, input validation, ETH rejection, rescue functions, ownership.

```bash
# Install Foundry if not already
curl -L https://foundry.paradigm.xyz | bash
foundryup

# In the project directory
forge install OpenZeppelin/openzeppelin-contracts
forge test --match-contract UnitTest -vvv
```

**What passes here:** The contract compiles, access modifiers work, invalid inputs revert correctly, no ETH can be sent to the contract, rescue functions work.

**What this doesn't test:** Real token interactions, Odos calldata, auction contract.

### Level 2: Foundry Fork Tests (mainnet state, no real tx)

Forks mainnet at the example tx's block and tests against real contracts.

```bash
forge test --match-contract ForkTest -vvv \
  --fork-url YOUR_RPC_URL \
  --fork-block-number 24698575
```

**What passes here:** Real token contracts exist and are queryable, auction contract has code and is callable, executor deploys on the fork, allowances start at zero.

**What this doesn't test:** Full execution flow (needs Odos calldata which can't be fetched from Solidity).

### Level 3: Python Integration Tests (real Odos API, unit math)

Tests the bot's off-chain components.

```bash
pip install pytest web3 requests python-dotenv eth-account

# API and encoding tests (no Anvil needed)
pytest bot/tests/test_integration.py -v -k "not TestAnvil"

# With Anvil running (see below)
pytest bot/tests/test_integration.py -v
```

**What passes here:** Odos API responds with valid quotes, token price ratios are sane (swETH ≈ 1 ETH), the `buy()` function selector matches `0x99d5ce49`, executor ABI encodes correctly, buffer math is correct.

### Level 4: Full E2E Simulation (Anvil fork + real Odos API)

This is the critical test. It deploys the executor on a local Anvil fork, gets REAL Odos calldata, and simulates the entire `execute()` call.

```bash
# Terminal 1: Start Anvil fork
anvil --fork-url YOUR_RPC_URL --fork-block-number 24698575 --port 8545

# Terminal 2: Compile and run simulation
forge build
python bot/tests/test_e2e_simulation.py
```

**Expected output:** The simulation will likely revert with "Insufficient profit" or "Auction buy() failed" because the fork is at a historical block where the epoch is already bought. **This is OK** — it proves the calldata pipeline works end-to-end. The key things to verify:

1. Executor deploys successfully
2. WETH funding and approval work
3. Odos calldata is assembled (non-empty, correct format)
4. The revert reason is about business logic (price/profit), not encoding errors
5. Allowances are zero after simulation

**To test with a live auction:** Fork at a block where an active (unbought) epoch exists. You can find these by checking the auction contract's events on Etherscan.

### Level 5: Mainnet Pre-flight Validation

Run this BEFORE your first real execution. Checks every prerequisite against live mainnet.

```bash
# Fill in .env first
python bot/tests/preflight_mainnet.py
```

**Expected output:** All green checks. Fix any red items before proceeding.

### After All Tests Pass: Go-Live Sequence

```
1. Deploy executor to mainnet:
   forge create contracts/SwellFeeFlowExecutor.sol:SwellFeeFlowExecutor \
     --constructor-args 0x0D05a7D3448512B78fa8A9e46c4872C88C4a0D05 0xf17b581496bc2669ce0931FAcAA1ADe35029E85D \
     --rpc-url $RPC_URL --private-key $PRIVATE_KEY --verify

2. Approve WETH:
   cast send $WETH "approve(address,uint256)" $EXECUTOR_ADDRESS $(cast max-uint) \
     --rpc-url $RPC_URL --private-key $PRIVATE_KEY

3. Run preflight:
   python bot/tests/preflight_mainnet.py

4. Run bot in dry-run:
   DRY_RUN=true python bot/main.py

5. Wait for active epoch, verify dry-run output

6. Go live:
   DRY_RUN=false python bot/main.py
```

## Testing Checklist

- [ ] Level 1: `forge test --match-contract UnitTest` passes
- [ ] Level 2: `forge test --match-contract ForkTest --fork-url ...` passes
- [ ] Level 3: `pytest bot/tests/test_integration.py` passes (Odos reachable)
- [ ] Level 4: `python bot/tests/test_e2e_simulation.py` runs (reverts on business logic, not encoding)
- [ ] Level 5: `python bot/tests/preflight_mainnet.py` all green
- [ ] Deploy executor to mainnet
- [ ] Verify on Etherscan
- [ ] Approve WETH to executor
- [ ] Re-run Level 5 preflight (now with executor address set)
- [ ] Dry-run during active epoch (`DRY_RUN=true python bot/main.py`)
- [ ] Go live (`DRY_RUN=false python bot/main.py`)

## File Structure
```
swell-auction/
├── README.md
├── .env
├── foundry.toml
├── contracts/
│   └── SwellFeeFlowExecutor.sol        # Solidity atomic executor
├── test/
│   ├── Level1_Unit.t.sol               # L1: Solidity unit tests
│   └── Level2_Fork.t.sol               # L2: Foundry mainnet fork tests
├── bot/
│   ├── main.py                         # Python monitoring + execution bot
│   └── tests/
│       ├── test_integration.py         # L3: Python API + encoding tests
│       ├── test_e2e_simulation.py      # L4: Full Anvil simulation
│       └── preflight_mainnet.py        # L5: Pre-live validation
└── lib/                                # Foundry dependencies (OZ)
```
