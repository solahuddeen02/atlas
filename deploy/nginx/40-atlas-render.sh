#!/bin/sh
# atlas-proxy: render /etc/nginx/conf.d/atlas.conf from the environment, then `nginx -t`.
# Runs from the nginx image's /docker-entrypoint.d/ before nginx starts. Any error
# stops the container with a message that names the setting (ADR-003).
#
# Inputs (all from deploy/.env via docker-compose.yml):
#   ATLAS_PUBLIC_URL           https://atlas.example.com   (required)
#   ATLAS_BIND_ADDRESS         host address the port is published on (default 127.0.0.1)
#   ATLAS_TRUSTED_PROXY_CIDRS  peers allowed to set ATLAS_REAL_IP_HEADER
#   ATLAS_REAL_IP_HEADER       default X-Forwarded-For
#   ATLAS_MAX_BODY_SIZE        default 10m
#   ATLAS_RATE_LIMIT_AUTH      default 10r/m   (/api/auth/)
#   ATLAS_RATE_LIMIT_API       default 20r/s   (/api/)
#
# Client-IP trust (ADR-003 §1): loopback is always trusted. With Docker's userland
# proxy, every connection to a published port arrives from the bridge gateway, so:
#   - loopback bind:     the auto-detected gateway is trusted too (the tunnel or
#                        reverse proxy must be on this host to reach the port);
#   - non-loopback bind: the gateway is NOT trusted implicitly; ATLAS_TRUSTED_PROXY_CIDRS
#                        must be set explicitly, or the proxy refuses to start.
# TODO(spike-s): the IdP virtual host for ATLAS_IDP_PUBLIC_URL, with re-encryption to
# the bundled IdP verified against the internal CA.

set -euf  # -f: no globbing, so a "*" in a setting is never expanded

# ATLAS_PROXY_CONF_DIR exists so tests can render into a scratch directory.
CONF_DIR="${ATLAS_PROXY_CONF_DIR:-/etc/nginx/conf.d}"
OUT="$CONF_DIR/atlas.conf"
INC="$CONF_DIR/atlas-proxy.inc"
UPSTREAM="${ATLAS_API_UPSTREAM:-atlas-api:8000}"
WEB_ROOT=/usr/share/atlas/web

die() {
    echo "atlas-proxy: refusing to start: $*" >&2
    exit 1
}

matches() {  # matches <value> <extended regex>
    printf '%s\n' "$1" | grep -Eq "$2"
}

# --- Public URL ----------------------------------------------------------------
url="${ATLAS_PUBLIC_URL:-}"
[ -n "$url" ] || die "ATLAS_PUBLIC_URL is not set (set it in deploy/.env)"
case "$url" in
    http://* | https://*) ;;
    *) die "ATLAS_PUBLIC_URL must be an absolute http(s) URL" ;;
esac
scheme="${url%%://*}"
rest="${url#*://}"
hostport="${rest%%/*}"
host="${hostport%%:*}"
matches "$host" '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$' \
    || die "ATLAS_PUBLIC_URL has an invalid host name"

# --- Other settings ------------------------------------------------------------
header="${ATLAS_REAL_IP_HEADER:-X-Forwarded-For}"
matches "$header" '^[A-Za-z0-9-]+$' || die "ATLAS_REAL_IP_HEADER must be a header name"
body="${ATLAS_MAX_BODY_SIZE:-10m}"
matches "$body" '^[0-9]+[kKmMgG]?$' || die "ATLAS_MAX_BODY_SIZE must look like 10m"
rate_auth="${ATLAS_RATE_LIMIT_AUTH:-10r/m}"
matches "$rate_auth" '^[0-9]+r/[sm]$' || die "ATLAS_RATE_LIMIT_AUTH must look like 10r/m"
rate_api="${ATLAS_RATE_LIMIT_API:-20r/s}"
matches "$rate_api" '^[0-9]+r/[sm]$' || die "ATLAS_RATE_LIMIT_API must look like 20r/s"

# --- Trusted peers -------------------------------------------------------------
bind="${ATLAS_BIND_ADDRESS:-127.0.0.1}"
case "$bind" in
    127.* | ::1 | "[::1]" | localhost) loopback_bind=1 ;;
    *) loopback_bind=0 ;;
esac

ipv4_cidr='^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$'
ipv6_cidr='^[0-9A-Fa-f:]*:[0-9A-Fa-f:]*(/[0-9]{1,3})?$'
explicit=""
for cidr in $(printf '%s' "${ATLAS_TRUSTED_PROXY_CIDRS:-}" | tr ',' ' '); do
    if matches "$cidr" "$ipv4_cidr" || matches "$cidr" "$ipv6_cidr"; then
        explicit="$explicit $cidr"
    else
        die "ATLAS_TRUSTED_PROXY_CIDRS has an invalid entry: $cidr"
    fi
