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
#   ATLAS_IDP_PUBLIC_URL       bundled IdP only, e.g. https://id.example.com (own host)
#   ATLAS_RATE_LIMIT_IDP_AUTH  default 30r/m   (credential POSTs to the IdP, per client IP)
#   ATLAS_IDP_CA_FILE          default /etc/atlas-idp-ca/ca.pem (the internal CA certificate)
#
# Client-IP trust (ADR-003 §1): loopback is always trusted. With Docker's userland
# proxy, every connection to a published port arrives from the bridge gateway, so:
#   - loopback bind:     the auto-detected gateway is trusted too (the tunnel or
#                        reverse proxy must be on this host to reach the port);
#   - non-loopback bind: the gateway is NOT trusted implicitly; ATLAS_TRUSTED_PROXY_CIDRS
#                        must be set explicitly, or the proxy refuses to start.
#
# Bundled IdP (ADR-001; only when ATLAS_IDP_PUBLIC_URL is set): a virtual host for the
# IdP's own hostname that re-encrypts to https://idp:8443, verifying the certificate
# against the internal CA (atlas-idp-init writes it). X-Forwarded-For is overwritten
# with the verified client address: Kanidm reads the first entry, so it must never be
# a client-supplied chain.

set -euf  # -f: no globbing, so a "*" in a setting is never expanded

# ATLAS_PROXY_CONF_DIR exists so tests can render into a scratch directory.
CONF_DIR="${ATLAS_PROXY_CONF_DIR:-/etc/nginx/conf.d}"
OUT="$CONF_DIR/atlas.conf"
INC="$CONF_DIR/atlas-proxy.inc"
UPSTREAM="${ATLAS_API_UPSTREAM:-atlas-api:8000}"
IDP_INC="$CONF_DIR/atlas-idp-proxy.inc"
IDP_UPSTREAM="${ATLAS_IDP_UPSTREAM:-idp:8443}"
IDP_CA="${ATLAS_IDP_CA_FILE:-/etc/atlas-idp-ca/ca.pem}"
WEB_ROOT=/usr/share/atlas/web

die() {
    echo "atlas-proxy: refusing to start: $*" >&2
    exit 1
}

matches() {  # matches <value> <extended regex>
    printf '%s\n' "$1" | grep -Eq "$2"
}

# The config dir is a tmpfs that must belong to the nginx user (see docker-compose.yml).
if ! [ -d "$CONF_DIR" ] || ! [ -w "$CONF_DIR" ]; then
    die "cannot write $CONF_DIR as uid $(id -u); it must be a directory owned by that user" \
        "(atlas-proxy's tmpfs mounts set uid/gid 101)"
fi

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
host_re='^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'
matches "$host" "$host_re" || die "ATLAS_PUBLIC_URL has an invalid host name"

# --- Bundled IdP URL (optional) --------------------------------------------------
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
idp_url="${ATLAS_IDP_PUBLIC_URL:-}"
idp_host=""
if [ -n "$idp_url" ]; then
    case "$idp_url" in
        https://*) ;;
        *) die "ATLAS_IDP_PUBLIC_URL must be an https:// URL (the IdP is only served over HTTPS)" ;;
    esac
    idp_rest="${idp_url#https://}"
    idp_hostport="${idp_rest%%/*}"
    idp_path="${idp_rest#"$idp_hostport"}"
    case "$idp_path" in
        "" | /) ;;
        *) die "ATLAS_IDP_PUBLIC_URL must have no path: the IdP needs its own host name" ;;
    esac
    case "$idp_hostport" in
        *@*) die "ATLAS_IDP_PUBLIC_URL must not contain a user name or password" ;;
    esac
    idp_host="${idp_hostport%%:*}"
    matches "$idp_host" "$host_re" || die "ATLAS_IDP_PUBLIC_URL has an invalid host name"
    [ "$(lower "$idp_host")" != "$(lower "$host")" ] \
        || die "ATLAS_IDP_PUBLIC_URL must use a different host name than ATLAS_PUBLIC_URL" \
            "(the IdP needs its own origin; ADR-001)"
    [ -r "$IDP_CA" ] \
        || die "ATLAS_IDP_PUBLIC_URL is set but the internal CA certificate $IDP_CA is" \
            "missing (atlas-idp-init writes it; is the bundled-idp profile enabled?)"
