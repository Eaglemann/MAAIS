from enum import StrEnum


class ExperimentStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class StrategyStage(StrEnum):
    RESEARCH = "research"
    SIMULATION = "simulation"
    PILOT = "pilot"
    FULL_PRODUCTION = "full_production"


class AgentMaturity(StrEnum):
    IMPLEMENTED = "implemented"
    PROXY = "proxy"
    DISABLED = "disabled"


class QualityStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class DecisionStatus(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class Disposition(StrEnum):
    NEUTRAL = "neutral"
    REJECTED = "rejected"
    APPROVED = "approved"


class ProposalStatus(StrEnum):
    NEUTRAL = "neutral"
    REJECTED = "rejected"
    APPROVED = "approved"
    EXPIRED = "expired"


class GateType(StrEnum):
    DATA_QUALITY = "data_quality"
    REGIME_COMPATIBILITY = "regime_compatibility"
    CONSENSUS = "consensus"
    ADVERSARIAL = "adversarial"
    EV = "ev"
    ALPHA = "alpha"
    MONITORING = "monitoring"
    DRAWDOWN = "drawdown"
    CORRELATION = "correlation"
    PORTFOLIO_RISK = "portfolio_risk"
    LEVERAGE = "leverage"
    EXCHANGE_FILTERS = "exchange_filters"
    PAPER_BROKER_CAPACITY = "paper_broker_capacity"


class ReasonCode(StrEnum):
    ACCEPTED = "accepted"
    NEUTRAL_CONSENSUS = "neutral_consensus"
    DISABLED_AGENT = "disabled_agent"
    INCOMPATIBLE_REGIME = "incompatible_regime"
    DATA_QUALITY_FAILED = "data_quality_failed"
    INSUFFICIENT_HISTORY = "insufficient_history"
    CONSENSUS_FAILED = "consensus_failed"
    ADVERSARIAL_BLOCKED = "adversarial_blocked"
    NON_POSITIVE_EV = "non_positive_ev"
    ALPHA_FAILED = "alpha_failed"
    MONITORING_UNHEALTHY = "monitoring_unhealthy"
    DRAWDOWN_HALT = "drawdown_halt"
    CORRELATION_BLOCKED = "correlation_blocked"
    PORTFOLIO_RISK_EXCEEDED = "portfolio_risk_exceeded"
    LEVERAGE_REJECTED = "leverage_rejected"
    EXCHANGE_FILTER_REJECTED = "exchange_filter_rejected"
    BROKER_CAPACITY_REJECTED = "broker_capacity_rejected"
    DUPLICATE_IDENTICAL = "duplicate_identical"


class PaperOrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PaperOrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"


class PaperOrderStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PositionEffect(StrEnum):
    OPEN = "open"
    REDUCE = "reduce"
