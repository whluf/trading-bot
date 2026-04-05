# Trading Bot Autónomo - Mean Reversion

Bot que opera BTC/USDT y ETH/USDT en Bybit automáticamente.
Sin TradingView. Calcula RSI, EMAs y ATR internamente.

## Deploy en Coolify

### 1. Subir a Git

```bash
cd trading-bot
git init
git add .
git commit -m "trading bot"
git remote add origin git@tu-repo:trading-bot.git
git push -u origin main
```

### 2. En Coolify

1. **New Resource** → **Application** → seleccionar tu servidor
2. **Source**: apuntar al repo Git
3. **Build Pack**: Dockerfile
4. **Port**: 3000

### 3. Variables de Entorno (en Coolify)

Ir a **Environment Variables** y agregar:

| Variable | Valor |
|---|---|
| `BYBIT_API_KEY` | Tu API key de Bybit |
| `BYBIT_API_SECRET` | Tu API secret de Bybit |
| `BYBIT_TESTNET` | `true` (cambiar a `false` para real) |
| `PORT` | `3000` |

### 4. Storage (persistencia)

En **Storages** → agregar:
- **Container path**: `/app/data`
- Esto persiste el log de trades y estado entre deploys.

### 5. Deploy

Click **Deploy**. El bot arranca solo.

## Monitoreo

- **Health**: `curl https://tu-dominio/health`
- **Trades**: `curl https://tu-dominio/trades`
- **Logs**: En Coolify → Logs del contenedor

## Pasar a dinero real

1. En Coolify → Environment Variables
2. Cambiar `BYBIT_TESTNET` a `false`
3. Redeploy

## API Key de Bybit

1. Ir a https://www.bybit.com/app/user/api-management
2. Crear API key con permisos:
   - ✅ Contract Trade (Read + Write)
   - ❌ Withdrawal (NUNCA activar)
   - ❌ Transfer
3. Restricción IP: agregar la IP de tu VPS
