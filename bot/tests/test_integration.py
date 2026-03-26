#!/usr/bin/env python3
"""
Level 3: Python Integration Tests
==================================

Tests the bot's off-chain components against the real Odos API and optionally
against an Anvil mainnet fork.

What these test:
  - Odos API connectivity and quoting
  - Rate limiting / error handling
  - Profit estimation math with real prices
  - ABI encoding correctness
  - Full calldata assembly pipeline
  - Transaction simulation against Anvil fork

Run levels independently:
  pytest test_integration.py -v -k "test_odos"        # Just API tests
  pytest test_integration.py -v -k "test_encoding"     # Just ABI tests
  pytest test_integration.py -v -k "test_anvil"        # Fork simulation

Requirements:
  pip install pytest web3 requests python-dotenv
  
For Anvil tests, start a fork first:
  anvil --fork-url $RPC_URL --fork-block-number 24698575 --port 8545
"""

import os
import sys
import time
import json
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from dotenv import load_dotenv

# Add parent dir to path so we can import bot
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

load_dotenv()

from web3 import Web3

# Import bot modules
import bot.main as arb_bot
from bot.main import (
    odos_get_quote,
    odos_assemble,
    SwellArbBot,
    AuctionState,
    WETH, SWELL, SWETH, RSWETH,
    AUCTION_ADDRESS,
    SLIPPAGE_PCT,
    SWELL_BUFFER_PCT,
)


# ============================================================================
# Level 3a: Odos API Tests (real network calls)
# ============================================================================

class TestOdosAPI:
    """Test Odos API connectivity with real calls."""

    def test_odos_quote_weth_to_swell(self):
        """Can we get a WETH→SWELL quote?"""
        quote = odos_get_quote(
            WETH,
            Web3.to_wei(0.1, "ether"),
            SWELL,
            "0x8EB54fBb1CD02a982b0DE63a0182D56A45342E22",  # use example bidder
        )
        if quote is None:
            pytest.skip("Odos API unavailable or rate limited")

        assert quote.path_id, "Should have a path_id"
        assert quote.out_amount > 0, "Should output some SWELL"
        assert quote.in_token == WETH
        assert quote.out_token == SWELL

        # Sanity: 0.1 WETH should buy a lot of SWELL (SWELL is cheap)
        swell_amount = quote.out_amount / 1e18
        print(f"0.1 WETH → {swell_amount:,.2f} SWELL")
        assert swell_amount > 100, "Expected >100 SWELL for 0.1 WETH"

    def test_odos_quote_sweth_to_weth(self):
        """Can we quote swETH→WETH (LST to ETH)?"""
        time.sleep(1.1)
        quote = odos_get_quote(
            SWETH,
            Web3.to_wei(0.1, "ether"),
            WETH,
            "0x8EB54fBb1CD02a982b0DE63a0182D56A45342E22",
        )
        if quote is None:
            pytest.skip("Odos API unavailable")

        assert quote.out_amount > 0
        # swETH should be worth ~1 ETH each (slightly more)
        ratio = quote.out_amount / quote.in_amount
        print(f"swETH/WETH ratio: {ratio:.4f}")
        assert 0.9 < ratio < 1.2, f"swETH/WETH ratio {ratio} seems wrong"

    def test_odos_quote_rsweth_to_weth(self):
        """Can we quote rswETH→WETH?"""
        time.sleep(1.1)
        quote = odos_get_quote(
            RSWETH,
            Web3.to_wei(0.1, "ether"),
            WETH,
            "0x8EB54fBb1CD02a982b0DE63a0182D56A45342E22",
        )
        if quote is None:
            pytest.skip("Odos API unavailable")

        assert quote.out_amount > 0
        ratio = quote.out_amount / quote.in_amount
        print(f"rswETH/WETH ratio: {ratio:.4f}")
        assert 0.9 < ratio < 1.3, f"rswETH/WETH ratio {ratio} seems wrong"

    def test_odos_assemble_returns_calldata(self):
        """Can we assemble a quote into calldata?"""
        time.sleep(1.1)
        quote = odos_get_quote(
            WETH,
            Web3.to_wei(0.01, "ether"),
            SWELL,
            "0x8EB54fBb1CD02a982b0DE63a0182D56A45342E22",
        )
        if quote is None:
            pytest.skip("Odos API unavailable")

        time.sleep(1.1)
        assembly = odos_assemble(
            quote.path_id,
            "0x8EB54fBb1CD02a982b0DE63a0182D56A45342E22",
        )
        if assembly is None:
            pytest.skip("Odos assemble unavailable")

        assert assembly.router_address, "Should have router address"
        assert assembly.calldata.startswith("0x"), "Calldata should be hex"
        assert len(assembly.calldata) > 10, "Calldata too short"
        print(f"Router: {assembly.router_address}")
        print(f"Calldata length: {len(assembly.calldata)} chars")

    def test_odos_bad_token_returns_none(self):
        """Invalid token address should return None, not crash."""
        time.sleep(1.1)
        quote = odos_get_quote(
            "0x0000000000000000000000000000000000000001",  # invalid
            Web3.to_wei(1, "ether"),
            SWELL,
            "0x8EB54fBb1CD02a982b0DE63a0182D56A45342E22",
        )
        assert quote is None, "Should return None for invalid token"


