# Bot BTC/USDT · Top-Down Pullback (Binance USDⓈ-M Futures)

Bot de trading algorítmico para **BTC/USDT perpetuo, margen aislado**, con estrategia de confluencia
multi-timeframe, gestión estricta del riesgo, avisos por **Telegram** y **dashboard gráfico** local.
Pensado para una cuenta pequeña (por defecto **$100 USDT**, margen $20, 10x, riesgo máx. $3).

> ⚠️ **Operar con apalancamiento puede hacerte perder todo el capital.** Empieza siempre en **Testnet**.
> Nada de lo que hay aquí garantiza ganancias. Lee la sección *Resultados del backtest* antes de usar dinero real.

---

## 1. Puesta en marcha (Windows)

1. Doble clic en **`iniciar_bot.bat`**. La primera vez crea el entorno virtual e instala las dependencias.
2. Se abre el dashboard en <http://127.0.0.1:5000> (solo accesible desde tu PC).
3. Ve a **⚙ Configuración**:
   - Pega la **API Key / Secret de Testnet** y pulsa **Guardar claves** → **Probar conexión Testnet**.
   - (Opcional) Pega el **token del bot de Telegram** (@BotFather), guarda, pulsa **Generar código de vinculación** y envía
     `/start CÓDIGO` a tu bot.
4. El bot ya vigila el mercado. Con claves válidas pasa de *SOLO MONITOREO* a *OPERANDO*.

Alternativa por consola para las claves: `python configurar_claves.py`.
Detener: `Ctrl+C` en la consola (los Stop Loss / Take Profit **siguen activos en Binance**).

### Aplicación de escritorio (`BotBTC.exe`)
`dist\BotBTC.exe` es un único ejecutable que **no necesita Python ni instalación**: cópialo a cualquier PC con Windows 10/11 y ábrelo.
Abre el panel en su propia ventana (Edge/Chrome en modo aplicación, sin barra de navegador) y deja un icono en la bandeja:
cerrar la ventana **no** detiene el bot; icono → **Abrir panel** / **Salir**. Si lo abres dos veces, solo muestra la ventana de la copia en marcha.
Los datos (configuración, historial, registros) se guardan en `%LOCALAPPDATA%\BotBinanceBTC`, y las claves en el Administrador de credenciales.
En un PC nuevo solo hay que: abrir el `.exe` → ⚙ Configuración → pegar la API → copiar la **IP pública** que muestra la pantalla y añadirla a la whitelist de la clave en Binance.
Para regenerarlo tras cambiar el código: `construir_exe.bat`. Windows SmartScreen o el antivirus pueden avisar porque el `.exe` no está firmado
(«Más información → Ejecutar de todas formas»).

### Cómo cambiar la API (el «espacio y botón» de Configuración)
Escribe la nueva clave en el campo y pulsa **Guardar claves**. Se guardan en el **Administrador de credenciales
de Windows** (cifradas y ligadas a tu usuario), nunca en la carpeta del bot. Las de Testnet y las de la cuenta real se
guardan por separado. Un campo vacío no modifica nada. No se pueden cambiar las claves del entorno activo con una posición abierta.

### Claves y whitelist de IP
- Permisos de la clave real: **lectura + Futuros**. **Sin retiros.**
- Binance exige restringir la IP para habilitar Futuros: añade la **IP pública de este PC** (se muestra en Configuración).
- Si tu IP cambia, el bot lo detecta, te avisa por Telegram y se recupera solo cuando actualices la whitelist.
  Mientras tanto las posiciones abiertas siguen protegidas (SL/TP viven en los servidores de Binance).
- Para pasar a **cuenta real**: Configuración → *Cuenta REAL* → escribir `OPERAR REAL`. El bot arranca siempre **en pausa** en modo real
  y hay que pulsar **Reanudar**.

### 24/7 en un PC con Windows
- `main.py` evita que Windows suspenda el equipo mientras corre (la pantalla sí puede apagarse).
- Para que arranque con Windows: `Win+R` → `shell:startup` → crea un acceso directo a `iniciar_bot.bat` con el argumento `--no-browser`.
- Desactiva las actualizaciones automáticas con reinicio en horario de trading o usa un VPS con IP fija.

---

## 2. Cómo «ve» el mercado el bot

No ve gráficos: lee datos. **REST** al inicio (historial de 1w/1d/4h/1h) + **WebSocket** `kline` en tiempo real; el campo
`x=true` marca el cierre de vela. Un vigilante comprueba por REST que no falte ninguna vela cerrada y rellena huecos tras
una desconexión. Los indicadores (EMA, SMA, RSI, ATR, ADX, Bollinger, Estocástico, MACD) se calculan localmente con
suavizado idéntico al de TradingView. Solo se usan **velas cerradas**.

