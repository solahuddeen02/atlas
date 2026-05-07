export const API_URL      = import.meta.env.VITE_API_URL      ?? "http://127.0.0.1:8000"
export const BASE_DOMAIN  = import.meta.env.VITE_BASE_DOMAIN  ?? "atlas.local"

/** Returns the subdomain part, or null if on the root domain. */
export function getSubdomain() {
    const host = window.location.hostname          // "drive.atlas.local"
    if (host === BASE_DOMAIN || host === `www.${BASE_DOMAIN}`) return null
    if (host === "localhost" || host === "127.0.0.1") return null
    // strip ".<BASE_DOMAIN>" from the end
    const suffix = `.${BASE_DOMAIN}`
    if (host.endsWith(suffix)) return host.slice(0, -suffix.length)
    return null
}

/** Build a subdomain URL, e.g. subdomainUrl("drive") → "http://drive.atlas.local" */
export function subdomainUrl(sub) {
    const { protocol } = window.location
    return `${protocol}//${sub}.${BASE_DOMAIN}`
}
