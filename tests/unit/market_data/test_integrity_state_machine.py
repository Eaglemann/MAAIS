from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from maais.domain.enums import QualityStatus
from maais.market_data.events import MarketEventKind, MarkFundingPayload, VenueClockPayload
from maais.market_data.integrity.state_machine import (
    FrameAdmission,
    IntegrityCheck,
    IntegrityContext,
    IntegrityPolicy,
    MarketIntegrityStateMachine,
)
from tests.unit.market_data.test_frame_builder import _inputs, _key


def _frame(events=None):
    from maais.market_data.frames import CausalMinuteFrameBuilder

    values = tuple(events or _inputs())
    bar = next(item for item in values if item.kind is MarketEventKind.CLOSED_BAR)
    return CausalMinuteFrameBuilder().build(_key(), bar, values)


def _context(frame, **changes) -> IntegrityContext:
    prior_sequences = {
        name: source.sequence - 1
        for name, source in frame.source_manifest.items()
        if source.sequence is not None and name in {"closed_bar", "order_book"}
    }
    base = IntegrityContext(
        frame=frame,
        evaluated_at=frame.cutoff_at + timedelta(milliseconds=100),
        previous_bar_close_at=frame.bar.bar_open_at,
        previous_close=Decimal("100"),
        prior_sequences=prior_sequences,
        recent_close_returns=(Decimal("0.009"), Decimal("0.011")) * 10,
        historical_bar_count=60,
    )
    return replace(base, **changes)


def _result(assessment, check: IntegrityCheck):
    return next(item for item in assessment.results if item.check is check)


def test_complete_warm_frame_is_admitted_with_every_required_check_recorded() -> None:
    assessment = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(
        _context(_frame())
    )

    assert assessment.admission is FrameAdmission.ADMITTED
    assert assessment.quality_status is QualityStatus.PASSED
    assert not assessment.blocking_checks
    assert {item.check for item in assessment.results} == set(IntegrityCheck)
    assert all(item.status is QualityStatus.PASSED for item in assessment.results)


def test_missing_secondary_reference_is_required_not_applicable_and_quarantines() -> None:
    events = tuple(
        item
        for item in _inputs()
        if not (item.kind is MarketEventKind.REFERENCE_PRICE and item.venue == "coinbase")
    )
    assessment = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(
        _context(_frame(events))
    )

    result = _result(assessment, IntegrityCheck.SECONDARY_REFERENCE)
    assert result.status is QualityStatus.NOT_APPLICABLE
    assert result.reason_code == "secondary_reference_missing"
    assert assessment.admission is FrameAdmission.QUARANTINED
    assert IntegrityCheck.SECONDARY_REFERENCE in assessment.blocking_checks


def test_futures_spot_basis_and_secondary_venue_are_distinct_checks() -> None:
    events = list(_inputs())
    mark_index = next(
        index for index, item in enumerate(events) if item.kind is MarketEventKind.MARK_FUNDING
    )
    mark = events[mark_index]
    assert isinstance(mark.payload, MarkFundingPayload)
    events[mark_index] = replace(
        mark,
        payload=replace(
            mark.payload,
            mark_price=Decimal("110"),
            index_price=Decimal("110"),
        ),
    )
    secondary_index = next(
        index
        for index, item in enumerate(events)
        if item.kind is MarketEventKind.REFERENCE_PRICE and item.venue == "coinbase"
    )
    secondary = events[secondary_index]
    events[secondary_index] = replace(
        secondary,
        payload=replace(secondary.payload, price=Decimal("110")),  # type: ignore[arg-type]
    )
    assessment = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(
        _context(_frame(events))
    )

    assert _result(assessment, IntegrityCheck.FUTURES_SPOT_BASIS).status is QualityStatus.FAILED
    assert _result(assessment, IntegrityCheck.SECONDARY_REFERENCE).status is QualityStatus.PASSED


def test_clock_drift_sequence_regression_and_stale_book_each_fail_closed() -> None:
    events = list(_inputs())
    clock_index = next(
        index for index, item in enumerate(events) if item.kind is MarketEventKind.VENUE_CLOCK
    )
    clock = events[clock_index]
    assert isinstance(clock.payload, VenueClockPayload)
    events[clock_index] = replace(
        clock,
        payload=replace(clock.payload, server_time=clock.observed_at + timedelta(seconds=5)),
    )
    book_index = next(
        index for index, item in enumerate(events) if item.kind is MarketEventKind.ORDER_BOOK
    )
    book = events[book_index]
    events[book_index] = replace(
        book,
        venue_event_at=book.venue_event_at - timedelta(seconds=5),
        observed_at=book.observed_at - timedelta(seconds=5),
    )
    frame = _frame(events)
    prior = {
        "closed_bar": frame.source_manifest["closed_bar"].sequence,
        "order_book": frame.source_manifest["order_book"].sequence,
    }
    assessment = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(
        _context(frame, prior_sequences=prior)
    )

    assert _result(assessment, IntegrityCheck.CLOCK_DRIFT).status is QualityStatus.FAILED
    assert _result(assessment, IntegrityCheck.SEQUENCE).status is QualityStatus.FAILED
    assert _result(assessment, IntegrityCheck.STALE_BOOK).status is QualityStatus.FAILED
    assert assessment.admission is FrameAdmission.QUARANTINED


def test_order_book_update_ranges_are_monotonic_not_artificially_contiguous() -> None:
    frame = _frame()
    current = frame.source_manifest["order_book"].sequence
    assert current is not None

    assessment = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(
        _context(frame, prior_sequences={"closed_bar": 99, "order_book": current - 10})
    )

    assert _result(assessment, IntegrityCheck.SEQUENCE).status is QualityStatus.PASSED


def test_history_and_outlier_warmup_are_blocking_not_applicable() -> None:
    assessment = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(
        _context(
            _frame(),
            previous_bar_close_at=None,
            previous_close=None,
            recent_close_returns=(),
            historical_bar_count=0,
        )
    )

    assert (
        _result(assessment, IntegrityCheck.MISSING_INTERVAL).status is QualityStatus.NOT_APPLICABLE
    )
    assert (
        _result(assessment, IntegrityCheck.HISTORICAL_COVERAGE).status
        is QualityStatus.NOT_APPLICABLE
    )
    assert (
        _result(assessment, IntegrityCheck.CLOSE_RETURN_OUTLIER).status
        is QualityStatus.NOT_APPLICABLE
    )
    assert assessment.admission is FrameAdmission.QUARANTINED