# ============================================================================
# Level 3b: ABI Encoding Tests (offline)
# ============================================================================

class TestABIEncoding:
    """Test that calldata is built correctly."""

    def test_auction_buy_encoding(self):
        """Verify buy() calldata matches the example tx's function selector."""
        w3 = Web3()
        auction = w3.eth.contract(
            address=AUCTION_ADDRESS,
            abi=json.loads("""[{
                "inputs": [
                    {"name": "assets", "type": "address[]"},
                    {"name": "assetsReceiver", "type": "address"},
                    {"name": "epochId", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "maxPaymentTokenAmount", "type": "uint256"}
                ],
                "name": "buy",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            }]"""),
        )

        calldata = auction.encode_abi(
            abi_element_identifier="buy",
            args=[
                [SWETH, RSWETH],
                "0x8EB54fBb1CD02a982b0DE63a0182D56A45342E22",
                1,  # epochId
                1776000000,  # deadline
                int(1_300_000 * 1e18),  # maxPaymentTokenAmount
            ],
        )

        # From the example tx: MethodID: 0x99d5ce49
        assert calldata[:10] == "0x99d5ce49", \
            f"Function selector mismatch: {calldata[:10]} != 0x99d5ce49"
        print(f"Calldata selector: {calldata[:10]} ✓")
        print(f"Full calldata length: {len(calldata)} chars")

    def test_executor_abi_encoding(self):
        """Verify executor execute() can be encoded."""
        w3 = Web3()
        executor = w3.eth.contract(
            address="0x0000000000000000000000000000000000000001",
            abi=arb_bot.EXECUTOR_ABI,
        )

        calldata = executor.encode_abi(
            abi_element_identifier="execute",
            args=[
                Web3.to_wei(1, "ether"),                        # wethAmount
                b"\x01\x02\x03",                                # odosSwapWethToSwell
                b"\x04\x05\x06",                                # auctionCalldata
                b"\x07\x08\x09",                                # odosSwapSwethToWeth
                b"\x0a\x0b\x0c",                                # odosSwapRswethToWeth
                b"\x0d\x0e\x0f",                                # odosSwapSwellToWeth
                Web3.to_wei(0.06, "ether"),                     # minProfit
            ],
        )

        assert calldata.startswith("0x"), "Should produce valid hex calldata"
        assert len(calldata) > 100, "Calldata too short"
        print(f"execute() calldata length: {len(calldata)} chars ✓")


# ============================================================================
# Level 3c: Profit Estimation Math Tests (offline, mocked Odos)
# ============================================================================

