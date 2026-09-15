from __future__ import annotations

from typing import Any

# 单轮多候选的张数上限（与 tools.py 的收敛逻辑共用）
GENERATION_COUNT_MIN = 1
GENERATION_COUNT_MAX = 4
DEFAULT_GENERATION_COUNT = 2

# 朋友圈分格导出的合法切法（schema 与实际切片共用，避免多处重复真相）
SUPPORTED_EXPORT_GRIDS = ("3x3", "2x2", "3x1", "1x3")


TOOL_SCHEMAS: list[dict[str, Any]] = [

        {
            "type": "function",
            "function": {
                "name": "generate_image",
                "description": (
                    "【做什么】根据文字描述从零生成全新图片/海报，异步任务，一次默认出 2 版候选供用户挑选。\n"
                    "【何时用】用户说'做一张/画一个/来一版/生成海报/出个主图/设计一张'，或要从零开始新设计时调用。\n"
                    "【何时不用】基于已有图改画面/风格用 edit_image；只改海报文字/价格/卖点用 rerender_poster_copy；"
                    "要成套素材（主图+海报+套图）用 run_product_pipeline；用户已说'发朋友圈多图'时出图后还应主动提议"
                    " export_moments_grid。调用后告知用户正在生成，不要虚构结果。\n"
                    "参数示例：{\"prompt\": \"开业竖版海报，红金配色，主标题'周年庆'\", \"count\": 2}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "具体的图片描述：主体、构图、色调、光线、文字位内容",
                        },
                        "count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 4,
                            "default": 2,
                            "description": "一次生成几张候选供挑选（1-4 张，默认 2，多版本让用户挑）",
                        },
                        "size": {
                            "type": "string",
                            "description": (
                                "可选，输出尺寸 宽x高（如 1024x1024）；"
                                "用户没说尺寸就按朋友圈竖版等默认来，不必追问"
                            ),
                        },
                        "template_asset_id": {
                            "type": "string",
                            "description": (
                                "可选：素材库中的模板 id。用户选中/上传了模板、要求'按这个风格'时必填，"
                                "系统会把该模板的布局/配色/字体气质约束注入生成提示，实现风格复刻。"
                            ),
                        },
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_image",
                "description": (
                    "【做什么】以已有图片为基础修改画面（换背景/调色/加元素/改风格），保留原图主体，异步任务。\n"
                    "【何时用】用户对上一轮结果说'换个背景/颜色调亮一点/加个气球/风格再高级些/再改改'时调用。\n"
                    "【何时不用】只改海报上的文字/价格/卖点用 rerender_poster_copy（秒出且不耗生图额度）；"
                    "画面完全不满意、想推倒重来用 generate_image。\n"
                    "【重要】'基于刚才那张继续改'时必须把那张图的 asset_id 传入，保持画面一致。\n"
                    "参数示例：{\"asset_id\": \"<上一轮产出的 asset_id>\","
                    " \"prompt\": \"背景换成清晨的街边，色调变暖\"}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "要修改的图片资产 id（上一轮生成结果的 asset_id，保持画面延续）",
                        },
                        "prompt": {"type": "string", "description": "修改要求的具体描述"},
                        "size": {"type": "string", "description": "可选，输出尺寸 宽x高"},
                    },
                    "required": ["asset_id", "prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_copy",
                "description": (
                    "【做什么】即时生成 2-3 版短营销文案（朋友圈配文/标题/卖点清单），直接在对话里给用户挑。\n"
                    "【何时用】用户说'帮我想个朋友圈文案/写个标题/列几条卖点/配文怎么写'这类轻量单条文案需求时调用。\n"
                    "【何时不用】用户要'一整套/一份报告/能下载的文档'类成体系文案用 write_copy_report；"
                    "绝不允许不调工具、自己在回复里现编文案。\n"
                    "参数示例：{\"brief\": \"烘焙店周年庆第二件半价，受众宝妈\", \"kind\": \"moments\", \"count\": 3}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "brief": {"type": "string", "description": "文案需求：产品/活动/受众/语气"},
                        "kind": {
                            "type": "string",
                            "enum": ["moments", "headline", "selling_points"],
                            "description": "moments=朋友圈配文 headline=标题 selling_points=卖点清单",
                        },
                        "count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "default": 3,
                            "description": "版本数（1-5，默认 3，多版本让用户挑）",
                        },
                    },
                    "required": ["brief"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_assets",
                "description": (
                    "【做什么】在素材库里按关键词检索已有模板/参考图/成品/品牌素材，返回带预览链接的清单。\n"
                    "【何时用】用户说'有没有现成的/找个类似的/看看素材库'，或出方案、选模板前先查库存时调用。\n"
                    "【何时不用】明确要全新设计时直接 generate_image；"
                    "检索不到时如实告知，并可建议上传模板或直接新画。\n"
                    "参数示例：{\"query\": \"开业 粉色 高级感\", \"kind\": \"template\"}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索词，如 开业 粉色 战报 高级感"},
                        "kind": {
                            "type": "string",
                            "enum": ["template", "reference", "output", "brand"],
                            "description": "可选，限定素材类型",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "analyze_template",
                "description": (
                    "【做什么】解析素材库中一张模板图的布局/配色/字体气质/文案位，生成结构化模板档案（供风格复刻）。\n"
                    "【何时用】用户上传了模板图、或要'看懂这张模板再照着做'时调用；之后 generate_image 传同一"
                    " asset_id 作 template_asset_id 实现复刻。\n"
                    "【何时不用】只是找素材/看清单用 search_assets；该模板已有档案时不必重复分析。\n"
                    "参数示例：{\"asset_id\": \"<素材库模板 id>\"}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string", "description": "素材库中的素材 id"},
                    },
                    "required": ["asset_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_asset",
                "description": (
                    "【做什么】把本会话生成的某张图存入素材库，成为可复用、可导出九宫格的成品素材。\n"
                    "【何时用】用户说'这张不错/就它了/存一下/收藏这张'时调用；重要成品交付后也可主动提议保存。\n"
                    "【何时不用】用户还没在多张候选里做选择时不要抢先保存；会话尚无产出图时无可保存。\n"
                    "参数示例：{\"asset_id\": \"<会话产出图的 asset_id>\", \"title\": \"周年庆主海报\"}；"
                    "缺省 asset_id 保存最新一张。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string", "description": "图片会话中的资产 id，缺省保存最新产出"},
                        "title": {"type": "string", "description": "可选，素材命名"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_product_pipeline",
                "description": (
                    "【做什么】提交商品批量流水线（商品理解→文案→自动生图），异步跑出成套商品素材。\n"
                    "【何时用】用户描述的是成套需求，如'帮我做全套主图+海报+朋友圈图/从商品图自动出一整套'时调用。\n"
                    "【何时不用】只要单独一张图用 generate_image；商品主图必须已在素材库（还没有就先请用户上传）；"
                    "商品海报只改文字用 rerender_poster_copy。\n"
                    "【重要】异步提交：调用后告知用户已在后台开工，并用 check_pipeline_status 跟进进度，"
                    "不要虚构产出。\n"
                    "参数示例："
                    "{\"product_name\": \"燕窝礼盒\", \"library_asset_id\": \"<素材库商品图 id>\", \"price\": \"299\"}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_name": {"type": "string", "description": "商品名称"},
                        "library_asset_id": {
                            "type": "string",
                            "description": "素材库中作为商品主图的图片 id（用户上传的商品图）",
                        },
                        "category": {"type": "string", "description": "可选，商品类目"},
                        "price": {"type": "string", "description": "可选，价格"},
                        "canvas_template_key": {
                            "type": "string",
                            "description": "可选，画布模板 key（缺省用电商主图模板）",
                        },
                    },
                    "required": ["product_name", "library_asset_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_pipeline_status",
                "description": (
                    "【做什么】查询商品流水线的执行状态与产出（运行状态/海报数量与下载链接/失败原因）。\n"
                    "【何时用】提交过 run_product_pipeline 后用户问'好了吗/跑完没/结果呢'，或新一轮对话接续时"
                    "主动查一次并汇报。\n"
                    "【何时不用】还没提交过流水线时无需查询；查询到进行中就告知稍候，不要虚构产出或编造海报链接。\n"
                    "参数示例：{\"product_id\": \"<run_product_pipeline 返回的 product_id>\"}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "description": "商品 id"},
                    },
                    "required": ["product_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "export_moments_grid",
                "description": (
                    "【做什么】把一张成品图切成朋友圈分格切片（3x3 九宫格/2x2 四宫格/3x1 三横连/1x3 三竖连），"
                    "返回按序发布的 zip 下载链接。\n"
                    "【何时用】用户说'发朋友圈/切九宫格/切成好几张/做成多图/发圈用'时必须主动调用，别只给单图链接。\n"
                    "【何时不用】用户只要单张下载时不切；会话还没有成品图且未指定 asset_id 时先出图再切。\n"
                    "参数示例：{\"grid\": \"3x3\"}（缺省用本会话最新产出；也可传 "
                    "{\"asset_id\": \"<素材库素材 id>\", \"grid\": \"2x2\"}）"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "素材库素材 id；缺省使用本会话最新产出的图",
                        },
                        "grid": {
                            "type": "string",
                            "enum": ["3x3", "2x2", "3x1", "1x3"],
                            "description": "分格方式，默认 3x3 九宫格",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recommend_designs",
                "description": (
                    "【做什么】结合用户需求检索素材库，产出 2-3 个带推荐理由的设计方案卡片供用户挑选。\n"
                    "【何时用】用户说'不知道怎么做/给点建议/有什么方案/帮我推荐'，或刚开场需求模糊时主动调用。\n"
                    "【何时不用】用户已明确要什么图时直接 generate_image；只想要素材清单用 search_assets；"
                    "用户选中某方案后出图要传对应 template_asset_id。\n"
                    "参数示例：{\"requirement\": \"奶茶店上新季，想要清新插画感\", \"kind\": \"template\"}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "requirement": {
                            "type": "string",
                            "description": "用户需求概括：行业/活动/想要的感觉",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["template", "reference", "output", "brand"],
                            "description": "可选，限定检索的素材类型",
                        },
                    },
                    "required": ["requirement"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_copy_report",
                "description": (
                    "【做什么】生成一份完整文案报告文档（朋友圈正文/卖点清单/话题标签/发布建议），落库并提供下载链接。\n"
                    "【何时用】用户说'来一份文案报告/整套文案/做成文档发我/给个能下载的'，或需要长期复用的成体系"
                    "文案时调用。\n"
                    "【何时不用】只要一两句配文/标题/卖点用 write_copy（更快，直接在对话里看）；"
                    "不要对同一需求把两者重复各调一遍。\n"
                    "参数示例：{\"brief\": \"鲜花店新店开业引流，主打 9.9 元花束\", \"tone\": \"亲切自然\"}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "brief": {"type": "string", "description": "产品/活动/受众/目标"},
                        "tone": {"type": "string", "description": "可选，语气偏好（亲切/高端/促销感）"},
                    },
                    "required": ["brief"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "rerender_poster_copy",
                "description": (
                    "【做什么】商品海报只改文字（价格/标题/卖点/商品名/行动号召）：本地秒级重渲一张新海报，"
                    "不消耗生图额度。\n"
                    "【何时用】用户对已建商品的海报说'价格改成 49.9/换个标题/卖点更新一下'时优先调用。\n"
                    "【何时不用】要改画面/配色/风格用 edit_image 或 generate_image；商品还没建过（没跑过流水线）"
                    "时先走 run_product_pipeline。\n"
                    "参数示例：{\"product_id\": \"<商品 id>\", \"poster_kind\": \"promo_poster\", "
                    "\"copy\": {\"price\": \"49.9\", \"instruction\": \"前 100 名再减 20\"}}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "description": "商品 id"},
                        "poster_kind": {
                            "type": "string",
                            "enum": ["main_image", "promo_poster"],
                            "description": "海报类型：main_image=电商主图，promo_poster=促销海报",
                        },
                        "copy": {
                            "type": "object",
                            "description": (
                                "要覆盖的文字字段（部分覆盖即可）：price=价格、product_name=商品名、"
                                "headline=主标题、selling_points=卖点数组、instruction=行动号召/卖点引导"
                            ),
                        },
                    },
                    "required": ["product_id", "poster_kind", "copy"],
                },
            },
        },
    ]


def schema_by_name() -> dict[str, dict[str, Any]]:
    """按工具名索引 schema，供注册表与测试使用。"""
    return {item["function"]["name"]: item for item in TOOL_SCHEMAS}
