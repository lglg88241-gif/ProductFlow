from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from sqlalchemy.orm import Session

from productflow_backend.application.isolation import current_owner_id, ensure_row_readable
from productflow_backend.application.product_workflows import (
    apply_node_group_template_to_workflow,
    archive_user_canvas_template,
    bind_workflow_node_image,
    cancel_product_workflow_run,
    create_user_canvas_template_from_workflow_nodes,
    create_workflow_edge,
    create_workflow_node,
    delete_workflow_edge,
    delete_workflow_node,
    duplicate_workflow_node_group,
    get_or_create_product_workflow,
    get_product_workflow_status,
    list_canvas_templates,
    rename_user_canvas_template,
    retry_product_workflow_run,
    submit_product_workflow_run,
    update_workflow_copy_set,
    update_workflow_node,
    upload_workflow_node_image,
)
from productflow_backend.infrastructure.db.models import (
    Product,
    ProductWorkflow,
    UserAccount,
    WorkflowEdge,
    WorkflowNode,
)
from productflow_backend.presentation.deps import get_session, require_admin, require_business_user
from productflow_backend.presentation.schemas.product_workflows import (
    ApplyWorkflowTemplateGroupRequest,
    BindWorkflowNodeImageRequest,
    CanvasTemplateListResponse,
    CanvasTemplateSummaryResponse,
    CreateUserTemplateGroupRequest,
    CreateWorkflowEdgeRequest,
    CreateWorkflowNodeRequest,
    DuplicateWorkflowNodeGroupRequest,
    ProductWorkflowResponse,
    ProductWorkflowStatusResponse,
    RunWorkflowRequest,
    UpdateUserTemplateGroupRequest,
    UpdateWorkflowCopySetRequest,
    UpdateWorkflowNodeRequest,
    serialize_canvas_template_summary,
    serialize_product_workflow,
    serialize_product_workflow_status,
    serialize_user_canvas_template_summary,
)
from productflow_backend.presentation.upload_validation import read_validated_image_upload


