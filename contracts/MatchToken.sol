// SPDX-License-Identifier: MIT
// Compatible with OpenZeppelin Contracts ^5.0.0
pragma solidity ^0.8.24;

/*
 * MATCH — utility + governance token for a community-verified sports match-data network.
 *
 * Economic loop (summary):
 *   1. Contributors submit match data (results, lineups, events) off-chain.
 *   2. Once other community members verify the record, the protocol's validator
 *      (MINTER_ROLE) calls rewardContributor(): new MATCH is minted to the contributor.
 *      Each match ID can be rewarded only once.
 *   3. Clubs, scouts and analysts buy data access with payForData(): part of the payment
 *      is BURNED (supply sink), the rest goes to the community treasury.
 *   4. Holders vote on parameters (burn share, wallet limit) via ERC20Votes-based governance.
 *
 * Base built with the OpenZeppelin Contracts Wizard (ERC20 + Burnable + Pausable +
 * Permit + Votes + AccessControl), extended manually with Capped supply, data-reward
 * minting, burn-on-payment and an anti-whale wallet limit.
 */

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {ERC20Capped} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Capped.sol";
import {ERC20Pausable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Pausable.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {ERC20Votes} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Votes.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Nonces} from "@openzeppelin/contracts/utils/Nonces.sol";

