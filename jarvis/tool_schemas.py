"""Small JSON schemas and validation for bounded structured work tools."""

import math


def text(maximum=500, *, empty=False):
    return {"type": "string", "minLength": 0 if empty else 1, "maxLength": maximum}


def array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def definition(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": obj(properties, required),
        },
    }


SOURCE = obj(
    {
        "title": text(300),
        "url": text(2048),
        "published_at": text(80, empty=True),
        "accessed_at": text(80, empty=True),
        "note": text(1200, empty=True),
    },
    ("title", "url"),
)
SOURCES = array(SOURCE, 30)
OUTPUT = {
    "path": text(500),
    "title": text(180),
    "sources": SOURCES,
    "overwrite": {"type": "boolean"},
}
SECTION = obj(
    {
        "heading": text(240, empty=True),
        "body": text(30000, empty=True),
        "bullets": array(text(2000), 30),
    },
    (),
)
SLIDE = obj(
    {
        "title": text(100),
        "bullets": array(text(240), 8, 1),
        "notes": text(3000, empty=True),
        "sources": SOURCES,
    },
    ("title", "bullets"),
)
CELL = {
    "type": ["string", "number", "boolean", "null"],
    "maxLength": 4000,
    "minimum": -(10**15),
    "maximum": 10**15,
}
SHIFT = obj(
    {
        "name": text(30),
        "start": text(5),
        "end": text(5),
        "required": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    ("name", "start", "end", "required"),
)


WORK_DEFINITIONS = [
    definition(
        "create_document",
        "生成真正的 Word .docx 文档，保留章节和可追溯来源。完成后可直接预览实际文件内容，默认不覆盖。",
        {**OUTPUT, "sections": array(SECTION, 60, 1)},
        ("path", "title", "sections"),
    ),
    definition(
        "create_spreadsheet",
        "生成真正的 Excel .xlsx 表格。明确列名和每行数据；数字保持数值类型，来源放单独工作表，默认不覆盖。",
        {
            **OUTPUT,
            "columns": array(text(160), 40, 1),
            "rows": array(array(CELL, 40, 1), 5000),
        },
        ("path", "title", "columns", "rows"),
    ),
    definition(
        "create_presentation",
        "生成真正的 PowerPoint .pptx。slides给出标题、要点、中文讲解notes和来源；每页1–8个要点、合计最多640字。要随后做讲解视频时最多6页、每页notes不超过200字。默认不覆盖。",
        {**OUTPUT, "slides": array(SLIDE, 24, 1)},
        ("path", "title", "slides"),
    ),
    definition(
        "create_schedule",
        "根据明确人数、周期和班次生成可检查的 .xlsx 排班与约束报告。未提供姓名时为匿名示例；缺实际规则先问用户，不得把默认值宣称为实际工作要求。默认7天、30人、三班各6人、班间至少12小时、连续/总班次最多5，覆盖不可行会报错。",
        {
            "path": text(500),
            "start_date": text(10),
            "days": {"type": "integer", "minimum": 1, "maximum": 31},
            "employee_count": {"type": "integer", "minimum": 1, "maximum": 200},
            "employees": array(text(80), 200, 1),
            "shifts": array(SHIFT, 5, 1),
            "min_rest_hours": {"type": "number", "minimum": 0, "maximum": 48},
            "max_consecutive_days": {"type": "integer", "minimum": 1, "maximum": 31},
            "max_shifts_per_employee": {"type": "integer", "minimum": 1, "maximum": 31},
            "overwrite": {"type": "boolean"},
        },
        ("path", "start_date"),
    ),
    definition(
        "create_explainer_video",
        "将工作目录里真实 .pptx 的1–6页及中文讲稿合成带真实本地合成语音的 H.264/AAC MP4，附SRT字幕和脚本。使用本地Kokoro，最长10分钟、单次制作预算240秒；仅生成本地文件，不发布B站。必须提供父任务实际PPT路径；每页notes最多200字。",
        {
            "path": text(500),
            "presentation_path": text(500),
            "manifest_path": text(500),
            "speaker": {"type": "integer", "minimum": 0, "maximum": 102},
            "speed": {"type": "number", "minimum": 0.5, "maximum": 2},
            "overwrite": {"type": "boolean"},
        },
        ("path", "presentation_path"),
    ),
    definition(
        "get_file_preview",
        "读取工作目录里真实 Word/Excel/PPT/Markdown/MP4 的有界内容预览，理解已有产物再继续编辑或制作视频。不会运行文档宏，不会计算Excel公式。",
        {"path": text(500)},
        ("path",),
    ),
]


def validate_value(value, schema, label="参数"):
    expected = schema.get("type")
    accepted = expected if isinstance(expected, list) else [expected]
    kinds = {
        "null": value is None,
        "string": type(value) is str,
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "array": type(value) is list,
        "object": type(value) is dict,
    }
    if not any(kinds.get(kind, False) for kind in accepted):
        raise ValueError(f"{label}类型无效，应为 {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{label}不在允许的选项中")
    if type(value) is str:
        if (
            not schema.get("minLength", 0)
            <= len(value)
            <= schema.get("maxLength", 100000)
        ):
            raise ValueError(f"{label}长度无效")
        if schema.get("minLength", 0) and not value.strip():
            raise ValueError(f"{label}不能为空")
    elif type(value) in (int, float):
        if (
            (type(value) is float and not math.isfinite(value))
            or value < schema.get("minimum", value)
            or value > schema.get("maximum", value)
        ):
            raise ValueError(f"{label}数值超出范围或不是有限值")
    elif type(value) is list:
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 5000):
            raise ValueError(f"{label}项目数量超出限制")
        for index, item in enumerate(value):
            validate_value(item, schema["items"], f"{label}[{index}]")
    elif type(value) is dict:
        properties = schema.get("properties", {})
        extra = set(value) - set(properties)
        if extra and not schema.get("additionalProperties", True):
            raise ValueError("不支持的参数：" + ", ".join(sorted(map(str, extra))))
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"缺少必填参数：{label}.{key}")
        for key, item in value.items():
            if key in properties:
                validate_value(item, properties[key], f"{label}.{key}")
