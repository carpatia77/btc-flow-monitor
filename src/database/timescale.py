"""
TimescaleDB Persistence Layer for BTC Options Flow.
Handles connection pooling, hypertables setup, and bulk inserts.
"""
from __future__ import annotations

import os
from typing import Any
import asyncpg
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "deribit_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "deribit_pass")
DB_NAME = os.getenv("DB_NAME", "btc_options")


async def get_db_pool() -> asyncpg.Pool:
    """Create and return an asyncpg connection pool."""
    pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        min_size=1,
        max_size=10
    )
    if not pool:
        raise RuntimeError("Failed to create database pool")
    return pool


async def init_db(pool: asyncpg.Pool) -> None:
    """Create tables and configure TimescaleDB hypertables."""
    async with pool.acquire() as conn:
        # Ensure TimescaleDB extension exists
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")

        # Options History Table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS options_history (
                time TIMESTAMPTZ NOT NULL,
                instrument_name VARCHAR(50) NOT NULL,
                strike NUMERIC NOT NULL,
                option_type VARCHAR(1) NOT NULL,
                expiration DATE NOT NULL,
                spot_price NUMERIC NOT NULL,
                open_interest NUMERIC NOT NULL,
                mark_iv NUMERIC NOT NULL,
                volume NUMERIC NOT NULL
            );
        """)
        
        # Check if hypertable already exists
        hyper_check = await conn.fetchval("""
            SELECT count(*) 
            FROM _timescaledb_catalog.hypertable 
            WHERE table_name = 'options_history';
        """)
        if hyper_check == 0:
            logger.info("Converting options_history to hypertable...")
            await conn.execute("SELECT create_hypertable('options_history', 'time');")
            # Index for fast OI lookup by instrument
            await conn.execute("CREATE INDEX ix_options_history_instrument_time ON options_history (instrument_name, time DESC);")

        # Analytics Snapshots Table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS analytics_snapshots (
                time TIMESTAMPTZ NOT NULL,
                spot_price NUMERIC NOT NULL,
                total_gex_usd NUMERIC NOT NULL,
                total_vex_usd NUMERIC NOT NULL,
                total_tex_usd_per_day NUMERIC NOT NULL,
                gex_regime VARCHAR(20) NOT NULL,
                vex_regime VARCHAR(20) NOT NULL,
                dealer_pin_strike NUMERIC NOT NULL
            );
        """)
        
        hyper_check_analytics = await conn.fetchval("""
            SELECT count(*) 
            FROM _timescaledb_catalog.hypertable 
            WHERE table_name = 'analytics_snapshots';
        """)
        if hyper_check_analytics == 0:
            logger.info("Converting analytics_snapshots to hypertable...")
            await conn.execute("SELECT create_hypertable('analytics_snapshots', 'time');")

        logger.info("TimescaleDB initialized successfully.")


async def save_options_chain(pool: asyncpg.Pool, instruments: list[Any], spot_price: float, timestamp: str) -> None:
    """Bulk insert options chain into options_history."""
    records = []
    for inst in instruments:
        records.append((
            timestamp,
            inst.instrument_name,
            inst.strike,
            inst.option_type,
            inst.expiration, # Date string 'YYYY-MM-DD'
            spot_price,
            inst.open_interest,
            inst.mark_iv,
            inst.volume
        ))

    query = """
        INSERT INTO options_history (
            time, instrument_name, strike, option_type, expiration, 
            spot_price, open_interest, mark_iv, volume
        ) VALUES ($1, $2, $3, $4, $5::date, $6, $7, $8, $9)
    """
    async with pool.acquire() as conn:
        await conn.executemany(query, records)
        logger.info(f"Saved {len(records)} instruments to options_history.")


async def save_analytics_snapshot(pool: asyncpg.Pool, payload: dict, timestamp: str) -> None:
    """Insert the summarized analytics payload."""
    query = """
        INSERT INTO analytics_snapshots (
            time, spot_price, total_gex_usd, total_vex_usd, total_tex_usd_per_day,
            gex_regime, vex_regime, dealer_pin_strike
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    """
    async with pool.acquire() as conn:
        await conn.execute(
            query,
            timestamp,
            payload["spot_price"],
            payload["total_gex_usd"],
            payload["total_vex_usd"],
            payload["tex_metrics"]["total_tex_usd_per_day"],
            payload["gex_regime"],
            payload["vex_regime"],
            payload["tex_metrics"]["dealer_pin_strike"]
        )


async def get_latest_oi_snapshot(pool: asyncpg.Pool) -> dict[str, float]:
    """
    Returns the most recent Open Interest for each instrument recorded in the last 15 minutes.
    Used to calculate OI difference (flow) for Dealer Positioning.
    """
    query = """
        SELECT DISTINCT ON (instrument_name) instrument_name, open_interest
        FROM options_history
        WHERE time > NOW() - INTERVAL '15 minutes'
        ORDER BY instrument_name, time DESC;
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query)
    
    return {row["instrument_name"]: float(row["open_interest"]) for row in rows}