contract MatchToken is
    ERC20,
    ERC20Burnable,
    ERC20Capped,
    ERC20Pausable,
    ERC20Permit,
    ERC20Votes,
    AccessControl
{
    // ---------------------------------------------------------------- roles
    /// @notice Protocol validator that mints rewards for verified match data.
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");
    /// @notice Emergency multisig that can pause all transfers.
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");

    // ---------------------------------------------------------- supply numbers
    /// @notice Hard cap: 100,000,000 MATCH (18 decimals).
    uint256 public constant MAX_SUPPLY = 100_000_000 * 1e18;
    /// @notice Minted at deployment to the treasury (team, liquidity, partners, ecosystem).
    ///         The remaining 45% of the cap is the community reward pool, minted only
    ///         through rewardContributor().
    uint256 public constant INITIAL_SUPPLY = 55_000_000 * 1e18;

    // ------------------------------------------------- governable parameters
    /// @notice Share of every data payment that is burned, in basis points (1 bp = 0.01%).
    uint16 public burnShareBps = 3_000; // 30 %
    /// @notice Hard upper bound governance cannot exceed for the burn share.
    uint16 public constant MAX_BURN_SHARE_BPS = 8_000; // 80 %

    /// @notice Anti-whale: max balance of a non-exempt wallet, in bps of MAX_SUPPLY.
    uint16 public maxWalletBps = 200; // 2 % = 2,000,000 MATCH
    /// @notice Governance may loosen the limit, but never below 0.5 % or above 10 %.
    uint16 public constant MIN_WALLET_BPS = 50;
    uint16 public constant MAX_WALLET_BPS = 1_000;

    /// @notice Single per-match reward ceiling (guards against a compromised minter).
    uint256 public maxRewardPerMatch = 500 * 1e18;

    /// @notice Address receiving the non-burned part of data payments.
    address public treasury;

    /// @notice Addresses excluded from the wallet limit (treasury, DEX pool, vesting).
    mapping(address => bool) public isLimitExempt;
    /// @notice Whether a given match record has already been rewarded.
    mapping(bytes32 => bool) public matchRewarded;

    // ------------------------------------------------------------------ events
    event ContributorRewarded(address indexed contributor, bytes32 indexed matchId, uint256 amount);
    event DataPurchased(address indexed buyer, bytes32 indexed datasetId, uint256 paid, uint256 burned);
    event BurnShareUpdated(uint16 oldBps, uint16 newBps);
    event MaxWalletUpdated(uint16 oldBps, uint16 newBps);
    event MaxRewardPerMatchUpdated(uint256 oldAmount, uint256 newAmount);
    event TreasuryUpdated(address oldTreasury, address newTreasury);
    event LimitExemptionSet(address indexed account, bool exempt);

    // ------------------------------------------------------------------ errors
    error MatchAlreadyRewarded(bytes32 matchId);
    error RewardTooLarge(uint256 amount, uint256 maxAllowed);
    error WalletLimitExceeded(address account, uint256 balance, uint256 limit);
    error ParameterOutOfBounds();
    error ZeroAddress();

    /// @param admin    Governance / admin multisig (DEFAULT_ADMIN_ROLE + PAUSER_ROLE).
    /// @param minter   Validator service that rewards verified data (MINTER_ROLE).
    /// @param treasury_ Community treasury that receives the initial supply.
    constructor(address admin, address minter, address treasury_)
        ERC20("Match Data Token", "MATCH")
        ERC20Capped(MAX_SUPPLY)
        ERC20Permit("Match Data Token")
    {
        if (admin == address(0) || minter == address(0) || treasury_ == address(0)) revert ZeroAddress();

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PAUSER_ROLE, admin);
        _grantRole(MINTER_ROLE, minter);

        treasury = treasury_;
        isLimitExempt[treasury_] = true;
        emit LimitExemptionSet(treasury_, true);

        _mint(treasury_, INITIAL_SUPPLY);
    }

    // ============================================================ core utility

    /// @notice Mint a reward to a contributor whose match record was verified.
    /// @dev Each matchId is rewarded at most once; amount is bounded by maxRewardPerMatch
    ///      and, through ERC20Capped, by the remaining room under MAX_SUPPLY.
    function rewardContributor(address contributor, bytes32 matchId, uint256 amount)
        external
        onlyRole(MINTER_ROLE)
    {
        if (matchRewarded[matchId]) revert MatchAlreadyRewarded(matchId);
        if (amount > maxRewardPerMatch) revert RewardTooLarge(amount, maxRewardPerMatch);
        matchRewarded[matchId] = true;
        _mint(contributor, amount);
        emit ContributorRewarded(contributor, matchId, amount);
    }

    /// @notice Pay for access to a dataset. burnShareBps of `amount` is burned,
    ///         the rest is transferred to the treasury.
    function payForData(bytes32 datasetId, uint256 amount) external {
        uint256 burned = (amount * burnShareBps) / 10_000;
        _burn(msg.sender, burned);
        _transfer(msg.sender, treasury, amount - burned);
        emit DataPurchased(msg.sender, datasetId, amount, burned);
    }

    // ===================================================== governance setters

    function setBurnShare(uint16 newBps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newBps > MAX_BURN_SHARE_BPS) revert ParameterOutOfBounds();
        emit BurnShareUpdated(burnShareBps, newBps);
        burnShareBps = newBps;
    }

    function setMaxWallet(uint16 newBps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newBps < MIN_WALLET_BPS || newBps > MAX_WALLET_BPS) revert ParameterOutOfBounds();
        emit MaxWalletUpdated(maxWalletBps, newBps);
        maxWalletBps = newBps;
    }

    function setMaxRewardPerMatch(uint256 newAmount) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newAmount == 0 || newAmount > 5_000 * 1e18) revert ParameterOutOfBounds();
        emit MaxRewardPerMatchUpdated(maxRewardPerMatch, newAmount);
        maxRewardPerMatch = newAmount;
    }

    function setTreasury(address newTreasury) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newTreasury == address(0)) revert ZeroAddress();
        emit TreasuryUpdated(treasury, newTreasury);
        treasury = newTreasury;
        isLimitExempt[newTreasury] = true;
        emit LimitExemptionSet(newTreasury, true);
    }

    function setLimitExempt(address account, bool exempt) external onlyRole(DEFAULT_ADMIN_ROLE) {
        isLimitExempt[account] = exempt;
        emit LimitExemptionSet(account, exempt);
    }

    // ========================================================= emergency stop

    function pause() public onlyRole(PAUSER_ROLE) {
        _pause();
    }

    function unpause() public onlyRole(PAUSER_ROLE) {
        _unpause();
    }

    // ================================================================== views

    /// @notice Current wallet limit in token units.
    function maxWalletAmount() public view returns (uint256) {
        return (MAX_SUPPLY * maxWalletBps) / 10_000;
    }

    /// @notice Tokens that can still be minted as community rewards.
    function remainingRewardRoom() external view returns (uint256) {
        return MAX_SUPPLY - totalSupply();
    }

    // ============================================ required Solidity overrides

    /// @dev Single hook for every mint / burn / transfer:
    ///      pause check (Pausable) -> cap check (Capped) -> vote checkpoints (Votes),
    ///      then the anti-whale limit on the receiving wallet.
    function _update(address from, address to, uint256 value)
        internal
        override(ERC20, ERC20Capped, ERC20Pausable, ERC20Votes)
    {
        super._update(from, to, value);

        if (to != address(0) && !isLimitExempt[to]) {
            uint256 limit = maxWalletAmount();
            uint256 bal = balanceOf(to);
            if (bal > limit) revert WalletLimitExceeded(to, bal, limit);
        }
    }

    function nonces(address owner) public view override(ERC20Permit, Nonces) returns (uint256) {
        return super.nonces(owner);
    }
}
