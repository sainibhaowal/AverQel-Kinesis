from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import String, delete, exists, select, text, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.ids import generate_uuid7_with_fallback
from app.deepspace.models.agent_memory import AgentMemory
from app.deepspace.models.agent_runtime import (
    DeepSpaceAgentRun,
    DeepSpaceAgentStep,
    DeepSpaceRunEvent,
)
from app.deepspace.models.conversation import Conversation
from app.deepspace.models.media_artifact import DeepSpaceMediaArtifact
from app.deepspace.models.message import Message
from app.deepspace.models.message_version import MessageVersion
from app.deepspace.models.mission_snapshot import DeepSpaceMissionSnapshot
from app.deepspace.models.queued_turn import DeepSpaceQueuedTurn
from app.deepspace.models.workspace_file import DeepSpaceWorkspaceFile
from app.deepspace.models.workspace_file_version import DeepSpaceWorkspaceFileVersion
from app.deepspace.models.workspace_folder import DeepSpaceWorkspaceFolder
from app.deepspace.services.context_cache import DeepSpaceContextCache
from app.system.models.storage_cleanup import StorageCleanupJob
from app.system.models.storage_lifecycle import StorageLifecycleItem
from app.system.services.storage_lifecycle import StorageLifecycleService
from app.system.services.storage_quota import StorageQuotaService
from app.system.services.storage_service import StorageService

logger = logging.getLogger(__name__)


