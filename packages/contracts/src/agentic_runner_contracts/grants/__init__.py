"""The attenuation layer's Grant schema, Verb Catalogue and evaluator (ADR-0011 §1-2, §7).

Pure: standard library and pydantic only, no I/O and no ORM, so the Runner evaluates a
Grant snapshot locally at a verb seam (ADR-0011 §11) without importing the platform.
The *authority* over Grants — who may write one, and the API that serves a snapshot —
stays platform-side in its services; only the schema and the decision function
are shared (ADR-0013 §4).

A Protected Path (ADR-0011 §13): a PR touching this package always hits a human gate, so
an Agent can never loosen the layer that bounds it.
"""

from agentic_runner_contracts.grants.catalogue import (
    CATALOGUE_V1,
    CATALOGUE_V2,
    CATALOGUE_V3,
    CATALOGUE_V4,
    CATALOGUE_V5,
    CURRENT_CATALOGUE_VERSION,
    MCP_RESOURCE_TYPE_PREFIX,
    UnknownCatalogueVersionError,
    VerbCatalogue,
    catalogue_for,
    is_mcp_resource_type,
    mcp_resource_type,
)
from agentic_runner_contracts.grants.evaluator import (
    EffectiveDecision,
    LinkDecision,
    Resource,
    decide_link,
    evaluate_effective_grant,
)
from agentic_runner_contracts.grants.model import (
    EMPTY_GRANT,
    Decision,
    Grant,
    GrantEntry,
    GrantLink,
    GrantRefusedError,
    expand_and_validate_grant,
)
from agentic_runner_contracts.grants.seam import (
    McpServerDecision,
    VerbDecision,
    decide_mcp_server,
    decide_verb,
)
from agentic_runner_contracts.grants.snapshot import (
    CHANNEL_READ_VERB,
    CHANNEL_RESOURCE_TYPE,
    CHANNEL_WRITE_VERB,
    DEFAULT_REQUIRED_HUMAN_APPROVALS,
    PR_COMMENT_VERB,
    PR_MERGE_VERB,
    PR_OPEN_VERB,
    PR_REVIEW_VERB,
    PUSH_VERB,
    REPO_RESOURCE_TYPE,
    UNENFORCED_SNAPSHOT,
    GrantSnapshot,
    ProductReach,
    ResourceRegistration,
)

__all__ = [
    "CATALOGUE_V1",
    "CATALOGUE_V2",
    "CATALOGUE_V3",
    "CATALOGUE_V4",
    "CATALOGUE_V5",
    "CHANNEL_READ_VERB",
    "CHANNEL_RESOURCE_TYPE",
    "CHANNEL_WRITE_VERB",
    "CURRENT_CATALOGUE_VERSION",
    "DEFAULT_REQUIRED_HUMAN_APPROVALS",
    "EMPTY_GRANT",
    "MCP_RESOURCE_TYPE_PREFIX",
    "PR_COMMENT_VERB",
    "PR_MERGE_VERB",
    "PR_OPEN_VERB",
    "PR_REVIEW_VERB",
    "PUSH_VERB",
    "REPO_RESOURCE_TYPE",
    "UNENFORCED_SNAPSHOT",
    "Decision",
    "EffectiveDecision",
    "Grant",
    "GrantEntry",
    "GrantLink",
    "GrantRefusedError",
    "GrantSnapshot",
    "LinkDecision",
    "McpServerDecision",
    "ProductReach",
    "Resource",
    "ResourceRegistration",
    "UnknownCatalogueVersionError",
    "VerbDecision",
    "VerbCatalogue",
    "catalogue_for",
    "decide_link",
    "decide_mcp_server",
    "decide_verb",
    "evaluate_effective_grant",
    "expand_and_validate_grant",
    "is_mcp_resource_type",
    "mcp_resource_type",
]
