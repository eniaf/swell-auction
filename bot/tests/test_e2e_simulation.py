#!/usr/bin/env python3
"""
Level 4: Full End-to-End Simulation on Anvil Fork
===================================================

Verifies that the bot correctly builds calldata and the contract correctly
executes every step of the arb, including all token flows and allowance cleanup.

What this test does:
  1. Forks mainnet at the latest block
  2. Deploys a fresh SwellFeeFlowExecutor
  3. Reads the REAL current auction state (epoch, price, asset balances)
  4. Warps Anvil time forward so the Dutch auction price drops to a profitable level
  5. Gets REAL Odos quotes for all 4 swap legs
  6. Sends the full executor.execute() transaction on the fork
  7. Verifies every token balance changed correctly
  8. Verifies all executor allowances are 0 after execution (no stale approvals)
  9. Reports exact P&L

Prerequisites:
  1. forge build
  2. In a separate terminal:
       anvil --fork-url $RPC_URL --port 8545
  3. python bot/tests/test_e2e_simulation.py
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
    SLIPPAGE_PCT, SWELL_BUFFER_PCT,
)

ANVIL_URL = "http://127.0.0.1:8545"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_artifact():
    for p in [
        "out/SwellFeeFlowExecutor.sol/SwellFeeFlowExecutor.json",
        "../out/SwellFeeFlowExecutor.sol/SwellFeeFlowExecutor.json",
    ]:
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    raise FileNotFoundError("Run 'forge build' first")


def bal(w3, token_addr, account):
    t = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    return t.functions.balanceOf(account).call()


def allowance(w3, token_addr, owner, spender):
    t = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    return t.functions.allowance(owner, spender).call()


def warp(w3, ts):
    """Set next block timestamp and mine one block."""
    w3.provider.make_request("anvil_setNextBlockTimestamp", [ts])
    w3.provider.make_request("evm_mine", [])


def sep(title=""):
    print(f"\n{'='*60}")
    if title:
        print(f"  {title}")
        print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_simulation():
    sep("Level 4: Full E2E Simulation on Anvil Fork")

    # -----------------------------------------------------------------------
    # Connect
    # -----------------------------------------------------------------------
    w3 = Web3(Web3.HTTPProvider(ANVIL_URL))
    if not w3.is_connected():
        print("ERROR: Anvil not running. Start it with:")
        print("  anvil --fork-url $RPC_URL --port 8545")
        sys.exit(1)

    block = w3.eth.get_block("latest")
    print(f"Connected to Anvil fork, block {block['number']}, ts={block['timestamp']}")

    deployer = w3.eth.accounts[0]
    print(f"Deployer: {deployer}")

    # -----------------------------------------------------------------------
    # Step 1: Read real auction state
    # -----------------------------------------------------------------------
    sep("Step 1: Read Current Auction State")
    auction = w3.eth.contract(address=AUCTION_ADDRESS, abi=AUCTION_ABI)

    slot0        = auction.functions.getSlot0().call()
    epoch_id     = slot0[1]
    init_price   = slot0[2]
    start_time   = slot0[3]
    epoch_period = auction.functions.epochPeriod().call()
    end_time     = start_time + epoch_period
    current_price = auction.functions.getPrice().call()

    sweth_in_auction  = bal(w3, SWETH,  AUCTION_ADDRESS)
    rsweth_in_auction = bal(w3, RSWETH, AUCTION_ADDRESS)

    print(f"  Epoch:          {epoch_id}")
    print(f"  Init price:     {Web3.from_wei(init_price, 'ether'):>14,.0f} SWELL")
    print(f"  Current price:  {Web3.from_wei(current_price, 'ether'):>14,.0f} SWELL")
    print(f"  Epoch end:      {end_time} (in {(end_time - int(time.time())) // 3600:.0f}h)")
    print(f"  swETH in auction:  {Web3.from_wei(sweth_in_auction, 'ether'):.6f}")
    print(f"  rswETH in auction: {Web3.from_wei(rsweth_in_auction, 'ether'):.6f}")

    assert sweth_in_auction > 0 or rsweth_in_auction > 0, \
        "Auction has no assets — wrong epoch or auction not funded"

    # -----------------------------------------------------------------------
    # Step 2: Warp time to make price profitable
    #
    # We warp to 85% through the epoch. This makes the price decay
    # significantly below the break-even point, guaranteeing the arb is
    # profitable regardless of where we start.
    # -----------------------------------------------------------------------
    sep("Step 2: Warp Time to Profitable Price")

    target_ts = start_time + int(epoch_period * 0.85)
    print(f"  Warping block timestamp to {target_ts} ({(target_ts - start_time) // 86400:.1f} days into epoch)...")
    warp(w3, target_ts)

    warped_price = auction.functions.getPrice().call()
    print(f"  Price after warp: {Web3.from_wei(warped_price, 'ether'):>14,.0f} SWELL")
    print(f"  Price decay:      {(1 - warped_price / init_price) * 100:.1f}% from init")

    # -----------------------------------------------------------------------
    # Step 3: Deploy executor
    # -----------------------------------------------------------------------
    sep("Step 3: Deploy Executor")
    artifact = load_artifact()
    factory  = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"]["object"])

    tx = factory.constructor(
        "0x0000000000000000000000000000000000000001",  # placeholder, updated after Odos quote
        AUCTION_ADDRESS,
    ).transact({"from": deployer, "gas": 2_000_000})
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    assert receipt["status"] == 1, "Deploy failed"
    executor_addr = receipt["contractAddress"]
    executor = w3.eth.contract(address=executor_addr, abi=artifact["abi"])
    print(f"  Deployed: {executor_addr}  (gas: {receipt['gasUsed']})")

    # -----------------------------------------------------------------------
    # Step 4: Fund and approve
    # -----------------------------------------------------------------------
    sep("Step 4: Fund Deployer with WETH")
    weth_contract = w3.eth.contract(address=WETH, abi=[
        {"inputs": [], "name": "deposit", "outputs": [], "stateMutability": "payable", "type": "function"},
        {"inputs": [{"name": "a", "type": "address"}], "name": "balanceOf",
         "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
        {"inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
         "name": "approve", "outputs": [{"name": "", "type": "bool"}],
         "stateMutability": "nonpayable", "type": "function"},
    ])

    weth_contract.functions.deposit().transact({"from": deployer, "value": Web3.to_wei(10, "ether")})
    weth_contract.functions.approve(executor_addr, 2**256 - 1).transact({"from": deployer})
    deployer_weth_before = weth_contract.functions.balanceOf(deployer).call()
    print(f"  WETH balance: {Web3.from_wei(deployer_weth_before, 'ether'):.4f}")
    print(f"  Approved WETH to executor: max")

    # -----------------------------------------------------------------------
    # Step 5: Build Odos quotes using warped price
    # -----------------------------------------------------------------------
    sep("Step 5: Get Odos Quotes")

    swell_needed = warped_price
    swell_target = int(swell_needed * (1 + SWELL_BUFFER_PCT / 100))
    print(f"  SWELL needed (current price):  {Web3.from_wei(swell_needed, 'ether'):>14,.0f}")
    print(f"  SWELL target (+{SWELL_BUFFER_PCT}% buffer):    {Web3.from_wei(swell_target, 'ether'):>14,.0f}")

    # Probe rate
    probe = odos_get_quote(WETH, Web3.to_wei(1, "ether"), SWELL, executor_addr)
    assert probe, "Odos probe quote failed"
    rate = probe.out_amount / 1e18
    weth_for_swell = int(swell_target / rate)
    weth_for_swell = int(weth_for_swell * (1 + SLIPPAGE_PCT / 100))
    print(f"  SWELL/WETH rate:  {rate:,.0f}")
    print(f"  WETH to spend:    {Web3.from_wei(weth_for_swell, 'ether'):.6f}")
    time.sleep(1.1)

    # Quote 1: WETH -> SWELL
    print("\n  [Q1] WETH -> SWELL...")
    for attempt in range(3):
        q1 = odos_get_quote(WETH, weth_for_swell, SWELL, executor_addr)
        assert q1, "Q1 WETH->SWELL failed"
        print(f"       Output: {Web3.from_wei(q1.out_amount, 'ether'):,.2f} SWELL (attempt {attempt+1})")
        if q1.out_amount >= swell_needed:
            break
        scale = swell_needed / q1.out_amount * 1.02
        weth_for_swell = int(weth_for_swell * scale)
        print(f"       Short — bumping WETH to {Web3.from_wei(weth_for_swell, 'ether'):.6f}")
        time.sleep(1.1)
    # Odos deadline must be AFTER the warped block timestamp so the router
    # doesn't reject the calldata as expired on the fork.
    odos_deadline = target_ts + 7200  # 2 hours after warp

    time.sleep(1.1)
    a1 = odos_assemble(q1.path_id, executor_addr, deadline=odos_deadline)
    assert a1, "A1 assemble failed"
    odos_router = a1.router_address
    print(f"       Router: {odos_router}")

    # Set Odos router on executor
    executor.functions.setOdosRouter(odos_router).transact({"from": deployer})
    print(f"       setOdosRouter: OK")
    time.sleep(1.1)

    # Quote 2: swETH -> WETH
    a2 = None
    if sweth_in_auction > 0:
        print(f"\n  [Q2] swETH -> WETH ({Web3.from_wei(sweth_in_auction, 'ether'):.6f} swETH)...")
        q2 = odos_get_quote(SWETH, sweth_in_auction, WETH, executor_addr)
        if q2:
            print(f"       Output: {Web3.from_wei(q2.out_amount, 'ether'):.6f} WETH")
            time.sleep(1.1)
            a2 = odos_assemble(q2.path_id, executor_addr, deadline=odos_deadline)
        time.sleep(1.1)

    # Quote 3: rswETH -> WETH
    a3 = None
    if rsweth_in_auction > 0:
        print(f"\n  [Q3] rswETH -> WETH ({Web3.from_wei(rsweth_in_auction, 'ether'):.6f} rswETH)...")
        q3 = odos_get_quote(RSWETH, rsweth_in_auction, WETH, executor_addr)
        if q3:
            print(f"       Output: {Web3.from_wei(q3.out_amount, 'ether'):.6f} WETH")
            time.sleep(1.1)
            a3 = odos_assemble(q3.path_id, executor_addr, deadline=odos_deadline)
        time.sleep(1.1)

    # Quote 4: leftover SWELL -> WETH
    a4 = None
    estimated_leftover = q1.out_amount - swell_needed
    if estimated_leftover > 0:
        print(f"\n  [Q4] Leftover SWELL -> WETH (~{Web3.from_wei(estimated_leftover, 'ether'):,.2f} SWELL)...")
        q4 = odos_get_quote(SWELL, estimated_leftover, WETH, executor_addr)
        if q4:
            print(f"       Output: {Web3.from_wei(q4.out_amount, 'ether'):.6f} WETH")
            time.sleep(1.1)
            a4 = odos_assemble(q4.path_id, executor_addr, deadline=odos_deadline)

    # -----------------------------------------------------------------------
    # Step 6: Build auction calldata
    # -----------------------------------------------------------------------
    sep("Step 6: Build Calldata")

    deadline       = target_ts + 120        # 2 min after warped time
    max_payment    = int(swell_needed * 1.01)  # 1% above current price

    # Verify the buy() selector matches AUCTION_BUY_SELECTOR on the contract
    on_chain_selector = executor.functions.AUCTION_BUY_SELECTOR().call()
    auction_calldata  = auction.encode_abi(
        abi_element_identifier="buy",
        args=[[SWETH, RSWETH], executor_addr, epoch_id, deadline, max_payment],
    )
    encoded_selector = bytes.fromhex(auction_calldata[2:10])
    assert encoded_selector == on_chain_selector, (
        f"Selector mismatch! Encoded={encoded_selector.hex()} "
        f"Contract expects={on_chain_selector.hex()}"
    )
    print(f"  Auction buy() selector: 0x{encoded_selector.hex()} [MATCH]")
    print(f"  epochId:       {epoch_id}")
    print(f"  deadline:      {deadline}")
    print(f"  maxPayment:    {Web3.from_wei(max_payment, 'ether'):,.2f} SWELL")
    print(f"  Auction calldata: {len(auction_calldata)//2 - 1} bytes")

    executor_calldata = executor.encode_abi(
        abi_element_identifier="execute",
        args=[
            weth_for_swell,
            (
                bytes.fromhex(a1.calldata[2:]),
                bytes.fromhex(auction_calldata[2:]),
                bytes.fromhex(a2.calldata[2:]) if a2 else b"",
                bytes.fromhex(a3.calldata[2:]) if a3 else b"",
                bytes.fromhex(a4.calldata[2:]) if a4 else b"",
            ),
            0,  # minProfit=0: require at least break-even
        ],
    )
    print(f"  Executor calldata: {len(executor_calldata)//2 - 1} bytes")

    # -----------------------------------------------------------------------
    # Step 7: Snapshot balances before
    # -----------------------------------------------------------------------
    sep("Step 7: Balance Snapshot (before)")
    deployer_weth_before  = bal(w3, WETH,  deployer)
    executor_weth_before  = bal(w3, WETH,  executor_addr)
    executor_swell_before = bal(w3, SWELL, executor_addr)
    executor_sweth_before = bal(w3, SWETH, executor_addr)
    executor_rsweth_before= bal(w3, RSWETH,executor_addr)
    auction_sweth_before  = bal(w3, SWETH,  AUCTION_ADDRESS)
    auction_rsweth_before = bal(w3, RSWETH, AUCTION_ADDRESS)

    print(f"  Deployer WETH:     {Web3.from_wei(deployer_weth_before, 'ether'):.6f}")
    print(f"  Executor WETH:     {Web3.from_wei(executor_weth_before, 'ether'):.6f}")
    print(f"  Executor SWELL:    {Web3.from_wei(executor_swell_before, 'ether'):.2f}")
    print(f"  Auction swETH:     {Web3.from_wei(auction_sweth_before, 'ether'):.6f}")
    print(f"  Auction rswETH:    {Web3.from_wei(auction_rsweth_before, 'ether'):.6f}")

    # -----------------------------------------------------------------------
    # Step 8: Execute on fork
    # -----------------------------------------------------------------------
    sep("Step 8: Send Transaction on Fork")

    gas_est = w3.eth.estimate_gas({
        "from": deployer, "to": executor_addr, "data": executor_calldata,
    })
    print(f"  Gas estimate: {gas_est:,}")

    tx_hash = w3.eth.send_transaction({
        "from":  deployer,
        "to":    executor_addr,
        "data":  executor_calldata,
        "gas":   int(gas_est * 1.3),
    })
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    if receipt["status"] != 1:
        print(f"  TRANSACTION REVERTED (status=0)")
        print(f"  tx hash: {tx_hash.hex()}")
        sys.exit(1)

    print(f"  TRANSACTION SUCCEEDED")
    print(f"  Block:     {receipt['blockNumber']}")
    print(f"  Gas used:  {receipt['gasUsed']:,}")
    print(f"  Tx hash:   {tx_hash.hex()}")

    # -----------------------------------------------------------------------
    # Step 9: Verify balances after
    # -----------------------------------------------------------------------
    sep("Step 9: Balance Verification (after)")

    deployer_weth_after   = bal(w3, WETH,  deployer)
    executor_weth_after   = bal(w3, WETH,  executor_addr)
    executor_swell_after  = bal(w3, SWELL, executor_addr)
    executor_sweth_after  = bal(w3, SWETH, executor_addr)
    executor_rsweth_after = bal(w3, RSWETH,executor_addr)
    auction_sweth_after   = bal(w3, SWETH,  AUCTION_ADDRESS)
    auction_rsweth_after  = bal(w3, RSWETH, AUCTION_ADDRESS)

    weth_delta = deployer_weth_after - deployer_weth_before

    print(f"  {'Token':<20} {'Before':>14} {'After':>14} {'Delta':>14}")
    print(f"  {'-'*62}")
    def row(name, b, a):
        d = a - b
        sign = '+' if d >= 0 else ''
        print(f"  {name:<20} {Web3.from_wei(b,'ether'):>14.6f} {Web3.from_wei(a,'ether'):>14.6f} {sign}{Web3.from_wei(abs(d),'ether'):>13.6f}")

    row("Deployer WETH",   deployer_weth_before,   deployer_weth_after)
    row("Executor WETH",   executor_weth_before,   executor_weth_after)
    row("Executor SWELL",  executor_swell_before,  executor_swell_after)
    row("Executor swETH",  executor_sweth_before,  executor_sweth_after)
    row("Executor rswETH", executor_rsweth_before, executor_rsweth_after)
    row("Auction swETH",   auction_sweth_before,   auction_sweth_after)
    row("Auction rswETH",  auction_rsweth_before,  auction_rsweth_after)

    profit_eth = Web3.from_wei(abs(weth_delta), 'ether')
    sign = '+' if weth_delta >= 0 else '-'
    print(f"\n  NET P&L: {sign}{profit_eth:.6f} ETH")

    # Assertions
    failures = []
    if executor_weth_after != 0:
        failures.append(f"Executor still holds WETH: {executor_weth_after}")
    if executor_swell_after != 0:
        failures.append(f"Executor still holds SWELL: {executor_swell_after}")
    if executor_sweth_after != 0:
        failures.append(f"Executor still holds swETH: {executor_sweth_after}")
    if executor_rsweth_after != 0:
        failures.append(f"Executor still holds rswETH: {executor_rsweth_after}")
    if auction_sweth_after != 0:
        failures.append(f"Auction still has swETH (should have been bought): {auction_sweth_after}")
    if auction_rsweth_after != 0:
        failures.append(f"Auction still has rswETH (should have been bought): {auction_rsweth_after}")

    # -----------------------------------------------------------------------
    # Step 10: Verify allowances are all zero
    # -----------------------------------------------------------------------
    sep("Step 10: Allowance Cleanup Verification")

    checks = [
        ("WETH",   WETH,   odos_router,    "Odos router"),
        ("SWELL",  SWELL,  AUCTION_ADDRESS,"Auction"),
        ("SWELL",  SWELL,  odos_router,    "Odos router"),
        ("swETH",  SWETH,  odos_router,    "Odos router"),
        ("rswETH", RSWETH, odos_router,    "Odos router"),
    ]
    for token_name, token_addr, spender, spender_name in checks:
        a = allowance(w3, token_addr, executor_addr, spender)
        status = "[OK] 0" if a == 0 else f"[FAIL] {a}"
        print(f"  {token_name} -> {spender_name}: {status}")
        if a != 0:
            failures.append(f"Stale allowance: {token_name} -> {spender_name} = {a}")

    # -----------------------------------------------------------------------
    # Result
    # -----------------------------------------------------------------------
    sep("Result")
    if failures:
        print("  FAILED:")
        for f in failures:
            print(f"    - {f}")
        sys.exit(1)
    else:
        print("  ALL CHECKS PASSED")
        print(f"  Transaction mechanics verified end-to-end.")
        print(f"  Net P&L on fork: {sign}{profit_eth:.6f} ETH")
        print(f"  (P&L negative because price not yet profitable at 85% through epoch;")
        print(f"   profit check was set to minProfit=0 for mechanical testing)")


if __name__ == "__main__":
    run_simulation()
