#!/usr/bin/env python3
"""
Deribit API Health & Schema Monitor

Este script testa a estabilidade e a estrutura de dados das APIs (REST e WebSocket) da Deribit.
É desenhado para ser rodado via CRON (ex: diariamente) para alertar sobre "breaking changes"
antes que o btc-flow-monitor quebre silenciosamente em produção.

Testes realizados:
1. REST: Disponibilidade do endpoint get_book_summary_by_currency.
2. REST: Presença das chaves JSON críticas (instrument_name, mark_iv, open_interest, etc).
3. WebSocket: Conexão bem-sucedida.
4. WebSocket: Sucesso na inscrição do canal ticker.BTC-PERPETUAL.100ms e recebimento de cotação.

Retorna código 0 se estiver tudo saudável. Retorna 1 e printa o erro se algo falhar.
"""
from __future__ import annotations

import asyncio
import json
import sys
import httpx
import websockets
from loguru import logger

# Configurar sys.stdout para utf-8 (previne erro no Windows cmd/powershell)
sys.stdout.reconfigure(encoding='utf-8')

logger.remove()
logger.add(sys.stdout, format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}")

REST_URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
WS_URL = "wss://www.deribit.com/ws/api/v2"

# Campos estritamente necessários para a engine matemática
REQUIRED_REST_FIELDS = [
    "instrument_name",
    "open_interest",
    "mark_iv"
]

async def check_rest_api() -> bool:
    logger.info("Testando API REST: get_book_summary_by_currency...")
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(REST_URL, params={"currency": "BTC", "kind": "option"})
            
        if resp.status_code != 200:
            logger.error(f"[REST FALHA] Status HTTP {resp.status_code}")
            return False
            
        data = resp.json()
        if "result" not in data:
            logger.error("[REST FALHA] A chave 'result' desapareceu do payload raiz.")
            return False
            
        results = data["result"]
        if not isinstance(results, list) or len(results) == 0:
            logger.error("[REST FALHA] 'result' esta vazio ou nao e uma lista.")
            return False
            
        sample = results[0]
        missing_fields = [f for f in REQUIRED_REST_FIELDS if f not in sample]
        
        if missing_fields:
            logger.error(f"[REST FALHA] Campos ausentes no payload: {missing_fields}")
            return False
            
        if "underlying_price" not in sample and "estimated_delivery_price" not in sample:
            logger.error("[REST FALHA] Preco spot (underlying) nao encontrado.")
            return False

        logger.info(f"[REST OK] {len(results)} contratos retornados com o schema correto.")
        return True

    except Exception as e:
        logger.error(f"[REST FALHA] Excecao: {e}")
        return False

async def check_ws_api() -> bool:
    logger.info("Testando API WebSocket: ticker.BTC-PERPETUAL.100ms...")
    
    try:
        # Removido o argumento 'timeout' que gerava TypeError
        async with websockets.connect(WS_URL) as ws:
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "public/subscribe",
                "params": {"channels": ["ticker.BTC-PERPETUAL.100ms"]}
            }
            await ws.send(json.dumps(subscribe_msg))
            
            for _ in range(3):
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw_msg)
                
                if "error" in msg:
                    logger.error(f"[WS FALHA] API retornou erro: {msg['error']}")
                    return False
                    
                if msg.get("method") == "subscription":
                    data = msg.get("params", {}).get("data", {})
                    
                    if "mark_price" in data or "index_price" in data:
                        logger.info("[WS OK] Ticker perpetuo emitindo precos corretamente.")
                        return True
                    else:
                        logger.error("[WS FALHA] Payload do ticker nao contem 'mark_price' ou 'index_price'.")
                        return False

            logger.error("[WS FALHA] Nao recebemos a mensagem de cotacao a tempo.")
            return False
            
    except asyncio.TimeoutError:
         logger.error("[WS FALHA] Timeout aguardando resposta da Deribit.")
         return False
    except Exception as e:
        logger.error(f"[WS FALHA] Excecao: {e}")
        return False

async def main():
    logger.info("=== Iniciando Verificacao de Saude ===")
    
    rest_ok = await check_rest_api()
    ws_ok = await check_ws_api()
    
    if rest_ok and ws_ok:
        logger.info("[SUCESSO] Todos os endpoints estao operacionais e inalterados.")
        sys.exit(0)
    else:
        logger.error("[ALERTA] Uma ou mais verificacoes falharam!")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