Los datos de mercado vienen siempre de la red **real** de Binance (endpoints públicos). En Testnet los precios/volúmenes
no son reales, así que solo se usa para ejecutar órdenes.

## 3. Estrategia (tres fases en cascada)

| Fase | Temporalidad | Condición |
|---|---|---|
| 1 · Tendencia | 1w + 1d | LONG: 1w Close>EMA20 y RSI>50; 1d Close>SMA200, EMA20>EMA50, ADX>25. SHORT: espejo. ADX(1d)<22 → STANDBY |
| 2 · Zona de valor | 4h (o 1d) | Precio a ≤0.5 ATR de la zona EMA20–EMA50, **o** en 50–61.8% de Fibonacci del último impulso; RSI(1d) 40–55 (long) / 45–60 (short) |
| 3 · Gatillo | 1h (o 2h) | 1 condición de agotamiento (Bollinger+RSI/Estocástico o divergencia) **y** 1 patrón (Pin Bar, Envolvente con volumen, Falsa ruptura de Inside Bar) |

### Interpretaciones que tuve que tomar (la especificación era ambigua) — todas configurables
- `iloc[-2]` del documento ≡ `iloc[-1]` de los DataFrames del bot, que contienen **solo velas cerradas**.
- «Precio» en la Fase 2 = cierre de la vela gatillo. Zona EMA20/50 medida en **4h** por defecto (`TF_ZONA`).
- **Fibonacci**: «último impulso mayor» se detecta con un ZigZag basado en ATR diario (reversión ≥ 2 ATR) y se exige un impulso ≥ 4 ATR diarios.
- **Agotamiento**: estricto, debe darse en la vela gatillo (`VENTANA_AGOTAMIENTO=1`; con 2 también cuenta la vela previa).
- **Divergencia**: la vela gatillo marca un mínimo/máximo más extremo que el último pivote (3 velas a cada lado) y el RSI o el MACD no lo confirman.
- **Stop Loss** = extremo de la vela gatillo ∓ 1.5×ATR(14). Se aplica como **distancia porcentual** sobre el precio de entrada real (así funciona igual en Testnet).
- Binance exige cantidades múltiplos de **0.001 BTC**: el nominal real puede ser menor que $200 (p. ej. con BTC a $82k son 0.002 BTC = $163.85).

### Temporalidades independientes (⚙ Configuración → Temporalidades)
Cada función usa la suya, y la combinación la eliges tú (orden obligatorio: gatillo ≤ retrocesos ≤ tendencia ≤ filtro superior):

| Función | Opciones |
|---|---|
| **Tendencia** (¿alcista o bajista?) | 4h · 8h · 12h · 16h · 20h · 1d · 2d · 3d · 1w · 1M |
| **Filtro de tendencia superior** (opcional) | ninguno o cualquiera de las anteriores |
| **Retrocesos** (zona EMA20/EMA50) y **Fibonacci / soportes** | 5m · 15m · 30m · 1h · 2h · 4h · 6h · 8h · 12h · 1d |
| **Acción / gatillo** (patrón + agotamiento) | 1m · 3m · 5m · 15m · 30m · 1h · 2h · 4h |

Ejemplo: tendencia confirmada en **4h** sin filtro superior (aunque el mensual sea bajista), retroceso visto en **15m**, gatillo en **5m**.
El **gráfico** permite ver 16 temporalidades (1m … 1M) con soportes/resistencias (S/R) dibujados.
Binance no ofrece 16h, 20h ni 2d: el bot las construye agrupando velas reales de 4h / 1d alineadas al epoch UTC (no coinciden con las de TradingView).
En 1M no hay 200 velas: la condición «Close > SMA(200)» se omite y se indica en pantalla.

### Martingala / entradas escalonadas (desactivada por defecto)
Nivel 1 a mercado y niveles 2…N como **órdenes límite** en Fibonacci (50 / 61.8 / 78.6%) o en soportes/resistencias (pivotes), cada uno
el múltiplo del anterior (2x por defecto), con **un único Stop Loss** detrás del último nivel (SL/TP `closePosition`, cubren toda la posición).
Al ejecutarse un nivel se recalcula el precio medio y el Take Profit; el break-even (1R sobre el precio medio) cancela los niveles pendientes;
al cerrar se cancelan todos los pendientes.
**Riesgo**: el margen por operación pasa a ser el presupuesto TOTAL de todos los niveles y la pérdida con todos los niveles ejecutados + SL
nunca supera `MARTINGALA_RIESGO_MAX_USDT` (0 = el riesgo máximo normal). Por eso la 1ª entrada es más pequeña. Si no cabe (tamaño mínimo de Binance
≈ 0,002 BTC por orden, margen o riesgo), el bot **usa menos niveles o una sola entrada y lo avisa**. Con $100 / margen $20 / riesgo $3 no cabe nunca:
para 2 niveles hacen falta ≈ margen $100 y riesgo ≈ $9.

