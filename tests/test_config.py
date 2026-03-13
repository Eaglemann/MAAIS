from maais.config import constants as C


def test_risk_percentages_are_valid_fractions():
    assert 0 < C.MIN_RISK_PER_TRADE_PCT < C.MAX_RISK_PER_TRADE_PCT < 1.0


def test_portfolio_risk_limits_are_valid():
    assert 0 < C.PORTFOLIO_RISK_LIMIT_MIN_PCT < C.PORTFOLIO_RISK_LIMIT_PCT < 1.0


def test_zscore_trigger():
    assert C.ZSCORE_TRIGGER == 3.0


def test_drawdown_thresholds_are_ordered():
    assert C.DRAWDOWN_REDUCE_25_THRESHOLD < C.DRAWDOWN_REDUCE_50_THRESHOLD < C.DRAWDOWN_HALT_THRESHOLD


def test_drawdown_reduction_factors():
    assert C.DRAWDOWN_REDUCE_25_FACTOR == 0.75
    assert C.DRAWDOWN_REDUCE_50_FACTOR == 0.50


def test_correlation_thresholds_are_ordered():
    assert C.CORRELATION_FULL_THRESHOLD < C.CORRELATION_REDUCE_20_THRESHOLD < C.CORRELATION_REDUCE_40_THRESHOLD


def test_exactly_eight_agents():
    assert len(C.ALL_AGENTS) == 8


def test_all_agent_names_are_unique():
    assert len(set(C.ALL_AGENTS)) == len(C.ALL_AGENTS)


def test_exactly_four_regimes():
    assert len(C.ALL_REGIMES) == 4


def test_capital_phase_thresholds_are_ordered():
    assert C.PHASE_1_MAX < C.PHASE_2_MAX < C.PHASE_3_MAX


def test_strategy_stages_defined():
    stages = {C.StrategyStage.RESEARCH, C.StrategyStage.SIMULATION,
              C.StrategyStage.PILOT, C.StrategyStage.FULL_PRODUCTION}
    assert len(stages) == 4
