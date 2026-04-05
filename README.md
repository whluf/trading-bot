Resumen del Proyecto: Trading Bot Autónomo
Qué es
Bot de trading automático que opera BTC/USDT y ETH/USDT en Bybit, sin TradingView. Calcula indicadores internamente y ejecuta solo.
Estrategia
	∙	Filtro diario: RSI(14) detecta extremos (< 30 sobreventa, > 70 sobrecompra)
	∙	Confirmación horaria: Cruce EMA 21/50 confirma cambio de estructura
	∙	Ejecución: Entrada a mercado, SL = 1.5×ATR, TP = 3.75×ATR (ratio 1:2.5)
Parámetros
	∙	Riesgo por trade: 3%
	∙	Apalancamiento: 3x
	∙	Circuit breaker: pausa si drawdown ≥ 15%
	∙	Chequeo: cada 60 segundos
Stack
	∙	Python 3.12 + ccxt + pandas-ta + Flask
	∙	Dockerizado para Coolify
	∙	Health check en /health, historial en /trades
	∙	Estado persistente en /app/data
Deploy
	1.	Descomprimir trading-bot.tar.gz → subir a repo Git
	2.	Coolify: New Resource → Dockerfile → Port 3000
	3.	Env vars: BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET=true
	4.	Storage: mount /app/data
	5.	Deploy
Ruta a producción
	1.	Crear API Key Bybit (solo Contract Trade, restringir IP del VPS)
	2.	Correr en testnet 2-4 semanas
	3.	Si OK → BYBIT_TESTNET=false + depositar $100-300 USDT vía P2P
Archivo
Todo está en el trading-bot.tar.gz ya descargado. Código completo, listo para deploy.​​​​​​​​​​​​​​​​