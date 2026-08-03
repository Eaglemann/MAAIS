"""Order book depth and imbalance features for the Liquidity and Order Flow Toxicity agents."""

from maais.market_data.schemas import OrderBookSnapshot


def compute_orderbook_features(snapshot: OrderBookSnapshot | None) -> dict[str, float | None]:
    if snapshot is None or not snapshot.bids or not snapshot.asks:
        return {"bid_ask_spread": None, "book_imbalance": None}

    best_bid = float(snapshot.bids[0][0])
    best_ask = float(snapshot.asks[0][0])
    mid = (best_bid + best_ask) / 2.0

    spread = (best_ask - best_bid) / mid if mid > 0 else None

    total_bid_vol = sum(float(q) for _, q in snapshot.bids)
    total_ask_vol = sum(float(q) for _, q in snapshot.asks)
    total_vol = total_bid_vol + total_ask_vol
    imbalance = (total_bid_vol - total_ask_vol) / total_vol if total_vol > 0 else None

    return {
        "bid_ask_spread": spread,
        "book_imbalance": imbalance,
    }