done

trusted="127.0.0.1/32 ::1/128"
if [ "$loopback_bind" = 1 ]; then
    # Default IPv4 route of this container, from /proc (no extra tools needed).
    gw_hex="$(awk '$2 == "00000000" { print $3; exit }' /proc/net/route)"
    if [ -n "$gw_hex" ]; then
        gateway="$(printf '%d.%d.%d.%d' \
            "0x$(echo "$gw_hex" | cut -c7-8)" "0x$(echo "$gw_hex" | cut -c5-6)" \
            "0x$(echo "$gw_hex" | cut -c3-4)" "0x$(echo "$gw_hex" | cut -c1-2)")"
        trusted="$trusted $gateway/32"
    fi
elif [ -z "$explicit" ]; then
    die "ATLAS_BIND_ADDRESS=$bind is not loopback, so the Docker gateway is not trusted" \
        "implicitly. Set ATLAS_TRUSTED_PROXY_CIDRS to the address(es) of the proxy in front" \
        "of Atlas. Note: with Docker's userland proxy, connections to a published port" \
        "arrive from the bridge gateway, so only list it if you mean to trust everything" \
        "that can reach $bind:${ATLAS_HTTP_PORT:-8080}."
fi
trusted="$trusted$explicit"

# --- Render ----------------------------------------------------------------------
{
    cat <<EOF
# Generated at container start by 40-atlas-render.sh. Do not edit.
server_tokens off;
client_max_body_size $body;

log_format atlas_json escape=json '{"time":"\$time_iso8601","client":"\$remote_addr",'
    '"peer":"\$realip_remote_addr","host":"\$host","method":"\$request_method",'
    '"uri":"\$uri","status":\$status,"bytes":\$body_bytes_sent,"rt":\$request_time}';

# Client IP (ADR-003 §1): taken from $header only when the TCP peer is trusted.
real_ip_header $header;
real_ip_recursive on;
EOF
    for cidr in $trusted; do echo "set_real_ip_from $cidr;"; done
    echo
    echo "# The TCP peer itself (not a forwarded address) is what decides trust."
    echo "geo \$realip_remote_addr \$atlas_trusted_peer {"
    echo "    default 0;"
    for cidr in $trusted; do echo "    $cidr 1;"; done
    echo "}"
    cat <<EOF

limit_req_zone \$binary_remote_addr zone=atlas_auth:10m rate=$rate_auth;
limit_req_zone \$binary_remote_addr zone=atlas_api:10m rate=$rate_api;
limit_req_status 429;

upstream atlas_api {
    server $UPSTREAM;
    keepalive 16;
}

# Unknown Host: drop the connection (444). /health still answers, because a
# loopback check like \`curl http://127.0.0.1:8080/health\` sends Host: 127.0.0.1
# and the body is exactly {"status":"ok"}.
server {
    listen 8080 default_server;
    server_name _;
    access_log /dev/stdout atlas_json;

    location = /health {
        include $INC;
    }
    location / {
        return 444;
    }
}

server {
    listen 8080;
    server_name $host;
    access_log /dev/stdout atlas_json;

    add_header Content-Security-Policy "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=()" always;
    add_header X-Frame-Options "DENY" always;
EOF
    if [ "$scheme" = https ]; then
        echo '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'
    fi
    cat <<EOF

    location = /health {
        include $INC;
    }
    # Readiness only for trusted peers (decided on the TCP peer, not a header).
    location = /health/ready {
        if (\$atlas_trusted_peer = 0) {
            return 403;
        }
        include $INC;
    }
    location /api/auth/ {
        limit_req zone=atlas_auth burst=5 nodelay;
        include $INC;
    }
    location /api/ {
        limit_req zone=atlas_api burst=40 nodelay;
        include $INC;
    }
    # The API serves these only outside production (ADR-004 §4); 404 otherwise.
    location ~ ^/(docs|redoc|openapi\.json)$ {
        include $INC;
    }
    # The SPA (Phase 7). Until then, a placeholder page.
    location / {
        root $WEB_ROOT;
        try_files \$uri \$uri/ /index.html;
    }
}
EOF
} > "$OUT"

cat > "$INC" <<EOF
proxy_pass http://atlas_api;
proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_set_header Host \$host;
# One overwritten client address: the app never sees a client-supplied chain.
proxy_set_header X-Real-IP \$remote_addr;
proxy_set_header X-Forwarded-For \$remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host \$host;
proxy_read_timeout 60s;
EOF

echo "atlas-proxy: serving $host; trusted peers:$(printf ' %s' $trusted)"
nginx -t -q || die "generated nginx configuration is invalid"
