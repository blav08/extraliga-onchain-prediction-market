"""
MATCH tokenomics: evidence and simulation for the white paper.

Every number in the tokenomics chapters of the report is produced by this script.
It does five things:

  1. EVIDENCE   reads the live contract parameters from Sepolia (cap, supply, burn
                share, wallet limit, betting fee) so the report quotes the real values
  2. ALLOCATION splits the 100 M cap into its buckets (table + pie chart)
  3. SCENARIOS  simulates supply month by month for 5 years under bear / base / bull
                adoption: rewards are minted, data payments and betting fees are burned
  4. ANALYSIS   break-even burn, sensitivity heat map, Monte Carlo uncertainty,
                governance concentration, parimutuel payout examples
  5. OUTPUT     figures (out/figures/*.png), tables to paste into the report
                (out/report_tables.md) and all numbers as JSON (out/summary.json)

Run:
    python tokenomics/tokenomics.py            # reads live parameters from Sepolia
    python tokenomics/tokenomics.py --offline  # uses the constants from the contract source

Units: everything is in whole MATCH tokens (not wei). The model deliberately has no
price: MATCH lives on a testnet and is never sold, so we model token QUANTITIES
(supply, inflation, burns), which is what the contract actually controls.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # render to files, no window needed
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
FIG = OUT / "figures"

# --------------------------------------------------------------------------
# 1. Contract parameters
#    Defaults are copied from MatchToken.sol / BettingPool.sol. With network
#    access they are overwritten by the values read from the deployed contracts,
#    so the report always quotes what is really on chain.
# --------------------------------------------------------------------------
RPC = "https://ethereum-sepolia-rpc.publicnode.com"
TOKEN = "0xb459280925651b0a4ee6b1f71C88C87cc1787512"
POOL = "0x684C429c009088b2Ed69410d8e974aec5b4fa7Af"


@dataclass
class Chain:
    cap: float = 100_000_000           # MAX_SUPPLY
    initial_supply: float = 55_000_000  # INITIAL_SUPPLY, minted to the treasury at deploy
    total_supply: float = 55_000_000    # current outstanding supply
    burn_share: float = 0.30            # burnShareBps / 10_000: share of a data payment burned
    max_burn_share: float = 0.80        # MAX_BURN_SHARE_BPS: governance cannot go above
    max_wallet_share: float = 0.02      # maxWalletBps / 10_000: anti-whale limit (of cap)
    max_reward_per_match: float = 500   # maxRewardPerMatch: per-record mint ceiling
    bet_fee: float = 0.02               # BettingPool.feeBps / 10_000: burned at settlement
    source: str = "contract source code"


def read_chain(offline: bool) -> Chain:
    c = Chain()
    if offline:
        return c
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
        abi = [{"name": n, "type": "function", "stateMutability": "view", "inputs": [],
                "outputs": [{"type": "uint256"}]} for n in
               ("cap", "totalSupply", "burnShareBps", "maxWalletBps", "maxRewardPerMatch", "feeBps")]
        tok = w3.eth.contract(address=TOKEN, abi=abi)
        pool = w3.eth.contract(address=POOL, abi=abi)
        e = 1e18
        c = Chain(
            cap=tok.functions.cap().call() / e,
            total_supply=tok.functions.totalSupply().call() / e,
            burn_share=tok.functions.burnShareBps().call() / 10_000,
            max_wallet_share=tok.functions.maxWalletBps().call() / 10_000,
            max_reward_per_match=tok.functions.maxRewardPerMatch().call() / e,
            bet_fee=pool.functions.feeBps().call() / 10_000,
            source=f"Sepolia, read {date.today().isoformat()}",
        )
    except Exception as err:  # no network: fall back to the source-code constants
        print(f"! could not read Sepolia ({type(err).__name__}), using contract constants")
    return c


# --------------------------------------------------------------------------
# 2. Allocation of the 100 M cap
#    The contract fixes only two numbers: 55 M minted to the treasury at deploy,
#    45 M of headroom that can only be minted as data rewards. How the treasury
#    part is used is a policy decision of this white paper (see report ch. 4.2).
#    There is no "public sale" or "liquidity" bucket on purpose: MATCH is earned
#    or granted, never sold (relevant for the regulatory chapter).
# --------------------------------------------------------------------------
ALLOCATION = {
    # bucket: (million MATCH, how it is released)
    "Community data rewards": (45, "minted only via rewardContributor(), per verified match"),
    "Ecosystem & grants": (20, "treasury, granted to apps/integrations (e.g. this betting game, faucet)"),
    "Team": (15, "treasury, 12-month cliff + 36-month linear vesting (planned vesting contract)"),
    "Partners (clubs, data providers)": (10, "treasury, onboarding grants for data buyers and sources"),
    "DAO reserve": (10, "treasury, spent only by governance vote"),
}


# --------------------------------------------------------------------------
# 3. Adoption scenarios
#    These are ASSUMPTIONS, not data. They are stated explicitly so a reader can
#    disagree with a number and rerun the model. Each one is commented with why
#    it is set where it is.
# --------------------------------------------------------------------------
# Czech Extraliga 2025/26 format: 14 teams x 52 games / 2 = 364 regular-season games,
# played September to March (7 months), playoffs in April. Source: Wikipedia,
# "2025–26 Czech Extraliga season" (accessed 2026-11-07).
REGULAR_GAMES = 364
GAMES_PER_MONTH = {9: 52, 10: 52, 11: 52, 12: 52, 1: 52, 2: 52, 3: 52, 4: 40}  # 7 x 52 = 364; ~40 playoff games in April
START = date(2027, 1, 1)   # first simulated month (launch after the testnet phase)
MONTHS = 60                # 5-year horizon


@dataclass(frozen=True)
class Scenario:
    name: str
    leagues_start: int          # leagues whose matches the network covers at launch
    leagues_added_per_year: int # new leagues onboarded each year (other European leagues)
    leagues_max: int
    reward_per_match: float     # MATCH minted per verified match record (contract max 500)
    subscribers_start: float    # paying data buyers (clubs, scouts, media, analysts)
    subscriber_growth: float    # monthly growth rate of data buyers
    spend_per_subscriber: float # MATCH spent on data per buyer per month (payForData)
    bettors_start: float        # active users of the prediction game
    bettor_growth: float        # monthly growth rate of bettors
    stake_per_bettor: float     # MATCH staked per bettor per month (in season only)


SCENARIOS = [
    # Pessimistic: stays a Czech-only niche; few paying buyers
    Scenario("bear", 1, 0, 1, 200, 5, 0.01, 4_000, 100, 0.01, 300),
    # Base: one new league per year up to 5 (e.g. Slovakia, Finland, Sweden, Switzerland)
    Scenario("base", 1, 1, 5, 200, 10, 0.04, 5_000, 300, 0.03, 400),
    # Optimistic: fast expansion and strong data demand
    Scenario("bull", 1, 2, 9, 200, 15, 0.07, 6_000, 500, 0.06, 500),
]


def simulate(s: Scenario, chain: Chain, burn_share: float | None = None) -> dict:
    """
    Month-by-month supply model. Order of events inside one month:
      a) data buyers pay with payForData(): burn_share of the payment is burned,
         the rest goes to the treasury (moves tokens, does not change supply)
      b) settled betting pools burn bet_fee of their volume
      c) verified match records mint rewards, but only into the free room under
         the cap. ERC20Capped checks OUTSTANDING supply, so tokens burned in
         a) and b) free room for new rewards. Rewards that do not fit are
         recorded as 'unpaid' (a warning sign: contributors would go unpaid).
    """
    burn_share = chain.burn_share if burn_share is None else burn_share
    supply = chain.total_supply
    rows = []
    for m in range(MONTHS):
        y, mo = START.year + (START.month - 1 + m) // 12, (START.month - 1 + m) % 12 + 1
        year_idx = m // 12
        leagues = min(s.leagues_max, s.leagues_start + s.leagues_added_per_year * year_idx)
        games = leagues * GAMES_PER_MONTH.get(mo, 0)          # off-season (May-Aug) = 0 games
        in_season = games > 0

        subscribers = s.subscribers_start * (1 + s.subscriber_growth) ** m
        data_spend = subscribers * s.spend_per_subscriber      # subscriptions run all year
        burned_data = burn_share * data_spend
        to_treasury = data_spend - burned_data

        bettors = s.bettors_start * (1 + s.bettor_growth) ** m
        bet_volume = bettors * s.stake_per_bettor if in_season else 0.0
        burned_bets = chain.bet_fee * bet_volume

        supply -= burned_data + burned_bets
        wanted = games * min(s.reward_per_match, chain.max_reward_per_match)
        minted = min(wanted, max(chain.cap - supply, 0.0))
        supply += minted

        rows.append(dict(month=f"{y}-{mo:02d}", games=games, leagues=leagues, minted=minted,
                         unpaid=wanted - minted, burned_data=burned_data, burned_bets=burned_bets,
                         to_treasury=to_treasury, data_spend=data_spend, bet_volume=bet_volume,
                         supply=supply))
    return {"scenario": s.name, "rows": rows}


def yearly(result: dict, start_supply: float) -> list[dict]:
    """Aggregate months into years: minted, burned, net change and inflation %."""
    out, prev = [], start_supply
    rows = result["rows"]
    for y in range(MONTHS // 12):
        chunk = rows[12 * y: 12 * (y + 1)]
        minted = sum(r["minted"] for r in chunk)
        burned = sum(r["burned_data"] + r["burned_bets"] for r in chunk)
        end = chunk[-1]["supply"]
        out.append(dict(year=y + 1, minted=minted, burned=burned, burned_bets=sum(r["burned_bets"] for r in chunk),
                        unpaid=sum(r["unpaid"] for r in chunk), end_supply=end,
                        inflation_pct=100 * (end / prev - 1)))
        prev = end
    return out


# --------------------------------------------------------------------------
# 4a. Break-even: how much data demand neutralises reward emissions?
#     In a season month one league mints  games x reward.  Supply is flat when
#        burn_share x data_spend + bet_fee x bet_volume = minted
#     =>  data_spend* = (minted - bet_fee x bet_volume) / burn_share
# --------------------------------------------------------------------------
def break_even(chain: Chain, s: Scenario) -> list[dict]:
    minted = GAMES_PER_MONTH[10] * s.reward_per_match                 # one league, in-season month
    bet_burn = chain.bet_fee * s.bettors_start * s.stake_per_bettor
    rows = []
    for bs in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80):
        spend = max(minted - bet_burn, 0) / bs
        rows.append(dict(burn_share_pct=round(bs * 100), data_spend=spend,
                         buyers_needed=spend / s.spend_per_subscriber,
                         current=abs(bs - chain.burn_share) < 1e-9))
    return rows


# --------------------------------------------------------------------------
# 4b. Sensitivity: 5-year supply change for every (burn share, reward) pair
# --------------------------------------------------------------------------
def sensitivity(chain: Chain, s: Scenario):
    shares = np.round(np.arange(0.10, 0.801, 0.05), 2)
    rewards = np.arange(50, 501, 50)
    grid = np.zeros((len(rewards), len(shares)))
    for i, r in enumerate(rewards):
        for j, bs in enumerate(shares):
            res = simulate(replace(s, reward_per_match=float(r)), chain, burn_share=float(bs))
            grid[i, j] = 100 * (res["rows"][-1]["supply"] / chain.total_supply - 1)
    return shares, rewards, grid


# --------------------------------------------------------------------------
# 4c. Monte Carlo: the base scenario with uncertain growth
#     Growth rates are drawn per run from normal distributions centred on the
#     base assumptions; the number of leagues added per year is Poisson.
#     Fixed seed = reproducible numbers in the report.
# --------------------------------------------------------------------------
def monte_carlo(chain: Chain, s: Scenario, runs: int = 2_000, seed: int = 2026) -> dict:
    rng = np.random.default_rng(seed)
    finals, paths, cap_hit, unpaid_any = [], [], 0, 0
    for _ in range(runs):
        draw = replace(
            s,
            subscriber_growth=float(np.clip(rng.normal(s.subscriber_growth, 0.03), -0.05, 0.15)),
            bettor_growth=float(np.clip(rng.normal(s.bettor_growth, 0.03), -0.05, 0.15)),
            spend_per_subscriber=float(max(rng.normal(s.spend_per_subscriber, 1_500), 500)),
            leagues_added_per_year=int(rng.poisson(s.leagues_added_per_year)),
            leagues_max=9,
        )
        res = simulate(draw, chain)
        supply = np.array([r["supply"] for r in res["rows"]])
        paths.append(supply)
        finals.append(supply[-1])
        cap_hit += bool(np.any(supply >= chain.cap - 1e-6))
        unpaid_any += any(r["unpaid"] > 0 for r in res["rows"])
    finals = np.array(finals)
    paths = np.array(paths)
    start = chain.total_supply
    return dict(
        runs=runs, seed=seed,
        p5=float(np.percentile(finals, 5)), p50=float(np.percentile(finals, 50)), p95=float(np.percentile(finals, 95)),
        prob_supply_up=float(np.mean(finals > start)),
        prob_deflation=float(np.mean(finals < start)),
        prob_cap_hit=cap_hit / runs, prob_unpaid_rewards=unpaid_any / runs,
        band=[np.percentile(paths, q, axis=0).tolist() for q in (5, 50, 95)],
    )


# --------------------------------------------------------------------------
# 4d. Governance concentration (ERC20Votes: 1 token = 1 vote, after delegation)
# --------------------------------------------------------------------------
def governance(chain: Chain, minted_rewards: float) -> dict:
    """
    ERC20Votes counts ONLY delegated tokens: an address that never calls delegate()
    has zero voting power, however much it holds. So the key design question is
    whether the treasury and the team ever delegate.

    Holder groups after 5 years (base scenario), from the allocation:
      treasury-controlled: DAO reserve (10 M)          - exempt from the wallet limit
      team:                15 M, fully vested by year 4
      community:           granted ecosystem + partner tokens (30 M) + minted rewards
    Two cases:
      A) treasury and team delegate        -> their share of all votes
      B) they never delegate (our policy)  -> only the community votes; how many
         wallets at the 2 % limit does a majority of community tokens need?
    The wallet limit stops one wallet, not one PERSON with many wallets (Sybil):
    an honest limitation for the risk chapter.
    """
    cap_per_wallet = chain.max_wallet_share * chain.cap
    reserve = ALLOCATION["DAO reserve"][0] * 1e6
    team = ALLOCATION["Team"][0] * 1e6
    grants = (ALLOCATION["Ecosystem & grants"][0] + ALLOCATION["Partners (clubs, data providers)"][0]) * 1e6
    community_cases = {"rewards only (grants not yet distributed)": minted_rewards,
                       "rewards + all grants distributed": minted_rewards + grants}
    out = dict(wallet_limit=cap_per_wallet, reserve=reserve, team=team, cases={})
    for label, community in community_cases.items():
        out["cases"][label] = dict(
            community=community,
            # A) everyone delegates (worst case for decentralisation)
            insiders_vote_share_pct=100 * (reserve + team) / (reserve + team + community),
            # B) insiders never delegate: smallest group of wallets, each at the
            #    limit, that holds more than half of all community votes
            min_wallets_for_community_majority=math.floor(0.5 * community / cap_per_wallet) + 1,
        )
    # Team tokens cannot sit in a few personal wallets: each one is capped at the limit
    out["min_team_wallets"] = math.ceil(team / cap_per_wallet)
    return out


def treasury_runway(result: dict) -> dict:
    """
    MATCH is never sold publicly, so data buyers need tokens from somewhere. If the
    treasury is their only source, every payment cycle drains it: it hands out D,
    gets back (1 - burn share) x D, so it loses exactly the burned part. The
    treasury buckets that can fund buyers are ecosystem, partners and DAO reserve.
    """
    funding = (ALLOCATION["Ecosystem & grants"][0] + ALLOCATION["Partners (clubs, data providers)"][0]
               + ALLOCATION["DAO reserve"][0]) * 1e6
    burned_data = sum(r["burned_data"] for r in result["rows"])
    return dict(treasury_funding_buckets=funding, data_burn_5y=burned_data,
                share_of_funding_used_pct=100 * burned_data / funding)


# --------------------------------------------------------------------------
# 4e. Parimutuel payouts (BettingPool)
#     payout = stake x (total - fee) / winning_pool   (same formula as the contract)
# --------------------------------------------------------------------------
def parimutuel(home: float, away: float, fee: float) -> dict:
    total = home + away
    dist = total - total * fee
    return dict(home_pool=home, away_pool=away,
                home_multiple=dist / home if home else None,
                away_multiple=dist / away if away else None,
                burned=total * fee)


# --------------------------------------------------------------------------
# 5. Figures and tables
# --------------------------------------------------------------------------
COL = {"bear": "#C8102E", "base": "#1F4FA3", "bull": "#2E8B57", "ink": "#23262B", "mute": "#5E6A78"}


def style(ax, title, ylabel=None):
    ax.set_title(title, loc="left", fontsize=12, color=COL["ink"])
    if ylabel:
        ax.set_ylabel(ylabel, color=COL["mute"])
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=.25)


def fig_allocation():
    fig, ax = plt.subplots(figsize=(7, 4.2))
    labels = list(ALLOCATION)
    vals = [v[0] for v in ALLOCATION.values()]
    colors = ["#1F4FA3", "#5B7FC4", "#C8102E", "#E8818F", "#9AA5B1"]
    ax.pie(vals, labels=[f"{l}\n{v} M ({v}%)" for l, v in zip(labels, vals)], colors=colors,
           startangle=90, counterclock=False, wedgeprops=dict(width=.42, edgecolor="white"), textprops=dict(fontsize=9))
    ax.set_title("Allocation of the 100 M MATCH cap", loc="left", fontsize=12)
    fig.tight_layout(); fig.savefig(FIG / "01_allocation.png", dpi=180); plt.close(fig)


def fig_supply(results, chain):
    fig, ax = plt.subplots(figsize=(8, 4.2))
    for res in results:
        ax.plot(range(1, MONTHS + 1), [r["supply"] / 1e6 for r in res["rows"]], color=COL[res["scenario"]],
                lw=2, label=res["scenario"])
    ax.axhline(chain.cap / 1e6, color=COL["ink"], ls="--", lw=1)
    ax.text(1, chain.cap / 1e6 + .4, "hard cap 100 M", fontsize=8, color=COL["ink"])
    ax.set_xlabel("month after launch", color=COL["mute"])
    style(ax, "Outstanding MATCH supply, 5-year scenarios", "million MATCH")
    ax.legend(frameon=False)
    fig.tight_layout(); fig.savefig(FIG / "02_supply_scenarios.png", dpi=180); plt.close(fig)


def fig_flows(res):
    rows = res["rows"]
    x = np.arange(1, MONTHS + 1)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.bar(x, [r["minted"] / 1e3 for r in rows], color=COL["base"], label="minted (data rewards)")
    ax.bar(x, [-r["burned_data"] / 1e3 for r in rows], color=COL["bear"], label="burned (data payments)")
    ax.bar(x, [-r["burned_bets"] / 1e3 for r in rows], bottom=[-r["burned_data"] / 1e3 for r in rows],
           color="#F2A0AA", label="burned (betting fees)")
    ax.axhline(0, color=COL["ink"], lw=.8)
    ax.set_xlabel("month after launch (gaps = off-season May-Aug: no matches, no rewards)", color=COL["mute"])
    style(ax, "Base scenario: monthly issuance vs burns", "thousand MATCH")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout(); fig.savefig(FIG / "03_flows_base.png", dpi=180); plt.close(fig)


def fig_sensitivity(shares, rewards, grid, chain):
    fig, ax = plt.subplots(figsize=(8, 4.6))
    lim = np.max(np.abs(grid))
    im = ax.imshow(grid, origin="lower", aspect="auto", cmap="RdBu_r", vmin=-lim, vmax=lim,
                   extent=[shares[0] * 100 - 2.5, shares[-1] * 100 + 2.5, rewards[0] - 25, rewards[-1] + 25])
    cs = ax.contour(shares * 100, rewards, grid, levels=[0], colors=COL["ink"], linewidths=1.5)
    ax.clabel(cs, fmt=lambda _: "flat supply", fontsize=8)
    ax.plot(chain.burn_share * 100, 200, marker="o", color=COL["ink"])
    ax.annotate("current setting", (chain.burn_share * 100, 200), xytext=(6, 6), textcoords="offset points", fontsize=8)
    ax.set_xlabel("burn share of data payments (%)", color=COL["mute"])
    ax.set_ylabel("reward per verified match (MATCH)", color=COL["mute"])
    ax.set_title("5-year supply change, base adoption (% of today's supply)", loc="left", fontsize=12)
    fig.colorbar(im, ax=ax, label="% change in supply after 5 years")
    fig.tight_layout(); fig.savefig(FIG / "04_sensitivity.png", dpi=180); plt.close(fig)


def fig_monte_carlo(mc, chain):
    p5, p50, p95 = (np.array(b) / 1e6 for b in mc["band"])
    x = np.arange(1, MONTHS + 1)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.fill_between(x, p5, p95, color=COL["base"], alpha=.18, label="5th-95th percentile")
    ax.plot(x, p50, color=COL["base"], lw=2, label="median")
    ax.axhline(chain.cap / 1e6, color=COL["ink"], ls="--", lw=1)
    ax.set_xlabel("month after launch", color=COL["mute"])
    style(ax, f"Monte Carlo, {mc['runs']:,} runs: uncertainty of supply", "million MATCH")
    ax.legend(frameon=False)
    fig.tight_layout(); fig.savefig(FIG / "05_monte_carlo.png", dpi=180); plt.close(fig)


def md_table(headers, rows):
    line = lambda cells: "| " + " | ".join(str(c) for c in cells) + " |"
    return "\n".join([line(headers), line(["---"] * len(headers))] + [line(r) for r in rows])


def n(x, d=0):
    return f"{x:,.{d}f}"


def main():
    ap = argparse.ArgumentParser(description="MATCH tokenomics simulation")
    ap.add_argument("--offline", action="store_true", help="do not read Sepolia, use contract constants")
    args = ap.parse_args()
    FIG.mkdir(parents=True, exist_ok=True)

    chain = read_chain(args.offline)
    assert sum(v[0] for v in ALLOCATION.values()) == chain.cap / 1e6, "allocation must add up to the cap"

    results = [simulate(s, chain) for s in SCENARIOS]
    years = {r["scenario"]: yearly(r, chain.total_supply) for r in results}
    base = next(s for s in SCENARIOS if s.name == "base")
    be = break_even(chain, base)
    shares, rewards, grid = sensitivity(chain, base)
    mc = monte_carlo(chain, base)
    base_rows = results[1]["rows"]
    gov = governance(chain, sum(r["minted"] for r in base_rows))
    runway = {r["scenario"]: treasury_runway(r) for r in results}
    bets = [parimutuel(*p, chain.bet_fee) for p in ((50, 50), (75, 25), (10, 90), (0, 75))]

    fig_allocation(); fig_supply(results, chain); fig_flows(results[1])
    fig_sensitivity(shares, rewards, grid, chain); fig_monte_carlo(mc, chain)

    # ---------------------------------------------------------------- tables for the report
    md = [f"# MATCH tokenomics: generated tables\n\nGenerated {date.today().isoformat()} by tokenomics/tokenomics.py. "
          f"Contract parameters from: {chain.source}.\n"]
    md.append("## Contract parameters (evidence)\n" + md_table(["Parameter", "Value"], [
        ["Hard cap", f"{n(chain.cap)} MATCH"], ["Outstanding supply", f"{n(chain.total_supply, 2)} MATCH"],
        ["Free room under the cap", f"{n(chain.cap - chain.total_supply, 2)} MATCH"],
        ["Burn share of data payments", f"{chain.burn_share:.0%} (governance max {chain.max_burn_share:.0%})"],
        ["Anti-whale wallet limit", f"{chain.max_wallet_share:.0%} of cap = {n(chain.max_wallet_share * chain.cap)} MATCH"],
        ["Max reward per verified match", f"{n(chain.max_reward_per_match)} MATCH"],
        ["Betting fee (burned)", f"{chain.bet_fee:.0%} of each settled pool"],
    ]))
    md.append("## 4.2 Initial allocation\n" + md_table(["Bucket", "MATCH", "Share", "Release"],
              [[k, f"{v[0]} M", f"{v[0]}%", v[1]] for k, v in ALLOCATION.items()]))
    md.append("## 4.3 Scenario assumptions\n" + md_table(
        ["Scenario", "Leagues (start / +per year / max)", "Reward per match", "Data buyers (start, growth/mo)",
         "Spend per buyer/mo", "Bettors (start, growth/mo)", "Stake per bettor/mo"],
        [[s.name, f"{s.leagues_start} / +{s.leagues_added_per_year} / {s.leagues_max}", n(s.reward_per_match),
          f"{n(s.subscribers_start)}, {s.subscriber_growth:.0%}", n(s.spend_per_subscriber),
          f"{n(s.bettors_start)}, {s.bettor_growth:.0%}", n(s.stake_per_bettor)] for s in SCENARIOS]))
    for name, ys in years.items():
        md.append(f"## Scenario '{name}': yearly supply\n" + md_table(
            ["Year", "Minted", "Burned (of which bets)", "End supply", "Inflation", "Unpaid rewards"],
            [[y["year"], n(y["minted"]), f"{n(y['burned'])} ({n(y['burned_bets'])})", n(y["end_supply"]),
              f"{y['inflation_pct']:+.2f} %", n(y["unpaid"])] for y in ys]))
    md.append("## 4.4 Break-even: data spend that neutralises one league's in-season rewards\n"
              f"One league in a season month mints {GAMES_PER_MONTH[10]} x {n(base.reward_per_match)} = "
              f"{n(GAMES_PER_MONTH[10] * base.reward_per_match)} MATCH.\n\n" + md_table(
                  ["Burn share", "Data spend needed / month", "Buyers needed (at base spend)"],
                  [[f"{r['burn_share_pct']} %" + (" (current)" if r["current"] else ""), n(r["data_spend"]),
                    n(r["buyers_needed"], 1)] for r in be]))
    md.append("## Monte Carlo (base scenario, uncertain growth)\n" + md_table(["Statistic", "Value"], [
        ["Runs / seed", f"{mc['runs']:,} / {mc['seed']}"],
        ["Supply after 5 years: 5th / 50th / 95th percentile",
         f"{n(mc['p5'] / 1e6, 2)} M / {n(mc['p50'] / 1e6, 2)} M / {n(mc['p95'] / 1e6, 2)} M"],
        ["P(supply higher than today)", f"{mc['prob_supply_up']:.1%}"],
        ["P(net deflation over 5 years)", f"{mc['prob_deflation']:.1%}"],
        ["P(cap reached at least once)", f"{mc['prob_cap_hit']:.1%}"],
        ["P(some rewards could not be minted)", f"{mc['prob_unpaid_rewards']:.1%}"]]))
    md.append("## 4.4 Treasury runway: who supplies data buyers with MATCH?\n"
              "MATCH is not sold, so buyers get it from the treasury (ecosystem + partners + DAO reserve = 40 M). "
              "Each payment cycle drains the treasury by exactly the burned share.\n\n" + md_table(
                  ["Scenario", "Data burn over 5 years", "Share of the 40 M treasury buckets"],
                  [[k, n(v["data_burn_5y"]), f"{v['share_of_funding_used_pct']:.1f} %"] for k, v in runway.items()]))
    md.append("## 5. Governance concentration (base scenario, year 5)\n"
              "ERC20Votes counts only delegated tokens. A: treasury and team delegate. "
              "B: they never delegate (policy of this white paper).\n\n" + md_table(
                  ["Community tokens", "Amount", "A: insiders' share of votes", "B: min. wallets (at 2 % limit) for a community majority"],
                  [[k, n(c["community"]), f"{c['insiders_vote_share_pct']:.1f} %", n(c["min_wallets_for_community_majority"])]
                   for k, c in gov["cases"].items()])
              + f"\n\nThe 15 M team allocation needs at least {gov['min_team_wallets']} wallets at the "
                f"{n(gov['wallet_limit'])} limit, or an exempt vesting contract.")
    md.append("## Parimutuel payout examples (BettingPool)\n" + md_table(
        ["Home pool", "Away pool", "Payout x if home wins", "Payout x if away wins", "Burned"],
        [[n(b["home_pool"]), n(b["away_pool"]),
          f"{b['home_multiple']:.3f}" if b["home_multiple"] else "refund (nobody backed)",
          f"{b['away_multiple']:.3f}" if b["away_multiple"] else "refund (nobody backed)",
          n(b["burned"], 2) if b["home_pool"] and b["away_pool"] else "0 if refunded"] for b in bets]))
    (OUT / "report_tables.md").write_text("\n\n".join(md) + "\n", encoding="utf-8")

    summary = dict(chain=asdict(chain), allocation={k: v[0] for k, v in ALLOCATION.items()},
                   scenarios=[asdict(s) for s in SCENARIOS], yearly=years, break_even=be,
                   monte_carlo={k: v for k, v in mc.items() if k != "band"}, governance=gov,
                   treasury_runway=runway, parimutuel=bets)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")

    # ---------------------------------------------------------------- console summary
    print(f"Parameters from {chain.source}: supply {n(chain.total_supply, 2)} / cap {n(chain.cap)}, "
          f"burn {chain.burn_share:.0%}, bet fee {chain.bet_fee:.0%}")
    for name, ys in years.items():
        print(f"  {name:4}: supply after 5 y {n(ys[-1]['end_supply'])}  "
              f"(y1 inflation {ys[0]['inflation_pct']:+.2f} %, y5 {ys[-1]['inflation_pct']:+.2f} %)")
    cur = next(r for r in be if r["current"])
    print(f"  break-even at {cur['burn_share_pct']} % burn: {n(cur['data_spend'])} MATCH/month "
          f"= {cur['buyers_needed']:.1f} buyers per league")
    print(f"  Monte Carlo: median {n(mc['p50'] / 1e6, 2)} M, P(deflation) {mc['prob_deflation']:.1%}, "
          f"P(cap hit) {mc['prob_cap_hit']:.1%}")
    print(f"Wrote {OUT / 'report_tables.md'}, {OUT / 'summary.json'} and {len(list(FIG.glob('*.png')))} figures.")


if __name__ == "__main__":
    main()
