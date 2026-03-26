#!/usr/bin/env python3
"""
Swell Fee Flow Dutch Auction - Atomic Arbitrage Bot
====================================================

Monitors the Swell Fee Flow auction contract for profitable opportunities,
builds atomic transactions via a custom executor contract, and submits
through Flashbots Protect for MEV protection.

Architecture:
  1. Monitor auction state (epoch, current price, assets available)
  2. When an epoch is active, continuously price the full arb loop:
     WETH → SWELL (Odos) → Auction bid → swETH+rswETH → WETH (Odos)
  3. If profit > threshold, build + submit via Flashbots Protect
  4. Contract enforces atomic execution & min profit check

Requirements:
  pip install web3 requests python-dotenv eth-account

Usage:
  1. Copy .env.example to .env and fill in values
  2. Deploy SwellFeeFlowExecutor.sol (pass odosRouter + auctionContract to constructor)
  3. Approve WETH to executor contract
  4. Run: python bot.py
"""

import os
import sys
import json
import time
import logging
import threading
import requests
from decimal import Decimal
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
from eth_account.signers.local import LocalAccount

load_dotenv()

# ============================================================================
# Configuration
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")],
)
log = logging.getLogger(__name__)

# --- Network ---
RPC_URL = os.getenv("RPC_URL", "https://eth.llamarpc.com")
CHAIN_ID = 1  # Ethereum Mainnet

# --- Flashbots ---
FLASHBOTS_RPC = "https://rpc.flashbots.net/fast"

# --- Tokens ---
WETH   = Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
SWELL  = Web3.to_checksum_address("0x0a6E7Ba5042B38349e437ec6Db6214AEC7B35676")
SWETH  = Web3.to_checksum_address("0xf951E335afb289353dc249e82926178EaC7DEd78")
RSWETH = Web3.to_checksum_address("0xFAe103DC9cf190eD75350761e95403b7b8aFa6c0")

# --- Contracts ---
AUCTION_ADDRESS  = Web3.to_checksum_address("0xf17b581496bc2669ce0931FAcAA1ADe35029E85D")
EXECUTOR_ADDRESS = Web3.to_checksum_address(os.getenv("EXECUTOR_ADDRESS", "0x0000000000000000000000000000000000000000"))

# --- Deposit monitoring ---
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEPOSIT_TOKENS = [SWETH, RSWETH]   # watch inbound transfers of these tokens to the auction

# --- Odos API ---
ODOS_QUOTE_URL    = "https://api.odos.xyz/sor/quote/v2"
ODOS_ASSEMBLE_URL = "https://api.odos.xyz/sor/assemble"
ODOS_API_KEY = os.getenv("ODOS_API_KEY", "")

# --- Bot params ---
MIN_PROFIT_ETH        = float(os.getenv("MIN_PROFIT_ETH", "0.06"))
GAS_SAFETY_MULT       = float(os.getenv("GAS_SAFETY_MULT", "1.5"))  # Multiply estimated gas cost for minProfit
POLL_INTERVAL         = int(os.getenv("POLL_INTERVAL", "30"))
DEPOSIT_POLL_INTERVAL = int(os.getenv("DEPOSIT_POLL_INTERVAL", "12"))   # seconds between log polls (~1 block)
HEARTBEAT_INTERVAL    = int(os.getenv("HEARTBEAT_INTERVAL",    "60"))   # fallback main-loop cadence
SLIPPAGE_PCT          = float(os.getenv("SLIPPAGE_PCT", "0.2"))
SWELL_BUFFER_PCT  = float(os.getenv("SWELL_BUFFER_PCT", "1.0"))
DRY_RUN           = os.getenv("DRY_RUN", "true").lower() == "true"

# ============================================================================
# ABIs (minimal)
# ============================================================================

