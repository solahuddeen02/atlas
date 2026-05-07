export async function authFetch(url, options = {}) {
    const res = await fetch(url, {
        ...options,
        credentials: "include",   // send cookie on every request
        headers: {
            ...options.headers,
        },
    })
    if (!res.ok) {
        let detail = res.statusText
        try {
            const body = await res.json()
            detail = body.detail ?? detail
        } catch (_) {}
        throw new Error(detail)
    }
    return res
}