fi

# --- Other settings ------------------------------------------------------------
header="${ATLAS_REAL_IP_HEADER:-X-Forwarded-For}"
matches "$header" '^[A-Za-z0-9-]+$' || die "ATLAS_REAL_IP_HEADER must be a header name"
body="${ATLAS_MAX_BODY_SIZE:-10m}"
matches "$body" '^[0-9]+[kKmMgG]?$' || die "ATLAS_MAX_BODY_SIZE must look like 10m"
rate_auth="${ATLAS_RATE_LIMIT_AUTH:-10r/m}"
matches "$rate_auth" '^[0-9]+r/[sm]$' || die "ATLAS_RATE_LIMIT_AUTH must look like 10r/m"
rate_api="${ATLAS_RATE_LIMIT_API:-20r/s}"
matches "$rate_api" '^[0-9]+r/[sm]$' || die "ATLAS_RATE_LIMIT_API must look like 20r/s"
rate_idp_auth="${ATLAS_RATE_LIMIT_IDP_AUTH:-30r/m}"
matches "$rate_idp_auth" '^[0-9]+r/[sm]$' \
    || die "ATLAS_RATE_LIMIT_IDP_AUTH must look like 30r/m"

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
    if [ -n "$idp_host" ]; then
        cat <<EOF

# --- Bundled IdP (ADR-001) ---------------------------------------------------------
upstream atlas_idp {
    server $IDP_UPSTREAM;
    keepalive 8;
}

# Credential submissions only (POST), per client IP: Kanidm's own soft-lock is per
# account, so this limits guessing across accounts from one address (ADR-001, Gap 1).
# Kanidm v1.12.0 routes: /ui/login/* (server/core/src/https/views/mod.rs:64-95, under
# /ui: https/mod.rs:316), /v1/auth and /v1/reauth (https/v1.rs:3451, 3454). Discovery, JWKS
# and the token endpoint stay unthrottled: Immich calls them server-to-server.
map \$request_method \$atlas_idp_auth_key {
    POST \$binary_remote_addr;
    default "";
}
limit_req_zone \$atlas_idp_auth_key zone=atlas_idp_auth:10m rate=$rate_idp_auth;

server {
    listen 8080;
    server_name $idp_host;
    access_log /dev/stdout atlas_json;

    location ^~ /ui/login/ {
        limit_req zone=atlas_idp_auth burst=15 nodelay;
        include $IDP_INC;
    }
    location = /v1/auth {
        limit_req zone=atlas_idp_auth burst=15 nodelay;
        include $IDP_INC;
    }
    location = /v1/reauth {
        limit_req zone=atlas_idp_auth burst=15 nodelay;
        include $IDP_INC;
    }
    location / {
        include $IDP_INC;
    }
}
EOF
    fi
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

if [ -n "$idp_host" ]; then
    cat > "$IDP_INC" <<EOF
# Re-encrypt to the bundled IdP and verify its certificate against the internal CA.
proxy_pass https://atlas_idp;
proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_set_header Host \$host;
proxy_set_header X-Forwarded-Proto https;
# Overwritten, never appended: Kanidm takes the FIRST X-Forwarded-For entry.
proxy_set_header X-Forwarded-For \$remote_addr;
proxy_ssl_verify on;
proxy_ssl_verify_depth 2;
proxy_ssl_trusted_certificate $IDP_CA;
proxy_ssl_name idp;
proxy_ssl_server_name on;
proxy_ssl_protocols TLSv1.2 TLSv1.3;
proxy_ssl_session_reuse on;
proxy_read_timeout 60s;
EOF
    [ -s "$IDP_INC" ] || die "could not write the nginx configuration to $CONF_DIR"
else
    rm -f "$IDP_INC"
fi

[ -s "$OUT" ] && [ -s "$INC" ] || die "could not write the nginx configuration to $CONF_DIR"

if [ -n "$idp_host" ]; then
    echo "atlas-proxy: serving $host and the IdP at $idp_host;" \
        "trusted peers:$(printf ' %s' $trusted)"
else
    echo "atlas-proxy: serving $host; trusted peers:$(printf ' %s' $trusted)"
fi
nginx -t -q || die "generated nginx configuration is invalid" \
    "${idp_host:+(with the IdP: is the idp service running? check COMPOSE_PROFILES)}"
