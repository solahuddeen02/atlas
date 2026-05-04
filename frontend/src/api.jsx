export async function authFetch(url, options = {}) {
    const token = localStorage.getItem("token")
    const res = await fetch(url, {
        ...options,
        headers: {
            ...options.headers,
            Authorization: `Bearer ${token}`,
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
