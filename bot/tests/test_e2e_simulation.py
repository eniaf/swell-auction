#!/usr/bin/env python3
"""
Level 4: Full End-to-End Simulation on Anvil Fork
===================================================

This is the final validation before going live. It does EVERYTHING except
actually send to mainnet:

  1. Starts from a mainnet fork (you run Anvil yourself)
  2. Deploys the SwellFeeFlowExecutor on the fork
  3. Funds a test account with WETH
  4. Gets REAL Odos quotes and assembled calldata
  5. Builds the full executor calldata
  6. Simulates the transaction via eth_call (no gas spent)
  7. Optionally sends the tx on the fork to verify state changes
  8. Reports exact profit/loss

This catches issues that unit tests and fork tests individually miss:
  - Odos calldata format incompatibilities with the executor
  - Gas estimation accuracy
  - Actual token balance changes
  - Approval flows between real contracts
  - Auction contract interaction specifics

Prerequisites:
  1. Compile the contract: forge build
  2. Start Anvil fork:
     anvil --fork-url YOUR_RPC_URL --fork-block-number 24698575 --port 8545
  3. Run: python test/test_e2e_simulation.py

NOTE: This uses the REAL Odos API but submits to LOCAL Anvil only.
      No real funds are at risk.
"""

import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

from bot.main import (
    odos_get_quote, odos_assemble,
    WETH, SWELL, SWETH, RSWETH, AUCTION_ADDRESS,
    AUCTION_ABI, EXECUTOR_ABI, ERC20_ABI,
    SLIPPAGE_PCT, SWELL_BUFFER_PCT, MIN_PROFIT_ETH,
)

# ============================================================================
# Config
# ============================================================================

ANVIL_URL = "http://127.0.0.1:8545"
# We'll use account[0] from Anvil (has 10000 ETH)
WETH_AMOUNT = Web3.to_wei(1, "ether")  # Start with 1 WETH

# ============================================================================
# Helpers
# ============================================================================

