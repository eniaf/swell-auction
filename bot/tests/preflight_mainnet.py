#!/usr/bin/env python3
"""
Level 5: Mainnet Pre-flight Checklist
=======================================

This is NOT a test — it's a validation script to run BEFORE your first real
execution. It checks every prerequisite against live mainnet.

Run: python test/preflight_mainnet.py

What it validates:
  ✓ RPC connectivity
  ✓ Executor contract is deployed and verified
  ✓ Executor owner matches your wallet
  ✓ Your WETH balance is sufficient
  ✓ WETH is approved to executor
  ✓ Executor has no stale allowances
  ✓ Auction contract is callable
  ✓ Flashbots RPC is reachable
  ✓ Odos API is responsive
  ✓ Bot config sanity checks
"""

import os
import sys
import json
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

from bot.main import (
    RPC_URL, FLASHBOTS_RPC,
    WETH, SWELL, SWETH, RSWETH,
    AUCTION_ADDRESS, EXECUTOR_ADDRESS,
    AUCTION_ABI, EXECUTOR_ABI, ERC20_ABI,
    MIN_PROFIT_ETH, SLIPPAGE_PCT, SWELL_BUFFER_PCT, DRY_RUN,
    odos_get_quote,
)

# ============================================================================

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"
checks_passed = 0
checks_failed = 0
checks_warned = 0


def check(name: str, condition: bool, detail: str = "", warn_only: bool = False):
    global checks_passed, checks_failed, checks_warned
    if condition:
        print(f"  {PASS} {name}" + (f" — {detail}" if detail else ""))
        checks_passed += 1
    elif warn_only:
        print(f"  {WARN} {name}" + (f" — {detail}" if detail else ""))
        checks_warned += 1
    else:
        print(f"  {FAIL} {name}" + (f" — {detail}" if detail else ""))
        checks_failed += 1


