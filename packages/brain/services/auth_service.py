import grpc
from core.grpc_setup import brain_pb2
from .base import BaseServicerMixin
from users import create_user, authenticate_user
from crud import list_workspaces, create_workspace

class AuthServicerMixin(BaseServicerMixin):
    """Handles user authentication and workspace management."""

    async def CreateUser(self, request, context):
        try:
            data = await create_user(request.email, request.password, request.display_name)
            return brain_pb2.UserResponse(
                id=data["id"],
                email=data["email"],
                display_name=data["displayName"]
            )
        except Exception as e:
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            context.set_details(str(e))
            return brain_pb2.UserResponse()

    async def AuthenticateUser(self, request, context):
        try:
            data = await authenticate_user(request.email, request.password)
            workspaces = [
                brain_pb2.WorkspaceMembership(id=w["id"], role=w["role"])
                for w in data["workspaces"]
            ]
            return brain_pb2.AuthenticateUserResponse(
                id=data["id"],
                email=data["email"],
                display_name=data["displayName"],
                workspaces=workspaces
            )
        except ValueError as e:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details(str(e))
            return brain_pb2.AuthenticateUserResponse()

    async def ListWorkspaces(self, request, context):
        raw_workspaces = await list_workspaces(request.user_id)
        workspaces = [
            brain_pb2.WorkspaceResponse(
                id=w["id"],
                name=w["name"],
                role=w["role"],
                created_at=w["createdAt"]
            ) for w in raw_workspaces
        ]
        return brain_pb2.ListWorkspacesResponse(workspaces=workspaces)

    async def CreateWorkspace(self, request, context):
        data = await create_workspace(request.user_id, request.name)
        return brain_pb2.WorkspaceResponse(
            id=data["id"],
            name=data["name"],
            role=data["role"]
        )
