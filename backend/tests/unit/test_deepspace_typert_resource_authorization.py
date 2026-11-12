from __future__ import annotations

import uuid
from importlib import import_module
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, Table, Uuid, create_engine
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.deepspace.models.conversation import Conversation
from app.deepspace.typert import api
from app.deepspace.typert.descriptors import RemoteContribution, RemoteDescriptor
from app.deepspace.typert.errors import RemoteError
from app.deepspace.typert.registry import TypertRegistry
from app.deepspace.typert.resource_authorization import built_in_resource_authorizers
from app.deepspace.typert.resources import ResourceKindRegistry, resource

pytestmark = pytest.mark.unit_no_db


def _auth(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    permissions: frozenset[str] = frozenset({"queries:run"}),
) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=frozenset({"user"}),
        token_id="test",
        permissions=permissions,
    )


def test_conversation_resource_is_authorized_only_for_its_tenant_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Conversation.__table__.create(engine)
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            Conversation.__table__.insert().values(
                id=conversation_id,
                tenant_id=tenant_id,
                user_id=owner_id,
                title="authorized test row",
            )
        )

    kinds = ResourceKindRegistry()
    kinds.register(
        "conversation",
        authorize=built_in_resource_authorizers()["conversation"],
    )
    descriptor = RemoteDescriptor(
        id="test/read-conversation",
        service="test",
        namespace="test",
        method="read",
        implementation=lambda: "ok",
        scope="queries:run",
        resources=(resource("conversation", conversation_id),),
    )
    registry = TypertRegistry(resource_kinds=kinds)
    registry.register(RemoteContribution(package="test", descriptors=(descriptor,)))
    monkeypatch.setattr(api, "get_typert_service", lambda: type("S", (), {"registry": registry})())

    owner = _auth(tenant_id, owner_id)
    another_user = _auth(tenant_id, uuid.uuid4())
    another_tenant = _auth(uuid.uuid4(), owner_id)
    with Session(engine) as db:
        assert api._authorize_invocation(owner, descriptor.id, db) is descriptor
        for outsider in (another_user, another_tenant):
            with pytest.raises(RemoteError) as error:
                api._authorize_invocation(outsider, descriptor.id, db)
            assert error.value.code == "resource/denied"
        with pytest.raises(RemoteError) as missing_db:
            api._authorize_invocation(owner, descriptor.id)
        assert missing_db.value.code == "resource/denied"


def test_resource_authorizers_cover_supported_owner_stores() -> None:
    authorizers = built_in_resource_authorizers()
    assert set(authorizers) == {
        "workspace",
        "library",
        "conversation",
        "document",
        "terminal",
        "ssh-host",
        "plugin",
        "artifact",
        "job",
        "schedule",
    }


def test_workspace_plugin_authorization_uses_the_callers_workspace_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from app.core import config
    from app.deepspace.services import plugins, workspace_registry_db

    owner = _auth(uuid.uuid4(), uuid.uuid4())
    workspace_root = tmp_path / "owned-workspace"
    workspace_root.mkdir()
    observed: dict[str, object] = {}

    class EmptySharedRegistry:
        @staticmethod
        def get(_plugin_id):
            return None

    class WorkspaceRegistry:
        def __init__(self, _db, settings, tenant_id, user_id):
            observed.update(settings=settings, tenant_id=tenant_id, user_id=user_id)

        def get_all_projects(self):
            if observed["user_id"] != owner.user_id:
                return []
            return [SimpleNamespace(path=str(workspace_root))]

    monkeypatch.setattr(plugins, "plugin_registry", lambda: EmptySharedRegistry())
    monkeypatch.setattr(
        plugins,
        "discover_workspace_plugins",
        lambda root: [SimpleNamespace(id="workspace-plugin")] if root == workspace_root else [],
    )
    monkeypatch.setattr(config, "get_settings", lambda: object())
    monkeypatch.setattr(workspace_registry_db, "WorkspaceRegistryService", WorkspaceRegistry)

    authorizer = built_in_resource_authorizers()["plugin"]
    assert authorizer(object(), owner, "workspace-plugin") is True
    other_user = _auth(owner.tenant_id, uuid.uuid4())
    assert authorizer(object(), other_user, "workspace-plugin") is False
    assert observed["tenant_id"] == owner.tenant_id


def test_deployment_plugin_is_authorized_from_the_shared_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import plugins

    manifest = SimpleNamespace(id="shared-plugin", source="package")

    class SharedRegistry:
        @staticmethod
        def get(plugin_id):
            return manifest if plugin_id == manifest.id else None

    monkeypatch.setattr(plugins, "plugin_registry", lambda: SharedRegistry())

    assert (
        built_in_resource_authorizers()["plugin"](
            object(), _auth(uuid.uuid4(), uuid.uuid4()), "shared-plugin"
        )
        is True
    )


def test_terminal_resource_authorizer_delegates_to_live_session_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import terminals

    observed: dict[str, object] = {}

    class Manager:
        def owns_identity(self, identity, *, tenant_id, user_id):
            observed.update(identity=identity, tenant_id=tenant_id, user_id=user_id)
            return identity == "owned-session"

    monkeypatch.setattr(terminals, "terminal_manager", lambda: Manager())
    authorizer = built_in_resource_authorizers()["terminal"]
    auth = _auth(uuid.uuid4(), uuid.uuid4())
    assert authorizer(None, auth, "owned-session") is True
    assert authorizer(None, auth, "other-session") is False
    assert observed == {
        "identity": "other-session",
        "tenant_id": auth.tenant_id,
        "user_id": auth.user_id,
    }


