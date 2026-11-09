"""Organization tool authoring shares the REST contract and requires a human handoff."""

from api.db import db_client
from api.schemas.tool import CreateToolRequest, UpdateToolRequest
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.tool_management import validate_tool_references
from api.services.workflow.builder_runtime.tool_schemas import tool_request_schema


def tool_revision(tool):
    return fingerprint(
        {
            k: getattr(tool, k)
            for k in (
                "name",
                "description",
                "definition",
                "category",
                "status",
                "icon",
                "icon_color",
            )
        }
    )


def organization_specs(tool):
    return [
        tool(
            "read_organization_tools",
            "List reusable tools, or read one scoped tool before proposing changes.",
            {"tool_uuid": {"type": "string"}},
        ),
        tool(
            "get_tool_definition_schema",
            "Read the authoritative schema before proposing a reusable tool.",
            {
                "category": {
                    "enum": [
                        "http_api",
                        "end_call",
                        "transfer_call",
                        "transfer_agent",
                        "calculator",
                        "mcp",
                    ]
                },
                "operation": {"enum": ["create", "update"]},
                "destination_source": {
                    "enum": ["static", "dynamic", "context_mapping"]
                },
            },
            ["category"],
        ),
        tool(
            "propose_tool_change",
            "Propose creating/updating a SHARED organization tool. "
            "Pauses for explicit human approval; never creates or updates immediately. "
            "Read existing tools and the authoritative schema first. "
            "Use existing credential references; never invent endpoints or secrets. "
            "A rejection is not permission to retry a mutation.",
            {
                "operation": {"enum": ["create", "update"]},
                "tool_uuid": {"type": "string"},
                "request": {"type": "object", "additionalProperties": True},
                "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            ["operation", "request", "reason"],
        ),
    ]


class OrganizationTools:
    async def read_organization_tools(self, tool_uuid=None):
        if tool_uuid:
            row = await db_client.get_tool_by_uuid(tool_uuid, self.org)
            if not row:
                raise WorkbenchError("Tool not found", 404)
            return {
                "tool_uuid": row.tool_uuid,
                "revision": tool_revision(row),
                "name": row.name,
                "category": row.category,
                "definition": row.definition,
                "description": row.description,
                "status": row.status,
            }
        rows = await db_client.get_tools_for_organization(self.org)
        return [
            {
                "tool_uuid": r.tool_uuid,
                "name": r.name,
                "category": r.category,
                "description": r.description,
            }
            for r in rows
        ]

    async def get_tool_definition_schema(
        self, category, operation="create", destination_source="static"
    ):
        return tool_request_schema(category, operation, destination_source)

    async def propose_tool_change(self, operation, request, reason, tool_uuid=None):
        if (
            not getattr(self, "orchestration", None)
            or self.intent != "build"
            or self.tuning
        ):
            raise WorkbenchError("Tool changes require an interactive build task", 409)
        model = CreateToolRequest if operation == "create" else UpdateToolRequest
        parsed = model.model_validate(request)
        if operation == "create" and tool_uuid:
            raise WorkbenchError("Create does not accept an existing tool UUID", 422)
        proposal = {
            "operation": operation,
            "request": parsed.model_dump(mode="json", exclude_unset=True),
            "reason": reason,
        }
        if operation == "update":
            existing = await db_client.get_tool_by_uuid(tool_uuid, self.org)
            if not existing:
                raise WorkbenchError("Tool not found", 404)
            if not request or (
                parsed.definition and parsed.definition.type != existing.category
            ):
                raise WorkbenchError(
                    "Provide changes without changing the tool category", 422
                )
            proposal.update(
                tool_uuid=tool_uuid,
                tool_revision=tool_revision(existing),
                before={
                    "name": existing.name,
                    "description": existing.description,
                    "definition": existing.definition,
                    "status": existing.status,
                },
            )
        if parsed.definition:
            await validate_tool_references(
                parsed.definition.model_dump(), organization_id=self.org
            )
        approval = await self.orchestration.request("tool_change", proposal)
        if approval["status"] == "rejected":
            return {"approved": False, "feedback": approval["response"]["feedback"]}
        await self.orchestration.apply_approved_tools()
        return self.orchestration.data["steps"]["approved-tool:" + approval["id"]]
