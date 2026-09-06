from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import (
    _execute_workflow_queue_inline,
    _login,
    _make_demo_image_bytes,
    _make_demo_image_bytes_with_size,
    _wait_for_workflow_run,
)

from productflow_backend.application.contracts import (
    PosterGenerationInput,
)
from productflow_backend.application.product_workflow_dependencies import WorkflowExecutionDependencies
from productflow_backend.domain.enums import (
    PosterKind,
    WorkflowNodeStatus,
    WorkflowRunStatus,
)
from productflow_backend.infrastructure.db.models import (
    AppSetting,
    CopySet,
    PosterVariant,
    WorkflowNode,
    WorkflowNodeRun,
    WorkflowRun,
)
from productflow_backend.infrastructure.db.session import get_session_factory


@pytest.fixture(autouse=True)
def _execute_workflow_queue_inline_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep API workflow tests deterministic while production delivery goes through Dramatiq."""

    _execute_workflow_queue_inline(monkeypatch)


def _create_poster_variant_for_binding(
    *,
    product_id: str,
    storage_root: Path,
    write_file: bool,
) -> str:
    session = get_session_factory()()
    try:
        copy_set = CopySet(
            product_id=product_id,
            structured_payload={
                "version": 2,
                "summary": "绑定海报标题",
                "content": {"kind": "blocks", "blocks": [{"id": "headline", "text": "绑定海报文案"}]},
            },
            model_structured_payload={
                "version": 2,
                "summary": "绑定海报标题",
                "content": {"kind": "blocks", "blocks": [{"id": "headline", "text": "绑定海报文案"}]},
            },
            provider_name="test",
            model_name="test",
            prompt_version="test",
        )
        session.add(copy_set)
        session.flush()
        storage_path = f"products/{product_id}/posters/manual-poster.png"
        if write_file:
            poster_path = storage_root / storage_path
            poster_path.parent.mkdir(parents=True, exist_ok=True)
            poster_path.write_bytes(_make_demo_image_bytes())
        poster = PosterVariant(
            product_id=product_id,
            copy_set_id=copy_set.id,
            kind=PosterKind.PROMO_POSTER,
            template_name="test",
            mime_type="image/png",
            storage_path=storage_path,
            width=1024,
            height=1024,
        )
        session.add(poster)
        session.commit()
        return poster.id
    finally:
        session.close()


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _contains_value(value: object, expected: object) -> bool:
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_value(item, expected) for item in value)
    return False


def test_product_workflow_status_endpoint_returns_lightweight_state(db_session) -> None:
    from productflow_backend.application.product_workflows import (
        get_or_create_product_workflow,
        get_product_workflow_status,
        run_product_workflow,
    )
    from productflow_backend.application.use_cases import create_product
    from productflow_backend.presentation.schemas.product_workflows import (
        serialize_product_workflow,
        serialize_product_workflow_status,
    )

    product = create_product(
        db_session,
        name="桌面收纳盒",
        category=None,
        price=None,
        source_note=None,
        image_bytes=_make_demo_image_bytes(),
        filename="box.png",
        content_type="image/png",
    )
    product_id = product.id

    persisted_workflow = get_or_create_product_workflow(db_session, product_id)
    workflow = serialize_product_workflow(persisted_workflow).model_dump(mode="json")
    status_payload = serialize_product_workflow_status(
        get_product_workflow_status(db_session, product_id)
    ).model_dump(mode="json")
    assert status_payload["id"] == workflow["id"]
    assert status_payload["product_id"] == product_id
    assert status_payload["title"] == workflow["title"]
    assert status_payload["active"] is True
    assert status_payload["has_active_workflow"] is False
    assert "edges" not in status_payload
    assert status_payload["nodes"]
    assert set(status_payload["nodes"][0]) == {
        "id",
        "workflow_id",
        "status",
        "failure_reason",
        "is_retryable",
        "attempt_count",
        "retry_count",
        "non_retryable_reason",
        "retry_hint",
        "last_run_at",
        "updated_at",
    }
    counted_node = persisted_workflow.nodes[0]
    base_started_at = datetime(2026, 5, 14, 0, 0)
    for index in range(11):
        run = WorkflowRun(
            workflow_id=persisted_workflow.id,
            status=WorkflowRunStatus.FAILED,
            started_at=base_started_at + timedelta(minutes=index),
            finished_at=base_started_at + timedelta(minutes=index, seconds=30),
            failure_reason=f"历史失败 {index}",
            is_retryable=True,
        )
        db_session.add(run)
        db_session.flush()
        db_session.add(
            WorkflowNodeRun(
                workflow_run_id=run.id,
                node_id=counted_node.id,
                status=WorkflowNodeStatus.FAILED,
                failure_reason=f"历史失败 {index}",
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
        )
    db_session.commit()
    status_with_history = serialize_product_workflow_status(
        get_product_workflow_status(db_session, product_id)
    ).model_dump(mode="json")
    counted_node_payload = next(node for node in status_with_history["nodes"] if node["id"] == counted_node.id)
    assert len(status_with_history["runs"]) == 10
    assert counted_node_payload["attempt_count"] == 11
    assert counted_node_payload["retry_count"] == 10

    run_product_workflow(db_session, product_id=product_id)
    db_session.expire_all()
    run_status_payload = serialize_product_workflow_status(
        get_product_workflow_status(db_session, product_id)
    ).model_dump(mode="json")
    assert run_status_payload["runs"]
    assert set(run_status_payload["runs"][0]) == {
        "id",
        "workflow_id",
        "status",
        "started_at",
        "finished_at",
        "failure_reason",
        "progress_metadata",
        "is_retryable",
        "is_cancelable",
        "queue_active_count",
        "queue_running_count",
        "queue_queued_count",
        "queue_max_concurrent_tasks",
        "queued_ahead_count",
        "queue_position",
        "node_runs",
    }
    assert run_status_payload["runs"][0]["status"] == "succeeded"
    assert run_status_payload["runs"][0]["node_runs"]
    assert set(run_status_payload["runs"][0]["node_runs"][0]) == {
        "id",
        "workflow_run_id",
        "node_id",
        "status",
        "failure_reason",
        "started_at",
        "finished_at",
    }
    assert "output_json" not in run_status_payload["runs"][0]["node_runs"][0]


def test_reference_workflow_node_upload_replaces_current_image(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    _login(client)

    created = client.post(
        "/api/products",
        data={"name": "桌面收纳盒"},
        files={"image": ("box.png", _make_demo_image_bytes(), "image/png")},
    )
    assert created.status_code == 201
    product_id = created.json()["id"]

    workflow_response = client.get(f"/api/products/{product_id}/workflow")
    assert workflow_response.status_code == 200
    reference_node = next(node for node in workflow_response.json()["nodes"] if node["node_type"] == "reference_image")

    first_upload = client.post(
        f"/api/workflow-nodes/{reference_node['id']}/image",
        data={"role": "style", "label": "第一次参考"},
        files={"image": ("first.png", _make_demo_image_bytes(), "image/png")},
    )
    assert first_upload.status_code == 200
    first_node = next(node for node in first_upload.json()["nodes"] if node["id"] == reference_node["id"])
    first_asset_id = first_node["output_json"]["source_asset_ids"][0]
    assert first_node["config_json"]["source_asset_ids"] == [first_asset_id]
    assert first_node["output_json"]["source_asset_ids"] == [first_asset_id]

    second_upload = client.post(
        f"/api/workflow-nodes/{reference_node['id']}/image",
        data={"role": "style", "label": "第二次参考"},
        files={"image": ("second.png", _make_demo_image_bytes_with_size(640, 480), "image/png")},
    )
    assert second_upload.status_code == 200
    second_node = next(node for node in second_upload.json()["nodes"] if node["id"] == reference_node["id"])
    second_asset_id = second_node["output_json"]["source_asset_ids"][0]
    assert second_asset_id != first_asset_id
    assert second_node["config_json"]["source_asset_ids"] == [second_asset_id]
    assert second_node["output_json"]["source_asset_ids"] == [second_asset_id]
    assert second_node["output_json"]["image_asset_ids"] == [second_asset_id]
    assert len(second_node["output_json"]["images"]) == 1

    product_after = client.get(f"/api/products/{product_id}")
    assert product_after.status_code == 200
    reference_asset_ids = {
        asset["id"] for asset in product_after.json()["source_assets"] if asset["kind"] == "reference_image"
    }
    assert {first_asset_id, second_asset_id}.issubset(reference_asset_ids)

def test_reference_workflow_node_can_bind_existing_source_or_poster_image(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    _login(client)

    created = client.post(
        "/api/products",
        data={"name": "桌面灯架"},
        files={"image": ("lamp-stand.png", _make_demo_image_bytes(), "image/png")},
    )
    assert created.status_code == 201
    product_id = created.json()["id"]

    poster_id = _create_poster_variant_for_binding(
        product_id=product_id,
        storage_root=configured_env,
        write_file=True,
    )

    workflow_response = client.get(f"/api/products/{product_id}/workflow")
    assert workflow_response.status_code == 200
    reference_node = next(node for node in workflow_response.json()["nodes"] if node["node_type"] == "reference_image")

    bound_poster = client.post(
        f"/api/workflow-nodes/{reference_node['id']}/image-source",
        json={"poster_variant_id": poster_id},
    )
    assert bound_poster.status_code == 200
    poster_bound_node = next(node for node in bound_poster.json()["nodes"] if node["id"] == reference_node["id"])
    materialized_asset_id = poster_bound_node["output_json"]["source_asset_ids"][0]
    assert poster_bound_node["config_json"]["source_asset_ids"] == [materialized_asset_id]
    assert poster_bound_node["config_json"]["source_poster_variant_id"] == poster_id
    assert poster_bound_node["output_json"]["source_poster_variant_id"] == poster_id

    product_after_poster = client.get(f"/api/products/{product_id}")
    assert product_after_poster.status_code == 200
    reference_assets_after_poster = [
        asset for asset in product_after_poster.json()["source_assets"] if asset["kind"] == "reference_image"
    ]
    materialized_asset = next(asset for asset in reference_assets_after_poster if asset["id"] == materialized_asset_id)
    assert materialized_asset["original_filename"] == f"poster-{poster_id}.png"
    assert materialized_asset["source_poster_variant_id"] == poster_id
    reference_asset_ids_after_poster = [asset["id"] for asset in reference_assets_after_poster]
    assert materialized_asset_id in reference_asset_ids_after_poster

    conflicting_upload = client.post(
        f"/api/products/{product_id}/reference-images",
        files={"reference_images": (f"poster-{poster_id}.png", _make_demo_image_bytes(), "image/png")},
    )
    assert conflicting_upload.status_code == 200
    conflicting_asset = next(
        asset
        for asset in conflicting_upload.json()["source_assets"]
        if asset["kind"] == "reference_image" and asset["id"] != materialized_asset_id
    )
    assert conflicting_asset["original_filename"] == f"poster-{poster_id}.png"
    assert conflicting_asset["source_poster_variant_id"] is None

    rebound_to_user_upload = client.post(
        f"/api/workflow-nodes/{reference_node['id']}/image-source",
        json={"source_asset_id": conflicting_asset["id"]},
    )
    assert rebound_to_user_upload.status_code == 200
    user_upload_bound_node = next(
        node for node in rebound_to_user_upload.json()["nodes"] if node["id"] == reference_node["id"]
    )
    assert user_upload_bound_node["output_json"]["source_asset_ids"] == [conflicting_asset["id"]]
    assert "source_poster_variant_id" not in user_upload_bound_node["output_json"]

    product_after_conflicting_upload = client.get(f"/api/products/{product_id}")
    assert product_after_conflicting_upload.status_code == 200
    reference_asset_ids_after_conflicting_upload = [
        asset["id"]
        for asset in product_after_conflicting_upload.json()["source_assets"]
        if asset["kind"] == "reference_image"
    ]
    assert sorted(reference_asset_ids_after_conflicting_upload) == sorted(
        [*reference_asset_ids_after_poster, conflicting_asset["id"]]
    )

    second_reference = client.post(
        f"/api/products/{product_id}/workflow/nodes",
        json={
            "node_type": "reference_image",
            "title": "复用参考图",
            "position_x": 720,
            "position_y": 320,
            "config_json": {"role": "reference", "label": "复用参考图"},
        },
    )
    assert second_reference.status_code == 201
    second_reference_node = next(node for node in second_reference.json()["nodes"] if node["title"] == "复用参考图")

    bound_source = client.post(
        f"/api/workflow-nodes/{second_reference_node['id']}/image-source",
        json={"source_asset_id": materialized_asset_id},
    )
    assert bound_source.status_code == 200
    source_bound_node = next(node for node in bound_source.json()["nodes"] if node["id"] == second_reference_node["id"])
    assert source_bound_node["output_json"]["source_asset_ids"] == [materialized_asset_id]
    assert source_bound_node["config_json"]["source_asset_ids"] == [materialized_asset_id]
    assert source_bound_node["config_json"]["source_poster_variant_id"] == poster_id
    assert source_bound_node["output_json"]["source_poster_variant_id"] == poster_id

    product_after_source = client.get(f"/api/products/{product_id}")
    assert product_after_source.status_code == 200
    reference_asset_ids_after_source = [
        asset["id"] for asset in product_after_source.json()["source_assets"] if asset["kind"] == "reference_image"
    ]
    assert sorted(reference_asset_ids_after_source) == sorted(reference_asset_ids_after_conflicting_upload)

    third_reference = client.post(
        f"/api/products/{product_id}/workflow/nodes",
        json={
            "node_type": "reference_image",
            "title": "复用海报",
            "position_x": 980,
            "position_y": 320,
            "config_json": {"role": "reference", "label": "复用海报"},
        },
    )
    assert third_reference.status_code == 201
    third_reference_node = next(node for node in third_reference.json()["nodes"] if node["title"] == "复用海报")

    rebound_poster = client.post(
        f"/api/workflow-nodes/{third_reference_node['id']}/image-source",
        json={"poster_variant_id": poster_id},
    )
    assert rebound_poster.status_code == 200
    rebound_node = next(node for node in rebound_poster.json()["nodes"] if node["id"] == third_reference_node["id"])
    assert rebound_node["output_json"]["source_asset_ids"] == [materialized_asset_id]
    assert rebound_node["output_json"]["source_poster_variant_id"] == poster_id

    product_after_rebound = client.get(f"/api/products/{product_id}")
    assert product_after_rebound.status_code == 200
    reference_asset_ids_after_rebound = [
        asset["id"] for asset in product_after_rebound.json()["source_assets"] if asset["kind"] == "reference_image"
    ]
    assert sorted(reference_asset_ids_after_rebound) == sorted(reference_asset_ids_after_conflicting_upload)

def test_reference_workflow_node_bind_poster_reports_missing_file_as_bad_request(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    _login(client)

    created = client.post(
        "/api/products",
        data={"name": "文件缺失海报"},
        files={"image": ("missing-poster.png", _make_demo_image_bytes(), "image/png")},
    )
    assert created.status_code == 201
    product_id = created.json()["id"]

    poster_id = _create_poster_variant_for_binding(
        product_id=product_id,
        storage_root=configured_env,
        write_file=False,
    )

    workflow_response = client.get(f"/api/products/{product_id}/workflow")
    assert workflow_response.status_code == 200
    reference_node = next(node for node in workflow_response.json()["nodes"] if node["node_type"] == "reference_image")

    response = client.post(
        f"/api/workflow-nodes/{reference_node['id']}/image-source",
        json={"poster_variant_id": poster_id},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "海报文件不存在"

def test_image_generation_fill_replaces_reference_node_current_image(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    _login(client)

    created = client.post(
        "/api/products",
        data={"name": "床头灯"},
        files={"image": ("lamp.png", _make_demo_image_bytes(), "image/png")},
    )
    assert created.status_code == 201
    product_id = created.json()["id"]

    workflow_response = client.get(f"/api/products/{product_id}/workflow")
    assert workflow_response.status_code == 200
    workflow = workflow_response.json()
    image_node = next(node for node in workflow["nodes"] if node["node_type"] == "image_generation")
    reference_node = next(node for node in workflow["nodes"] if node["node_type"] == "reference_image")

    upload = client.post(
        f"/api/workflow-nodes/{reference_node['id']}/image",
        data={"role": "reference", "label": "旧参考"},
        files={"image": ("old.png", _make_demo_image_bytes(), "image/png")},
    )
    assert upload.status_code == 200
    uploaded_reference = next(node for node in upload.json()["nodes"] if node["id"] == reference_node["id"])
    old_asset_id = uploaded_reference["output_json"]["source_asset_ids"][0]

    connected = client.post(
        f"/api/products/{product_id}/workflow/edges",
        json={
            "source_node_id": image_node["id"],
            "target_node_id": reference_node["id"],
            "source_handle": "output",
            "target_handle": "input",
        },
    )
    assert connected.status_code == 201

    run_response = client.post(f"/api/products/{product_id}/workflow/run", json={"start_node_id": image_node["id"]})
    assert run_response.status_code == 200
    payload = _wait_for_workflow_run(client, product_id, status="succeeded")
    filled_reference = next(node for node in payload["nodes"] if node["id"] == reference_node["id"])
    new_asset_id = filled_reference["output_json"]["source_asset_ids"][0]
    assert new_asset_id != old_asset_id
    assert filled_reference["config_json"]["source_asset_ids"] == [new_asset_id]
    assert filled_reference["output_json"]["source_asset_ids"] == [new_asset_id]
    assert len(filled_reference["output_json"]["images"]) == 1

    product_after = client.get(f"/api/products/{product_id}")
    assert product_after.status_code == 200
    reference_asset_ids = {
        asset["id"] for asset in product_after.json()["source_assets"] if asset["kind"] == "reference_image"
    }
    assert {old_asset_id, new_asset_id}.issubset(reference_asset_ids)

def test_image_generation_fills_multiple_targets_with_concurrent_provider_calls(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from productflow_backend.infrastructure.image.base import GeneratedImagePayload
    from productflow_backend.presentation.api import create_app

    session = get_session_factory()()
    try:
        session.add(AppSetting(key="poster_generation_mode", value="generated"))
        session.commit()
    finally:
        session.close()

    class CoordinatedImageProvider:
        provider_name = "coordinated"
        prompt_version = "coordinated-v1"

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._both_started = threading.Event()
            self.started = 0
            self.max_in_flight = 0
            self._in_flight = 0
            self.thread_ids: list[int] = []

        def generate_poster_image(
            self,
            poster: PosterGenerationInput,
            kind: PosterKind,
        ) -> tuple[GeneratedImagePayload, str]:
            del poster
            with self._lock:
                self.thread_ids.append(threading.get_ident())
                self.started += 1
                self._in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self._in_flight)
                call_index = self.started
                if self.started >= 2:
                    self._both_started.set()
            if not self._both_started.wait(timeout=1.0):
                raise AssertionError("provider calls were not initiated concurrently")
            try:
                return (
                    GeneratedImagePayload(
                        kind=kind,
                        bytes_data=_make_demo_image_bytes(),
                        mime_type="image/png",
                        width=800,
                        height=800,
                        variant_label=f"coordinated-{call_index}",
                    ),
                    "coordinated-v1",
                )
            finally:
                with self._lock:
                    self._in_flight -= 1

    fake_provider = CoordinatedImageProvider()
    provider_factory_thread_ids: list[int] = []

    def fake_provider_factory() -> CoordinatedImageProvider:
        provider_factory_thread_ids.append(threading.get_ident())
        return fake_provider

    _execute_workflow_queue_inline(
        monkeypatch,
        dependencies=WorkflowExecutionDependencies(
            image_provider_resolver=fake_provider_factory,
        ),
    )

    app = create_app()
    client = TestClient(app)
    _login(client)

    created = client.post(
        "/api/products",
        data={"name": "并发生图商品"},
        files={"image": ("parallel.png", _make_demo_image_bytes(), "image/png")},
    )
    assert created.status_code == 201
    product_id = created.json()["id"]

    workflow_response = client.get(f"/api/products/{product_id}/workflow")
    assert workflow_response.status_code == 200
    workflow = workflow_response.json()
    image_node = next(node for node in workflow["nodes"] if node["node_type"] == "image_generation")
    second_target = client.post(
        f"/api/products/{product_id}/workflow/nodes",
        json={
            "node_type": "reference_image",
            "title": "并发参考图 2",
            "position_x": 1180,
            "position_y": 240,
            "config_json": {"role": "reference", "label": "并发参考图 2"},
        },
    )
    assert second_target.status_code == 201
    second_target_node = next(node for node in second_target.json()["nodes"] if node["title"] == "并发参考图 2")
    connected = client.post(
        f"/api/products/{product_id}/workflow/edges",
        json={
            "source_node_id": image_node["id"],
            "target_node_id": second_target_node["id"],
            "source_handle": "output",
            "target_handle": "input",
        },
    )
    assert connected.status_code == 201

    run_response = client.post(f"/api/products/{product_id}/workflow/run", json={})
    assert run_response.status_code == 200
    payload = _wait_for_workflow_run(client, product_id, status="succeeded")
    image_output = next(node for node in payload["nodes"] if node["id"] == image_node["id"])["output_json"]
    assert len(provider_factory_thread_ids) == 2
    assert set(provider_factory_thread_ids).isdisjoint(fake_provider.thread_ids)
    assert fake_provider.started == 2
    assert fake_provider.max_in_flight == 2
    assert image_output["target_count"] == 2
    assert len(image_output["filled_reference_node_ids"]) == 2
    assert len(image_output["filled_source_asset_ids"]) == 2
    assert len(image_output["generated_poster_variant_ids"]) == 2
    assert "poster_variant_ids" not in image_output


def test_image_generation_batches_downstream_targets_with_batch_provider(
    configured_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from productflow_backend.infrastructure.image.base import GeneratedImagePayload
    from productflow_backend.presentation.api import create_app

    session = get_session_factory()()
    try:
        session.add(AppSetting(key="poster_generation_mode", value="generated"))
        session.commit()
    finally:
        session.close()

    class BatchImageProvider:
        provider_name = "batch"
        prompt_version = "batch-v1"

        def __init__(self) -> None:
            self.batch_counts: list[int] = []
            self.single_calls = 0

        def generate_poster_images(
            self,
            poster: PosterGenerationInput,
            kind: PosterKind,
            count: int,
        ) -> list[tuple[GeneratedImagePayload, str]]:
            assert poster.tool_options is None or "n" not in poster.tool_options
            self.batch_counts.append(count)
            return [
                (
                    GeneratedImagePayload(
                        kind=kind,
                        bytes_data=_make_demo_image_bytes(),
                        mime_type="image/png",
                        width=800,
                        height=800,
                        variant_label=f"batch-{index}",
                    ),
                    "batch-v1",
                )
                for index in range(1, count + 1)
            ]

        def generate_poster_image(
            self,
            poster: PosterGenerationInput,
            kind: PosterKind,
        ) -> tuple[GeneratedImagePayload, str]:
            del poster, kind
            self.single_calls += 1
            raise AssertionError("batch provider should receive one generate_poster_images call")

    fake_provider = BatchImageProvider()
    provider_factory_thread_ids: list[int] = []

    def fake_provider_factory() -> BatchImageProvider:
        provider_factory_thread_ids.append(threading.get_ident())
        return fake_provider

    _execute_workflow_queue_inline(
        monkeypatch,
        dependencies=WorkflowExecutionDependencies(
            image_provider_resolver=fake_provider_factory,
        ),
    )

    app = create_app()
    client = TestClient(app)
    _login(client)

    created = client.post(
        "/api/products",
        data={"name": "批量承接商品"},
        files={"image": ("batch.png", _make_demo_image_bytes(), "image/png")},
    )
    assert created.status_code == 201
    product_id = created.json()["id"]

    workflow_response = client.get(f"/api/products/{product_id}/workflow")
    assert workflow_response.status_code == 200
    workflow = workflow_response.json()
    image_node = next(node for node in workflow["nodes"] if node["node_type"] == "image_generation")
    second_target = client.post(
        f"/api/products/{product_id}/workflow/nodes",
        json={
            "node_type": "reference_image",
            "title": "批量参考图 2",
            "position_x": 1180,
            "position_y": 240,
            "config_json": {"role": "reference", "label": "批量参考图 2"},
        },
    )
    assert second_target.status_code == 201
    second_target_node = next(node for node in second_target.json()["nodes"] if node["title"] == "批量参考图 2")
    connected = client.post(
        f"/api/products/{product_id}/workflow/edges",
        json={
            "source_node_id": image_node["id"],
            "target_node_id": second_target_node["id"],
            "source_handle": "output",
            "target_handle": "input",
        },
    )
    assert connected.status_code == 201

    run_response = client.post(f"/api/products/{product_id}/workflow/run", json={})
    assert run_response.status_code == 200
    payload = _wait_for_workflow_run(client, product_id, status="succeeded")
    image_output = next(node for node in payload["nodes"] if node["id"] == image_node["id"])["output_json"]
    assert provider_factory_thread_ids
    assert fake_provider.batch_counts == [2]
    assert fake_provider.single_calls == 0
    assert image_output["target_count"] == 2
    assert len(image_output["filled_reference_node_ids"]) == 2
    assert len(image_output["filled_source_asset_ids"]) == 2
    assert len(image_output["generated_poster_variant_ids"]) == 2
    assert [result["target_index"] for result in image_output["provider_results"]] == [1, 2]


def test_workflow_node_can_be_deleted_with_connected_edges(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    _login(client)

    created = client.post(
        "/api/products",
        data={"name": "可删节点商品"},
        files={"image": ("node.png", _make_demo_image_bytes(), "image/png")},
    )
    assert created.status_code == 201
    product_id = created.json()["id"]
    workflow_response = client.get(f"/api/products/{product_id}/workflow")
    assert workflow_response.status_code == 200
    workflow = workflow_response.json()
    copy_node = next(node for node in workflow["nodes"] if node["node_type"] == "copy_generation")
    connected_edge_ids = {
        edge["id"]
        for edge in workflow["edges"]
        if edge["source_node_id"] == copy_node["id"] or edge["target_node_id"] == copy_node["id"]
    }
    assert connected_edge_ids

    deleted = client.delete(f"/api/workflow-nodes/{copy_node['id']}")
    assert deleted.status_code == 200
    deleted_payload = deleted.json()
    assert copy_node["id"] not in {node["id"] for node in deleted_payload["nodes"]}
    assert all(
        edge["source_node_id"] != copy_node["id"] and edge["target_node_id"] != copy_node["id"]
        for edge in deleted_payload["edges"]
    )
    assert connected_edge_ids.isdisjoint({edge["id"] for edge in deleted_payload["edges"]})

    refreshed = client.get(f"/api/products/{product_id}/workflow")
    assert refreshed.status_code == 200
    assert copy_node["id"] not in {node["id"] for node in refreshed.json()["nodes"]}


def test_duplicate_workflow_node_group_sanitizes_artifacts_omits_product_context_and_preserves_internal_edges(
    configured_env: Path,
) -> None:
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    _login(client)

    created = client.post(
        "/api/products",
        data={"name": "复制节点组商品"},
        files={"image": ("duplicate.png", _make_demo_image_bytes(), "image/png")},
    )
    assert created.status_code == 201
    product_id = created.json()["id"]
    workflow_response = client.get(f"/api/products/{product_id}/workflow")
    assert workflow_response.status_code == 200
    workflow = workflow_response.json()
    context_node = next(node for node in workflow["nodes"] if node["node_type"] == "product_context")
    copy_node = next(node for node in workflow["nodes"] if node["node_type"] == "copy_generation")
    image_node = next(node for node in workflow["nodes"] if node["node_type"] == "image_generation")
    reference_node = next(node for node in workflow["nodes"] if node["node_type"] == "reference_image")
    original_ids = {node["id"] for node in workflow["nodes"]}

    session = get_session_factory()()
    try:
        persisted_copy = session.get(WorkflowNode, copy_node["id"])
        persisted_image = session.get(WorkflowNode, image_node["id"])
        persisted_reference = session.get(WorkflowNode, reference_node["id"])
        assert persisted_copy is not None
        assert persisted_image is not None
        assert persisted_reference is not None
        persisted_copy.config_json = {
            "instruction": "保留文案方向",
            "copy_set_id": "copy-artifact",
        }
        persisted_copy.output_json = {"copy_set_id": "copy-artifact", "summary": "不应复制的文案"}
        persisted_copy.status = WorkflowNodeStatus.SUCCEEDED
        persisted_image.config_json = {
            "instruction": "保留生图方向",
            "size": "1024x1024",
            "generated_poster_variant_ids": ["poster-artifact"],
            "filled_source_asset_ids": ["asset-artifact"],
        }
        persisted_image.output_json = {
            "generated_poster_variant_ids": ["poster-artifact"],
            "filled_source_asset_ids": ["asset-artifact"],
        }
        persisted_image.status = WorkflowNodeStatus.FAILED
        persisted_image.failure_reason = "不应复制的失败状态"
        persisted_reference.config_json = {
            "role": "reference",
            "label": "保留参考图标签",
            "source_asset_ids": ["asset-artifact"],
            "source_poster_variant_id": "poster-artifact",
        }
        persisted_reference.output_json = {
            "source_asset_ids": ["asset-artifact"],
            "image_asset_ids": ["asset-artifact"],
        }
        persisted_reference.status = WorkflowNodeStatus.SUCCEEDED
        run = WorkflowRun(workflow_id=workflow["id"], status=WorkflowRunStatus.SUCCEEDED)
        session.add(run)
        session.flush()
        session.add(
            WorkflowNodeRun(
                workflow_run_id=run.id,
                node_id=persisted_image.id,
                status=WorkflowNodeStatus.SUCCEEDED,
                output_json={"poster_variant_id": "poster-artifact"},
                poster_variant_id=None,
            )
        )
        session.commit()
    finally:
        session.close()

    duplicated = client.post(
        f"/api/products/{product_id}/workflow/node-groups/duplicate",
        json={
            "node_ids": [context_node["id"], copy_node["id"], image_node["id"], reference_node["id"]],
            "position_x": 1200,
            "position_y": 600,
        },
    )

    assert duplicated.status_code == 201
    payload = duplicated.json()
    created_nodes = [node for node in payload["nodes"] if node["id"] not in original_ids]
    created_ids = {node["id"] for node in created_nodes}
    assert len(created_nodes) == 3
    assert [node["node_type"] for node in created_nodes].count("product_context") == 0
    assert {node["node_type"] for node in created_nodes} == {
        "copy_generation",
        "image_generation",
        "reference_image",
    }
    assert all(node["status"] == "idle" for node in created_nodes)
    assert all(node["output_json"] is None for node in created_nodes)
    assert all(node["failure_reason"] is None for node in created_nodes)
    assert all(node["last_run_at"] is None for node in created_nodes)
    assert not any(_contains_key(node["config_json"], "copy_set_id") for node in created_nodes)
    assert not any(_contains_value(node["config_json"], "copy-artifact") for node in created_nodes)
    assert not any(_contains_value(node["config_json"], "poster-artifact") for node in created_nodes)
    assert not any(_contains_value(node["config_json"], "asset-artifact") for node in created_nodes)

    created_by_type = {node["node_type"]: node for node in created_nodes}
    assert created_by_type["copy_generation"]["position_x"] == 1200
    assert created_by_type["copy_generation"]["position_y"] == 600
    assert created_by_type["reference_image"]["config_json"] == {
        "role": "reference",
        "label": "保留参考图标签",
    }

    node_types_by_id = {node["id"]: node["node_type"] for node in payload["nodes"]}
    duplicated_internal_edges = [
        edge
        for edge in payload["edges"]
        if edge["source_node_id"] in created_ids and edge["target_node_id"] in created_ids
    ]
    assert {
        (node_types_by_id[edge["source_node_id"]], node_types_by_id[edge["target_node_id"]])
        for edge in duplicated_internal_edges
    } == {("copy_generation", "image_generation"), ("image_generation", "reference_image")}
    assert not any(
        edge["source_node_id"] == context_node["id"] and edge["target_node_id"] in created_ids
        for edge in payload["edges"]
    )

    session = get_session_factory()()
    try:
        assert session.query(WorkflowNodeRun).filter(WorkflowNodeRun.node_id.in_(created_ids)).count() == 0
    finally:
        session.close()
