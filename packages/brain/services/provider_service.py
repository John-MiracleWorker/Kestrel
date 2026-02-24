from provider_config import ProviderConfig
import grpc
from core.grpc_setup import brain_pb2
from .base import BaseServicerMixin
from providers_registry import list_provider_configs, set_provider_config, delete_provider_config
from core.config import load_tool_catalog, logger

class ProviderServicerMixin(BaseServicerMixin):
    async def ListProviderConfigs(self, request, context):
        rows = await list_provider_configs(request.workspace_id)
        configs = []
        for row in rows:
            configs.append(brain_pb2.ProviderConfig(
                workspace_id=str(row['workspace_id']),
                provider=row['provider'],
                model=row['model'] or "",
                temperature=row['temperature'],
                max_tokens=row['max_tokens'],
                system_prompt=row['system_prompt'] or "",
                rag_enabled=row['rag_enabled'],
                rag_top_k=row['rag_top_k'],
                rag_min_similarity=row['rag_min_similarity'],
                is_default=row['is_default'],
                api_key_encrypted="***" if row['api_key_encrypted'] else "",
                created_at=row['created_at'].isoformat(),
                updated_at=row['updated_at'].isoformat()
            ))
        return brain_pb2.ListProviderConfigsResponse(configs=configs)

    async def SetProviderConfig(self, request, context):
        config_dict = {
            'model': request.model,
            'temperature': request.temperature,
            'max_tokens': request.max_tokens,
            'system_prompt': request.system_prompt,
            'rag_enabled': request.rag_enabled,
            'rag_top_k': request.rag_top_k,
            'rag_min_similarity': request.rag_min_similarity,
            'is_default': request.is_default,
        }
        if request.api_key_encrypted:
            from encryption import encrypt
            config_dict['api_key_encrypted'] = encrypt(request.api_key_encrypted)
            
        row = await set_provider_config(request.workspace_id, request.provider, config_dict)
        
        return brain_pb2.SetProviderConfigResponse(
            config=brain_pb2.ProviderConfig(
                workspace_id=str(row['workspace_id']),
                provider=row['provider'],
                model=row['model'] or "",
                temperature=row['temperature'],
                max_tokens=row['max_tokens'],
                system_prompt=row['system_prompt'] or "",
                rag_enabled=row['rag_enabled'],
                rag_top_k=row['rag_top_k'],
                rag_min_similarity=row['rag_min_similarity'],
                is_default=row['is_default'],
                created_at=row['created_at'].isoformat(),
                updated_at=row['updated_at'].isoformat()
            )
        )

    async def ListTools(self, request, context):
        try:
            tools = load_tool_catalog()
            return brain_pb2.ListToolsResponse(
                tools=[
                    brain_pb2.ToolMetadata(
                        name=tool.get("name", ""),
                        description=tool.get("description", ""),
                        category=tool.get("category", ""),
                        risk_level=tool.get("riskLevel", "low"),
                        enabled=bool(tool.get("enabled", True)),
                    )
                    for tool in tools
                ]
            )
        except Exception as e:
            logger.error(f"ListTools failed: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return brain_pb2.ListToolsResponse()

    async def DeleteProviderConfig(self, request, context):
        await delete_provider_config(request.workspace_id, request.provider)
        return brain_pb2.DeleteProviderConfigResponse(success=True)


