# MATCH tokenomics: generated tables

Generated 2026-11-07 by tokenomics/tokenomics.py. Contract parameters from: Sepolia, read 2026-11-07.


## Contract parameters (evidence)
| Parameter | Value |
| --- | --- |
| Hard cap | 100,000,000 MATCH |
| Outstanding supply | 55,000,099.80 MATCH |
| Free room under the cap | 44,999,900.20 MATCH |
| Burn share of data payments | 30% (governance max 80%) |
| Anti-whale wallet limit | 2% of cap = 2,000,000 MATCH |
| Max reward per verified match | 500 MATCH |
| Betting fee (burned) | 2% of each settled pool |

## 4.2 Initial allocation
| Bucket | MATCH | Share | Release |
| --- | --- | --- | --- |
| Community data rewards | 45 M | 45% | minted only via rewardContributor(), per verified match |
| Ecosystem & grants | 20 M | 20% | treasury, granted to apps/integrations (e.g. this betting game, faucet) |
| Team | 15 M | 15% | treasury, 12-month cliff + 36-month linear vesting (planned vesting contract) |
| Partners (clubs, data providers) | 10 M | 10% | treasury, onboarding grants for data buyers and sources |
| DAO reserve | 10 M | 10% | treasury, spent only by governance vote |

## 4.3 Scenario assumptions
| Scenario | Leagues (start / +per year / max) | Reward per match | Data buyers (start, growth/mo) | Spend per buyer/mo | Bettors (start, growth/mo) | Stake per bettor/mo |
| --- | --- | --- | --- | --- | --- | --- |
| bear | 1 / +0 / 1 | 200 | 5, 1% | 4,000 | 100, 1% | 300 |
| base | 1 / +1 / 5 | 200 | 10, 4% | 5,000 | 300, 3% | 400 |
| bull | 1 / +2 / 9 | 200 | 15, 7% | 6,000 | 500, 6% | 500 |

## Scenario 'bear': yearly supply
| Year | Minted | Burned (of which bets) | End supply | Inflation | Unpaid rewards |
| --- | --- | --- | --- | --- | --- |
| 1 | 80,800 | 81,169 (5,074) | 54,999,730 | -0.00 % | 0 |
| 2 | 80,800 | 91,464 (5,718) | 54,989,067 | -0.02 % | 0 |
| 3 | 80,800 | 103,064 (6,443) | 54,966,803 | -0.04 % | 0 |
| 4 | 80,800 | 116,135 (7,260) | 54,931,469 | -0.06 % | 0 |
| 5 | 80,800 | 130,863 (8,181) | 54,881,405 | -0.09 % | 0 |

## Scenario 'base': yearly supply
| Year | Minted | Burned (of which bets) | End supply | Inflation | Unpaid rewards |
| --- | --- | --- | --- | --- | --- |
| 1 | 80,800 | 248,147 (22,760) | 54,832,753 | -0.30 % | 0 |
| 2 | 161,600 | 393,302 (32,450) | 54,601,050 | -0.42 % | 0 |
| 3 | 242,400 | 624,002 (46,266) | 54,219,449 | -0.70 % | 0 |
| 4 | 323,200 | 990,938 (65,965) | 53,551,710 | -1.23 % | 0 |
| 5 | 404,000 | 1,574,962 (94,050) | 52,380,748 | -2.19 % | 0 |

## Scenario 'bull': yearly supply
| Year | Minted | Burned (of which bets) | End supply | Inflation | Unpaid rewards |
| --- | --- | --- | --- | --- | --- |
| 1 | 80,800 | 539,724 (56,735) | 54,541,176 | -0.83 % | 0 |
| 2 | 242,400 | 1,201,945 (114,163) | 53,581,631 | -1.76 % | 0 |
| 3 | 404,000 | 2,679,611 (229,718) | 51,306,020 | -4.25 % | 0 |
| 4 | 565,600 | 5,979,867 (462,238) | 45,891,753 | -10.55 % | 0 |
| 5 | 727,200 | 13,356,871 (930,114) | 33,262,081 | -27.52 % | 0 |

## 4.4 Break-even: data spend that neutralises one league's in-season rewards
One league in a season month mints 52 x 200 = 10,400 MATCH.

| Burn share | Data spend needed / month | Buyers needed (at base spend) |
| --- | --- | --- |
| 10 % | 80,000 | 16.0 |
| 20 % | 40,000 | 8.0 |
| 30 % (current) | 26,667 | 5.3 |
| 40 % | 20,000 | 4.0 |
| 50 % | 16,000 | 3.2 |
| 60 % | 13,333 | 2.7 |
| 70 % | 11,429 | 2.3 |
| 80 % | 10,000 | 2.0 |

## Monte Carlo (base scenario, uncertain growth)
| Statistic | Value |
| --- | --- |
| Runs / seed | 2,000 / 2026 |
| Supply after 5 years: 5th / 50th / 95th percentile | 29.17 M / 52.09 M / 55.74 M |
| P(supply higher than today) | 12.8% |
| P(net deflation over 5 years) | 87.2% |
| P(cap reached at least once) | 0.0% |
| P(some rewards could not be minted) | 0.0% |

## 4.4 Treasury runway: who supplies data buyers with MATCH?
MATCH is not sold, so buyers get it from the treasury (ecosystem + partners + DAO reserve = 40 M). Each payment cycle drains the treasury by exactly the burned share.

| Scenario | Data burn over 5 years | Share of the 40 M treasury buckets |
| --- | --- | --- |
| bear | 490,018 | 1.2 % |
| base | 3,569,860 | 8.9 % |
| bull | 21,965,050 | 54.9 % |

## 5. Governance concentration (base scenario, year 5)
ERC20Votes counts only delegated tokens. A: treasury and team delegate. B: they never delegate (policy of this white paper).

| Community tokens | Amount | A: insiders' share of votes | B: min. wallets (at 2 % limit) for a community majority |
| --- | --- | --- | --- |
| rewards only (grants not yet distributed) | 1,212,000 | 95.4 % | 1 |
| rewards + all grants distributed | 31,212,000 | 44.5 % | 8 |

The 15 M team allocation needs at least 8 wallets at the 2,000,000 limit, or an exempt vesting contract.

## Parimutuel payout examples (BettingPool)
| Home pool | Away pool | Payout x if home wins | Payout x if away wins | Burned |
| --- | --- | --- | --- | --- |
| 50 | 50 | 1.960 | 1.960 | 2.00 |
| 75 | 25 | 1.307 | 3.920 | 2.00 |
| 10 | 90 | 9.800 | 1.089 | 2.00 |
| 0 | 75 | refund (nobody backed) | 0.980 | 0 if refunded |