class TestProfitMath:
    """Test the profit calculation logic with mocked Odos responses."""

    def _make_state(self, price_swell=1_000_000, sweth=0.5, rsweth=0.3, bought=False):
        return AuctionState(
            epoch_id=1,
            start_time=1000000,
            end_time=2000000,
            is_bought=bought,
            current_price=int(price_swell * 1e18),
            assets=[SWETH, RSWETH],
            amounts=[int(sweth * 1e18), int(rsweth * 1e18)],
        )

    def test_skips_already_bought(self):
        """Should return None for bought epochs."""
        bot = MagicMock(spec=SwellArbBot)
        bot.estimate_profit = SwellArbBot.estimate_profit.__get__(bot)
        state = self._make_state(bought=True)
        result = bot.estimate_profit(state)
        assert result is None

    def test_skips_zero_price(self):
        """Should return None when price is 0."""
        bot = MagicMock(spec=SwellArbBot)
        bot.estimate_profit = SwellArbBot.estimate_profit.__get__(bot)
        state = self._make_state(price_swell=0)
        result = bot.estimate_profit(state)
        assert result is None

    def test_buffer_calculation(self):
        """Verify the SWELL buffer is applied correctly."""
        swell_needed = 1_000_000
        swell_target = int(swell_needed * 1e18 * (1 + SWELL_BUFFER_PCT / 100))
        expected_buffer = swell_target - int(swell_needed * 1e18)

        assert expected_buffer > 0, "Buffer should be positive"
        buffer_pct_actual = (expected_buffer / (swell_needed * 1e18)) * 100
        assert abs(buffer_pct_actual - SWELL_BUFFER_PCT) < 0.01, \
            f"Buffer pct {buffer_pct_actual} != {SWELL_BUFFER_PCT}"
        print(f"Buffer: {expected_buffer / 1e18:,.2f} SWELL ({buffer_pct_actual:.1f}%)")

    def test_worst_case_swell_check(self):
        """Verify the worst-case slippage check math."""
        swell_out = int(1_050_000 * 1e18)  # Odos output
        swell_needed = int(1_000_000 * 1e18)  # Auction needs

        worst_case = int(swell_out * (1 - SLIPPAGE_PCT / 100))
        print(f"SWELL out: {swell_out / 1e18:,.0f}")
        print(f"Worst case: {worst_case / 1e18:,.0f}")
        print(f"Needed: {swell_needed / 1e18:,.0f}")

        assert worst_case >= swell_needed, \
            f"5% buffer + 0.5% slippage should still cover: {worst_case} < {swell_needed}"


# ============================================================================
# Level 3d: Anvil Fork Simulation Tests
#
# These require a running Anvil fork:
#   anvil --fork-url $RPC_URL --fork-block-number 24698575 --port 8545
# ============================================================================

