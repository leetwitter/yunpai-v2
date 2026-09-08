from __future__ import annotations

"""M1 (document intelligence) contract surface shared by registry, skills and gates.

The M1 module follows the same pattern as ``m3_m4_tooling``: a dedicated HTTP
adapter implements the transport semantics of the standalone M1 service (T8
history) on top of the current Tool contract, and the Skill operation map is the
single source of truth for ``yunpai-m1-document-parser`` dispatch.  No full M1
domain service is embedded into the orchestrator workers; production parsing
always happens on the independent M1 service reached over HTTP.
"""

M1_TOOL_NAMES = (
    "ingest_document",
    "ingest_m1_archive",
    "get_m1_task",
    "get_m1_batch",
    "get_m1_document",
    "search_m1_orders",
    "export_m1_order",
    "search_m1_documents",
    "list_m1_tasks",
    "list_m1_review_queue",
    "submit_m1_review",
    "generate_m1_report",
    "search_m1_knowledge",
    "list_m1_knowledge_entities",
    "get_m1_knowledge_entity",
    "get_m1_knowledge_graph",
    "get_m1_knowledge_stats",
)

#: Tools that only stage/parse a candidate locally and never pretend to be a
#: full production M1 parse.  In local transport these stay bound to fixture
#: handlers; in HTTP transport they are replaced by the real M1 adapter.
LOCAL_M1_FIXTURE_TOOLS = frozenset({"ingest_document"})

#: Every M1 tool in production HTTP transport must go through the dedicated M1
#: adapter (tenant header mapping, actor propagation, 202 polling, stable error
#: mapping and contract normalization).  Keeping this list explicit prevents the
#: generic adapter from silently binding M1 tools with wrong semantics.
M1_HTTP_ADAPTER_TOOL_NAMES = frozenset(M1_TOOL_NAMES)

#: M1 tools bound to the dedicated HTTP adapter in the default/local registry
#: (all M1 tools except the local fixture handler for ingest_document).
M1_ADAPTER_TOOL_NAMES = frozenset(
    name for name in M1_TOOL_NAMES if name not in LOCAL_M1_FIXTURE_TOOLS
)

M1_SKILL_OPERATION_MAP = {
    "default": "ingest_document",
    "parse": "ingest_document",
    "ingest": "ingest_document",
    "archive": "ingest_m1_archive",
    "task": "get_m1_task",
    "batch": "get_m1_batch",
    "document": "get_m1_document",
    "orders": "search_m1_orders",
    "export_order": "export_m1_order",
    "documents": "search_m1_documents",
    "tasks": "list_m1_tasks",
    "review_queue": "list_m1_review_queue",
    "review": "submit_m1_review",
    "report": "generate_m1_report",
    "knowledge_search": "search_m1_knowledge",
    "knowledge_entities": "list_m1_knowledge_entities",
    "knowledge_entity": "get_m1_knowledge_entity",
    "knowledge_graph": "get_m1_knowledge_graph",
    "knowledge_stats": "get_m1_knowledge_stats",
}

#: Read-only skill operations never open a pre-execution authorization Gate.
M1_READ_ONLY_SKILL_OPERATIONS = frozenset({
    "task",
    "batch",
    "document",
    "orders",
    "export_order",
    "documents",
    "tasks",
    "review_queue",
    "knowledge_search",
    "knowledge_entities",
    "knowledge_entity",
    "knowledge_graph",
    "knowledge_stats",
})

#: Side-effect skill operations (uploads/archive, human review, report
#: generation) follow the current Agent/Gate contract instead.
M1_WRITE_SKILL_OPERATIONS = frozenset(M1_SKILL_OPERATION_MAP) - M1_READ_ONLY_SKILL_OPERATIONS


def unique_tools(operation_map: dict[str, str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(operation_map.values()))


def bind_m1_http(registry, *, urls=None, headers_by_module=None, tool_names=None, overwrite=True) -> int:
    """Bind M1 tools to the dedicated HTTP adapter.

    Implemented in registry.py-friendly style: for every registered M1 spec with
    a resolvable module base URL, install a handler that speaks the standalone
    M1 HTTP contract (see module docstring).  Returns the number of handlers
    installed.
    """
    from .m1_http_adapter import bind_m1_http as _bind

    return _bind(
        registry,
        urls=urls,
        headers_by_module=headers_by_module,
        tool_names=tool_names,
        overwrite=overwrite,
    )
