"""Locale-aware text for the Harness-compatible Todo contract.

The Harness ships one locale file per supported language for the tool's
metadata.  DeepSpace has no request-wide locale module, so this module keeps
the task-plan copy self-contained: the eight locales the Harness packages
declare, the summary line the tool renders, and the small amount of
negotiation needed to choose one from an ``Accept-Language`` header.

Copy is never generated at request time from a model; it is a fixed table so
the same plan always reads the same way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: The locales the Todo contract declares, in preference order for a tie.
SUPPORTED_LOCALES: tuple[str, ...] = (
    "en",
    "zh",
    "ja",
    "de",
    "fr",
    "es",
    "pt",
    "ru",
)

#: The summary line of one whole-list write, keyed by language.
_TODO_SUMMARY: dict[str, str] = {
    "en": "Updated todo list: {pending} pending, {in_progress} in progress, {completed} completed.",
    "zh": "已更新待办列表：{pending} 项待处理，{in_progress} 项进行中，{completed} 项已完成。",
    "ja": "ToDo リストを更新しました: 未着手 {pending} 件、進行中 {in_progress} 件、完了 {completed} 件。",
    "de": "To-Do-Liste aktualisiert: {pending} ausstehend, {in_progress} in Bearbeitung, {completed} abgeschlossen.",
    "fr": "Liste de tâches mise à jour : {pending} en attente, {in_progress} en cours, {completed} terminées.",
    "es": "Lista de tareas actualizada: {pending} pendientes, {in_progress} en curso, {completed} completadas.",
    "pt": "Lista de tarefas atualizada: {pending} pendentes, {in_progress} em andamento, {completed} concluídas.",
    "ru": "Список задач обновлён: {pending} в ожидании, {in_progress} в работе, {completed} выполнено.",
}

#: The tool-call card title, keyed by language.
_TODO_TITLE: dict[str, str] = {
    "en": "Update todo list",
    "zh": "更新待办列表",
    "ja": "ToDo リストを更新",
    "de": "To-Do-Liste aktualisieren",
    "fr": "Mettre à jour la liste de tâches",
    "es": "Actualizar lista de tareas",
    "pt": "Atualizar lista de tarefas",
    "ru": "Обновить список задач",
}

_ENGLISH = "en"


def normalize_locale(value: str | None) -> str:
    """Return the supported language of one locale tag.

    A tag's language subtag decides the match, so ``de-DE`` and ``de-CH``
    both select German and ``zh-Hant`` selects Chinese.  An unknown or empty
    tag falls back to English rather than failing a tool call.

    Args:
        value: A BCP 47 tag or ``None``.

    Returns:
        One of :data:`SUPPORTED_LOCALES`.
    """
    if not value:
        return _ENGLISH
    normalized = str(value).strip().replace("_", "-").lower()
    if not normalized:
        return _ENGLISH
    language = normalized.split("-", 1)[0]
    return language if language in _TODO_SUMMARY else _ENGLISH


def negotiate_locale(header: str | None) -> str:
    """Choose the best supported locale from an ``Accept-Language`` header.

    Quality values are honored; an entry without one sorts after entries that
    declare a positive quality.  A wildcard selects English, because the
    caller did not state a preference the table can honor.

    Args:
        header: The raw ``Accept-Language`` value, when the request has one.

    Returns:
        One of :data:`SUPPORTED_LOCALES`.
    """
    if not header:
        return _ENGLISH
    candidates: list[tuple[float, int, str]] = []
    for index, raw in enumerate(str(header).split(",")):
        part = raw.strip()
        if not part:
            continue
        tag, _, parameters = part.partition(";")
        quality = 1.0
        for parameter in parameters.split(";"):
            name, _, value = parameter.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        normalized = normalize_locale(tag)
        if normalized == _ENGLISH and tag.strip().lower() not in {"en", "en-us", "en-gb"}:
            # An unsupported tag must not outrank a supported later entry.
            if "*" not in tag and not tag.strip().lower().startswith("en"):
                continue
        candidates.append((quality, -index, normalized))
    if not candidates:
        return _ENGLISH
    candidates.sort(reverse=True)
    return candidates[0][2]


def request_locale(request: Any | None) -> str:
    """Read the negotiated locale of an incoming request.

    Args:
        request: A Starlette/FastAPI request, a mapping with a ``headers``
            key, or ``None`` when the caller has no request.

    Returns:
        One of :data:`SUPPORTED_LOCALES`.
    """
    if request is None:
        return _ENGLISH
    headers = getattr(request, "headers", None)
    if headers is None and isinstance(request, Mapping):
        headers = request.get("headers")
    value: str | None = None
    if headers is not None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            value = getter("accept-language")
    return negotiate_locale(value)


def todo_summary(
    counts: Mapping[str, Any],
    *,
    locale: str = _ENGLISH,
) -> str:
    """Render the whole-list summary line.

    Args:
        counts: The normalized counts with ``pending``, ``inProgress``, and
            ``completed`` keys.
        locale: A locale tag; an unknown tag falls back to English.

    Returns:
        The summary line in the selected language.
    """
    language = normalize_locale(locale)
    template = _TODO_SUMMARY[language]
    return template.format(
        pending=int(counts.get("pending") or 0),
        in_progress=int(counts.get("inProgress") or 0),
        completed=int(counts.get("completed") or 0),
    )


def todo_title(*, locale: str = _ENGLISH) -> str:
    """Render the tool-call card title in the selected language."""
    return _TODO_TITLE[normalize_locale(locale)]


def todo_copy(locale: str = _ENGLISH) -> dict[str, str]:
    """Return the localized copy a client needs for the drawer.

    Args:
        locale: A locale tag; an unknown tag falls back to English.

    Returns:
        ``title``, ``summary``, and the three status labels.
    """
    language = normalize_locale(locale)
    templates = _TODO_SUMMARY[language]
    labels = _STATUS_LABELS[language]
    return {
        "locale": language,
        "title": _TODO_TITLE[language],
        "summary": templates,
        "pending": labels["pending"],
        "in_progress": labels["in_progress"],
        "completed": labels["completed"],
    }


#: The three status labels, keyed by language.
_STATUS_LABELS: dict[str, dict[str, str]] = {
    "en": {"pending": "Pending", "in_progress": "In progress", "completed": "Completed"},
    "zh": {"pending": "待处理", "in_progress": "进行中", "completed": "已完成"},
    "ja": {"pending": "未着手", "in_progress": "進行中", "completed": "完了"},
    "de": {"pending": "Ausstehend", "in_progress": "In Bearbeitung", "completed": "Abgeschlossen"},
    "fr": {"pending": "En attente", "in_progress": "En cours", "completed": "Terminées"},
    "es": {"pending": "Pendientes", "in_progress": "En curso", "completed": "Completadas"},
    "pt": {"pending": "Pendentes", "in_progress": "Em andamento", "completed": "Concluídas"},
    "ru": {"pending": "В ожидании", "in_progress": "В работе", "completed": "Выполнено"},
}


def all_locale_copy() -> dict[str, dict[str, str]]:
    """Return the drawer copy of every supported locale."""
    return {locale: todo_copy(locale) for locale in SUPPORTED_LOCALES}


def plan_lines(todos: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return one ``[status] content`` line per todo, for logs and tests."""
    return [
        f"[{str(item.get('status') or 'pending')}] {str(item.get('content') or '').strip()}"
        for item in todos
    ]


__all__ = [
    "SUPPORTED_LOCALES",
    "all_locale_copy",
    "negotiate_locale",
    "normalize_locale",
    "plan_lines",
    "request_locale",
    "todo_copy",
    "todo_summary",
    "todo_title",
]