def run_preflight():
    print("=" * 60)
    print("Mainnet Pre-flight Checklist")
    print("=" * 60)

    # --- 1. RPC ---
    print("\n1. RPC Connectivity")
    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        connected = w3.is_connected()
        check("RPC connected", connected, RPC_URL[:50])
        if connected:
            chain_id = w3.eth.chain_id
            check("Chain ID is 1 (mainnet)", chain_id == 1, f"got {chain_id}")
            block = w3.eth.block_number
            check("Block number reasonable", block > 20_000_000, f"block {block}")
    except Exception as e:
        check("RPC connected", False, str(e)[:80])
        print("\n  Cannot continue without RPC. Exiting.")
        sys.exit(1)

    # --- 2. Wallet ---
    print("\n2. Wallet")
    pk = os.getenv("PRIVATE_KEY")
    check("PRIVATE_KEY is set", pk is not None and len(pk) > 10)

    if pk:
        try:
            from eth_account import Account
            account = Account.from_key(pk)
            addr = account.address
            check("Wallet address", True, addr)

            eth_bal = w3.eth.get_balance(addr)
            check("ETH balance > 0.01",
                  eth_bal > Web3.to_wei(0.01, "ether"),
                  f"{Web3.from_wei(eth_bal, 'ether'):.4f} ETH")

            weth_contract = w3.eth.contract(address=WETH, abi=ERC20_ABI)
            weth_bal = weth_contract.functions.balanceOf(addr).call()
            check("WETH balance > 0.1",
                  weth_bal > Web3.to_wei(0.1, "ether"),
                  f"{Web3.from_wei(weth_bal, 'ether'):.4f} WETH",
                  warn_only=True)
        except Exception as e:
            check("Wallet loads", False, str(e)[:80])
            addr = None
    else:
        addr = None

    # --- 3. Executor contract ---
    print("\n3. Executor Contract")
    is_zero = EXECUTOR_ADDRESS == "0x0000000000000000000000000000000000000000"
    check("EXECUTOR_ADDRESS is set", not is_zero, EXECUTOR_ADDRESS)

    if not is_zero:
        code = w3.eth.get_code(EXECUTOR_ADDRESS)
        check("Contract has code", len(code) > 2, f"{len(code)} bytes")

        try:
            # Check owner
            executor = w3.eth.contract(address=EXECUTOR_ADDRESS, abi=[
                {"inputs": [], "name": "owner", "outputs": [
                    {"name": "", "type": "address"}
                ], "stateMutability": "view", "type": "function"},
            ])
            contract_owner = executor.functions.owner().call()
            if addr:
                check("Owner matches wallet", contract_owner.lower() == addr.lower(),
                      f"owner={contract_owner[:10]}...")
            else:
                check("Owner readable", True, contract_owner[:10])
        except Exception as e:
            check("Owner readable", False, str(e)[:80])

        # Check WETH allowance from wallet to executor
        if addr:
            weth_contract = w3.eth.contract(address=WETH, abi=ERC20_ABI)
            allowance = weth_contract.functions.allowance(addr, EXECUTOR_ADDRESS).call()
            check("WETH approved to executor",
                  allowance > Web3.to_wei(0.1, "ether"),
                  f"allowance={Web3.from_wei(allowance, 'ether'):.2f} WETH")

        # Check executor has no stale allowances
        for name, token_addr, spender in [
            ("WETH→OdosRouter", WETH, "dynamic"),
            ("SWELL→Auction", SWELL, AUCTION_ADDRESS),
            ("swETH→OdosRouter", SWETH, "dynamic"),
            ("rswETH→OdosRouter", RSWETH, "dynamic"),
        ]:
            if spender == "dynamic":
                # Can't check Odos router without knowing address
                continue
            token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
            try:
                all_ = token.functions.allowance(EXECUTOR_ADDRESS, spender).call()
                check(f"No stale allowance: {name}", all_ == 0,
                      f"allowance={all_}", warn_only=True)
            except:
                pass

    # --- 4. Auction contract ---
    print("\n4. Auction Contract")
    code = w3.eth.get_code(AUCTION_ADDRESS)
    check("Auction has code", len(code) > 2, f"{len(code)} bytes")

    try:
        auction = w3.eth.contract(address=AUCTION_ADDRESS, abi=AUCTION_ABI)
        epoch = auction.functions.currentEpoch().call()
        check("currentEpoch() callable", True, f"epoch={epoch}")
    except Exception as e:
        check("currentEpoch() callable", False, str(e)[:80])
        print(f"    → You may need to update the auction ABI")

    # --- 5. Flashbots ---
    print("\n5. Flashbots Protect")
    try:
        resp = requests.post(
            FLASHBOTS_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
            timeout=10,
        )
        check("Flashbots RPC reachable", resp.status_code == 200,
              f"status={resp.status_code}")
    except Exception as e:
        check("Flashbots RPC reachable", False, str(e)[:80])

    # --- 6. Odos API ---
    print("\n6. Odos API")
    try:
        probe_addr = addr or "0x8EB54fBb1CD02a982b0DE63a0182D56A45342E22"
        quote = odos_get_quote(WETH, Web3.to_wei(0.01, "ether"), SWELL, probe_addr)
        check("Odos quote works", quote is not None,
              f"pathId={quote.path_id[:16]}..." if quote else "returned None")
        if quote:
            check("Odos returns SWELL output", quote.out_amount > 0,
                  f"{quote.out_amount / 1e18:,.2f} SWELL")
    except Exception as e:
        check("Odos API works", False, str(e)[:80])

    # --- 7. Bot config ---
    print("\n7. Bot Configuration")
    check("MIN_PROFIT_ETH > 0", MIN_PROFIT_ETH > 0, f"{MIN_PROFIT_ETH} ETH")
    check("SLIPPAGE_PCT reasonable", 0.1 <= SLIPPAGE_PCT <= 5.0,
          f"{SLIPPAGE_PCT}%")
    check("SWELL_BUFFER_PCT reasonable", 1.0 <= SWELL_BUFFER_PCT <= 20.0,
          f"{SWELL_BUFFER_PCT}%")
    check("SWELL_BUFFER > SLIPPAGE",
          SWELL_BUFFER_PCT > SLIPPAGE_PCT,
          f"buffer {SWELL_BUFFER_PCT}% > slippage {SLIPPAGE_PCT}%")
    check("DRY_RUN is true (for first run)", DRY_RUN,
          "set DRY_RUN=false only after this passes", warn_only=True)

    # --- Summary ---
    print("\n" + "=" * 60)
    total = checks_passed + checks_failed + checks_warned
    print(f"Results: {checks_passed}/{total} passed, "
          f"{checks_failed} failed, {checks_warned} warnings")

    if checks_failed == 0:
        print(f"\n{PASS} ALL CRITICAL CHECKS PASSED")
        if checks_warned > 0:
            print(f"{WARN} Review warnings before going live")
        print("\nNext steps:")
        print("  1. Run bot in DRY_RUN=true mode: python bot.py")
        print("  2. Wait for an active auction epoch")
        print("  3. Verify dry-run output looks correct")
        print("  4. Set DRY_RUN=false and run: python bot.py")
    else:
        print(f"\n{FAIL} {checks_failed} CRITICAL CHECKS FAILED")
        print("Fix the issues above before proceeding.")

    print("=" * 60)
    return checks_failed == 0


if __name__ == "__main__":
    success = run_preflight()
    sys.exit(0 if success else 1)