class DeepSpaceChatRepository:
    """Persistence operations for DeepSpace conversations only."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None = None,
        title: str = "Untitled Note",
        content_html: str | None = None,
        kind: str = "deepspace",
    ) -> Conversation:
        conversation = Conversation(
            id=conversation_id or generate_uuid7_with_fallback(),
            tenant_id=tenant_id,
            user_id=user_id,
            title=title,
            kind=kind,
            content_html=content_html,
        )
        StorageQuotaService(self.db).ensure_capacity(
            tenant_id=tenant_id,
            user_id=user_id,
            additional_bytes=StorageQuotaService.estimate_bytes(title, content_html),
        )
        self.db.add(conversation)
        self.db.flush()
        StorageLifecycleService(self.db).record_activity(
            tenant_id=tenant_id,
            owner_user_id=user_id,
            category="chat_history",
            source_type="conversation",
            source_id=str(conversation.id),
            activity_kind="conversation_created",
            size_bytes=StorageQuotaService.estimate_bytes(title, content_html),
        )
        return conversation

    def fork_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        source_conversation_id: uuid.UUID,
        title: str | None = None,
        copy_files: bool = False,
        copy_messages: bool = True,
        copy_events: bool = True,
        fork_turn: int | None = None,
        fork_message_id: uuid.UUID | None = None,
    ) -> Conversation | None:
        """Create a new conversation forked from an existing one.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            source_conversation_id: The conversation to fork from.
            title: Optional title for the new conversation.
            copy_files: Whether to copy associated files/attachments.
            copy_messages: Whether to copy conversation messages.
            copy_events: Whether to copy session events.
            fork_turn: Optional zero-based turn boundary.
            fork_message_id: Optional message boundary, inclusive.

        Returns:
            The new conversation, or None if the source doesn't exist.
        """
        source = self.get_conversation(
            tenant_id=tenant_id,
            conversation_id=source_conversation_id,
            user_id=user_id,
        )
        if source is None:
            return None

        if fork_turn is not None and fork_turn < 0:
            raise ValueError("Fork turn must be zero or greater.")

        from app.deepspace.models.session_event import DeepSpaceSessionEvent

        source_messages = list(
            self.db.execute(
                select(Message)
                .where(Message.conversation_id == source_conversation_id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
            .scalars()
            .all()
        )
        fork_message = None
        if fork_message_id is not None:
            fork_message = self.db.execute(
                select(Message).where(
                    Message.id == fork_message_id,
                    Message.conversation_id == source_conversation_id,
                )
            ).scalar_one_or_none()
            if fork_message is None:
                raise ValueError("The fork message does not belong to the source conversation.")

        selected_messages = source_messages
        if fork_message is not None:
            selected_messages = source_messages[: source_messages.index(fork_message) + 1]
        elif fork_turn is not None:
            turn_events = list(
                self.db.execute(
                    select(DeepSpaceSessionEvent)
                    .where(
                        DeepSpaceSessionEvent.tenant_id == tenant_id,
                        DeepSpaceSessionEvent.user_id == user_id,
                        DeepSpaceSessionEvent.conversation_id == source_conversation_id,
                        DeepSpaceSessionEvent.turn.isnot(None),
                        DeepSpaceSessionEvent.turn <= fork_turn,
                    )
                    .order_by(DeepSpaceSessionEvent.seq.asc())
                )
                .scalars()
                .all()
            )
            if turn_events:
                cutoff = max(event.created_at for event in turn_events)
                selected_messages = [
                    message for message in source_messages if message.created_at <= cutoff
                ]
            else:
                # Legacy conversations have no turn-tagged events.  Their
                # durable message order is user/assistant pairs, so retain
                # the requested number of complete pairs.
                selected_messages = source_messages[: 2 * (fork_turn + 1)]

        if not copy_messages:
            selected_messages = []
        selected_message_ids = {message.id for message in selected_messages}
        source_events = list(
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == source_conversation_id,
                )
                .order_by(DeepSpaceSessionEvent.seq.asc())
            )
            .scalars()
            .all()
        )
        if not copy_events:
            selected_event_rows = []
        elif fork_message is not None:
            selected_event_rows = [
                event for event in source_events if event.created_at <= fork_message.created_at
            ]
        elif fork_turn is not None:
            selected_event_rows = [
                event
                for event in source_events
                if event.turn is not None and event.turn <= fork_turn
            ]
        else:
            selected_event_rows = source_events

        new_title = title or f"{source.title} (fork)"
        new_conversation = self.create_conversation(
            tenant_id=tenant_id,
            user_id=user_id,
            title=new_title,
            content_html=source.content_html,
            kind=source.kind,
        )

        message_ids: dict[uuid.UUID, uuid.UUID] = {}
        source_versions = list(
            self.db.execute(
                select(MessageVersion).where(MessageVersion.message_id.in_(selected_message_ids))
            )
            .scalars()
            .all()
        )
        versions_by_message: dict[uuid.UUID, list[MessageVersion]] = {}
        for version in source_versions:
            versions_by_message.setdefault(version.message_id, []).append(version)

        pending_active_versions: list[tuple[Message, uuid.UUID | None]] = []
        for msg in selected_messages:
            new_message_id = uuid.uuid4()
            message_ids[msg.id] = new_message_id
            new_msg = Message(
                id=new_message_id,
                conversation_id=new_conversation.id,
                role=msg.role,
                content=msg.content,
                metadata_json=msg.metadata_json,
                tokens=msg.tokens,
                created_at=msg.created_at,
            )
            self.db.add(new_msg)

            copied_versions = sorted(
                versions_by_message.get(msg.id, []), key=lambda item: item.version_index
            )
            active_version_id = None
            for version in copied_versions:
                new_version_id = uuid.uuid4()
                self.db.add(
                    MessageVersion(
                        id=new_version_id,
                        message_id=new_message_id,
                        version_index=version.version_index,
                        content=version.content,
                        metadata_json=version.metadata_json,
                        source_type=version.source_type,
                        created_at=version.created_at,
                    )
                )
                if version.id == msg.active_version_id:
                    active_version_id = new_version_id
            pending_active_versions.append((new_msg, active_version_id))

        # The active-version foreign key points at rows inserted in the same
        # operation. Insert the messages with a null pointer first, then set
        # the pointer after their copied versions exist.
        self.db.flush()
        for new_msg, active_version_id in pending_active_versions:
            new_msg.active_version_id = active_version_id

        for event_index, event in enumerate(selected_event_rows, start=1):
            self.db.add(
                DeepSpaceSessionEvent(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=new_conversation.id,
                    event_type=event.event_type,
                    tool_name=event.tool_name,
                    turn=event.turn,
                    step=event.step,
                    seq=event_index,
                    run_id=event.run_id,
                    subagent_id=event.subagent_id,
                    payload=event.payload,
                    created_at=event.created_at,
                )
            )

        if copy_files:
            self._copy_workspace_files(
                tenant_id=tenant_id,
                user_id=user_id,
                source_conversation_id=source_conversation_id,
                destination_conversation_id=new_conversation.id,
            )
            self._copy_media_artifacts(
                tenant_id=tenant_id,
                user_id=user_id,
                source_conversation_id=source_conversation_id,
                destination_conversation_id=new_conversation.id,
                message_ids=message_ids,
            )

        self.db.flush()
        return new_conversation

    def _copy_media_artifacts(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        source_conversation_id: uuid.UUID,
        destination_conversation_id: uuid.UUID,
        message_ids: dict[uuid.UUID, uuid.UUID],
    ) -> None:
        """Copy conversation media and remap artifacts to forked messages."""
        artifacts = list(
            self.db.execute(
                select(DeepSpaceMediaArtifact).where(
                    DeepSpaceMediaArtifact.tenant_id == tenant_id,
                    DeepSpaceMediaArtifact.user_id == user_id,
                    DeepSpaceMediaArtifact.conversation_id == source_conversation_id,
                    (
                        DeepSpaceMediaArtifact.message_id.is_(None)
                        | DeepSpaceMediaArtifact.message_id.in_(message_ids)
                    ),
                )
            )
            .scalars()
            .all()
        )
        if not artifacts:
            return
        storage = StorageService(get_settings())
        copied_objects: list[tuple[str, str]] = []
        try:
            for artifact in artifacts:
                new_id = uuid.uuid4()
                stored = storage.copy_object(
                    bucket=artifact.storage_bucket,
                    source_key=artifact.storage_key,
                    tenant_id=tenant_id,
                    document_id=new_id,
                    filename=artifact.title,
                    content_type=artifact.content_type,
                )
                copied_objects.append((stored.bucket, stored.object_key))
                self.db.add(
                    DeepSpaceMediaArtifact(
                        id=new_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        conversation_id=destination_conversation_id,
                        message_id=message_ids.get(artifact.message_id),
                        kind=artifact.kind,
                        status=artifact.status,
                        title=artifact.title,
                        content_type=artifact.content_type,
                        storage_bucket=stored.bucket,
                        storage_key=stored.object_key,
                        size_bytes=stored.size_bytes,
                        provider_type=artifact.provider_type,
                        model_name=artifact.model_name,
                        metadata_json=artifact.metadata_json,
                        created_at=artifact.created_at,
                        updated_at=artifact.updated_at,
                    )
                )
        except Exception:
            for bucket, object_key in copied_objects:
                storage.delete_object(bucket=bucket, object_key=object_key)
            raise

    def _copy_workspace_files(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        source_conversation_id: uuid.UUID,
        destination_conversation_id: uuid.UUID,
    ) -> None:
        """Copy Library folders, files, versions, and their private blobs."""
        settings = get_settings()
        storage = StorageService(settings)
        folders = list(
            self.db.execute(
                select(DeepSpaceWorkspaceFolder)
                .where(
                    DeepSpaceWorkspaceFolder.tenant_id == tenant_id,
                    DeepSpaceWorkspaceFolder.user_id == user_id,
                    DeepSpaceWorkspaceFolder.conversation_id == source_conversation_id,
                )
                .order_by(DeepSpaceWorkspaceFolder.created_at.asc())
            )
            .scalars()
            .all()
        )
        folder_ids: dict[uuid.UUID, uuid.UUID] = {}
        pending = folders[:]
        while pending:
            progress = False
            for folder in pending[:]:
                if (
                    folder.parent_folder_id is not None
                    and folder.parent_folder_id not in folder_ids
                ):
                    continue
                new_id = uuid.uuid4()
                folder_ids[folder.id] = new_id
                self.db.add(
                    DeepSpaceWorkspaceFolder(
                        id=new_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        conversation_id=destination_conversation_id,
                        parent_folder_id=(
                            folder_ids.get(folder.parent_folder_id)
                            if folder.parent_folder_id is not None
                            else None
                        ),
                        name=folder.name,
                        metadata_json=folder.metadata_json,
                        created_at=folder.created_at,
                        updated_at=folder.updated_at,
                    )
                )
                pending.remove(folder)
                progress = True
            if not progress:
                raise ValueError("The workspace folder tree contains a cycle.")
        self.db.flush()

        files = list(
            self.db.execute(
                select(DeepSpaceWorkspaceFile)
                .where(
                    DeepSpaceWorkspaceFile.tenant_id == tenant_id,
                    DeepSpaceWorkspaceFile.user_id == user_id,
                    DeepSpaceWorkspaceFile.conversation_id == source_conversation_id,
                )
                .order_by(DeepSpaceWorkspaceFile.created_at.asc())
            )
            .scalars()
            .all()
        )
        file_ids = [file.id for file in files]
        versions = list(
            self.db.execute(
                select(DeepSpaceWorkspaceFileVersion).where(
                    DeepSpaceWorkspaceFileVersion.tenant_id == tenant_id,
                    DeepSpaceWorkspaceFileVersion.user_id == user_id,
                    DeepSpaceWorkspaceFileVersion.conversation_id == source_conversation_id,
                    DeepSpaceWorkspaceFileVersion.file_id.in_(file_ids),
                )
            )
            .scalars()
            .all()
            if file_ids
            else []
        )
        versions_by_file: dict[uuid.UUID, list[DeepSpaceWorkspaceFileVersion]] = {}
        for version in versions:
            versions_by_file.setdefault(version.file_id, []).append(version)

        copied_objects: list[tuple[str, str]] = []
        try:
            for file in files:
                new_id = uuid.uuid4()
                copied = DeepSpaceWorkspaceFile(
                    id=new_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=destination_conversation_id,
                    parent_folder_id=(
                        folder_ids.get(file.parent_folder_id)
                        if file.parent_folder_id is not None
                        else None
                    ),
                    name=file.name,
                    content_type=file.content_type,
                    content=file.content,
                    source=file.source,
                    metadata_json=file.metadata_json,
                    size_bytes=file.size_bytes,
                    checksum_sha256=file.checksum_sha256,
                    extracted_text=file.extracted_text,
                    version=file.version,
                    is_binary=file.is_binary,
                    created_at=file.created_at,
                    updated_at=file.updated_at,
                )
                if file.storage_bucket and file.storage_key:
                    stored = storage.copy_object(
                        bucket=file.storage_bucket,
                        source_key=file.storage_key,
                        tenant_id=tenant_id,
                        document_id=new_id,
                        filename=file.name,
                        content_type=file.content_type,
                    )
                    copied.storage_bucket = stored.bucket
                    copied.storage_key = stored.object_key
                    copied_objects.append((stored.bucket, stored.object_key))
                self.db.add(copied)
                self.db.flush()
                for version in versions_by_file.get(file.id, []):
                    new_version_id = uuid.uuid4()
                    copied_version = DeepSpaceWorkspaceFileVersion(
                        id=new_version_id,
                        file_id=new_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        conversation_id=destination_conversation_id,
                        version=version.version,
                        name=version.name,
                        content_type=version.content_type,
                        content=version.content,
                        checksum_sha256=version.checksum_sha256,
                        size_bytes=version.size_bytes,
                        metadata_json=version.metadata_json,
                        created_at=version.created_at,
                    )
                    if version.storage_bucket and version.storage_key:
                        stored_version = storage.copy_object(
                            bucket=version.storage_bucket,
                            source_key=version.storage_key,
                            tenant_id=tenant_id,
                            document_id=new_version_id,
                            filename=version.name,
                            content_type=version.content_type,
                        )
                        copied_version.storage_bucket = stored_version.bucket
                        copied_version.storage_key = stored_version.object_key
                        copied_objects.append((stored_version.bucket, stored_version.object_key))
                    self.db.add(copied_version)
        except Exception:
            for bucket, object_key in copied_objects:
                storage.delete_object(bucket=bucket, object_key=object_key)
            raise

    def get_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        kind: str = "deepspace",
        for_update: bool = False,
    ) -> Conversation | None:
        stmt = select(Conversation).where(
            Conversation.tenant_id == tenant_id,
            Conversation.id == conversation_id,
            Conversation.kind == kind,
        )
        if user_id is not None:
            stmt = stmt.where(Conversation.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.execute(stmt).scalar_one_or_none()

    def list_conversations(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        kind: str = "deepspace",
        include_archived: bool = False,
    ) -> Sequence[Conversation]:
        stmt = select(Conversation).where(
            Conversation.tenant_id == tenant_id,
            Conversation.user_id == user_id,
            Conversation.kind == kind,
        )
        if not include_archived:
            stmt = stmt.where(
                ~exists().where(
                    StorageLifecycleItem.tenant_id == tenant_id,
                    StorageLifecycleItem.category == "chat_history",
                    StorageLifecycleItem.source_type == "conversation",
                    StorageLifecycleItem.source_id == sql_cast(Conversation.id, String),
                    StorageLifecycleItem.state == "archived",
                )
            )
        stmt = stmt.order_by(Conversation.updated_at.desc()).limit(limit).offset(offset)
        return self.db.execute(stmt).scalars().all()

    def list_archived_conversations(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
        kind: str = "deepspace",
    ) -> Sequence[Conversation]:
        stmt = (
            select(Conversation)
            .join(
                StorageLifecycleItem,
                (StorageLifecycleItem.tenant_id == Conversation.tenant_id)
                & (StorageLifecycleItem.source_type == "conversation")
                & (StorageLifecycleItem.source_id == sql_cast(Conversation.id, String))
                & (StorageLifecycleItem.category == "chat_history")
                & (StorageLifecycleItem.state == "archived"),
            )
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.user_id == user_id,
                Conversation.kind == kind,
            )
            .order_by(StorageLifecycleItem.archived_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        return self.db.execute(stmt).scalars().all()

    def update_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str | None = None,
        content_html: str | None = None,
        kind: str = "deepspace",
    ) -> bool:
        values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        if title is not None:
            values["title"] = title
        if content_html is not None:
            values["content_html"] = content_html
        conversation = self.get_conversation(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            user_id=user_id,
            kind=kind,
        )
        if conversation is None:
            return False
        old_bytes = StorageQuotaService.estimate_bytes(
            conversation.title, conversation.content_html
        )
        new_bytes = StorageQuotaService.estimate_bytes(
            title if title is not None else conversation.title,
            content_html if content_html is not None else conversation.content_html,
        )
        StorageQuotaService(self.db).ensure_capacity(
            tenant_id=tenant_id,
            user_id=user_id,
            additional_bytes=new_bytes,
            replacing_bytes=old_bytes,
        )
        result = self.db.execute(
            update(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.kind == kind,
            )
            .values(**values)
        )
        if getattr(result, "rowcount", 0):
            StorageLifecycleService(self.db).record_activity(
                tenant_id=tenant_id,
                owner_user_id=user_id,
                category="chat_history",
                source_type="conversation",
                source_id=str(conversation_id),
                activity_kind="conversation_edited",
                size_bytes=new_bytes,
            )
        return bool(getattr(result, "rowcount", 0))

    def append_conversation_content(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        content_html: str,
        title: str | None = None,
        kind: str = "deepspace",
    ) -> Conversation | None:
        """Append content under a row lock without replacing existing notes."""
        conversation = self.db.execute(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.kind == kind,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if conversation is None:
            return None

        separator = '<hr data-averqel-document-separator="true" />'
        existing = (conversation.content_html or "").strip()
        conversation.content_html = (
            f"{existing}{separator}{content_html}" if existing else content_html
        )
        StorageQuotaService(self.db).ensure_capacity(
            tenant_id=tenant_id,
            user_id=user_id,
            additional_bytes=StorageQuotaService.estimate_bytes(
                conversation.title, conversation.content_html
            ),
            replacing_bytes=StorageQuotaService.estimate_bytes(conversation.title, existing),
        )
        if title and conversation.title.strip().lower() in {"", "untitled note"}:
            conversation.title = title
        conversation.updated_at = datetime.now(UTC)
        StorageLifecycleService(self.db).record_activity(
            tenant_id=tenant_id,
            owner_user_id=user_id,
            category="chat_history",
            source_type="conversation",
            source_id=str(conversation_id),
            activity_kind="note_edited",
            size_bytes=StorageQuotaService.estimate_bytes(
                conversation.title, conversation.content_html
            ),
        )
        self.db.flush()
        return conversation

    def _cleanup_conversation_artifacts(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_ids: list[uuid.UUID],
    ) -> None:
        if not conversation_ids:
            return

        # 1. Clean up MinIO / S3 object storage blobs for media and workspace files
        try:
            settings = get_settings()
            storage = StorageService(settings)

            media_blobs = cast(
                list[tuple[Any, Any]],
                self.db.execute(
                    select(
                        DeepSpaceMediaArtifact.storage_bucket, DeepSpaceMediaArtifact.storage_key
                    ).where(
                        DeepSpaceMediaArtifact.tenant_id == tenant_id,
                        DeepSpaceMediaArtifact.user_id == user_id,
                        DeepSpaceMediaArtifact.conversation_id.in_(conversation_ids),
                    )
                ).all(),
            )

            file_blobs = cast(
                list[tuple[Any, Any]],
                self.db.execute(
                    select(
                        DeepSpaceWorkspaceFile.storage_bucket, DeepSpaceWorkspaceFile.storage_key
                    ).where(
                        DeepSpaceWorkspaceFile.tenant_id == tenant_id,
                        DeepSpaceWorkspaceFile.user_id == user_id,
                        DeepSpaceWorkspaceFile.conversation_id.in_(conversation_ids),
                        DeepSpaceWorkspaceFile.storage_bucket.isnot(None),
                        DeepSpaceWorkspaceFile.storage_key.isnot(None),
                    )
                ).all(),
            )

            version_blobs = cast(
                list[tuple[Any, Any]],
                self.db.execute(
                    select(
                        DeepSpaceWorkspaceFileVersion.storage_bucket,
                        DeepSpaceWorkspaceFileVersion.storage_key,
                    ).where(
                        DeepSpaceWorkspaceFileVersion.tenant_id == tenant_id,
                        DeepSpaceWorkspaceFileVersion.user_id == user_id,
                        DeepSpaceWorkspaceFileVersion.conversation_id.in_(conversation_ids),
                        DeepSpaceWorkspaceFileVersion.storage_bucket.isnot(None),
                        DeepSpaceWorkspaceFileVersion.storage_key.isnot(None),
                    )
                ).all(),
            )

            for bucket, key in (*media_blobs, *file_blobs, *version_blobs):
                if bucket and key:
                    try:
                        storage.delete_object(bucket=str(bucket), object_key=str(key))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Storage delete failed for %s/%s; enqueuing StorageCleanupJob: %s",
                            bucket,
                            key,
                            exc,
                        )
                        try:
                            job = StorageCleanupJob(
                                tenant_id=tenant_id,
                                owner_user_id=user_id,
                                bucket=str(bucket),
                                object_key=str(key),
                                status="pending",
                                last_error=str(exc)[:1000],
                            )
                            self.db.add(job)
                        except Exception:  # noqa: BLE001
                            logger.error(
                                "Failed to enqueue StorageCleanupJob for %s/%s",
                                bucket,
                                key,
                                exc_info=True,
                            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "Storage cleanup encountered an error during conversation deletion", exc_info=True
            )

        # 2. Explicitly remove un-cascaded runtime and snapshot data
        try:
            self.db.execute(
                delete(DeepSpaceAgentStep).where(
                    DeepSpaceAgentStep.tenant_id == tenant_id,
                    DeepSpaceAgentStep.user_id == user_id,
                    DeepSpaceAgentStep.conversation_id.in_(conversation_ids),
                )
            )
            self.db.execute(
                delete(DeepSpaceRunEvent).where(
                    DeepSpaceRunEvent.tenant_id == tenant_id,
                    DeepSpaceRunEvent.user_id == user_id,
                    DeepSpaceRunEvent.conversation_id.in_(conversation_ids),
                )
            )
            self.db.execute(
                delete(DeepSpaceAgentRun).where(
                    DeepSpaceAgentRun.tenant_id == tenant_id,
                    DeepSpaceAgentRun.user_id == user_id,
                    DeepSpaceAgentRun.conversation_id.in_(conversation_ids),
                )
            )
            self.db.execute(
                delete(DeepSpaceMissionSnapshot).where(
                    DeepSpaceMissionSnapshot.tenant_id == tenant_id,
                    DeepSpaceMissionSnapshot.user_id == user_id,
                    DeepSpaceMissionSnapshot.conversation_id.in_(conversation_ids),
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("Runtime table cleanup encountered an error", exc_info=True)

        # 3. Purge memories linked to these conversations
        try:
            conv_id_strs = [str(cid) for cid in conversation_ids]
            self.db.execute(
                delete(AgentMemory).where(
                    AgentMemory.tenant_id == str(tenant_id),
                    AgentMemory.user_id == str(user_id),
                    AgentMemory.conversation_id.in_(conv_id_strs),
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("AgentMemory conversation cleanup encountered an error", exc_info=True)

        # 4. Remove queued turns
        try:
            self.db.execute(
                delete(DeepSpaceQueuedTurn).where(
                    DeepSpaceQueuedTurn.tenant_id == tenant_id,
                    DeepSpaceQueuedTurn.user_id == user_id,
                    DeepSpaceQueuedTurn.conversation_id.in_(conversation_ids),
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("Queued turns cleanup encountered an error", exc_info=True)

        # 5. Clear Redis context cache
        try:
            DeepSpaceContextCache().invalidate_conversations(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_ids=conversation_ids,
            )
        except Exception:  # noqa: BLE001
            logger.debug("Context cache invalidation encountered an error", exc_info=True)

    def delete_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: str = "deepspace",
    ) -> bool:
        self._cleanup_conversation_artifacts(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_ids=[conversation_id],
        )
        result = self.db.execute(
            delete(Conversation).where(
                Conversation.tenant_id == tenant_id,
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.kind == kind,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    def bulk_delete_conversations(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_ids: list[uuid.UUID],
        user_id: uuid.UUID,
        kind: str = "deepspace",
    ) -> int:
        if not conversation_ids:
            return 0
        self._cleanup_conversation_artifacts(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_ids=conversation_ids,
        )
        result = self.db.execute(
            delete(Conversation).where(
                Conversation.tenant_id == tenant_id,
                Conversation.user_id == user_id,
                Conversation.kind == kind,
                Conversation.id.in_(conversation_ids),
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def add_message(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        metadata_json: dict[str, Any] | None = None,
        kind: str = "deepspace",
        user_id: uuid.UUID | None = None,
    ) -> Message:
        conversation = self.get_conversation(
            tenant_id=tenant_id, conversation_id=conversation_id, kind=kind
        )
        if conversation is None:
            raise ValueError("DeepSpace conversation not found")
        metadata = dict(metadata_json or {})
        message_bytes = StorageQuotaService.estimate_bytes(content, metadata)
        StorageQuotaService(self.db).ensure_capacity(
            tenant_id=tenant_id,
            user_id=user_id,
            additional_bytes=message_bytes * 2,
        )
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            metadata_json=metadata,
        )
        self.db.add(message)
        self.db.flush()
        version = MessageVersion(
            message_id=message.id,
            version_index=1,
            content=content,
            metadata_json=metadata,
            source_type="initial",
        )
        self.db.add(version)
        self.db.flush()
        message.active_version_id = version.id
        self.db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(updated_at=datetime.now(UTC))
        )
        StorageLifecycleService(self.db).record_activity(
            tenant_id=tenant_id,
            owner_user_id=user_id,
            category="chat_history",
            source_type="conversation",
            source_id=str(conversation_id),
            activity_kind="message_added",
            size_bytes=StorageQuotaService.estimate_bytes(
                conversation.title, conversation.content_html, content, metadata
            ),
        )
        self.db.flush()
        return message

    def get_messages(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: str = "deepspace",
    ) -> Sequence[Message]:
        stmt = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.user_id == user_id,
                Conversation.id == conversation_id,
                Conversation.kind == kind,
            )
            .options(selectinload(Message.versions), selectinload(Message.active_version))
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
        return self.db.execute(stmt).scalars().unique().all()

    def get_message_by_conversation(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: str = "deepspace",
    ) -> Message | None:
        stmt = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.user_id == user_id,
                Conversation.id == conversation_id,
                Conversation.kind == kind,
                Message.id == message_id,
            )
            .options(selectinload(Message.versions), selectinload(Message.active_version))
        )
        return self.db.execute(stmt).scalars().unique().one_or_none()

    def get_latest_turn_pair(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: str = "deepspace",
    ) -> tuple[Message | None, Message | None]:
        messages = list(
            self.get_messages(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                user_id=user_id,
                kind=kind,
            )
        )
        if len(messages) < 2:
            return None, None
        for index in range(len(messages) - 2, -1, -1):
            user_message, assistant_message = messages[index], messages[index + 1]
            if user_message.role == "user" and assistant_message.role == "assistant":
                return user_message, assistant_message
        return None, None

    def find_turn_by_request_id(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID,
        request_id: str,
        kind: str = "deepspace",
    ) -> tuple[Message, Message] | None:
        """Return an existing turn for a client idempotency key."""
        normalized_request_id = str(request_id or "").strip()
        if not normalized_request_id:
            return None
        if conversation_id is None:
            stmt = (
                select(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.user_id == user_id,
                    Conversation.kind == kind,
                    Message.role.in_({"user", "assistant"}),
                    Message.metadata_json["client_request_id"].astext == normalized_request_id,
                )
                .options(selectinload(Message.versions), selectinload(Message.active_version))
                .order_by(Message.created_at.desc(), Message.id.desc())
            )
            request_message = self.db.execute(stmt).scalars().first()
            if request_message is None:
                return None
            conversation_id = request_message.conversation_id
            messages = list(
                self.get_messages(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    kind=kind,
                )
            )
        else:
            messages = list(
                self.get_messages(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    kind=kind,
                )
            )
        for index, message in enumerate(messages[:-1]):
            if message.role != "user":
                continue
            metadata = message.metadata_json if isinstance(message.metadata_json, dict) else {}
            assistant = messages[index + 1]
            assistant_metadata = (
                assistant.metadata_json if isinstance(assistant.metadata_json, dict) else {}
            )
            if assistant.role == "assistant" and normalized_request_id in {
                str(metadata.get("client_request_id") or "").strip(),
                str(assistant_metadata.get("client_request_id") or "").strip(),
            }:
                return message, assistant
        return None

    def lock_request_id(self, request_id: str) -> None:
        """Serialize concurrent retries for one request key in PostgreSQL."""
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:request_id))"),
            {"request_id": str(request_id)},
        )

    def delete_message(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: str = "deepspace",
    ) -> bool:
        message = self.get_message_by_conversation(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user_id,
            kind=kind,
        )
        if message is None:
            return False
        self.db.delete(message)
        self.db.flush()
        try:
            DeepSpaceContextCache().invalidate_conversation(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug("Context cache invalidation failed on message delete", exc_info=True)
        return True

    def create_message_version(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        content: str,
        source_type: str = "user_edit",
        activate: bool = True,
        kind: str = "deepspace",
        metadata_json: dict[str, Any] | None = None,
    ) -> Message | None:
        message = self.get_message_by_conversation(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user_id,
            kind=kind,
        )
        if message is None:
            return None
        old_content = str(message.content or "")
        next_metadata = dict(metadata_json or message.metadata_json or {})
        new_version_bytes = StorageQuotaService.estimate_bytes(content, next_metadata)
        StorageQuotaService(self.db).ensure_capacity(
            tenant_id=tenant_id,
            user_id=user_id,
            additional_bytes=new_version_bytes,
            replacing_bytes=(len(old_content.encode("utf-8")) if activate else 0),
        )
        next_index = max((version.version_index for version in message.versions), default=0) + 1
        version = MessageVersion(
            message_id=message.id,
            version_index=next_index,
            content=content,
            metadata_json=next_metadata,
            source_type=source_type,
        )
        self.db.add(version)
        self.db.flush()
        if activate:
            message.active_version_id = version.id
            message.content = content
            # Keep the denormalized message row and its relationship cache in
            # sync with the newly active immutable version. This matters for
            # same-session API responses (for example visual-block edits),
            # where SQLAlchemy may otherwise return the previously loaded
            # active version metadata until the session is expired.
            message.metadata_json = next_metadata
            message.active_version = version
        self.db.flush()
        return message

    def activate_message_version(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        version_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: str = "deepspace",
    ) -> Message | None:
        message = self.get_message_by_conversation(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user_id,
            kind=kind,
        )
        if message is None:
            return None
        version = next((item for item in message.versions if item.id == version_id), None)
        if version is None:
            return None
        message.active_version_id = version.id
        message.content = version.content
        message.metadata_json = dict(version.metadata_json or {})
        self.db.flush()
        return message

    def complete_assistant_message(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        content: str,
        metadata_json: dict[str, Any],
    ) -> bool:
        message = self.get_message_by_conversation(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user_id,
        )
        if message is None or message.role != "assistant":
            return False
        message.content = content
        message.metadata_json = dict(metadata_json)
        if message.active_version is not None:
            message.active_version.content = content
            message.active_version.metadata_json = dict(metadata_json)
        self.db.flush()
        return True