AUCTION_ABI = json.loads("""[
    {
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
    },
    {
        "inputs": [],
        "name": "currentEpoch",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "paymentToken",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "epochId", "type": "uint256"}],
        "name": "getEpochInfo",
        "outputs": [
            {"name": "startTime", "type": "uint256"},
            {"name": "endTime", "type": "uint256"},
            {"name": "startPrice", "type": "uint256"},
            {"name": "endPrice", "type": "uint256"},
            {"name": "isBought", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "epochId", "type": "uint256"}],
        "name": "getCurrentPrice",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "epochId", "type": "uint256"}],
        "name": "getAvailableAssets",
        "outputs": [
            {"name": "assets", "type": "address[]"},
            {"name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]""")

ERC20_ABI = json.loads("""[
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"stateMutability":"view","type":"function"}
]""")

# Updated executor ABI — odosRouterAddr removed (now stored on-chain)
EXECUTOR_ABI = json.loads("""[
    {
        "inputs": [
            {"name": "wethAmount", "type": "uint256"},
            {"name": "odosSwapWethToSwell", "type": "bytes"},
            {"name": "auctionCalldata", "type": "bytes"},
            {"name": "odosSwapSwethToWeth", "type": "bytes"},
            {"name": "odosSwapRswethToWeth", "type": "bytes"},
            {"name": "odosSwapSwellToWeth", "type": "bytes"},
            {"name": "minProfit", "type": "uint256"}
        ],
        "name": "execute",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "_router", "type": "address"}],
        "name": "setOdosRouter",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "_auction", "type": "address"}],
        "name": "setAuctionContract",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "odosRouter",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "auctionContract",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]""")


# ============================================================================
# Data classes
# ============================================================================

@dataclass
class AuctionState:
    epoch_id: int
    start_time: int
    end_time: int
    is_bought: bool
    current_price: int
    assets: list
    amounts: list


@dataclass
class OdosQuote:
    path_id: str
    in_token: str
    in_amount: int
    out_token: str
    out_amount: int
    gas_estimate: int


@dataclass
class OdosAssembly:
    router_address: str
    calldata: str
    value: str
    gas: int


# ============================================================================
# Odos API helpers
# ============================================================================

def odos_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if ODOS_API_KEY:
        h["x-api-key"] = ODOS_API_KEY
    return h


def odos_get_quote(
    from_token: str,
    from_amount: int,
    to_token: str,
    user_addr: str,
) -> Optional[OdosQuote]:
    """Get a swap quote from Odos API."""
    body = {
        "chainId": CHAIN_ID,
        "inputTokens": [{"tokenAddress": from_token, "amount": str(from_amount)}],
        "outputTokens": [{"tokenAddress": to_token, "proportion": 1}],
        "userAddr": user_addr,
        "slippageLimitPercent": SLIPPAGE_PCT,
        "compact": True,
        "simple": False,
    }
    try:
        resp = requests.post(ODOS_QUOTE_URL, json=body, headers=odos_headers(), timeout=15)
        if resp.status_code != 200:
            log.warning(f"Odos quote failed: {resp.status_code} {resp.text[:200]}")
            return None
        data = resp.json()
        return OdosQuote(
            path_id=data["pathId"],
            in_token=from_token,
            in_amount=from_amount,
            out_token=to_token,
            out_amount=int(data["outAmounts"][0]),
            gas_estimate=data.get("gasEstimate", 0),
        )
    except Exception as e:
        log.error(f"Odos quote error: {e}")
        return None


def odos_assemble(path_id: str, user_addr: str) -> Optional[OdosAssembly]:
    """Assemble transaction calldata from an Odos quote."""
    body = {
        "userAddr": user_addr,
        "pathId": path_id,
        "simulate": False,
    }
    try:
        resp = requests.post(ODOS_ASSEMBLE_URL, json=body, headers=odos_headers(), timeout=15)
        if resp.status_code != 200:
            log.warning(f"Odos assemble failed: {resp.status_code} {resp.text[:200]}")
            return None
        data = resp.json()
        tx = data["transaction"]
        return OdosAssembly(
            router_address=tx["to"],
            calldata=tx["data"],
            value=tx.get("value", "0"),
            gas=tx.get("gas", 500000),
        )
    except Exception as e:
        log.error(f"Odos assemble error: {e}")
        return None


# ============================================================================
# Core bot logic
# ============================================================================

