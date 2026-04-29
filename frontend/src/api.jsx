export function authFetch(url, options = {}) {
    const token = localStorage.getItem("token")
    return fetch(url, {
        ...options,
        headers: {
            ...options.headers,
            Authorization: `Bearer ${token}`,
        },
    })
}