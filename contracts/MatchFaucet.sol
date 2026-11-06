// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*
 * MatchFaucet: hands out free test MATCH so anyone can try the betting game.
 * Funded once by the treasury with a plain transfer. TESTNET ONLY.
 */

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

contract MatchFaucet is Ownable {
    using SafeERC20 for IERC20;

    IERC20 public immutable token;
    uint256 public dripAmount = 100e18;      // 100 MATCH per claim
    uint64 public constant COOLDOWN = 1 days;

    mapping(address => uint64) public lastClaim;

    event Dripped(address indexed to, uint256 amount);

    error CooldownActive(uint64 nextClaimAt);

    constructor(IERC20 token_, address owner_) Ownable(owner_) {
        token = token_;
    }

    function drip() external {
        uint64 next = lastClaim[msg.sender] + COOLDOWN;
        // lastClaim == 0 means first claim, always allowed
        if (lastClaim[msg.sender] != 0 && block.timestamp < next) revert CooldownActive(next);
        lastClaim[msg.sender] = uint64(block.timestamp);
        token.safeTransfer(msg.sender, dripAmount);
        emit Dripped(msg.sender, dripAmount);
    }

    function setDripAmount(uint256 amount) external onlyOwner {
        dripAmount = amount;
    }

    /// @notice Return unused tokens (e.g. to the treasury) when the demo ends.
    function withdraw(address to, uint256 amount) external onlyOwner {
        token.safeTransfer(to, amount);
    }
}
