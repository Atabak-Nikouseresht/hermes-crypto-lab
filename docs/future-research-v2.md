# Future research version requirements

The current forward-validation candidate remains frozen. The items below are legitimate research ideas, but implementing any of them would create a new economic specification and must not be applied retrospectively to the current candidate:

- historical intraday execution aligned to Monday 00:10 UTC bid/ask evidence;
- a dynamic point-in-time market-cap or liquidity universe;
- explicit inclusion and survivorship treatment of delisted assets;
- true chronological retrain → select → test walk-forward optimization;
- nonzero risk-free-rate Sharpe assumptions;
- covariance or correlation portfolio optimization;
- volatility targeting;
- liquidity and market-impact modeling;
- partial-fill modeling;
- any new strategy parameters, asset universe, or candidate search.

A future implementation requires all of the following:

1. a new research and economic-specification version;
2. a new predeclared training and validation procedure;
3. a newly selected and locked candidate with separate governance anchors;
4. a genuinely new, untouched out-of-sample final-test period.

The current sealed experiments, locked candidate, economic hashes, and forward observations must remain unchanged and must not be reused for retuning.
