"""Read-only tool discovery derived from the executable registry and role scopes."""

from copy import deepcopy


_PRESENTATION = {
    "list_files": ("浏览工作文件", "文件", "读取工作目录"),
    "read_file": ("读取文本", "文件", "读取工作目录"),
    "write_file": ("保存文本与文章", "文件", "写入工作目录"),
    "get_file_preview": ("读取产物内容", "文件", "读取工作目录"),
    "search_web": ("搜索网页", "联网检索", "访问公开网络"),
    "fetch_page": ("阅读网页", "联网检索", "访问公开网络"),
    "analyze_csv": ("分析 CSV 数据", "数据与文档", "读取工作目录"),
    "create_document": ("生成 Word 文档", "数据与文档", "写入工作目录"),
    "create_spreadsheet": ("生成 Excel 表格", "数据与文档", "写入工作目录"),
    "create_presentation": ("生成 PPT 演示", "数据与文档", "写入工作目录"),
    "create_schedule": ("生成排班计划", "数据与文档", "写入工作目录"),
    "create_explainer_video": ("制作中文讲解视频", "视频", "读取演示并生成本地视频"),
    "save_note": ("保存笔记", "个人助理", "保存本地笔记"),
    "list_notes": ("查看笔记", "个人助理", "读取本地笔记"),
    "add_todo": ("添加待办", "个人助理", "保存本地待办"),
    "list_todos": ("查看待办", "个人助理", "读取本地待办"),
    "complete_todo": ("完成待办", "个人助理", "更新本地待办"),
    "current_time": ("查看当前时间", "个人助理", "读取系统时间"),
}
_ROLES = {
    "coordinator": "总控助手",
    "research": "检索助手",
    "analyst": "数据助手",
    "writer": "文档助手",
    "media": "视频助手",
}


def build_tool_catalog(definitions, *, orchestrator="maf"):
    # The orchestration module keeps all framework/provider imports inside
    # run_maf. Discovery never starts an agent, loads a model, or accesses files.
    from .orchestration import PERSONAL_TOOLS, SPECIALISTS

    catalog = []
    for definition in definitions:
        function = definition["function"]
        name = function["name"]
        title, category, effect = _PRESENTATION.get(name, (name, "其他", "工具操作"))
        agents = [_ROLES["coordinator"]] if orchestrator != "maf" else []
        if orchestrator == "maf":
            if name in PERSONAL_TOOLS:
                agents.append(_ROLES["coordinator"])
            agents.extend(
                _ROLES[role]
                for role, (_, _, names) in SPECIALISTS.items()
                if name in names
            )
        catalog.append(
            {
                "name": name,
                "title": title,
                "description": function["description"],
                "category": category,
                "agents": agents,
                "parameters": deepcopy(function["parameters"]),
                "kind": "tool",
                "effect": effect,
            }
        )
    if orchestrator == "maf":
        for role, (description, _, _) in SPECIALISTS.items():
            catalog.append(
                {
                    "name": f"delegate_{role}",
                    "title": "委派" + _ROLES[role],
                    "description": description,
                    "category": "智能体协作",
                    "agents": [_ROLES["coordinator"]],
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "任务及必要上下文",
                            }
                        },
                        "required": ["task"],
                        "additionalProperties": False,
                    },
                    "kind": "delegation",
                    "effect": "使用对应助手的工具完成子任务",
                }
            )
    return catalog
