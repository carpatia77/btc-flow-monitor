# BTC Flow Monitor — Institutional-Grade Bitcoin Gamma Exposure Engine

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Data-Deribit%20API-blueviolet?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Math-NumPy%20%7C%20SciPy-orange?style=for-the-badge&logo=numpy" />
  <img src="https://img.shields.io/badge/I%2FO-WebSocket%20%2B%20REST-blue?style=for-the-badge" />
</p>

> **Real-time Gamma Exposure (GEX) analysis for Bitcoin options on Deribit.**
> Vectorized Black-Scholes engine, async WebSocket + REST ingestion, and process-isolated analytics.

---

## 📊 What Does This Do?

This system monitors the **entire BTC options chain on Deribit** (which controls ~90% of global crypto options volume) and calculates Gamma Exposure (GEX) — the derivative metric that reveals where **Market Makers are forced to hedge**.

| GEX Regime | Market Behaviour |
|---|---|
| **Positive GEX** *(above Gamma Flip)* | Market makers buy dips & sell rallies → **mean-reverting, low-vol** |
| **Negative GEX** *(below Gamma Flip)* | Market makers amplify moves → **trending, high-vol** |

The **Call Wall** is the price ceiling. The **Put Wall** is the price floor. The **Gamma Flip** is where the regime changes.

---

## 🏗️ Architecture

```
btc-flow-monitor/
├── src/
│   ├── ingestion/                # I/O Layer (Network Only)
│   │   ├── deribit_ws.py         # WebSocket consumer (spot price streaming)
│   │   └── deribit_rest.py       # REST client (full chain fetch)
│   │
│   ├── engine/                   # CPU Layer (Math Only)
│   │   ├── black_scholes.py      # Vectorized gamma (NumPy/SciPy)
│   │   └── gex_analytics.py      # GEX calculation + Walls + Flip detection
│   │
│   ├── core/
│   │   └── state_manager.py      # Thread-safe state with bounded memory
│   │
│   └── main.py                   # Async orchestrator (gather + ProcessPool)
│
├── btc_gex_worker.py             # Headless REST-only worker for cron jobs
├── pyproject.toml
└── requirements.txt
```

### Key Design Decisions

1. **I/O ↔ CPU Separation**: WebSocket consumer and math engine never share a thread. The GIL is bypassed via `ProcessPoolExecutor`.
2. **Memory Safety**: `deque(maxlen=N)` for trades, dict overwrite for chain. No unbounded lists.
3. **No Deribit Greeks**: The `get_book_summary_by_currency` endpoint does NOT return Greeks. We compute Gamma locally using vectorized Black-Scholes (sub-millisecond for 1000+ contracts).
4. **Dual Mode**: Live streaming (`main.py`) or headless cron (`btc_gex_worker.py`).

---

## 🚀 Quickstart

### 1. Install

```bash
git clone https://github.com/carpatia77/btc-flow-monitor.git
cd btc-flow-monitor
pip install -e .
```

### 2. Run (Live Streaming Mode)

```bash
python -m src
```

This starts:
- A WebSocket connection to Deribit for real-time spot price.
- A REST refresh of the full options chain every 5 minutes.
- A GEX recalculation every 60 seconds in a separate process.

### 3. Run (Headless Cron Mode)

```bash
python btc_gex_worker.py
```

### Cron (VPS Linux com Alertas por E-mail)

Criamos um script que configura automaticamente a *crontab* na sua VPS para rodar a checagem de saúde da API todos os dias às 06:00 da manhã (Horário de Brasília) e notificar o e-mail cadastrado em caso de falha:

```bash
chmod +x scripts/setup_cron.sh
./scripts/setup_cron.sh
```

Para o cálculo de GEX propriamente dito (a cada 5min), você pode agendar o worker headless:
```bash
*/5 * * * * cd /path/to/btc-flow-monitor && python btc_gex_worker.py >> /var/log/btc_gex.jsonl 2>&1
```

---

## 📤 Output Format

```json
{
    "asset": "BTC/USD",
    "spot_price": 73444.97,
    "total_gex_usd": 145230000.50,
    "gex_regime": "Positive",
    "call_wall_strike": 80000,
    "call_wall_gex_usd": 45200000.10,
    "put_wall_strike": 65000,
    "put_wall_gex_usd": -38100000.20,
    "gamma_flip": 71250.75,
    "instruments_analyzed": 847,
    "computation_time_ms": 12.34,
    "status": "ok"
}
```

---

## 🧮 Mathematics

### Black-Scholes Gamma (Vectorized)

```
         N'(d1)
Γ = ──────────────────
      S · σ · √T
```

Where `d1 = [ln(F/K) + 0.5σ²T] / (σ√T)`, `F = S·e^(r)T` (no dividends for BTC).

### Dollar Gamma Exposure (Deribit)

```
GEX(K) = Γ · OI · S² · 0.01 · sign
```

- `sign` = **+1 for calls**, **−1 for puts**
- 1 Deribit contract = 1 BTC (no 100x multiplier like SPX)
- The **Gamma Profile** is computed across a `[0.8S, 1.2S]` price grid using 2D NumPy broadcasting

---

## 🧬 Lineage

Engine transplanted from [`spx-gex-pro`](https://github.com/carpatia77/spx-gex-pro), adapted for BTC/Deribit specifics (no dividends, contract multiplier = 1, REST + WS dual ingestion).

---

## 📄 License

MIT License.
