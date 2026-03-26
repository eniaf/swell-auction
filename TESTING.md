# Testing Guide

Five test levels, each requiring more infra than the last. Run them in order — earlier levels gate later ones.

---

## Prerequisites

```bash
# Solidity toolchain
curl -L https://foundry.paradigm.xyz | bash
foundryup

# Python deps (in venv)
pip install web3 requests python-dotenv eth-account pytest

# Verify .env is filled out
cat .env   # needs RPC_URL at minimum
```

---

## Level 1 — Unit Tests (no network)

Pure Solidity logic. No fork, no RPC needed.

```bash
forge build
forge test --match-contract UnitTest -vvv
```

**What passes here:**
- `onlyOwner` on all mutating functions
- `setOdosRouter(address(0))` reverts
- `execute(0, ...)` reverts with `"Zero WETH input"`
- Raw ETH sent to the contract is rejected
- `rescueToken` / `rescueETH` work correctly
- `auctionContract()` returns what was passed to the constructor

**If it fails:** Fix compilation errors first. The contract address constants and ABI must match.

---

## Level 2 — Fork Integration Tests (needs RPC)

Forks mainnet at a known block and runs the executor against real token contracts.

```bash
forge test --match-contract ForkTest -vvv \
  --fork-url $RPC_URL \
  --fork-block-number 24698575
```

Or with the URL inline:

```bash
forge test --match-contract ForkTest -vvv \
  --fork-url https://eth.llamarpc.com \
  --fork-block-number 24698575
```

**What passes here:**
- WETH, SWELL, swETH, rswETH all exist and have 18 decimals at the fork block
- Auction contract has code
- `executor.auctionContract()` matches the deployed auction address
- Initial SWELL allowance to the auction is 0
- A mock router that does nothing causes `"No SWELL received"` (profit check path exercised)

**If it fails:** Check your `RPC_URL` supports `eth_getLogs` and `debug_` namespaces (Alchemy/Infura work; public RPCs may not).

---

## Level 3 — Python Integration Tests (needs network + optional Anvil)

Tests the bot's off-chain components: Odos API calls, ABI encoding, and profit math.

### 3a–3d: API + math tests (no Anvil needed)

```bash
cd /path/to/swell-auction

# All Python tests
pytest bot/tests/test_integration.py -v

# Or by category:
pytest bot/tests/test_integration.py -v -k "TestOdosAPI"      # live Odos calls (~5 req)
pytest bot/tests/test_integration.py -v -k "TestABIEncoding"  # offline, instant
pytest bot/tests/test_integration.py -v -k "TestProfitMath"   # offline, instant
pytest bot/tests/test_integration.py -v -k "TestExampleReplay"# offline, instant
```

> Odos tests call the real API at ~1 req/s (free tier). They auto-skip if the API is unavailable.

**What passes here:**
- `buy()` function selector matches `0x99d5ce49` (from the example tx)
- `execute()` ABI encodes correctly with 7 args (no router address)
- SWELL buffer and slippage math is correct
- Odos returns valid quotes and assembles calldata

### 3e: Anvil simulation tests (needs Anvil running)

```bash
# Terminal 1 — start Anvil fork
anvil --fork-url $RPC_URL --fork-block-number 24698575 --port 8545

# Terminal 2 — run Anvil tests
pytest bot/tests/test_integration.py -v -k "TestAnvilSimulation"
```

**What passes here:**
- Executor deploys on the fork (constructor args accepted)
- WETH can be deposited and approved to executor
- Basic deploy-and-fund flow works end-to-end

---

## Level 4 — Full E2E Simulation (needs Anvil + live Odos)

Deploys the executor on an Anvil fork, calls the real Odos API to get fresh calldata, builds the full `execute()` calldata, and simulates via `eth_call`.

```bash
# Terminal 1 — Anvil fork (keep running)
anvil --fork-url $RPC_URL --fork-block-number 24698575 --port 8545

# Terminal 2 — run simulation
forge build  # must be compiled first
python bot/tests/test_e2e_simulation.py
```

**What this exercises:**
1. Deploys executor with placeholder Odos router
2. Wraps 5 ETH → WETH, approves executor
3. Checks auction state at fork block
4. Calls Odos API 3–4 times for real swap quotes
5. Calls `setOdosRouter()` with the actual Odos router from the assembly response
6. Builds `execute()` calldata (7 args, no router param)
7. Simulates via `eth_call` — reports profit or revert reason
8. Estimates gas and reports cost

**Expected output at block 24698575:** The simulation will likely revert with `"Insufficient profit"` or `"Auction buy() failed"` — this is normal because:
- The auction epoch at that block is probably already bought or expired
- Odos quotes are live prices, not block-24698575 prices

A `"No SWELL received"` or `"WETH->SWELL failed"` means Odos calldata is incompatible with the fork state — try a more recent fork block.

---

## Level 5 — Mainnet Pre-flight (run before going live)

Validates every prerequisite against real mainnet. Not a test — a checklist.

```bash
# Requires a fully filled .env with PRIVATE_KEY and EXECUTOR_ADDRESS
python bot/tests/preflight_mainnet.py
```

**Checks run:**
- RPC is connected and on chain ID 1
- `PRIVATE_KEY` loads a valid wallet
- Wallet has ETH and WETH
- `EXECUTOR_ADDRESS` has code deployed
- Executor `owner()` matches your wallet
- WETH is approved to the executor
- No stale allowances on the executor
- Auction contract is callable (`currentEpoch()`)
- Flashbots RPC responds
- Odos API returns a valid quote
- Bot config values are sane (`SWELL_BUFFER_PCT > SLIPPAGE_PCT`, etc.)

All critical checks must pass before setting `DRY_RUN=false`.

---

## Level 6 — Live Dry Run

With all checks passing:

```bash
DRY_RUN=true python bot/main.py
```

Watch the logs. For each poll cycle the bot will print the current auction price, Odos quotes, estimated profit, and whether it would fire. It will NOT submit any transaction.

When you're confident:

```bash
DRY_RUN=false python bot/main.py
```

---

## Quick reference

| Level | Command | Needs |
|-------|---------|-------|
| 1 Unit | `forge test --match-contract UnitTest` | nothing |
| 2 Fork | `forge test --match-contract ForkTest --fork-url $RPC_URL --fork-block-number 24698575` | RPC |
| 3 Python | `pytest bot/tests/test_integration.py -v` | RPC + internet |
| 3e Anvil | same + Anvil on :8545 | Anvil |
| 4 E2E | `python bot/tests/test_e2e_simulation.py` | Anvil + Odos |
| 5 Preflight | `python bot/tests/preflight_mainnet.py` | mainnet RPC + deployed contract |
| 6 Dry run | `DRY_RUN=true python bot/main.py` | all of the above |

## Deployment

Before Level 5 you need to deploy the contract. The constructor takes two arguments:

```bash
# Via forge script or cast:
cast send --rpc-url $RPC_URL --private-key $PRIVATE_KEY \
  --create $(cat out/SwellFeeFlowExecutor.sol/SwellFeeFlowExecutor.json | jq -r .bytecode.object) \
  "constructor(address,address)" \
  <ODOS_ROUTER_ADDRESS> \
  0xf17b581496bc2669ce0931FAcAA1ADe35029E85D
```

Then set `EXECUTOR_ADDRESS` in `.env` to the deployed address.

If the Odos router address ever changes, call `setOdosRouter(newAddress)` from your owner wallet.
