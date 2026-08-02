"""Kafka producer for the Market Data Layer.

Publishes parsed market events to the appropriate Kafka topic.
Topics (from constants.py):
  maais.klines      — KlineData (all timeframes)
  maais.orderbook   — OrderBookSnapshot
  maais.trades      — TradeData
  maais.funding     — FundingRateData

Messages are JSON-serialised. The symbol is used as the partition key
so that all events for a given symbol land on the same partition (ordering).
"""

import json
from typing import TYPE_CHECKING

from kafka import KafkaProducer
from kafka.errors import KafkaError

from maais.config.constants import (
    KAFKA_TOPIC_FUNDING,
    KAFKA_TOPIC_KLINES,
    KAFKA_TOPIC_ORDERBOOK,
    KAFKA_TOPIC_TRADES,
)
from maais.config.settings import get_settings
from maais.core.logging import get_logger
from maais.market_data.schemas import FundingRateData, KlineData, OrderBookSnapshot, TradeData

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


def _serialize_value(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def _serialize_key(key: object) -> bytes:
    if not isinstance(key, str):
        raise TypeError("Kafka market-data keys must be strings")
    return key.encode("utf-8")


class MarketDataProducer:
    """Synchronous Kafka producer wrapping kafka-python-ng.

    kafka-python-ng's KafkaProducer is synchronous under the hood.
    We wrap it in a thin class and call flush() after each batch.
    For true async, switch to aiokafka in a future batch.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._producer = KafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers.split(","),
            value_serializer=_serialize_value,
            key_serializer=_serialize_key,
            acks="all",
            retries=3,
            linger_ms=10,
        )

    def _send(self, topic: str, key: str, value: dict) -> None:
        try:
            self._producer.send(topic, key=key, value=value)
        except KafkaError as exc:
            logger.error("kafka_send_failed", topic=topic, key=key, error=str(exc))

    def publish_kline(self, kline: KlineData) -> None:
        self._send(KAFKA_TOPIC_KLINES, kline.symbol, kline.to_dict())

    def publish_orderbook(self, snapshot: OrderBookSnapshot) -> None:
        self._send(KAFKA_TOPIC_ORDERBOOK, snapshot.symbol, snapshot.to_dict())

    def publish_trade(self, trade: TradeData) -> None:
        self._send(KAFKA_TOPIC_TRADES, trade.symbol, trade.to_dict())

    def publish_funding(self, funding: FundingRateData) -> None:
        self._send(KAFKA_TOPIC_FUNDING, funding.symbol, funding.to_dict())

    def flush(self) -> None:
        self._producer.flush()

    def close(self) -> None:
        self._producer.flush()
        self._producer.close()

    def __enter__(self) -> "MarketDataProducer":
        return self

    def __exit__(self, *_) -> None:
        self.close()