def require_resource_owner(
    request: Request,
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> None:
    """路由级归属守卫（审计 S0-01）。

    工作流端点此前只挂 require_admin：持有他人的 product_id / node_id / edge_id 即可
    跨用户读写。这里按**路径参数**解析锚点资源并统一判定归属——一处覆盖全部端点，
    新增端点也自动受保护，避免"逐端点补校验"再次漏项。

    node/edge → workflow → product → owner；跨用户一律 404 语义；
    owner_id 为 None（隔离关闭）时直接放行，保持现状。
    """
    owner_id = current_owner_id(user)
    if owner_id is None:
        return
    params = request.path_params
    if "product_id" in params:
        _owned_product(session, params["product_id"], owner_id)
    elif "node_id" in params:
        _owner_for_node(session, params["node_id"], owner_id)
    elif "edge_id" in params:
        _owner_for_edge(session, params["edge_id"], owner_id)


def _owned_product(session: Session, product_id: str, owner_id: str | None) -> None:
    product = session.get(Product, product_id)
    if product is None:
        from productflow_backend.domain.errors import NotFoundError

        raise NotFoundError("商品不存在")
    ensure_row_readable(product, owner_id, message="商品不存在")


def _owner_for_node(session: Session, node_id: str, owner_id: str | None) -> None:
    """按节点归属判定：node → workflow → product → owner。"""
    from productflow_backend.domain.errors import NotFoundError

    node = session.get(WorkflowNode, node_id)
    if node is None:
        raise NotFoundError("工作流节点不存在")
    workflow = session.get(ProductWorkflow, node.workflow_id)
    if workflow is None:
        raise NotFoundError("工作流节点不存在")
    _owned_product(session, workflow.product_id, owner_id)


def _owner_for_edge(session: Session, edge_id: str, owner_id: str | None) -> None:
    """按连线归属判定：edge → workflow → product → owner。"""
    from productflow_backend.domain.errors import NotFoundError

    edge = session.get(WorkflowEdge, edge_id)
    if edge is None:
        raise NotFoundError("工作流连线不存在")
    workflow = session.get(ProductWorkflow, edge.workflow_id)
    if workflow is None:
        raise NotFoundError("工作流连线不存在")
    _owned_product(session, workflow.product_id, owner_id)


def _guard_and_owner(session: Session, user: UserAccount | None) -> str | None:
    """统一的 owner 取值：隔离开启返回用户 id，关闭返回 None（守卫内自行短路）。"""
    return current_owner_id(user)


router = APIRouter(
    prefix="/api",
    tags=["product-workflows"],
    dependencies=[Depends(require_admin), Depends(require_resource_owner)],
)


# ---------------------------------------------------------------------------
# 归属守卫（审计 S0-01）：工作流端点此前只挂 require_admin，没有任何 owner 校验，
# 持有他人 product_id / node_id / edge_id 即可跨用户读写。这里在路由层用**锚点资源**
# 统一判定：node/edge → workflow → product → owner。跨用户一律 NotFoundError
# （与"不存在"同文案，不泄漏存在性）；owner_id 为 None（隔离关闭）时放行保持现状。
# ---------------------------------------------------------------------------


@router.get("/products/{product_id}/workflow", response_model=ProductWorkflowResponse)
def get_product_workflow_endpoint(product_id: str, session: Session = Depends(get_session)) -> ProductWorkflowResponse:
    workflow = get_or_create_product_workflow(session, product_id)
    return serialize_product_workflow(workflow)


@router.get("/products/{product_id}/workflow/status", response_model=ProductWorkflowStatusResponse)
def get_product_workflow_status_endpoint(
    product_id: str,
    session: Session = Depends(get_session),
) -> ProductWorkflowStatusResponse:
    workflow = get_product_workflow_status(session, product_id)
    return serialize_product_workflow_status(workflow)


@router.get("/workflow/canvas-templates", response_model=CanvasTemplateListResponse)
def list_canvas_templates_endpoint(session: Session = Depends(get_session)) -> CanvasTemplateListResponse:
    templates = [serialize_canvas_template_summary(template) for template in list_canvas_templates(session)]
    return CanvasTemplateListResponse(items=templates)


@router.post(
    "/products/{product_id}/workflow/user-template-groups",
    response_model=CanvasTemplateSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_user_template_group_endpoint(
    product_id: str,
    payload: CreateUserTemplateGroupRequest,
    session: Session = Depends(get_session),
) -> CanvasTemplateSummaryResponse:
    template = create_user_canvas_template_from_workflow_nodes(
        session,
        product_id=product_id,
        title=payload.title,
        description=payload.description,
        node_ids=payload.node_ids,
    )
    return serialize_user_canvas_template_summary(template)


@router.patch("/workflow/user-template-groups/{template_id}", response_model=CanvasTemplateSummaryResponse)
def update_user_template_group_endpoint(
    template_id: str,
    payload: UpdateUserTemplateGroupRequest,
    session: Session = Depends(get_session),
) -> CanvasTemplateSummaryResponse:
    template = rename_user_canvas_template(
        session,
        template_id=template_id,
        title=payload.title,
        description=payload.description,
    )
    return serialize_user_canvas_template_summary(template)


@router.delete("/workflow/user-template-groups/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_user_template_group_endpoint(template_id: str, session: Session = Depends(get_session)) -> None:
    archive_user_canvas_template(session, template_id=template_id)


@router.post(
    "/products/{product_id}/workflow/nodes",
    response_model=ProductWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_workflow_node_endpoint(
    product_id: str,
    payload: CreateWorkflowNodeRequest,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = create_workflow_node(
        session,
        product_id=product_id,
        node_type=payload.node_type,
        title=payload.title,
        position_x=payload.position_x,
        position_y=payload.position_y,
        config_json=payload.config_json,
    )
    return serialize_product_workflow(workflow)


@router.post(
    "/products/{product_id}/workflow/template-groups",
    response_model=ProductWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
def apply_workflow_template_group_endpoint(
    product_id: str,
    payload: ApplyWorkflowTemplateGroupRequest,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = apply_node_group_template_to_workflow(
        session,
        product_id=product_id,
        template_key=payload.template_key,
        position_x=payload.position_x,
        position_y=payload.position_y,
        template_language=payload.template_language,
    )
    return serialize_product_workflow(workflow)


@router.post(
    "/products/{product_id}/workflow/node-groups/duplicate",
    response_model=ProductWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
def duplicate_workflow_node_group_endpoint(
    product_id: str,
    payload: DuplicateWorkflowNodeGroupRequest,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = duplicate_workflow_node_group(
        session,
        product_id=product_id,
        node_ids=payload.node_ids,
        position_x=payload.position_x,
        position_y=payload.position_y,
        offset_x=payload.offset_x,
        offset_y=payload.offset_y,
    )
    return serialize_product_workflow(workflow)


@router.patch("/workflow-nodes/{node_id}", response_model=ProductWorkflowResponse)
def update_workflow_node_endpoint(
    node_id: str,
    payload: UpdateWorkflowNodeRequest,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = update_workflow_node(
        session,
        node_id=node_id,
        title=payload.title,
        position_x=payload.position_x,
        position_y=payload.position_y,
        config_json=payload.config_json,
    )
    return serialize_product_workflow(workflow)


@router.patch("/workflow-nodes/{node_id}/copy", response_model=ProductWorkflowResponse)
def update_workflow_copy_set_endpoint(
    node_id: str,
    payload: UpdateWorkflowCopySetRequest,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = update_workflow_copy_set(
        session,
        node_id=node_id,
        structured_payload=payload.structured_payload,
    )
    return serialize_product_workflow(workflow)


@router.post("/workflow-nodes/{node_id}/image", response_model=ProductWorkflowResponse)
async def upload_workflow_node_image_endpoint(
    node_id: str,
    image: UploadFile = File(...),
    role: str | None = Form(default=None),
    label: str | None = Form(default=None),
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    validated = await read_validated_image_upload(image, fallback_filename="workflow-image.bin")
    workflow = upload_workflow_node_image(
        session,
        node_id=node_id,
        image_bytes=validated.content,
        filename=validated.filename,
        content_type=validated.mime_type,
        role=role,
        label=label,
    )
    return serialize_product_workflow(workflow)


@router.post("/workflow-nodes/{node_id}/image-source", response_model=ProductWorkflowResponse)
def bind_workflow_node_image_endpoint(
    node_id: str,
    payload: BindWorkflowNodeImageRequest,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = bind_workflow_node_image(
        session,
        node_id=node_id,
        source_asset_id=payload.source_asset_id,
        poster_variant_id=payload.poster_variant_id,
    )
    return serialize_product_workflow(workflow)


@router.post(
    "/products/{product_id}/workflow/edges",
    response_model=ProductWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_workflow_edge_endpoint(
    product_id: str,
    payload: CreateWorkflowEdgeRequest,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = create_workflow_edge(
        session,
        product_id=product_id,
        source_node_id=payload.source_node_id,
        target_node_id=payload.target_node_id,
        source_handle=payload.source_handle,
        target_handle=payload.target_handle,
    )
    return serialize_product_workflow(workflow)


@router.delete("/workflow-edges/{edge_id}", response_model=ProductWorkflowResponse)
def delete_workflow_edge_endpoint(edge_id: str, session: Session = Depends(get_session)) -> ProductWorkflowResponse:
    workflow = delete_workflow_edge(session, edge_id=edge_id)
    return serialize_product_workflow(workflow)


@router.delete("/workflow-nodes/{node_id}", response_model=ProductWorkflowResponse)
def delete_workflow_node_endpoint(node_id: str, session: Session = Depends(get_session)) -> ProductWorkflowResponse:
    workflow = delete_workflow_node(session, node_id=node_id)
    return serialize_product_workflow(workflow)


@router.post("/products/{product_id}/workflow/run", response_model=ProductWorkflowResponse)
def run_product_workflow_endpoint(
    product_id: str,
    payload: RunWorkflowRequest | None = None,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = submit_product_workflow_run(
        session,
        product_id=product_id,
        start_node_id=payload.start_node_id if payload else None,
    )
    return serialize_product_workflow(workflow)


@router.post("/products/{product_id}/workflow/runs/{run_id}/cancel", response_model=ProductWorkflowResponse)
def cancel_product_workflow_run_endpoint(
    product_id: str,
    run_id: str,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = cancel_product_workflow_run(session, product_id=product_id, run_id=run_id)
    return serialize_product_workflow(workflow)


@router.post(
    "/products/{product_id}/workflow/runs/{run_id}/retry",
    response_model=ProductWorkflowResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_product_workflow_run_endpoint(
    product_id: str,
    run_id: str,
    session: Session = Depends(get_session),
) -> ProductWorkflowResponse:
    workflow = retry_product_workflow_run(session, product_id=product_id, run_id=run_id)
    return serialize_product_workflow(workflow)
