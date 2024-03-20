# ESPA-Bandit

This repo holds a multi-armed bandit approach to ESPA-Comp based on 
the dummy algorithm provided to ESPA-Comp participants.

dummy_algorithm.py:

	Computes an offer based on forecasted prices (LMP). This algorithm first computes an optimal (profit maximizing) charge/discharge schedule based on prices. It then computes the opportunity cost of charging/discharging in other windows and submits these as its offer. For the RTM a SoC bidding scheme is used with offers derived from the DAM prices.

market_participant.py:

	Parses WEASLE inputs, creates a dummy algorithm agent, and then generates an offer.
