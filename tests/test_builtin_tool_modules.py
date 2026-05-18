from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.command_tools import (
    CodexExecTool,
    LintRunTool,
    PatchApplyTool,
    ShellRunTool,
    TestRunTool,
)
from nested_memvid_agent.tools.diagnosis_tools import DiagnosisClassifyTool, DiagnosisRecallTool
from nested_memvid_agent.tools.discovery_tools import (
    McpRegistryTool,
    PluginRegistryTool,
    ProjectScriptsTool,
    SkillDiscoverTool,
    SkillInspectTool,
    ToolRegistryTool,
)
from nested_memvid_agent.tools.git_tools import (
    GitBranchTool,
    GitCommitTool,
    GitCreateLocalBranchTool,
    GitDiffTool,
    GitExportPatchTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
)
from nested_memvid_agent.tools.repair_tools import (
    RepairApplyPatchTool,
    RepairOrchestrateValidateTool,
    RepairPrepareTool,
    RepairReviewTool,
    RepairRollbackTool,
    RepairStatusTool,
    RepairValidateTool,
)
from nested_memvid_agent.tools.web_tools import WebFetchTool, WebSearchTool
from nested_memvid_agent.tools.workspace_tools import (
    FileStatTool,
    FindFilesTool,
    ListFilesTool,
    ReadFileTool,
    RepoMapTool,
    RepoSearchTool,
    WriteFileTool,
)


def test_default_registry_keeps_extracted_builtin_tools() -> None:
    registry = build_default_tools()
    specs = {spec.name: spec for spec in registry.specs()}
    registered_types = {name: type(tool) for name, tool in registry._tools.items()}

    assert specs["tool.registry"].name == ToolRegistryTool.spec.name
    assert registered_types["tool.registry"] is ToolRegistryTool
    assert specs["skill.discover"].name == SkillDiscoverTool.spec.name
    assert registered_types["skill.discover"] is SkillDiscoverTool
    assert specs["skill.inspect"].name == SkillInspectTool.spec.name
    assert registered_types["skill.inspect"] is SkillInspectTool
    assert specs["plugin.registry"].name == PluginRegistryTool.spec.name
    assert registered_types["plugin.registry"] is PluginRegistryTool
    assert specs["mcp.registry"].name == McpRegistryTool.spec.name
    assert registered_types["mcp.registry"] is McpRegistryTool
    assert specs["project.scripts"].name == ProjectScriptsTool.spec.name
    assert registered_types["project.scripts"] is ProjectScriptsTool
    assert specs["diagnosis.classify"].name == DiagnosisClassifyTool.spec.name
    assert registered_types["diagnosis.classify"] is DiagnosisClassifyTool
    assert specs["diagnosis.recall"].name == DiagnosisRecallTool.spec.name
    assert registered_types["diagnosis.recall"] is DiagnosisRecallTool
    assert specs["web.search"].name == WebSearchTool.spec.name
    assert registered_types["web.search"] is WebSearchTool
    assert specs["web.fetch"].name == WebFetchTool.spec.name
    assert registered_types["web.fetch"] is WebFetchTool
    assert specs["file.list"].name == ListFilesTool.spec.name
    assert registered_types["file.list"] is ListFilesTool
    assert specs["file.read"].name == ReadFileTool.spec.name
    assert registered_types["file.read"] is ReadFileTool
    assert specs["file.find"].name == FindFilesTool.spec.name
    assert registered_types["file.find"] is FindFilesTool
    assert specs["file.stat"].name == FileStatTool.spec.name
    assert registered_types["file.stat"] is FileStatTool
    assert specs["file.write"].name == WriteFileTool.spec.name
    assert registered_types["file.write"] is WriteFileTool
    assert specs["repo.search"].name == RepoSearchTool.spec.name
    assert registered_types["repo.search"] is RepoSearchTool
    assert specs["repo.map"].name == RepoMapTool.spec.name
    assert registered_types["repo.map"] is RepoMapTool
    assert specs["shell.run"].name == ShellRunTool.spec.name
    assert registered_types["shell.run"] is ShellRunTool
    assert specs["codex.exec"].name == CodexExecTool.spec.name
    assert registered_types["codex.exec"] is CodexExecTool
    assert specs["patch.apply"].name == PatchApplyTool.spec.name
    assert registered_types["patch.apply"] is PatchApplyTool
    assert specs["test.run"].name == TestRunTool.spec.name
    assert registered_types["test.run"] is TestRunTool
    assert specs["lint.run"].name == LintRunTool.spec.name
    assert registered_types["lint.run"] is LintRunTool
    assert specs["repair.prepare"].name == RepairPrepareTool.spec.name
    assert registered_types["repair.prepare"] is RepairPrepareTool
    assert specs["repair.status"].name == RepairStatusTool.spec.name
    assert registered_types["repair.status"] is RepairStatusTool
    assert specs["repair.apply_patch"].name == RepairApplyPatchTool.spec.name
    assert registered_types["repair.apply_patch"] is RepairApplyPatchTool
    assert specs["repair.validate"].name == RepairValidateTool.spec.name
    assert registered_types["repair.validate"] is RepairValidateTool
    assert specs["repair.orchestrate_validate"].name == RepairOrchestrateValidateTool.spec.name
    assert registered_types["repair.orchestrate_validate"] is RepairOrchestrateValidateTool
    assert specs["repair.review"].name == RepairReviewTool.spec.name
    assert registered_types["repair.review"] is RepairReviewTool
    assert specs["repair.rollback"].name == RepairRollbackTool.spec.name
    assert registered_types["repair.rollback"] is RepairRollbackTool
    assert specs["git.status"].name == GitStatusTool.spec.name
    assert registered_types["git.status"] is GitStatusTool
    assert specs["git.diff"].name == GitDiffTool.spec.name
    assert registered_types["git.diff"] is GitDiffTool
    assert specs["git.export_patch"].name == GitExportPatchTool.spec.name
    assert registered_types["git.export_patch"] is GitExportPatchTool
    assert specs["git.branch"].name == GitBranchTool.spec.name
    assert registered_types["git.branch"] is GitBranchTool
    assert specs["git.create_local_branch"].name == GitCreateLocalBranchTool.spec.name
    assert registered_types["git.create_local_branch"] is GitCreateLocalBranchTool
    assert specs["git.log"].name == GitLogTool.spec.name
    assert registered_types["git.log"] is GitLogTool
    assert specs["git.show"].name == GitShowTool.spec.name
    assert registered_types["git.show"] is GitShowTool
    assert specs["git.commit"].name == GitCommitTool.spec.name
    assert registered_types["git.commit"] is GitCommitTool