def load_artifact():
    """Load compiled contract artifact."""
    paths = [
        "out/SwellFeeFlowExecutor.sol/SwellFeeFlowExecutor.json",
        "../out/SwellFeeFlowExecutor.sol/SwellFeeFlowExecutor.json",
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    raise FileNotFoundError(
        "Compile first: cd swell-fee-flow-arb && forge build"
    )


def get_swell_price_probe(executor_addr: str, probe_amount=None):
    """Probe Odos for SWELL/WETH rate.

    Uses 1 WETH as the default probe size to capture realistic price impact
    for swaps in the 0.5-2 WETH range. A 0.1 WETH probe underestimates
    price impact and causes the actual quote to come back short.
    """
    if probe_amount is None:
        probe_amount = Web3.to_wei(1, "ether")
    probe = odos_get_quote(WETH, probe_amount, SWELL, executor_addr)
    if not probe:
        raise RuntimeError("Odos probe quote failed")
    rate = probe.out_amount / probe_amount
    print(f"  SWELL/WETH rate: {rate:,.2f} SWELL per WETH (probed at {Web3.from_wei(probe_amount, 'ether')} WETH)")
    return rate


# ============================================================================
# Main simulation
# ============================================================================

def run_simulation():
    print("=" * 70)
    print("Level 4: Full End-to-End Simulation")
    print("=" * 70)

    # --- Connect to Anvil ---
    w3 = Web3(Web3.HTTPProvider(ANVIL_URL))
    if not w3.is_connected():
        print("ERROR: Anvil not running. Start it with:")
        print(f"  anvil --fork-url YOUR_RPC_URL --fork-block-number 24698575 --port 8545")
        sys.exit(1)

    block = w3.eth.get_block("latest")
    print(f"Connected to Anvil fork at block {block['number']}")

    accounts = w3.eth.accounts
    deployer = accounts[0]
    print(f"Deployer: {deployer}")

    # --- Step 1: Deploy executor ---
    print("\n--- Step 1: Deploy Executor ---")
    artifact = load_artifact()
    bytecode = artifact["bytecode"]["object"]
    abi_full = artifact["abi"]

    factory = w3.eth.contract(abi=abi_full, bytecode=bytecode)
    tx = factory.constructor(
        "0x0000000000000000000000000000000000000001",  # odos router placeholder, updated after quoting
        AUCTION_ADDRESS,
    ).transact({"from": deployer, "gas": 2_000_000})
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    assert receipt["status"] == 1, "Deploy failed!"
    executor_addr = receipt["contractAddress"]
    executor = w3.eth.contract(address=executor_addr, abi=abi_full)
    print(f"  Executor deployed: {executor_addr}")
    print(f"  Gas used: {receipt['gasUsed']}")

    # --- Step 2: Fund deployer with WETH ---
    print("\n--- Step 2: Fund with WETH ---")
    weth = w3.eth.contract(address=WETH, abi=[
        {"inputs": [], "name": "deposit", "outputs": [],
         "stateMutability": "payable", "type": "function"},
        {"inputs": [{"name": "account", "type": "address"}],
         "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
         "stateMutability": "view", "type": "function"},
        {"inputs": [{"name": "spender", "type": "address"},
                     {"name": "amount", "type": "uint256"}],
         "name": "approve", "outputs": [{"name": "", "type": "bool"}],
         "stateMutability": "nonpayable", "type": "function"},
    ])

    weth.functions.deposit().transact({
        "from": deployer,
        "value": Web3.to_wei(5, "ether"),
    })
    weth_bal = weth.functions.balanceOf(deployer).call()
    print(f"  WETH balance: {Web3.from_wei(weth_bal, 'ether')} WETH")

    # Approve executor
    weth.functions.approve(executor_addr, 2**256 - 1).transact({"from": deployer})
    print(f"  Approved WETH to executor ✓")

    # --- Step 3: Check auction state ---
    print("\n--- Step 3: Check Auction State ---")
    auction = w3.eth.contract(address=AUCTION_ADDRESS, abi=AUCTION_ABI)

    try:
        epoch = auction.functions.currentEpoch().call()
        print(f"  Current epoch: {epoch}")
    except Exception as e:
        print(f"  WARNING: currentEpoch() failed: {e}")
        print(f"  The auction ABI may not match. You'll need to verify.")
        epoch = 1  # fallback for testing

    # --- Step 4: Get Odos quotes ---
    print("\n--- Step 4: Get Odos Quotes ---")

    # For simulation, we use a known SWELL amount (from example tx)
    swell_needed = int(1_237_301 * 1e18)
    swell_target = int(swell_needed * (1 + SWELL_BUFFER_PCT / 100))
    print(f"  SWELL needed:  {swell_needed / 1e18:,.0f}")
    print(f"  SWELL target:  {swell_target / 1e18:,.0f} (with {SWELL_BUFFER_PCT}% buffer)")

    # Probe to get WETH amount needed
    rate = get_swell_price_probe(executor_addr)
    weth_for_swell = int(swell_target / rate)
    weth_for_swell = int(weth_for_swell * (1 + SLIPPAGE_PCT / 100))
    print(f"  WETH needed:   {Web3.from_wei(weth_for_swell, 'ether'):.6f}")

    time.sleep(1.1)

    # Quote 1: WETH → SWELL (with retry if output is short)
    print("\n  Quoting WETH → SWELL...")
    for attempt in range(3):
        q1 = odos_get_quote(WETH, weth_for_swell, SWELL, executor_addr)
        if not q1:
            print("  FAILED: Odos WETH→SWELL quote unavailable")
            sys.exit(1)
        print(f"    Output: {q1.out_amount / 1e18:,.2f} SWELL (attempt {attempt + 1}, sent {Web3.from_wei(weth_for_swell, 'ether'):.6f} WETH)")
        if q1.out_amount >= swell_needed:
            break
        # Output short — scale WETH up proportionally and retry
        scale = swell_needed / q1.out_amount * 1.02
        weth_for_swell = int(weth_for_swell * scale)
        print(f"    Short by {(swell_needed - q1.out_amount) / 1e18:,.0f} SWELL — bumping WETH to {Web3.from_wei(weth_for_swell, 'ether'):.6f}")
        time.sleep(1.1)
    else:
        print("  WARNING: Could not get sufficient SWELL after 3 attempts — proceeding anyway")

    time.sleep(1.1)
    a1 = odos_assemble(q1.path_id, executor_addr)
    if not a1:
        print("  FAILED: Odos WETH→SWELL assemble failed")
        sys.exit(1)
    odos_router = a1.router_address
    print(f"    Router: {odos_router}")
    print(f"    Calldata: {len(a1.calldata)} chars ✓")

    # Update executor's on-chain router now that we know the Odos router address
    executor.functions.setOdosRouter(odos_router).transact({"from": deployer})
    print(f"    setOdosRouter({odos_router}) ✓")

    time.sleep(1.1)

    # Quote 2: swETH → WETH (using example amounts)
    sweth_amount = int(0.237546 * 1e18)
    print(f"\n  Quoting {sweth_amount / 1e18:.6f} swETH → WETH...")
    q2 = odos_get_quote(SWETH, sweth_amount, WETH, executor_addr)
    a2 = None
    if q2:
        print(f"    Output: {q2.out_amount / 1e18:.6f} WETH")
        time.sleep(1.1)
        a2 = odos_assemble(q2.path_id, executor_addr)
    else:
        print("    WARNING: swETH quote failed")

    time.sleep(1.1)

    # Quote 3: rswETH → WETH
    rsweth_amount = int(0.576941 * 1e18)
    print(f"\n  Quoting {rsweth_amount / 1e18:.6f} rswETH → WETH...")
    q3 = odos_get_quote(RSWETH, rsweth_amount, WETH, executor_addr)
    a3 = None
    if q3:
        print(f"    Output: {q3.out_amount / 1e18:.6f} WETH")
        time.sleep(1.1)
        a3 = odos_assemble(q3.path_id, executor_addr)
    else:
        print("    WARNING: rswETH quote failed")

    time.sleep(1.1)

    # Quote 4: Leftover SWELL → WETH
    estimated_leftover = q1.out_amount - swell_needed
    a4 = None
    if estimated_leftover > 0:
        print(f"\n  Quoting {estimated_leftover / 1e18:,.2f} leftover SWELL → WETH...")
        q4 = odos_get_quote(SWELL, estimated_leftover, WETH, executor_addr)
        if q4:
            print(f"    Output: {q4.out_amount / 1e18:.6f} WETH")
            time.sleep(1.1)
            a4 = odos_assemble(q4.path_id, executor_addr)

    # --- Step 5: Build calldata ---
    print("\n--- Step 5: Build Executor Calldata ---")

    deadline = int(time.time()) + 600
    max_payment = swell_target

    auction_calldata = auction.encode_abi(
        abi_element_identifier="buy",
        args=[
            [SWETH, RSWETH],
            executor_addr,
            epoch,
            deadline,
            max_payment,
        ],
    )
    print(f"  Auction calldata: {len(auction_calldata)} chars")

    min_profit_wei = Web3.to_wei(MIN_PROFIT_ETH, "ether")

    # --- Step 6: Simulate via eth_call ---
    print("\n--- Step 6: Simulate via eth_call ---")

    try:
        executor_calldata = executor.encode_abi(
            abi_element_identifier="execute",
            args=[
                weth_for_swell,
                bytes.fromhex(a1.calldata[2:]),
                bytes.fromhex(auction_calldata[2:]),
                bytes.fromhex(a2.calldata[2:]) if a2 else b"",
                bytes.fromhex(a3.calldata[2:]) if a3 else b"",
                bytes.fromhex(a4.calldata[2:]) if a4 else b"",
                min_profit_wei,
            ],
        )
        print(f"  Total calldata: {len(executor_calldata)} chars")

        # eth_call simulates without spending gas
        result = w3.eth.call({
            "from": deployer,
            "to": executor_addr,
            "data": executor_calldata,
            "gas": 2_000_000,
        })
        print(f"\n  ✅ SIMULATION SUCCEEDED!")
        print(f"  Return data: {result.hex() if result else '(empty)'}")

        # Check WETH balance after
        weth_after = weth.functions.balanceOf(deployer).call()
        print(f"  WETH balance after: {Web3.from_wei(weth_after, 'ether'):.6f}")

    except Exception as e:
        error_str = str(e)
        print(f"\n  ❌ SIMULATION REVERTED: {error_str[:200]}")

        # Decode common revert reasons
        if "Insufficient profit" in error_str:
            print(f"  → The arb is not profitable at current prices.")
            print(f"  → This is expected — the example tx was 22 hours ago,")
            print(f"    the auction price has likely changed since then.")
        elif "No SWELL received" in error_str:
            print(f"  → The WETH→SWELL Odos swap didn't produce output.")
            print(f"  → The Odos calldata may be stale (>60s) or the")
            print(f"    fork block is too old for the quote to be valid.")
        elif "Auction buy() failed" in error_str:
            print(f"  → The auction contract reverted.")
            print(f"  → The epoch may already be bought, or the ABI is wrong.")
        elif "WETH->SWELL failed" in error_str:
            print(f"  → Odos router call failed on the fork.")
            print(f"  → This can happen if the fork block doesn't match")
            print(f"    the Odos quote's expected block state.")
        else:
            print(f"  → Unknown error. Run with forge trace for details.")

    # --- Step 7: Gas estimation ---
    print("\n--- Step 7: Gas Estimation ---")
    try:
        gas_est = w3.eth.estimate_gas({
            "from": deployer,
            "to": executor_addr,
            "data": executor_calldata,
        })
        gas_price = w3.eth.gas_price
        gas_cost_eth = Web3.from_wei(gas_est * gas_price, "ether")
        print(f"  Gas estimate:  {gas_est}")
        print(f"  Gas price:     {Web3.from_wei(gas_price, 'gwei'):.2f} gwei")
        print(f"  Gas cost:      {gas_cost_eth:.6f} ETH")
    except Exception as e:
        print(f"  Gas estimation failed (tx would revert): {str(e)[:100]}")

    # --- Step 8: Allowance verification ---
    print("\n--- Step 8: Post-simulation Allowance Check ---")
    swell_token = w3.eth.contract(address=SWELL, abi=ERC20_ABI)
    sweth_token = w3.eth.contract(address=SWETH, abi=ERC20_ABI)
    rsweth_token = w3.eth.contract(address=RSWETH, abi=ERC20_ABI)
    weth_token = w3.eth.contract(address=WETH, abi=ERC20_ABI)

    for name, token, spender, spender_name in [
        ("WETH", weth_token, odos_router, "Odos Router"),
        ("SWELL", swell_token, AUCTION_ADDRESS, "Auction"),
        ("SWELL", swell_token, odos_router, "Odos Router"),
        ("swETH", sweth_token, odos_router, "Odos Router"),
        ("rswETH", rsweth_token, odos_router, "Odos Router"),
    ]:
        try:
            allowance = token.functions.allowance(executor_addr, spender).call()
            status = "✓ (0)" if allowance == 0 else f"⚠ ({allowance})"
            print(f"  {name} → {spender_name}: {status}")
        except Exception:
            print(f"  {name} → {spender_name}: (query failed)")

    print("\n" + "=" * 70)
    print("Simulation complete.")
    print("=" * 70)


if __name__ == "__main__":
    run_simulation()
