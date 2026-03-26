// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/**
 * @title SwellFeeFlowExecutor
 * @notice Atomic arbitrage contract for Swell Fee Flow Dutch Auction.
 *
 * Security measures:
 *   - ReentrancyGuard on execute() and emergency functions
 *   - Whitelisted Odos router — no arbitrary external call targets
 *   - Auction selector validation — only buy() can be called
 *   - Exact allowances via forceApprove: set to exact amount before each
 *     external call, revoked to 0 immediately after — no stale allowances
 *   - No receive()/fallback — cannot accept raw ETH, tighter surface
 *   - onlyOwner on all mutating functions
 *   - On-chain profit floor — reverts if net WETH < input + minProfit
 *   - Revert reason bubbling on failed external calls
 *
 * Slippage strategy:
 *   The bot quotes Odos for slightly MORE WETH→SWELL than the auction
 *   requires (e.g. 3-5% buffer). Even if the Odos swap hits max slippage,
 *   the received SWELL still covers the auction price. After the auction
 *   consumes what it needs, any leftover SWELL is sold back to WETH in a
 *   4th swap leg. The final profit check accounts for all legs.
 *
 * Flow:
 *   1. Pull WETH from owner
 *   2. Swap WETH → SWELL via Odos (over-bought with slippage buffer)
 *   3. Approve exact SWELL to auction, call buy(), revoke
 *   4. Sell received swETH → WETH via Odos
 *   5. Sell received rswETH → WETH via Odos
 *   6. Sell any leftover SWELL → WETH via Odos
 *   7. Verify WETH profit >= minProfit, return everything to owner
 *   8. Sweep any dust tokens back to owner
 */
