"""Client for the bundled Kanidm: its REST API and its admin unix socket (ADR-001).

Used by the IdP bootstrap and, later, by the `kanidm` IdentityAdmin adapter. The
caller passes the URL, the CA file and the credentials; nothing here reads settings.
"""

from atlas.kernel.identity.kanidm.admin_socket import AdminSocket
from atlas.kernel.identity.kanidm.client import (
    ENTRY_ATTRIBUTES,
    ApiTokenInfo,
    Entry,
    EntryKind,
    KanidmClient,
    ResetIntent,
    api_token_id,
)
from atlas.kernel.identity.kanidm.errors import (
    AuthFailure,
    KanidmAdminSocketError,
    KanidmAuthError,
    KanidmConflict,
    KanidmError,
    KanidmInvalidRequest,
    KanidmNotFound,
    KanidmPermissionDenied,
    KanidmProtocolError,
    KanidmServerError,
    KanidmUnavailable,
)

__all__ = [
    "ENTRY_ATTRIBUTES",
    "AdminSocket",
    "ApiTokenInfo",
    "AuthFailure",
    "Entry",
    "EntryKind",
    "KanidmAdminSocketError",
    "KanidmAuthError",
    "KanidmClient",
    "KanidmConflict",
    "KanidmError",
    "KanidmInvalidRequest",
    "KanidmNotFound",
    "KanidmPermissionDenied",
    "KanidmProtocolError",
    "KanidmServerError",
    "KanidmUnavailable",
    "ResetIntent",
    "api_token_id",
]