def test_uuid_authorization_rejects_bad_ids_and_checks_tenant_and_user() -> None:
    from app.deepspace.typert.resource_authorization import _owns_uuid_row

    table = Table(
        "authorization_test_rows",
        MetaData(),
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("tenant_id", Uuid(as_uuid=True), nullable=False),
        Column("user_id", Uuid(as_uuid=True), nullable=False),
    )
    model = SimpleNamespace(
        id=table.c.id,
        tenant_id=table.c.tenant_id,
        user_id=table.c.user_id,
    )
    auth = _auth(uuid.uuid4(), uuid.uuid4())

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class SessionStub:
        def __init__(self, value):
            self.value = value
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)
            return Result(self.value)

    owned = SessionStub(uuid.uuid4())
    assert _owns_uuid_row(model, str(uuid.uuid4()), auth, owned, user_column=model.user_id) is True
    assert len(owned.statements) == 1

    missing = SessionStub(None)
    assert (
        _owns_uuid_row(model, str(uuid.uuid4()), auth, missing, user_column=model.user_id) is False
    )
    assert len(missing.statements) == 1

    tenant_owned = SessionStub(uuid.uuid4())
    assert _owns_uuid_row(model, str(uuid.uuid4()), auth, tenant_owned) is True
    assert len(tenant_owned.statements) == 1

    invalid = SessionStub(uuid.uuid4())
    assert _owns_uuid_row(model, "not-a-uuid", auth, invalid) is False
    assert invalid.statements == []


def test_each_database_resource_adapter_uses_its_owner_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.typert import resource_authorization

    cases = (
        ("conversation", "app.deepspace.models.conversation", "Conversation", "user_id"),
        (
            "workspace",
            "app.deepspace.models.workspace_registry",
            "WorkspaceRegistry",
            "user_id",
        ),
        (
            "library",
            "app.deepspace.models.workspace_file",
            "DeepSpaceWorkspaceFile",
            "user_id",
        ),
        ("document", "app.documents.models.document", "Document", "uploaded_by_user_id"),
        ("ssh-host", "app.deepspace.models.ssh_host", "DeepSpaceSshHost", "user_id"),
        (
            "artifact",
            "app.deepspace.models.media_artifact",
            "DeepSpaceMediaArtifact",
            "user_id",
        ),
        ("job", "app.deepspace.models.workflow_job", "DeepSpaceWorkflowJob", "user_id"),
        ("schedule", "app.deepspace.models.schedule", "DeepSpaceSchedule", "user_id"),
    )
    authorizers = built_in_resource_authorizers()
    observed = []

    def capture(model, identity, auth, db, *, user_column=None):
        observed.append((model, identity, auth, db, user_column))
        return True

    monkeypatch.setattr(resource_authorization, "_owns_uuid_row", capture)
    auth = _auth(uuid.uuid4(), uuid.uuid4())
    fake_db = object()
    resource_id = str(uuid.uuid4())
    for kind, module_name, symbol, user_attribute in cases:
        model = getattr(import_module(module_name), symbol)
        assert authorizers[kind](fake_db, auth, resource_id) is True
        assert observed[-1] == (
            model,
            resource_id,
            auth,
            fake_db,
            getattr(model, user_attribute),
        )


def test_workspace_plugin_authorization_rejects_workspace_registry_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from app.core import config
    from app.deepspace.services import plugins, workspace_registry_db

    class WorkspaceManifestRegistry:
        @staticmethod
        def get(_plugin_id):
            return SimpleNamespace(source="workspace")

    class EmptyWorkspaceRegistry:
        def __init__(self, *_args):
            pass

        def get_all_projects(self):
            return [
                SimpleNamespace(path=str(tmp_path / "missing")),
                SimpleNamespace(path=str(tmp_path)),
            ]

    monkeypatch.setattr(plugins, "plugin_registry", lambda: WorkspaceManifestRegistry())
    monkeypatch.setattr(
        plugins,
        "discover_workspace_plugins",
        lambda _root: [SimpleNamespace(id="another-plugin")],
    )
    monkeypatch.setattr(config, "get_settings", lambda: object())
    monkeypatch.setattr(
        workspace_registry_db,
        "WorkspaceRegistryService",
        EmptyWorkspaceRegistry,
    )
    authorize = built_in_resource_authorizers()["plugin"]
    auth = _auth(uuid.uuid4(), uuid.uuid4())
    assert authorize(object(), auth, " ") is False
    assert authorize(object(), auth, "x" * 257) is False
    assert authorize(object(), auth, "workspace-plugin") is False


def test_unconfigured_resource_kind_stays_denied() -> None:
    kinds = ResourceKindRegistry()
    kinds.register("unmanaged", description="no authoritative owner store")
    descriptor = RemoteDescriptor(
        id="test/unmanaged",
        service="test",
        namespace="test",
        method="read",
        implementation=lambda: None,
        scope="queries:run",
        resources=(resource("unmanaged", "resource-1"),),
    )
    registry = TypertRegistry(resource_kinds=kinds)
    registry.register(RemoteContribution(package="test", descriptors=(descriptor,)))

    class Service:
        def __init__(self):
            self.registry = registry

    original = api.get_typert_service
    api.get_typert_service = lambda: Service()
    try:
        with pytest.raises(RemoteError) as error:
            api._authorize_invocation(_auth(uuid.uuid4(), uuid.uuid4()), descriptor.id, object())
        assert error.value.code == "resource/denied"
    finally:
        api.get_typert_service = original