class SwellArbBot:
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))

        pk = os.getenv("PRIVATE_KEY")
        if pk:
            self.account: LocalAccount = Account.from_key(pk)
            log.info(f"Bot wallet: {self.account.address}")
        else:
            self.account = None
            log.warning("No PRIVATE_KEY set — running in read-only mode")

        self.auction = self.w3.eth.contract(
            address=AUCTION_ADDRESS, abi=AUCTION_ABI
        )

        if EXECUTOR_ADDRESS != "0x0000000000000000000000000000000000000000":
            self.executor = self.w3.eth.contract(
                address=EXECUTOR_ADDRESS, abi=EXECUTOR_ABI
            )
        else:
            self.executor = None
            log.warning("No EXECUTOR_ADDRESS set — cannot submit txs")

        self._exec_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._last_checked_block: int = 0

    # --- Startup validation ---

    def validate_setup(self):
        """Verify the executor's on-chain router/auction match what we expect."""
        if not self.executor:
            return

        on_chain_router = self.executor.functions.odosRouter().call()
        on_chain_auction = self.executor.functions.auctionContract().call()

        log.info(f"Executor on-chain router:  {on_chain_router}")
        log.info(f"Executor on-chain auction: {on_chain_auction}")

        if on_chain_auction.lower() != AUCTION_ADDRESS.lower():
            log.warning(
                f"⚠ Auction mismatch! Contract has {on_chain_auction}, "
                f"bot expects {AUCTION_ADDRESS}. Call setAuctionContract() or update .env"
            )

    # --- Auction state ---

    def get_auction_state(self) -> Optional[AuctionState]:
        """Read current epoch info from the auction contract."""
        try:
            epoch_id = self.auction.functions.currentEpoch().call()
            log.info(f"Current epoch: {epoch_id}")

            try:
                info = self.auction.functions.getEpochInfo(epoch_id).call()
                start_time, end_time, start_price, end_price, is_bought = info
            except Exception:
                log.warning("getEpochInfo not available, trying alternative...")
                start_time = 0
                end_time = 0
                is_bought = False

            try:
                current_price = self.auction.functions.getCurrentPrice(epoch_id).call()
            except Exception:
                current_price = 0
                log.warning("getCurrentPrice not available")

            try:
                assets, amounts = self.auction.functions.getAvailableAssets(epoch_id).call()
            except Exception:
                assets = [SWETH, RSWETH]
                amounts = [0, 0]
                log.warning("getAvailableAssets not available")

            return AuctionState(
                epoch_id=epoch_id,
                start_time=start_time,
                end_time=end_time,
                is_bought=is_bought,
                current_price=current_price,
                assets=assets,
                amounts=amounts,
            )
        except Exception as e:
            log.error(f"Failed to read auction state: {e}")
            return None

    # --- Profitability calculation ---

    def estimate_profit(self, state: AuctionState) -> Optional[Dict[str, Any]]:
        """
        Estimate full arb profitability with SWELL over-buy buffer.

        Quote chain (respecting 1 RPS free tier):
          1. WETH → SWELL (probe + actual)
          2. swETH → WETH
          3. rswETH → WETH
          4. leftover SWELL → WETH
        """
        if state.is_bought:
            log.info(f"Epoch {state.epoch_id} already bought, skipping")
            return None

        if state.current_price == 0:
            log.info("Current price is 0, auction may not be active")
            return None

        swell_needed = state.current_price
        swell_target = int(swell_needed * (1 + SWELL_BUFFER_PCT / 100))
        swell_buffer = swell_target - swell_needed

        log.info(f"SWELL needed for auction:  {Web3.from_wei(swell_needed, 'ether'):,.2f}")
        log.info(f"SWELL target (w/ {SWELL_BUFFER_PCT}% buf): {Web3.from_wei(swell_target, 'ether'):,.2f}")

        # --- Quote 1: probe to get WETH→SWELL rate ---
        probe_weth = Web3.to_wei(0.5, "ether")
        probe_quote = odos_get_quote(WETH, probe_weth, SWELL, EXECUTOR_ADDRESS)
        if not probe_quote or probe_quote.out_amount == 0:
            log.warning("Odos probe quote failed")
            return None

        swell_per_weth = probe_quote.out_amount / probe_weth
        weth_for_swell = int(swell_target / swell_per_weth)
        weth_for_swell = int(weth_for_swell * (1 + SLIPPAGE_PCT / 100))

        log.info(f"Rate: {swell_per_weth:.2f} SWELL/WETH")
        log.info(f"WETH needed (w/ slippage): {Web3.from_wei(weth_for_swell, 'ether'):.6f}")

        time.sleep(1.1)

        # Actual quote with calculated WETH amount
        quote_swell = odos_get_quote(WETH, weth_for_swell, SWELL, EXECUTOR_ADDRESS)
        if not quote_swell:
            log.warning("Odos WETH→SWELL quote failed")
            return None

        weth_cost = quote_swell.in_amount
        swell_out = quote_swell.out_amount
        log.info(f"WETH cost:     {Web3.from_wei(weth_cost, 'ether'):.6f}")
        log.info(f"SWELL output:  {Web3.from_wei(swell_out, 'ether'):,.2f}")

        worst_case_swell = int(swell_out * (1 - SLIPPAGE_PCT / 100))
        if worst_case_swell < swell_needed:
            log.warning(f"Worst-case SWELL ({Web3.from_wei(worst_case_swell, 'ether'):,.2f}) "
                        f"< needed ({Web3.from_wei(swell_needed, 'ether'):,.2f}). "
                        f"Buffer insufficient, need more WETH")
            return None

        log.info(f"Worst-case SWELL after slippage: {Web3.from_wei(worst_case_swell, 'ether'):,.2f} ✓")

        time.sleep(1.1)

        # --- Quote 2: swETH → WETH ---
        sweth_amount = 0
        rsweth_amount = 0
        for i, asset in enumerate(state.assets):
            asset_lower = asset.lower()
            if asset_lower == SWETH.lower():
                sweth_amount = state.amounts[i]
            elif asset_lower == RSWETH.lower():
                rsweth_amount = state.amounts[i]

        weth_from_sweth = 0
        if sweth_amount > 0:
            quote_sweth = odos_get_quote(SWETH, sweth_amount, WETH, EXECUTOR_ADDRESS)
            if quote_sweth:
                weth_from_sweth = quote_sweth.out_amount
                log.info(f"swETH → WETH:  {Web3.from_wei(weth_from_sweth, 'ether'):.6f}")
            time.sleep(1.1)

        # --- Quote 3: rswETH → WETH ---
        weth_from_rsweth = 0
        if rsweth_amount > 0:
            quote_rsweth = odos_get_quote(RSWETH, rsweth_amount, WETH, EXECUTOR_ADDRESS)
            if quote_rsweth:
                weth_from_rsweth = quote_rsweth.out_amount
                log.info(f"rswETH → WETH: {Web3.from_wei(weth_from_rsweth, 'ether'):.6f}")
            time.sleep(1.1)

        # --- Quote 4: leftover SWELL → WETH ---
        estimated_leftover = swell_out - swell_needed
        weth_from_leftover = 0
        if estimated_leftover > 0:
            quote_leftover = odos_get_quote(SWELL, estimated_leftover, WETH, EXECUTOR_ADDRESS)
            if quote_leftover:
                weth_from_leftover = quote_leftover.out_amount
                log.info(f"SWELL leftover → WETH: {Web3.from_wei(weth_from_leftover, 'ether'):.6f}")

        # --- Calculate total profit ---
        total_weth_out = weth_from_sweth + weth_from_rsweth + weth_from_leftover
        profit = total_weth_out - weth_cost
        profit_eth = Web3.from_wei(abs(profit), 'ether')

        log.info("=" * 60)
        log.info(f"WETH in:              {Web3.from_wei(weth_cost, 'ether'):.6f}")
        log.info(f"WETH from swETH:      {Web3.from_wei(weth_from_sweth, 'ether'):.6f}")
        log.info(f"WETH from rswETH:     {Web3.from_wei(weth_from_rsweth, 'ether'):.6f}")
        log.info(f"WETH from leftover:   {Web3.from_wei(weth_from_leftover, 'ether'):.6f}")
        log.info(f"WETH out total:       {Web3.from_wei(total_weth_out, 'ether'):.6f}")
        log.info(f"Profit:               {'+' if profit > 0 else '-'}{profit_eth:.6f} ETH")
        log.info(f"Min required:         {MIN_PROFIT_ETH} ETH")
        log.info(f"Profitable:           {'YES ✓' if profit > 0 and float(profit_eth) >= MIN_PROFIT_ETH else 'NO ✗'}")
        log.info("=" * 60)

        if profit > 0 and float(profit_eth) >= MIN_PROFIT_ETH:
            return {
                "epoch_id": state.epoch_id,
                "weth_in": weth_cost,
                "swell_needed": swell_needed,
                "swell_target": swell_target,
                "estimated_leftover": estimated_leftover,
                "sweth_out": sweth_amount,
                "rsweth_out": rsweth_amount,
                "weth_out": total_weth_out,
                "profit": profit,
            }
        return None

    # --- Transaction building ---

    def build_and_submit(self, arb_info: Dict[str, Any]) -> Optional[str]:
        """
        Build the atomic arb transaction and submit via Flashbots Protect.

        Gas-aware minProfit: the on-chain minProfit is set to
        (MIN_PROFIT_ETH + estimated_gas_cost * GAS_SAFETY_MULT)
        so the contract reverts if profit doesn't cover gas.
        """
        if not self.account or not self.executor:
            log.error("No account or executor configured")
            return None

        executor_addr = EXECUTOR_ADDRESS
        epoch_id = arb_info["epoch_id"]
        weth_amount = arb_info["weth_in"]

        log.info("Building atomic arb transaction...")

        # --- Get fresh Odos quotes & assemble ---

        # 1) WETH → SWELL (with buffer)
        log.info("Assembling WETH → SWELL...")
        q1 = odos_get_quote(WETH, weth_amount, SWELL, executor_addr)
        if not q1:
            log.error("Failed to quote WETH → SWELL")
            return None
        a1 = odos_assemble(q1.path_id, executor_addr)
        if not a1:
            log.error("Failed to assemble WETH → SWELL")
            return None

        # Verify the assembled router matches the on-chain whitelist
        on_chain_router = self.executor.functions.odosRouter().call()
        if a1.router_address.lower() != on_chain_router.lower():
            log.error(
                f"Router mismatch! Odos returned {a1.router_address}, "
                f"contract expects {on_chain_router}. "
                f"Call setOdosRouter() if Odos upgraded their router."
            )
            return None

        time.sleep(1.1)

        # 2) swETH → WETH
        log.info("Assembling swETH → WETH...")
        sweth_amount = arb_info["sweth_out"]
        a2 = None
        if sweth_amount > 0:
            q2 = odos_get_quote(SWETH, sweth_amount, WETH, executor_addr)
            if q2:
                a2 = odos_assemble(q2.path_id, executor_addr)
            time.sleep(1.1)

        # 3) rswETH → WETH
        log.info("Assembling rswETH → WETH...")
        rsweth_amount = arb_info["rsweth_out"]
        a3 = None
        if rsweth_amount > 0:
            q3 = odos_get_quote(RSWETH, rsweth_amount, WETH, executor_addr)
            if q3:
                a3 = odos_assemble(q3.path_id, executor_addr)
            time.sleep(1.1)

        # 4) Leftover SWELL → WETH
        log.info("Assembling leftover SWELL → WETH...")
        estimated_leftover = arb_info.get("estimated_leftover", 0)
        a4 = None
        if estimated_leftover > 0:
            q4 = odos_get_quote(SWELL, estimated_leftover, WETH, executor_addr)
            if q4:
                a4 = odos_assemble(q4.path_id, executor_addr)
            time.sleep(1.1)

        # --- Build auction calldata ---
        deadline = int(time.time()) + 600
        max_payment = arb_info["swell_target"]

        auction_calldata = self.auction.encodeABI(
            fn_name="buy",
            args=[
                [SWETH, RSWETH],
                executor_addr,
                epoch_id,
                deadline,
                max_payment,
            ],
        )

        # --- Estimate gas cost for gas-aware minProfit ---
        base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        max_priority_fee = Web3.to_wei(2, "gwei")
        max_fee = base_fee * 2 + max_priority_fee

        # Rough gas estimate for the full arb (4 swaps + auction + overhead)
        estimated_gas_units = 1_000_000  # Conservative; refined after eth_estimateGas
        estimated_gas_cost_wei = estimated_gas_units * max_fee
        log.info(f"Estimated gas cost: {Web3.from_wei(estimated_gas_cost_wei, 'ether'):.6f} ETH")

        # On-chain minProfit = user threshold + gas cost * safety multiplier
        # This way the contract reverts if profit doesn't cover gas
        min_profit_wei = Web3.to_wei(MIN_PROFIT_ETH, "ether") + int(estimated_gas_cost_wei * GAS_SAFETY_MULT)
        log.info(f"On-chain minProfit (gas-aware): {Web3.from_wei(min_profit_wei, 'ether'):.6f} ETH")

        # --- Build executor calldata (no odosRouterAddr — it's on-chain now) ---
        executor_calldata = self.executor.encodeABI(
            fn_name="execute",
            args=[
                weth_amount,
                bytes.fromhex(a1.calldata[2:]),                        # WETH→SWELL
                bytes.fromhex(auction_calldata[2:]),                    # auction buy()
                bytes.fromhex(a2.calldata[2:]) if a2 else b"",         # swETH→WETH
                bytes.fromhex(a3.calldata[2:]) if a3 else b"",         # rswETH→WETH
                bytes.fromhex(a4.calldata[2:]) if a4 else b"",         # leftover SWELL→WETH
                min_profit_wei,
            ],
        )

        # --- Build transaction ---
        nonce = self.w3.eth.get_transaction_count(self.account.address)

        tx = {
            "chainId": CHAIN_ID,
            "from": self.account.address,
            "to": executor_addr,
            "data": executor_calldata,
            "nonce": nonce,
            "gas": 1_000_000,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority_fee,
            "type": 2,
            "value": 0,
        }

        # --- Estimate gas (also serves as a dry-run simulation) ---
        try:
            gas_estimate = self.w3.eth.estimate_gas(tx)
            tx["gas"] = int(gas_estimate * 1.3)
            log.info(f"Gas estimate: {gas_estimate}, using: {tx['gas']}")

            # Re-calculate gas-aware minProfit with the real estimate
            real_gas_cost = gas_estimate * max_fee
            min_profit_refined = Web3.to_wei(MIN_PROFIT_ETH, "ether") + int(real_gas_cost * GAS_SAFETY_MULT)
            if min_profit_refined > min_profit_wei:
                log.info(f"Refining minProfit upward: {Web3.from_wei(min_profit_refined, 'ether'):.6f} ETH")
                # Rebuild calldata with refined minProfit
                executor_calldata = self.executor.encodeABI(
                    fn_name="execute",
                    args=[
                        weth_amount,
                        bytes.fromhex(a1.calldata[2:]),
                        bytes.fromhex(auction_calldata[2:]),
                        bytes.fromhex(a2.calldata[2:]) if a2 else b"",
                        bytes.fromhex(a3.calldata[2:]) if a3 else b"",
                        bytes.fromhex(a4.calldata[2:]) if a4 else b"",
                        min_profit_refined,
                    ],
                )
                tx["data"] = executor_calldata
        except Exception as e:
            log.warning(f"Gas estimation failed (tx would revert): {e}")
            log.warning("This likely means the arb is not actually profitable on-chain")
            return None

        if DRY_RUN:
            log.info("=" * 60)
            log.info("DRY RUN — Transaction would be:")
            log.info(f"  To:       {tx['to']}")
            log.info(f"  Gas:      {tx['gas']}")
            log.info(f"  MaxFee:   {Web3.from_wei(tx['maxFeePerGas'], 'gwei'):.2f} gwei")
            log.info(f"  Profit:   {Web3.from_wei(arb_info['profit'], 'ether'):.6f} ETH")
            log.info(f"  minProfit (on-chain): {Web3.from_wei(min_profit_refined if 'min_profit_refined' in dir() else min_profit_wei, 'ether'):.6f} ETH")
            log.info("=" * 60)
            return "DRY_RUN"

        # --- Sign & submit via Flashbots Protect ---
        signed = self.account.sign_transaction(tx)

        log.info("Submitting via Flashbots Protect (private mempool)...")
        fb_response = requests.post(
            FLASHBOTS_RPC,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_sendRawTransaction",
                "params": [signed.raw_transaction.hex()],
            },
            headers={"Content-Type": "application/json"},
            timeout=30,
        )

        result = fb_response.json()
        if "result" in result:
            tx_hash = result["result"]
            log.info(f"✓ Transaction submitted via Flashbots: {tx_hash}")
            log.info(f"  Note: tx won't appear on Etherscan until mined")
            log.info(f"  Check: https://protect.flashbots.net/tx/{tx_hash}")
            return tx_hash
        else:
            log.error(f"Flashbots submission failed: {result}")
            return None

    # --- Deposit monitoring ---

    def _get_last_block(self) -> int:
        return self.w3.eth.block_number

    def _poll_deposits(self, from_block: int, to_block: int) -> bool:
        """Return True if any rswETH or swETH was transferred into the auction contract."""
        auction_topic = "0x" + AUCTION_ADDRESS[2:].lower().zfill(64)
        try:
            logs = self.w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock":   to_block,
                "address":   DEPOSIT_TOKENS,
                "topics": [
                    TRANSFER_TOPIC,
                    None,           # any sender
                    auction_topic,  # to == auction contract
                ],
            })
            if logs:
                for entry in logs:
                    token = entry["address"]
                    symbol = "swETH" if token.lower() == SWETH.lower() else "rswETH"
                    log.info(f"Deposit detected: {symbol} → auction (block {entry['blockNumber']})")
            return len(logs) > 0
        except Exception as e:
            log.warning(f"eth_getLogs error: {e}")
            return False

    def _try_execute(self, trigger: str) -> None:
        """Run the full arb check pipeline. Skips if another execution is already in progress."""
        if not self._exec_lock.acquire(blocking=False):
            log.info(f"[{trigger}] Execution already in progress, skipping")
            return
        try:
            state = self.get_auction_state()
            if state and not state.is_bought:
                log.info(f"[{trigger}] Active epoch {state.epoch_id}, checking profitability...")
                arb = self.estimate_profit(state)
                if arb:
                    log.info(f"[{trigger}] Profitable opportunity found!")
                    result = self.build_and_submit(arb)
                    if result:
                        log.info(f"[{trigger}] Done: {result}")
                        if not DRY_RUN:
                            time.sleep(120)  # cooldown — lock stays held during this period
            elif state and state.is_bought:
                log.info(f"[{trigger}] Epoch {state.epoch_id} already bought. Waiting for next epoch...")
        except Exception as e:
            log.error(f"[{trigger}] Execution error: {e}", exc_info=True)
        finally:
            self._exec_lock.release()

    def _run_deposit_watcher(self) -> None:
        """Background thread: polls for inbound rswETH/swETH deposits and triggers execution."""
        log.info("Deposit watcher started")
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=DEPOSIT_POLL_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                current_block = self._get_last_block()
                from_block = self._last_checked_block + 1
                if from_block > current_block:
                    continue  # no new block yet
                found = self._poll_deposits(from_block, current_block)
                self._last_checked_block = current_block
                if found:
                    self._try_execute("deposit")
            except Exception as e:
                log.error(f"[deposit-watcher] Error: {e}", exc_info=True)
        log.info("Deposit watcher stopped")

    # --- Main loop ---

    def run(self):
        """Main monitoring loop with event-driven deposit detection."""
        log.info("=" * 60)
        log.info("Swell Fee Flow Arbitrage Bot")
        log.info(f"  Auction:      {AUCTION_ADDRESS}")
        log.info(f"  Executor:     {EXECUTOR_ADDRESS}")
        log.info(f"  Min profit:   {MIN_PROFIT_ETH} ETH")
        log.info(f"  Gas safety:   {GAS_SAFETY_MULT}x")
        log.info(f"  Dry run:      {DRY_RUN}")
        log.info(f"  Deposit poll: {DEPOSIT_POLL_INTERVAL}s (~1 block)")
        log.info(f"  Heartbeat:    {HEARTBEAT_INTERVAL}s (fallback)")
        log.info("=" * 60)

        # Validate on-chain config at startup
        self.validate_setup()

        # Anchor the deposit watcher to the current chain tip so it doesn't
        # replay historic events from block 0.
        self._last_checked_block = self._get_last_block()
        log.info(f"Deposit watcher starting from block {self._last_checked_block}")

        watcher_thread = threading.Thread(
            target=self._run_deposit_watcher,
            name="deposit-watcher",
            daemon=True,
        )
        watcher_thread.start()

        # Initial check at startup before the first heartbeat fires.
        self._try_execute("startup")

        while True:
            try:
                self._stop_event.wait(timeout=HEARTBEAT_INTERVAL)
                if self._stop_event.is_set():
                    break
                self._try_execute("heartbeat")
            except KeyboardInterrupt:
                log.info("Shutting down...")
                self._stop_event.set()
                watcher_thread.join(timeout=5)
                break
            except Exception as e:
                log.error(f"Loop error: {e}", exc_info=True)