contract SwellFeeFlowExecutor is Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // --- Token addresses (Ethereum Mainnet) ---
    address public constant WETH   = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;
    address public constant SWELL  = 0x0a6E7Ba5042B38349e437ec6Db6214AEC7B35676;
    address public constant SWETH  = 0xf951E335afb289353dc249e82926178EaC7DEd78;
    address public constant RSWETH = 0xFAe103DC9cf190eD75350761e95403b7b8aFa6c0;

    // --- Whitelisted external contracts (owner-settable) ---
    address public odosRouter;
    address public auctionContract;

    // --- Expected auction buy() selector ---
    // buy(address[],address,uint256,uint256,uint256)
    bytes4 public constant AUCTION_BUY_SELECTOR = 0x3d7aa8f5;

    // --- Events ---
    event ArbitrageExecuted(
        uint256 wethIn,
        uint256 wethOut,
        uint256 profit,
        uint256 swethReceived,
        uint256 rswethReceived,
        uint256 swellLeftover,
        address indexed router
    );
    event OdosRouterUpdated(address indexed oldRouter, address indexed newRouter);
    event AuctionContractUpdated(address indexed oldAuction, address indexed newAuction);

    constructor(address _odosRouter, address _auctionContract) Ownable(msg.sender) {
        require(_odosRouter != address(0), "Zero router");
        require(_auctionContract != address(0), "Zero auction");
        odosRouter = _odosRouter;
        auctionContract = _auctionContract;
        emit OdosRouterUpdated(address(0), _odosRouter);
        emit AuctionContractUpdated(address(0), _auctionContract);
    }

    // =====================================================================
    // Admin: update whitelisted addresses
    // =====================================================================

    function setOdosRouter(address _router) external onlyOwner {
        require(_router != address(0), "Zero router");
        emit OdosRouterUpdated(odosRouter, _router);
        odosRouter = _router;
    }

    function setAuctionContract(address _auction) external onlyOwner {
        require(_auction != address(0), "Zero auction");
        emit AuctionContractUpdated(auctionContract, _auction);
        auctionContract = _auction;
    }

    // =====================================================================
    // Core execution
    // =====================================================================

    /// @dev Packs the five calldata blobs to avoid stack-too-deep in execute().
    struct SwapCalldata {
        bytes wethToSwell;    // Odos: WETH → SWELL
        bytes auctionBuy;     // Fee Flow auction buy()
        bytes swethToWeth;    // Odos: swETH → WETH
        bytes rswethToWeth;   // Odos: rswETH → WETH
        bytes swellToWeth;    // Odos: leftover SWELL → WETH (empty if none)
    }

    /**
     * @notice Execute the full atomic arbitrage.
     * @param wethAmount  Amount of WETH to pull from owner as input
     * @param swaps       Packed calldata for all five swap/auction steps
     * @param minProfit   Minimum net WETH profit — reverts if not met
     */
    function execute(
        uint256 wethAmount,
        SwapCalldata calldata swaps,
        uint256 minProfit
    ) external onlyOwner nonReentrant {
        require(wethAmount > 0, "Zero WETH input");

        // Validate auction calldata calls buy() and nothing else
        require(swaps.auctionBuy.length >= 4, "Auction calldata too short");
        require(
            bytes4(swaps.auctionBuy[:4]) == AUCTION_BUY_SELECTOR,
            "Invalid auction selector"
        );

        // Cache storage reads
        address _router = odosRouter;
        address _auction = auctionContract;

        // Snapshot starting WETH balance (should be 0, but handles edge cases)
        uint256 wethBefore = IERC20(WETH).balanceOf(address(this));

        // =================================================================
        // Step 1: Pull WETH from owner
        // =================================================================
        IERC20(WETH).safeTransferFrom(msg.sender, address(this), wethAmount);

        // =================================================================
        // Step 2: Swap WETH → SWELL via Odos (over-bought with buffer)
        // =================================================================
        _approveExact(WETH, _router, wethAmount);
        _doCall(_router, swaps.wethToSwell, "WETH->SWELL failed");
        _revoke(WETH, _router);

        uint256 swellBalance = IERC20(SWELL).balanceOf(address(this));
        require(swellBalance > 0, "No SWELL received");

        // =================================================================
        // Step 3: Approve SWELL to auction, call buy(), revoke
        // =================================================================
        _approveExact(SWELL, _auction, swellBalance);
        _doCall(_auction, swaps.auctionBuy, "Auction buy() failed");
        _revoke(SWELL, _auction);

        // =================================================================
        // Step 4: Swap swETH → WETH via Odos
        // =================================================================
        uint256 swethBalance = IERC20(SWETH).balanceOf(address(this));
        if (swethBalance > 0) {
            _approveExact(SWETH, _router, swethBalance);
            _doCall(_router, swaps.swethToWeth, "swETH->WETH failed");
            _revoke(SWETH, _router);
        }

        // =================================================================
        // Step 5: Swap rswETH → WETH via Odos
        // =================================================================
        uint256 rswethBalance = IERC20(RSWETH).balanceOf(address(this));
        if (rswethBalance > 0) {
            _approveExact(RSWETH, _router, rswethBalance);
            _doCall(_router, swaps.rswethToWeth, "rswETH->WETH failed");
            _revoke(RSWETH, _router);
        }

        // =================================================================
        // Step 6: Sell leftover SWELL → WETH (from the over-buy buffer)
        // =================================================================
        uint256 swellLeftover = IERC20(SWELL).balanceOf(address(this));
        if (swellLeftover > 0 && swaps.swellToWeth.length > 0) {
            _approveExact(SWELL, _router, swellLeftover);
            _doCall(_router, swaps.swellToWeth, "SWELL leftover swap failed");
            _revoke(SWELL, _router);
        }

        // =================================================================
        // Step 7: Profit check & return funds
        // =================================================================
        uint256 wethAfter = IERC20(WETH).balanceOf(address(this));
        require(wethAfter >= wethBefore + wethAmount + minProfit, "Insufficient profit");

        uint256 profit = wethAfter - wethBefore - wethAmount;

        // Return ALL WETH to owner
        IERC20(WETH).safeTransfer(owner(), wethAfter);

        // =================================================================
        // Step 8: Sweep any dust
        // =================================================================
        _sweepDust(SWELL);
        _sweepDust(SWETH);
        _sweepDust(RSWETH);

        emit ArbitrageExecuted(
            wethAmount,
            wethAfter - wethBefore,
            profit,
            swethBalance,
            rswethBalance,
            swellLeftover,
            _router
        );
    }

    // =====================================================================
    // Internal helpers
    // =====================================================================

    function _approveExact(address token, address spender, uint256 amount) internal {
        IERC20(token).forceApprove(spender, amount);
    }

    function _revoke(address token, address spender) internal {
        if (IERC20(token).allowance(address(this), spender) > 0) {
            IERC20(token).forceApprove(spender, 0);
        }
    }

    function _doCall(address target, bytes calldata data, string memory errMsg) internal {
        (bool ok, bytes memory ret) = target.call(data);
        if (!ok) {
            if (ret.length > 0) {
                /// @solidity memory-safe-assembly
                assembly { revert(add(ret, 0x20), mload(ret)) }
            }
            revert(errMsg);
        }
    }

    function _sweepDust(address token) internal {
        uint256 bal = IERC20(token).balanceOf(address(this));
        if (bal > 0) {
            IERC20(token).safeTransfer(owner(), bal);
        }
    }

    // =====================================================================
    // Emergency functions (also protected by nonReentrant)
    // =====================================================================

    function rescueToken(address token, uint256 amount) external onlyOwner nonReentrant {
        IERC20(token).safeTransfer(owner(), amount);
    }

    function rescueEth() external onlyOwner nonReentrant {
        uint256 bal = address(this).balance;
        if (bal > 0) {
            (bool ok, ) = owner().call{value: bal}("");
            require(ok, "ETH rescue failed");
        }
    }

    // No receive() or fallback() — rejects all raw ETH transfers
}
