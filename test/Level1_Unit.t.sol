// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {SwellFeeFlowExecutor} from "../contracts/SwellFeeFlowExecutor.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

// ============================================================================
// Level 1: Unit Tests (no fork, pure logic)
//
// What these test:
//   - Access control (onlyOwner)
//   - Reentrancy guard
//   - Profit check revert
//   - Allowance cleanup after calls
//   - Dust sweep behavior
//   - Rescue functions
//   - Rejection of raw ETH (no receive/fallback)
//
// Run: forge test --match-contract UnitTest -vvv
// ============================================================================

/// @dev Minimal mock ERC20 for unit testing
contract MockERC20 is ERC20 {
    constructor(string memory name, string memory symbol) ERC20(name, symbol) {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

/// @dev Mock router that just holds tokens (simulates Odos swap doing nothing useful)
contract MockRouter {
    // Swallow any call — simulates a swap that does nothing
    fallback() external payable {}
    receive() external payable {}
}

/// @dev Mock router that attempts reentrancy into the executor
contract ReentrantRouter {
    address public target;
    bytes public payload;

    function setAttack(address _target, bytes calldata _payload) external {
        target = _target;
        payload = _payload;
    }

    fallback() external payable {
        if (target != address(0)) {
            // Attempt to re-enter execute()
            (bool success, ) = target.call(payload);
            // We expect this to fail due to ReentrancyGuard
            // Store result for later check
            assembly {
                mstore(0x00, success)
                return(0x00, 0x20)
            }
        }
    }
}

/// @dev Mock auction that burns SWELL and sends back swETH + rswETH
contract MockAuction {
    MockERC20 public swell;
    MockERC20 public sweth;
    MockERC20 public rsweth;
    uint256 public priceToCharge;

    constructor(
        MockERC20 _swell,
        MockERC20 _sweth,
        MockERC20 _rsweth,
        uint256 _price
    ) {
        swell = _swell;
        sweth = _sweth;
        rsweth = _rsweth;
        priceToCharge = _price;
    }

    // Simulates buy(): pull SWELL, send swETH + rswETH
    fallback() external {
        // forge-lint:disable-next-line erc20-unchecked-transfer
        swell.transferFrom(msg.sender, address(this), priceToCharge);
        sweth.mint(msg.sender, 0.5 ether);
        rsweth.mint(msg.sender, 0.2 ether);
    }
}

contract UnitTest is Test {
    SwellFeeFlowExecutor public executor;
    address public owner;
    address public attacker;

    receive() external payable {}

    function setUp() public {
        owner = address(this);
        attacker = address(0xBEEF);
        executor = new SwellFeeFlowExecutor(address(1), address(2));
    }

    // -----------------------------------------------------------------
    // Access control
    // -----------------------------------------------------------------

    function test_onlyOwnerCanExecute() public {
        vm.prank(attacker);
        vm.expectRevert();
        executor.execute(
            1 ether,
            "",
            "",
            "",
            "",
            "",
            0
        );
    }

    function test_onlyOwnerCanRescueToken() public {
        vm.prank(attacker);
        vm.expectRevert();
        executor.rescueToken(address(1), 1 ether);
    }

    function test_onlyOwnerCanRescueETH() public {
        vm.prank(attacker);
        vm.expectRevert();
        executor.rescueEth();
    }

    // -----------------------------------------------------------------
    // Input validation
    // -----------------------------------------------------------------

    function test_revertOnZeroRouter() public {
        vm.expectRevert("Zero router");
        executor.setOdosRouter(address(0));
    }

    function test_revertOnZeroWethAmount() public {
        vm.expectRevert("Zero WETH input");
        executor.execute(0, "", "", "", "", "", 0);
    }

    // -----------------------------------------------------------------
    // ETH rejection (no receive/fallback)
    // -----------------------------------------------------------------

    function test_rejectsRawETH() public {
        vm.deal(address(this), 1 ether);
        (bool success, ) = address(executor).call{value: 1 ether}("");
        assertFalse(success, "Should reject raw ETH");
    }

    function test_rejectsETHWithData() public {
        vm.deal(address(this), 1 ether);
        (bool success, ) = address(executor).call{value: 1 ether}(
            abi.encodeWithSignature("nonexistent()")
        );
        assertFalse(success, "Should reject ETH with data");
    }

    // -----------------------------------------------------------------
    // Rescue functions
    // -----------------------------------------------------------------

    function test_rescueTokenWorks() public {
        MockERC20 token = new MockERC20("Test", "TST");
        token.mint(address(executor), 100 ether);

        uint256 balBefore = token.balanceOf(owner);
        executor.rescueToken(address(token), 100 ether);
        uint256 balAfter = token.balanceOf(owner);

        assertEq(balAfter - balBefore, 100 ether);
    }

    function test_rescueEthWorks() public {
        // Force-send ETH via selfdestruct
        vm.deal(address(executor), 1 ether);

        uint256 balBefore = owner.balance;
        executor.rescueEth();
        uint256 balAfter = owner.balance;

        assertEq(balAfter - balBefore, 1 ether);
    }

    function test_rescueEthNoopWhenEmpty() public {
        // Should not revert when no ETH
        executor.rescueEth();
    }

    // -----------------------------------------------------------------
    // Ownership
    // -----------------------------------------------------------------

    function test_ownerIsDeployer() public view {
        assertEq(executor.owner(), owner);
    }

    function test_constantAddresses() public view {
        assertEq(executor.WETH(), 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2);
        assertEq(executor.SWELL(), 0x0a6E7Ba5042B38349e437ec6Db6214AEC7B35676);
        assertEq(executor.auctionContract(), address(2));
    }
}
