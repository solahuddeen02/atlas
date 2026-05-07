const ROLE_RANK = { member: 0, admin: 1, owner: 2 }

export function decodeToken(token) {
  try {
    return JSON.parse(atob(token.split(".")[1]))
  } catch {
    return null
  }
}

export function hasRole(userRole, minRole) {
  return (ROLE_RANK[userRole] ?? -1) >= (ROLE_RANK[minRole] ?? 0)
}
