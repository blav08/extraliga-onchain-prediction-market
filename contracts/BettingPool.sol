// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*
 * BettingPool: parimutuel (pool-based) prediction game on Czech Extraliga matches,
 * paid in MATCH. TESTNET ONLY, tokens have no monetary value.
 *
 * How it works:
 *   1. The oracle (off-chain Python script) creates a match before it starts.
 *   2. Users stake MATCH on HOME or AWAY (winner incl. overtime / shootout).
 *      Betting closes at the match start time.
 *   3. After the match, the oracle settles the winner. A fee (feeBps) of the
 *      total pool is BURNED (supply sink that ties into MATCH tokenomics).
 *   4. Winners claim: stake * distributablePool / winningPool.
 *   5. If the match is cancelled, or the oracle does not settle within
 *      SETTLEMENT_TIMEOUT, every bettor gets the full stake back (oracle liveness fallback).
 */

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @dev MatchToken inherits ERC20Burnable, so it exposes burn(amount).
interface IBurnable {
    function burn(uint256 amount) external;
}

contract BettingPool is AccessControl, ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant ORACLE_ROLE = keccak256("ORACLE_ROLE");

    enum Side { HOME, AWAY }
    enum Status { NONE, OPEN, SETTLED, CANCELLED }

    struct Match {
        uint64 startTime;        // betting closes here (unix seconds)
        Status status;
        Side winner;             // valid only when SETTLED
        uint256[2] pool;         // total stake per side
        uint256 distributable;   // total pool minus burned fee, set on settlement
    }

    IERC20 public immutable token;

    /// @notice Fee burned from every settled pool, in bps. Hard-capped at 10 %.
    uint16 public feeBps = 200; // 2 %
    uint16 public constant MAX_FEE_BPS = 1_000;

    /// @notice After start + timeout, anyone may cancel an unsettled match (refunds).
    uint64 public constant SETTLEMENT_TIMEOUT = 3 days;

    /// @notice Minimum stake to keep dust bets out.
    uint256 public constant MIN_BET = 1e18; // 1 MATCH

    // matchId = keccak256(abi.encodePacked("api-sports", fixtureId)), built off-chain
    mapping(bytes32 => Match) private _matches;
    // matchId => bettor => stake per side
    mapping(bytes32 => mapping(address => uint256[2])) private _stakes;
    mapping(bytes32 => mapping(address => bool)) public claimed;

    event MatchCreated(bytes32 indexed matchId, uint64 startTime);
    event BetPlaced(bytes32 indexed matchId, address indexed bettor, Side side, uint256 amount);
    event MatchSettled(bytes32 indexed matchId, Side winner, uint256 totalPool, uint256 burnedFee);
    event MatchCancelled(bytes32 indexed matchId, address indexed by);
    event Claimed(bytes32 indexed matchId, address indexed bettor, uint256 amount);
    event FeeUpdated(uint16 oldBps, uint16 newBps);

    error MatchExists();
    error MatchNotOpen();
    error BettingClosed();
    error TooEarly();
    error StartInPast();
    error BetTooSmall();
    error NothingToClaim();
    error AlreadyClaimed();
    error ParameterOutOfBounds();

    constructor(IERC20 token_, address admin, address oracle) {
        token = token_;
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(ORACLE_ROLE, oracle);
    }

    // ================================================================ oracle

    function createMatch(bytes32 matchId, uint64 startTime) external onlyRole(ORACLE_ROLE) {
        if (_matches[matchId].status != Status.NONE) revert MatchExists();
        if (startTime <= block.timestamp) revert StartInPast();
        Match storage m = _matches[matchId];
        m.startTime = startTime;
        m.status = Status.OPEN;
        emit MatchCreated(matchId, startTime);
    }

    /// @notice Record the result and burn the fee.
    /// @dev If nobody backed the winner, there is nobody to pay, so the match is
    ///      cancelled instead and everyone is refunded.
    function settle(bytes32 matchId, Side winner) external onlyRole(ORACLE_ROLE) {
        Match storage m = _matches[matchId];
        if (m.status != Status.OPEN) revert MatchNotOpen();
        if (block.timestamp < m.startTime) revert TooEarly();

        if (m.pool[uint8(winner)] == 0) {
            m.status = Status.CANCELLED;
            emit MatchCancelled(matchId, msg.sender);
            return;
        }

        uint256 total = m.pool[0] + m.pool[1];
        uint256 fee = (total * feeBps) / 10_000;

        // Effects before the external call (checks-effects-interactions)
        m.status = Status.SETTLED;
        m.winner = winner;
        m.distributable = total - fee;

        if (fee > 0) IBurnable(address(token)).burn(fee);
        emit MatchSettled(matchId, winner, total, fee);
    }

    /// @notice Oracle can cancel any open match (postponed game, bad data).
    ///         Anyone can cancel once the settlement timeout has passed.
    function cancel(bytes32 matchId) external {
        Match storage m = _matches[matchId];
        if (m.status != Status.OPEN) revert MatchNotOpen();
        bool isOracle = hasRole(ORACLE_ROLE, msg.sender);
        if (!isOracle && block.timestamp < m.startTime + SETTLEMENT_TIMEOUT) revert TooEarly();
        m.status = Status.CANCELLED;
        emit MatchCancelled(matchId, msg.sender);
    }

    // ================================================================= users

    function placeBet(bytes32 matchId, Side side, uint256 amount) public nonReentrant {
        Match storage m = _matches[matchId];
        if (m.status != Status.OPEN) revert MatchNotOpen();
        if (block.timestamp >= m.startTime) revert BettingClosed();
        if (amount < MIN_BET) revert BetTooSmall();

        m.pool[uint8(side)] += amount;
        _stakes[matchId][msg.sender][uint8(side)] += amount;

        // Requires a prior approve(), or use placeBetWithPermit below
        token.safeTransferFrom(msg.sender, address(this), amount);
        emit BetPlaced(matchId, msg.sender, side, amount);
    }

    /// @notice One-transaction bet: the user signs an EIP-2612 permit off-chain
    ///         (MatchToken supports ERC20Permit), so no separate approve tx is needed.
    function placeBetWithPermit(
        bytes32 matchId, Side side, uint256 amount,
        uint256 deadline, uint8 v, bytes32 r, bytes32 s
    ) external {
        // try/catch: if someone front-runs the permit, the allowance already exists,
        // so we just continue instead of reverting (recommended OZ pattern)
        try IERC20Permit(address(token)).permit(msg.sender, address(this), amount, deadline, v, r, s) {} catch {}
        placeBet(matchId, side, amount);
    }

    /// @notice Collect winnings (settled match) or refund (cancelled match).
    function claim(bytes32 matchId) external nonReentrant {
        if (claimed[matchId][msg.sender]) revert AlreadyClaimed();
        uint256 payout = claimable(matchId, msg.sender);
        if (payout == 0) revert NothingToClaim();

        claimed[matchId][msg.sender] = true;
        token.safeTransfer(msg.sender, payout);
        emit Claimed(matchId, msg.sender, payout);
    }

    // ================================================================= views

    function claimable(bytes32 matchId, address bettor) public view returns (uint256) {
        if (claimed[matchId][bettor]) return 0;
        Match storage m = _matches[matchId];
        uint256[2] storage st = _stakes[matchId][bettor];

        if (m.status == Status.CANCELLED) return st[0] + st[1];
        if (m.status == Status.SETTLED) {
            uint8 w = uint8(m.winner);
            // Multiply first, divide last to keep precision; rounding dust stays in the contract
            return (st[w] * m.distributable) / m.pool[w];
        }
        return 0;
    }

    function getMatch(bytes32 matchId)
        external view
        returns (uint64 startTime, Status status, Side winner, uint256 homePool, uint256 awayPool)
    {
        Match storage m = _matches[matchId];
        return (m.startTime, m.status, m.winner, m.pool[0], m.pool[1]);
    }

    function stakesOf(bytes32 matchId, address bettor) external view returns (uint256 home, uint256 away) {
        uint256[2] storage st = _stakes[matchId][bettor];
        return (st[0], st[1]);
    }

    // ================================================================= admin

    function setFee(uint16 newBps) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newBps > MAX_FEE_BPS) revert ParameterOutOfBounds();
        emit FeeUpdated(feeBps, newBps);
        feeBps = newBps;
    }
}