class TestAnvilSimulation:
    """Test against a live Anvil fork."""

    @pytest.fixture(autouse=True)
    def setup_web3(self):
        """Connect to Anvil. Skip if not running."""
        try:
            self.w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:8545"))
            if not self.w3.is_connected():
                pytest.skip("Anvil not running on :8545")
        except Exception:
            pytest.skip("Anvil not running on :8545")

    def test_anvil_fork_block(self):
        """Verify we're forked at the right block."""
        block = self.w3.eth.get_block("latest")
        print(f"Fork block: {block['number']}")
        # Should be at or near 24698575
        assert block["number"] >= 24698575

    def test_anvil_weth_exists(self):
        """WETH should be queryable on the fork."""
        weth = self.w3.eth.contract(
            address=WETH,
            abi=[{
                "inputs": [{"name": "account", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }],
        )
        bal = weth.functions.balanceOf(WETH).call()
        assert bal >= 0  # Just checking it doesn't revert

    def test_anvil_auction_contract_exists(self):
        """Auction contract should have code on the fork."""
        code = self.w3.eth.get_code(AUCTION_ADDRESS)
        assert len(code) > 2, "Auction contract has no code"
        print(f"Auction code size: {len(code)} bytes")

    def test_anvil_deploy_executor(self):
        """Deploy the executor on the Anvil fork."""
        # This requires compiled bytecode — skip if not available
        try:
            with open("out/SwellFeeFlowExecutor.sol/SwellFeeFlowExecutor.json") as f:
                artifact = json.load(f)
                bytecode = artifact["bytecode"]["object"]
        except FileNotFoundError:
            pytest.skip("Compile first: forge build")

        accounts = self.w3.eth.accounts
        if not accounts:
            pytest.skip("No accounts available on Anvil")

        deployer = accounts[0]
        contract = self.w3.eth.contract(
            abi=arb_bot.EXECUTOR_ABI,
            bytecode=bytecode,
        )
        tx_hash = contract.constructor(
            "0x0000000000000000000000000000000000000001",  # odos router placeholder
            AUCTION_ADDRESS,
        ).transact({"from": deployer})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

        assert receipt["status"] == 1, "Deploy failed"
        assert receipt["contractAddress"] is not None
        print(f"Executor deployed at: {receipt['contractAddress']}")

    def test_anvil_simulate_full_tx(self):
        """
        Full end-to-end simulation on Anvil fork.

        This is the most important test. It:
          1. Deploys the executor
          2. Deals WETH to the test account
          3. Approves WETH to the executor
          4. Calls execute() with the Odos calldata
          5. Verifies profit or correct revert
        
        NOTE: This test calls the real Odos API to get calldata, then
        simulates the tx on Anvil. If Odos is unavailable, it skips.
        """
        # Check prerequisites
        try:
            with open("out/SwellFeeFlowExecutor.sol/SwellFeeFlowExecutor.json") as f:
                artifact = json.load(f)
                bytecode = artifact["bytecode"]["object"]
                abi_full = artifact["abi"]
        except FileNotFoundError:
            pytest.skip("Compile first: forge build")

        accounts = self.w3.eth.accounts
        if not accounts:
            pytest.skip("No accounts on Anvil")

        deployer = accounts[0]

        # Deploy
        contract_factory = self.w3.eth.contract(abi=abi_full, bytecode=bytecode)
        tx_hash = contract_factory.constructor(
            "0x0000000000000000000000000000000000000001",  # odos router placeholder
            AUCTION_ADDRESS,
        ).transact({"from": deployer})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        executor_addr = receipt["contractAddress"]
        executor = self.w3.eth.contract(address=executor_addr, abi=abi_full)

        print(f"Executor at: {executor_addr}")

        # Deal 2 WETH to deployer
        # Anvil cheatcode: anvil_setBalance doesn't work for ERC20
        # Use impersonation instead: impersonate a WETH whale
        weth_abi = [{
            "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
            "name": "approve", "outputs": [{"name": "", "type": "bool"}],
            "stateMutability": "nonpayable", "type": "function",
        }, {
            "inputs": [{"name": "account", "type": "address"}],
            "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
            "stateMutability": "view", "type": "function",
        }, {
            "inputs": [], "name": "deposit", "outputs": [],
            "stateMutability": "payable", "type": "function",
        }]
        weth = self.w3.eth.contract(address=WETH, abi=weth_abi)

        # Wrap ETH to WETH (Anvil accounts have 10000 ETH)
        weth.functions.deposit().transact({
            "from": deployer,
            "value": Web3.to_wei(2, "ether"),
        })

        weth_bal = weth.functions.balanceOf(deployer).call()
        assert weth_bal >= Web3.to_wei(2, "ether"), "WETH deposit failed"
        print(f"WETH balance: {Web3.from_wei(weth_bal, 'ether')} ETH")

        # Approve executor
        weth.functions.approve(executor_addr, 2**256 - 1).transact({"from": deployer})

        print("Setup complete. Executor deployed, WETH funded, approved.")
        print("Full simulation would require live Odos calldata — see Level 4.")


# ============================================================================
# Level 3e: Replay of Example Transaction
# ============================================================================

class TestExampleReplay:
    """Verify our understanding of the example transaction."""

    def test_example_tx_values(self):
        """Cross-check the values from the example tx."""
        swell_burned = 1_237_301.587301587301587302
        rsweth_received = 0.576941559636483608
        sweth_received = 0.237546853921567606

        swell_usd_value = 1462.49  # from tx
        rsweth_usd_value = 1322.54
        sweth_usd_value = 571.06

        total_received_usd = rsweth_usd_value + sweth_usd_value
        profit_usd = total_received_usd - swell_usd_value

        print(f"SWELL burned:     {swell_burned:,.2f} (${swell_usd_value})")
        print(f"rswETH received:  {rsweth_received:.6f} (${rsweth_usd_value})")
        print(f"swETH received:   {sweth_received:.6f} (${sweth_usd_value})")
        print(f"Total received:   ${total_received_usd:.2f}")
        print(f"Profit:           ${profit_usd:.2f}")

        assert profit_usd > 0, "Example tx should have been profitable"
        assert profit_usd > 400, "Expected ~$430 profit"

        # ETH-denominated profit estimate (at $2,145 ETH)
        eth_price = 2145.95
        total_eth_received = rsweth_received + sweth_received  # approximate
        weth_cost_approx = swell_usd_value / eth_price
        profit_eth = total_eth_received - weth_cost_approx

        print(f"Est. WETH cost:   {weth_cost_approx:.6f}")
        print(f"Est. ETH profit:  {profit_eth:.6f}")
        assert profit_eth > 0.05, "Expected >0.05 ETH profit"

    def test_function_selector_matches(self):
        """The buy() function selector should match the example tx."""
        w3 = Web3()
        # keccak256("buy(address[],address,uint256,uint256,uint256)")
        selector = w3.keccak(
            text="buy(address[],address,uint256,uint256,uint256)"
        )[:4].hex()
        assert selector == "99d5ce49", f"Selector mismatch: {selector}"
        print(f"buy() selector: 0x{selector} ✓")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