# ============================================================================
# One-off simulation mode
# ============================================================================

def simulate_from_example():
    """
    Simulate profitability using data from the example transaction.
    """
    log.info("Simulating from example transaction data...")

    swell_amount = int(1_237_301.587 * 1e18)
    rsweth_received = int(0.576941559636483608 * 1e18)
    sweth_received = int(0.237546853921567606 * 1e18)

    log.info(f"SWELL needed: {swell_amount / 1e18:,.2f}")
    log.info(f"Would receive: {rsweth_received / 1e18:.6f} rswETH + {sweth_received / 1e18:.6f} swETH")

    executor = EXECUTOR_ADDRESS if EXECUTOR_ADDRESS != "0x0000000000000000000000000000000000000000" else "0x8EB54fBb1CD02a982b0DE63a0182D56A45342E22"

    log.info("Getting Odos quote: WETH → SWELL...")
    q1 = odos_get_quote(WETH, int(0.7 * 1e18), SWELL, executor)
    if q1:
        log.info(f"  0.7 WETH → {q1.out_amount / 1e18:,.2f} SWELL")
        swell_per_weth = q1.out_amount / (0.7 * 1e18)
        weth_needed = swell_amount / (swell_per_weth * 1e18)
        log.info(f"  Estimated WETH needed: {weth_needed:.6f}")

    time.sleep(1.1)

    log.info("Getting Odos quote: swETH → WETH...")
    q2 = odos_get_quote(SWETH, sweth_received, WETH, executor)
    if q2:
        log.info(f"  {sweth_received / 1e18:.6f} swETH → {q2.out_amount / 1e18:.6f} WETH")

    time.sleep(1.1)

    log.info("Getting Odos quote: rswETH → WETH...")
    q3 = odos_get_quote(RSWETH, rsweth_received, WETH, executor)
    if q3:
        log.info(f"  {rsweth_received / 1e18:.6f} rswETH → {q3.out_amount / 1e18:.6f} WETH")

    if q1 and q2 and q3:
        total_out = (q2.out_amount + q3.out_amount) / 1e18
        total_in = weth_needed
        profit = total_out - total_in
        log.info("=" * 60)
        log.info(f"Estimated WETH in:  {total_in:.6f}")
        log.info(f"Estimated WETH out: {total_out:.6f}")
        log.info(f"Estimated profit:   {profit:.6f} ETH")
        log.info(f"Min threshold:      {MIN_PROFIT_ETH} ETH")
        log.info(f"Would execute:      {'YES' if profit >= MIN_PROFIT_ETH else 'NO'}")
        log.info("=" * 60)


# ============================================================================
# Entry point
# ============================================================================

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "simulate":
        simulate_from_example()
    else:
        bot = SwellArbBot()
        bot.run()
