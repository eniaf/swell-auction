// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console} from "forge-std/Test.sol";
import {SwellFeeFlowExecutor} from "../contracts/SwellFeeFlowExecutor.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

// ============================================================================
// Level 0: Gas Estimation
//
// Runs the full execute() happy path with lightweight mock contracts to
// isolate the executor's own gas overhead. Mock swaps cost ~30k each vs
// ~200-400k for real Odos routes. See the comment block at the bottom for
// how to translate these numbers into a mainnet estimate.
//
// Run:
//   forge test --match-contract GasTest --gas-report -vv
// ============================================================================

contract GasMockERC20 is ERC20 {
    constructor(string memory name, string memory symbol) ERC20(name, symbol) {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

/// @dev Simulates Odos router: pulls tokenIn from caller, sends tokenOut back.
///      Pre-funded with output tokens before each call.
contract SwapRouter {
    function swap(
        address tokenIn,
        uint256 amountIn,
        address tokenOut,
        uint256 amountOut
    ) external {
        // forge-lint:disable-next-line erc20-unchecked-transfer
        IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn);
        // forge-lint:disable-next-line erc20-unchecked-transfer
        IERC20(tokenOut).transfer(msg.sender, amountOut);
    }
}

/// @dev Simulates the Fee Flow auction: pulls SWELL at priceToCharge,
///      mints swETH and rswETH back to caller.
///      Token addresses are stored so they can be set after etch.
contract FeeFlowAuction {
    address public swell;
    address public sweth;
    address public rsweth;
    uint256 public price;
    address public swellTarget; // where to send minted swETH+rswETH — uses GasMockERC20.mint

    function init(address _swell, address _sweth, address _rsweth, uint256 _price) external {
        swell  = _swell;
        sweth  = _sweth;
        rsweth = _rsweth;
        price  = _price;
    }

    // Matches buy(address[],address,uint256,uint256,uint256) selector = 0x3d7aa8f5
    fallback() external {
        // forge-lint:disable-next-line erc20-unchecked-transfer
        IERC20(swell).transferFrom(msg.sender, address(this), price);
        GasMockERC20(sweth).mint(msg.sender, 0.5 ether);
        GasMockERC20(rsweth).mint(msg.sender, 0.2 ether);
    }
}

contract GasTest is Test {
    SwellFeeFlowExecutor executor;
    SwapRouter   router;
    FeeFlowAuction auction;

    // Local handles pointing at the EXECUTOR'S hardcoded addresses (after etch)
    GasMockERC20 weth;
    GasMockERC20 swell;
    GasMockERC20 sweth;
    GasMockERC20 rsweth;

    address owner;

    // Scenario parameters (roughly matching the example tx)
    uint256 constant SWELL_PRICE        = 1_237_302 ether;
    uint256 constant SWELL_BUFFER       = 12_373 ether;   // ~1% over-buy
    uint256 constant SWELL_BOUGHT       = SWELL_PRICE + SWELL_BUFFER;
    uint256 constant WETH_IN            = 1.0 ether;
    // Mock returns: 0.51 + 0.35 + 0.16 = 1.02 ETH — just profitable
    uint256 constant WETH_FROM_SWETH    = 0.51 ether;
    uint256 constant WETH_FROM_RSWETH   = 0.35 ether;
    uint256 constant WETH_FROM_SWELL    = 0.16 ether;

    function setUp() public {
        owner = address(this);

        // ---- Step 1: deploy infrastructure (router + auction) ----
        router  = new SwapRouter();
        auction = new FeeFlowAuction();

        // ---- Step 2: deploy executor (uses hardcoded token addresses) ----
        executor = new SwellFeeFlowExecutor(address(router), address(auction));

        // ---- Step 3: etch mock ERC20 bytecode at the executor's hardcoded addresses ----
        // We deploy one reference mock to get its bytecode, then etch to each address.
        GasMockERC20 _ref = new GasMockERC20("", "");
        bytes memory mockCode = address(_ref).code;

        vm.etch(executor.WETH(),   mockCode);
        vm.etch(executor.SWELL(),  mockCode);
        vm.etch(executor.SWETH(),  mockCode);
        vm.etch(executor.RSWETH(), mockCode);

        // ---- Step 4: point local handles at the now-etched addresses ----
        weth   = GasMockERC20(executor.WETH());
        swell  = GasMockERC20(executor.SWELL());
        sweth  = GasMockERC20(executor.SWETH());
        rsweth = GasMockERC20(executor.RSWETH());

        // ---- Step 5: initialise auction with the CORRECT (etched) token addresses ----
        auction.init(address(swell), address(sweth), address(rsweth), SWELL_PRICE);

        // ---- Step 6: fund owner and approve executor ----
        weth.mint(owner, 10 ether);
        IERC20(address(weth)).approve(address(executor), type(uint256).max);

        // ---- Step 7: pre-fund router for each swap leg ----
        //   leg 1 (WETH→SWELL):  router gives out SWELL
        //   leg 2 (swETH→WETH):  router gives out WETH
        //   leg 3 (rswETH→WETH): router gives out WETH
        //   leg 4 (SWELL→WETH):  router gives out WETH
        swell.mint(address(router), SWELL_BOUGHT);
        weth.mint(address(router),  WETH_FROM_SWETH + WETH_FROM_RSWETH + WETH_FROM_SWELL);
    }

    // -------------------------------------------------------------------------

    function _swapCalldata(
        address tokenIn, uint256 amountIn,
        address tokenOut, uint256 amountOut
    ) internal pure returns (bytes memory) {
        return abi.encodeWithSignature(
            "swap(address,uint256,address,uint256)",
            tokenIn, amountIn, tokenOut, amountOut
        );
    }

    // -------------------------------------------------------------------------
    // Test 1: full execute() — 4 active swap legs (worst-case gas)
    // -------------------------------------------------------------------------

    /// @notice Full happy-path: WETH→SWELL → auction → swETH→WETH → rswETH→WETH → SWELL→WETH
    function test_gas_fullExecute() public {
        bytes memory leg1 = _swapCalldata(address(weth),   WETH_IN,      address(swell),  SWELL_BOUGHT);
        bytes memory leg2 = _swapCalldata(address(sweth),  0.5 ether,    address(weth),   WETH_FROM_SWETH);
        bytes memory leg3 = _swapCalldata(address(rsweth), 0.2 ether,    address(weth),   WETH_FROM_RSWETH);
        bytes memory leg4 = _swapCalldata(address(swell),  SWELL_BUFFER, address(weth),   WETH_FROM_SWELL);

        bytes memory auctionData = abi.encodeWithSelector(
            executor.AUCTION_BUY_SELECTOR(),
            new address[](0), address(0), uint256(0), uint256(0), uint256(0)
        );

        uint256 gasBefore = gasleft();
        executor.execute(WETH_IN, leg1, auctionData, leg2, leg3, leg4, 0);
        uint256 gasUsed = gasBefore - gasleft();

        console.log("--- Gas report: full execute() (4 swap legs) ---");
        console.log("Mock overhead (this test):        ", gasUsed);
        console.log("Expected real Odos cost (4 legs): ~1,000,000");
        console.log("Estimated mainnet total:          ~", gasUsed + 1_000_000);
    }

    // -------------------------------------------------------------------------
    // Test 2: execute() without leftover SWELL leg (no over-buy)
    // -------------------------------------------------------------------------

    /// @notice 3-leg variant: no leftover SWELL to sell back.
    function test_gas_executeNoLeftover() public {
        // 2 WETH-output legs must together exceed WETH_IN for the profit check to pass
        uint256 wethFromSweth3  = 0.55 ether;
        uint256 wethFromRsweth3 = 0.50 ether;  // total 1.05 > WETH_IN 1.0

        weth.mint(address(router), wethFromSweth3 + wethFromRsweth3);

        bytes memory leg1 = _swapCalldata(address(weth),   WETH_IN,   address(swell),  SWELL_PRICE);
        bytes memory leg2 = _swapCalldata(address(sweth),  0.5 ether, address(weth),   wethFromSweth3);
        bytes memory leg3 = _swapCalldata(address(rsweth), 0.2 ether, address(weth),   wethFromRsweth3);

        bytes memory auctionData = abi.encodeWithSelector(
            executor.AUCTION_BUY_SELECTOR(),
            new address[](0), address(0), uint256(0), uint256(0), uint256(0)
        );

        uint256 gasBefore = gasleft();
        executor.execute(WETH_IN, leg1, auctionData, leg2, leg3, "", 0);
        uint256 gasUsed = gasBefore - gasleft();

        console.log("--- Gas report: execute() (3 swap legs, no leftover SWELL) ---");
        console.log("Mock overhead (this test):        ", gasUsed);
        console.log("Expected real Odos cost (3 legs): ~750,000");
        console.log("Estimated mainnet total:          ~", gasUsed + 750_000);
    }
}

// ============================================================================
// Real-world gas breakdown
//
// Mock swaps (simple ERC20 transferFrom + transfer) cost ~30k gas each.
// Real Odos v2 routes are 200–400k per leg, varying by:
//   - Pool types in the route (UniV3 ~80k/hop, Curve ~100k/hop, etc.)
//   - Number of hops
//
// Approximate mainnet breakdown for a 4-leg execute():
//
//   Executor logic (overhead, checks, approvals, sweeps)    ~100k
//   Leg 1: WETH → SWELL  (Odos — likely UniV3 + Balancer)  ~300k
//   Leg 2: swETH → WETH  (Odos — typically 1-2 hops)       ~200k
//   Leg 3: rswETH → WETH (Odos — typically 1-2 hops)       ~200k
//   Leg 4: leftover SWELL → WETH (Odos)                     ~200k
//   ─────────────────────────────────────────────────────────────
//   Total estimate                                    ~950k – 1.2M gas
//
// Gas cost at different base fees (no priority fee):
//
//   Base fee │  950k gas    │  1.2M gas
//   ─────────┼──────────────┼─────────────
//    10 gwei │ 0.0095 ETH   │ 0.012 ETH
//    20 gwei │ 0.019  ETH   │ 0.024 ETH
//    50 gwei │ 0.0475 ETH   │ 0.060 ETH
//   100 gwei │ 0.095  ETH   │ 0.120 ETH
//
// The bot sets MIN_PROFIT_ETH=0.06 and adds estimated gas * GAS_SAFETY_MULT (1.5x)
// to the on-chain minProfit, so the contract atomically reverts if gas isn't covered.
// ============================================================================
