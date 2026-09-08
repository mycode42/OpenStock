#!/bin/bash
# OpenStock Monitoring Script using Yahoo Finance
# Reads stock config from database, fetches prices from Yahoo Finance, evaluates triggers, sends Telegram

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="/home/ubuntu/openstock/memory/monitor.log"
ERROR_LOG="/home/ubuntu/openstock/memory/monitor-error.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
errlog() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" | tee -a "$ERROR_LOG" >> "$LOG_FILE"; }

# Load env
if [ -f "$APP_DIR/.env" ]; then
    set -a
    source "$APP_DIR/.env"
    set +a
fi

DB_PATH="${DATABASE_PATH:-$APP_DIR/database/openstock.db}"
if [ ! -f "$DB_PATH" ]; then
    errlog "Database not found: $DB_PATH"
    exit 2
fi

EXCHANGE_RATE=""
CACHE_FILE="/home/ubuntu/openstock/memory/exchange_rate_cache.json"
CACHE_TTL=${EXCHANGE_RATE_CACHE_SECONDS:-3600}

# Get exchange rate (USD/EUR) with caching
get_exchange_rate() {
    local now=$(date +%s)
    
    # Check cache
    if [ -f "$CACHE_FILE" ]; then
        local cached_rate=$(jq -r '.rate' "$CACHE_FILE" 2>/dev/null)
        local cached_time=$(jq -r '.timestamp' "$CACHE_FILE" 2>/dev/null)
        if [ -n "$cached_rate" ] && [ -n "$cached_time" ]; then
            local age=$((now - cached_time))
            if [ "$age" -lt "$CACHE_TTL" ]; then
                echo "$cached_rate"
                return 0
            fi
        fi
    fi
    
    # Fetch from exchangerate-api.com
    local response=$(curl -sf --max-time 10 "https://api.exchangerate-api.com/v4/latest/USD" 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$response" ]; then
        errlog "Failed to fetch exchange rate from API"
        return 1
    fi
    
    local rate=$(echo "$response" | jq -r '.rates.EUR' 2>/dev/null)
    if [ -z "$rate" ] || [ "$rate" = "null" ]; then
        errlog "Invalid exchange rate response: $response"
        return 1
    fi
    
    # Cache
    echo "{\"rate\":$rate,\"timestamp\":$now}" > "$CACHE_FILE" 2>/dev/null
    echo "$rate"
    return 0
}

# Send Telegram notification
send_telegram() {
    local message="$1"
    local chat_id="${2:-${TELEGRAM_CHAT_ID:-}}"
    
    if [ -z "$chat_id" ]; then
        # Get chat_id from owner allowlist
        chat_id=$(cat /home/ubuntu/.openclaw/credentials/telegram-default-allowFrom.json 2>/dev/null | jq -r '.allowFrom[0]' 2>/dev/null)
    fi
    
    if [ -z "$chat_id" ] || [ "$chat_id" = "null" ]; then
        errlog "No Telegram chat_id configured"
        return 1
    fi
    
    local response=$(curl -sf --max-time 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$chat_id" \
        -d text="$message" \
        -d parse_mode="HTML" 2>/dev/null)
    
    if [ $? -ne 0 ]; then
        errlog "Telegram send failed: $response"
        return 1
    fi
    
    echo "$response" | jq -r '.result.message_id // "sent"' 2>/dev/null || echo "sent"
    return 0
}

# Fetch stock price from Yahoo Finance
get_stock_price() {
    local symbol="$1"
    local response=$(curl -sf --max-time 10 \
        -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36" \
        -H "Accept: application/json" \
        -H "Cache-Control: no-cache" \
        -H "Pragma: no-cache" \
        "https://query1.finance.yahoo.com/v8/finance/chart/$symbol?range=1d&interval=1m&includePrePost=true&corsDomain=finance.yahoo.com&_=$(date +%s)" 2>/dev/null)
    
    if [ $? -ne 0 ] || [ -z "$response" ]; then
        errlog "Failed to fetch price for $symbol"
        return 1
    fi
    
    # Extract data using Python for better parsing
    local python_script="
import sys, json
try:
    data = json.loads(sys.stdin.read())
    result = data.get('chart', {}).get('result', [])
    if not result:
        print('NO_DATA')
        sys.exit(1)
    
    chart = result[0]
    meta = result[0].get('meta', {})
    quote = chart.get('indicators', {}).get('quote', [{}])[0]
    closes = quote.get('close', []) or []
    price = None
    quote_time = None
    for index in range(len(closes) - 1, -1, -1):
        value = closes[index]
        if value is not None:
            price = value
            timestamps = chart.get('timestamp', []) or []
            if index < len(timestamps):
                quote_time = timestamps[index]
            break
    periods = meta.get('currentTradingPeriod', {}) or {}
    session = 'extended'
    if quote_time is not None:
        for candidate in ('pre', 'regular', 'post'):
            period = periods.get(candidate, {}) or {}
            if period.get('start', float('inf')) <= quote_time <= period.get('end', float('-inf')):
                session = candidate
                break
    source = f'yahoo_{session}_close'
    if price is None:
        price = meta.get('regularMarketPrice')
        source = 'yahoo_regular_market_price'
    name = meta.get('shortName') or meta.get('longName') or ''

    if price:
        print(f'PRICE={price}')
        print(f'SOURCE={source}')
        if quote_time is not None:
            print(f'QUOTE_TIME={quote_time}')
        print(f'NAME={name}')
    else:
        print('NO_DATA')
        sys.exit(1)
except Exception as e:
    print(f'ERROR: {str(e)}')
    sys.exit(1)
"
    
    local price_data=$(echo "$response" | python3 -c "$python_script" 2>/dev/null)
    if [ $? -ne 0 ]; then
        errlog "Failed to parse Yahoo Finance response for $symbol: $price_data"
        return 1
    fi
    
    echo "$price_data"
    return 0
}

# Main monitoring cycle
log "=== Monitoring cycle started ==="

# Get exchange rate
EXCHANGE_RATE=$(get_exchange_rate)
if [ -z "$EXCHANGE_RATE" ]; then
    errlog "Could not get exchange rate, skipping cycle"
    exit 3
fi
log "USD/EUR rate: $EXCHANGE_RATE"

# Get enabled stocks from DB
STOCKS=$(sqlite3 "$DB_PATH" "SELECT id,symbol,buy_threshold_eur,sell_threshold_eur,trigger_state FROM stocks WHERE enabled=1 ORDER BY symbol" 2>/dev/null)
if [ -z "$STOCKS" ]; then
    log "No enabled stocks"
    exit 0
fi

# Resolve the Telegram recipient. A missing notification target must not stop
# price collection or trigger-state updates; the delivery failure is logged.
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
if [ -z "$CHAT_ID" ] || [ "$CHAT_ID" = "null" ]; then
    CHAT_ID=$(jq -r '.allowFrom[0] // empty' /home/ubuntu/.openclaw/credentials/telegram-default-allowFrom.json 2>/dev/null || true)
fi
if [ -z "$CHAT_ID" ] || [ "$CHAT_ID" = "null" ]; then
    errlog "No Telegram recipient configured; monitoring will continue without trigger notifications"
    CHAT_ID=""
fi

# Process each stock
while IFS='|' read -r id symbol buy_threshold sell_threshold current_trigger; do
    log "Processing $symbol (id=$id)"
    
    # Get price
    PRICE_DATA=$(get_stock_price "$symbol")
    if [ $? -ne 0 ]; then
        log "Skipping $symbol due to API error"
        continue
    fi
    
    USD_PRICE=$(echo "$PRICE_DATA" | grep "^PRICE=" | cut -d= -f2)
    PRICE_SOURCE=$(echo "$PRICE_DATA" | grep "^SOURCE=" | cut -d= -f2)
    QUOTE_TIME=$(echo "$PRICE_DATA" | grep "^QUOTE_TIME=" | cut -d= -f2)
    STOCK_NAME=$(echo "$PRICE_DATA" | grep "^NAME=" | cut -d= -f2)
    
    if [ -z "$USD_PRICE" ]; then
        log "No price for $symbol, skipping"
        continue
    fi
    
    # Calculate EUR price
    EUR_PRICE=$(echo "$USD_PRICE $EXCHANGE_RATE" | awk '{printf "%.4f", $1 * $2}')
    if [ -n "$QUOTE_TIME" ]; then
        QUOTE_TIME_HUMAN=$(date -d "@$QUOTE_TIME" '+%Y-%m-%d %H:%M:%S %Z' 2>/dev/null || echo "$QUOTE_TIME")
    else
        QUOTE_TIME_HUMAN="unknown"
    fi
    log "$symbol [$PRICE_SOURCE @ $QUOTE_TIME_HUMAN]: \$${USD_PRICE} → €${EUR_PRICE} (buy=€${buy_threshold}, sell=€${sell_threshold}, state=${current_trigger})"
    
    # Update database
    sqlite3 "$DB_PATH" "UPDATE stocks SET current_price_usd=$USD_PRICE, current_price_eur=$EUR_PRICE, last_price_update=datetime('now'), last_exchange_rate=$EXCHANGE_RATE, updated_at=datetime('now') WHERE id=$id" 2>/dev/null

    # Keep an immutable local history. A unique (stock_id, recorded_at) pair
    # makes simultaneous timer/manual refreshes idempotent at second precision.
    if [[ "$QUOTE_TIME" =~ ^[0-9]+$ ]]; then
        SNAPSHOT_QUOTE_TIME=$(date -u -d "@$QUOTE_TIME" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || true)
    else
        SNAPSHOT_QUOTE_TIME=""
    fi
    SNAPSHOT_QUOTE_SQL="NULL"
    if [ -n "$SNAPSHOT_QUOTE_TIME" ]; then
        SNAPSHOT_QUOTE_SQL="'$SNAPSHOT_QUOTE_TIME'"
    fi
    case "$PRICE_SOURCE" in
        yahoo_pre_close|yahoo_regular_close|yahoo_post_close|yahoo_extended_close|yahoo_regular_market_price) ;;
        *) PRICE_SOURCE="unknown" ;;
    esac
    sqlite3 "$DB_PATH" "INSERT INTO price_history (stock_id, price_usd, price_eur, exchange_rate, source, quote_time, recorded_at) VALUES ($id, $USD_PRICE, $EUR_PRICE, $EXCHANGE_RATE, '$PRICE_SOURCE', $SNAPSHOT_QUOTE_SQL, datetime('now')) ON CONFLICT(stock_id, recorded_at) DO UPDATE SET price_usd=excluded.price_usd, price_eur=excluded.price_eur, exchange_rate=excluded.exchange_rate, source=excluded.source, quote_time=excluded.quote_time" 2>/dev/null
    
    # Update name if empty
    if [ -n "$STOCK_NAME" ] && [ "$STOCK_NAME" != "null" ]; then
        sqlite3 "$DB_PATH" "UPDATE stocks SET name='$STOCK_NAME' WHERE id=$id AND (name IS NULL OR name='')" 2>/dev/null
    fi
    
    # Evaluate triggers
    NEW_TRIGGER="NONE"
    
    # Buy trigger: EUR price <= buy threshold
    if [ "$(echo "$EUR_PRICE $buy_threshold" | awk '{print ($1 <= $2) ? 1 : 0}')" = "1" ]; then
        NEW_TRIGGER="BUY_TRIGGERED"
    fi
    
    # Sell trigger: EUR price >= sell threshold
    if [ "$(echo "$EUR_PRICE $sell_threshold" | awk '{print ($1 >= $2) ? 1 : 0}')" = "1" ]; then
        NEW_TRIGGER="SELL_TRIGGERED"
    fi
    
    # Debouncing: only fire alert on state transition INTO trigger zone
    if [ "$NEW_TRIGGER" != "$current_trigger" ]; then
        sqlite3 "$DB_PATH" "UPDATE stocks SET trigger_state='$NEW_TRIGGER', updated_at=datetime('now') WHERE id=$id" 2>/dev/null
        
        # Send alert only when NEW_TRIGGER != NONE and transitioning INTO trigger
        if [ "$NEW_TRIGGER" != "NONE" ]; then
            log "TRIGGER: $symbol → $NEW_TRIGGER"
            
            # Format message
            if [ "$NEW_TRIGGER" = "BUY_TRIGGERED" ]; then
                MSG="🔼 <b>BUY TRIGGER</b>%0A%0A<b>$STOCK_NAME ($symbol)</b>%0ACurrent price: €$(printf '%.2f' $EUR_PRICE)%0ABuy target: €$(printf '%.2f' $buy_threshold)%0ASell target: €$(printf '%.2f' $sell_threshold)%0AExchange rate: $EXCHANGE_RATE%0ATime: $(date '+%Y-%m-%d %H:%M')"
            else
                MSG="🔽 <b>SELL TRIGGER</b>%0A%0A<b>$STOCK_NAME ($symbol)</b>%0ACurrent price: €$(printf '%.2f' $EUR_PRICE)%0ABuy target: €$(printf '%.2f' $buy_threshold)%0ASell target: €$(printf '%.2f' $sell_threshold)%0AExchange rate: $EXCHANGE_RATE%0ATime: $(date '+%Y-%m-%d %H:%M')"
            fi
            
            RESULT=$(send_telegram "$MSG" "$CHAT_ID")
            if [ $? -eq 0 ]; then
                ALERT_TYPE="${NEW_TRIGGER%_TRIGGERED}"
                sqlite3 "$DB_PATH" "INSERT INTO alerts_log (stock_id, alert_type, price_eur, threshold_eur, telegram_message_id) VALUES ($id, '$ALERT_TYPE', $EUR_PRICE, $([ "$ALERT_TYPE" = "BUY" ] && echo $buy_threshold || echo $sell_threshold), '$RESULT')" 2>/dev/null
                log "Telegram sent: $RESULT"
            else
                errlog "Failed to send Telegram for $symbol"
            fi
        else
            log "Trigger reset for $symbol"
        fi
    fi
    
done <<< "$STOCKS"

log "=== Monitoring cycle complete ==="
exit 0