## 4. Gestión de riesgo (módulo pre-ejecución)

Antes de cada orden se calcula y valida (y se imprime el **Tablero de Diagnóstico Financiero** también al arrancar):
- Distancia de liquidación `= 1/apal − mantenimiento − 2×comisión` (9.5% con los valores por defecto) y precio de liquidación long/short.
- Pérdida real si salta el SL `= nominal×dist_SL + nominal×comisión×2`. Si supera el riesgo máximo → **reduce el tamaño** o **bloquea**.
- Si el SL queda más lejos que la liquidación → **bloquea**. Tras abrir, compara con el `liquidationPrice` real de Binance y cierra si fuera peor.
- Take Profit = `dist_SL × ratio R:B`. Break-even automático a 1:1 (SL a entrada +0.1%).

**Ejecución atómica**: entrada MARKET → inmediatamente `STOP_MARKET` y `TAKE_PROFIT_MARKET` (`reduceOnly`, precio de marca) en los servidores
de Binance mediante `/fapi/v1/algoOrder` (obligatorio desde 2025-12-09). Si el Stop Loss no se puede colocar → **cierre de emergencia**.
El bot repone SL/TP si alguien los cancela a mano, recupera su estado tras un reinicio y **no toca posiciones que no abrió**.
Una orden de entrada nunca se reintenta a ciegas (tras un timeout comprueba si se ejecutó).

## 5. Backtest

Pestaña **Backtest** del dashboard o por consola:
```
.venv\Scripts\python.exe backtest.py --desde 2022-01-01 --hasta 2026-10-01
```
Usa la **misma** `strategy.evaluate` y `risk.build_plan` que el bot en vivo. Reglas conservadoras: entrada en la apertura de la vela
siguiente, comisión taker + slippage, y si una vela toca SL y TP a la vez se asume SL. No incluye funding.

### Resultados del backtest (BTCUSDT, 2022-01-01 → 2026-10-01, parámetros por defecto)
- 41 307 velas de 1h evaluadas → 1 487 con zona de valor cumplida → **16 señales completas** → 10 operadas (6 bloqueadas por riesgo).
- 10 operaciones: 1 ganadora, 4 en break-even, 5 perdedoras; **−6.1 USDT** (−6.1%); *comprar y mantener* hizo +83.6%.
- **La muestra (≈3-4 señales/año) es demasiado pequeña para concluir nada**, pero tampoco aporta evidencia a favor.
  El cuello de botella es la Fase 3: de las velas con zona de valor cumplida, solo ~5% tienen agotamiento y ~13% tienen patrón,
  y coinciden en ~1.4%.
  Úsalo en **Testnet** y con el backtest para decidir si relajar algún filtro antes de arriesgar dinero.

## 6. Estructura

```
main.py · backtest.py · configurar_claves.py · iniciar_bot.bat
botbinance/
  config.py         parámetros (data/config.json)         secrets_store.py  claves (Credenciales de Windows)
  binance_api.py    cliente REST firmado                  data_feed.py      velas REST + WebSocket
  indicators.py     indicadores                           patterns.py       velas, divergencias, agotamiento
  strategy.py       fases 1-2-3, ZigZag/Fibonacci         risk.py           liquidación, SL/TP, tamaño, tablero
  executor.py       órdenes, break-even, reconciliación   engine.py         orquestación y control
  notifier.py       Telegram                              backtester.py     simulador
  dashboard/        servidor Flask + interfaz web
tests/              115 pruebas (pytest)
data/ logs/         configuración, estado, historial de operaciones, registros   (no se versionan)
```

Pruebas: `.venv\Scripts\python.exe -m pytest tests -q`

## 7. Seguridad del dashboard
Escucha solo en `127.0.0.1`, valida `Host`/`Origin` y un token por arranque en toda acción (anti-CSRF y anti DNS-rebinding),
no devuelve nunca las claves (solo los últimos 4 caracteres de la API Key) y no usa scripts de terceros.

## 8. Limitaciones conocidas
- Probado con datos reales de mercado y un exchange simulado; **las órdenes reales en Testnet/Real no se han podido probar sin tus claves**.
  Haz las primeras operaciones en Testnet y revisa que SL/TP aparezcan en Binance.
- Solo *One-way Mode* (si tu cuenta está en *Hedge Mode* el bot se niega a operar y te lo indica).
- Sin funding, sin gestión parcial de posición y una única posición a la vez.
