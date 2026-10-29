"""Tests for LSP service."""

from __future__ import annotations

import pytest

from app.deepspace.services.lsp import (
    LspProviderConfig,
    LspService,
    final_extension,
    normalize_extension,
)


def test_normalize_extension():
    assert normalize_extension(".ts") == ".ts"
    assert normalize_extension("ts") == ".ts"
    assert normalize_extension(".TS") == ".ts"
    assert normalize_extension("TS") == ".ts"
    assert normalize_extension(".d.ts") == ".d.ts"


def test_final_extension():
    assert final_extension("file.ts") == ".ts"
    assert final_extension("path/to/file.ts") == ".ts"
    assert final_extension("path\\to\\file.ts") == ".ts"
    assert final_extension("file") == ""
    assert final_extension(".bashrc") == ""
    assert final_extension("file.d.ts") == ".ts"
    assert final_extension("FILE.TS") == ".ts"


def test_lsp_provider_config():
    config = LspProviderConfig(
        id="test",
        command=["node", "server.js"],
        extension_to_language={".ts": "typescript", ".js": "javascript"},
    )
    assert config.id == "test"
    assert config.command == ["node", "server.js"]
    assert config.extension_to_language[".ts"] == "typescript"


def test_lsp_service_register_provider():
    service = LspService(None)  # type: ignore

    config = LspProviderConfig(
        id="test",
        command=["node", "server.js"],
        extension_to_language={".ts": "typescript"},
    )
    service.register_provider(config)

    assert "test" in service._providers
    assert ".ts" in service._routes
    assert service._routes[".ts"].language_id == "typescript"


def test_lsp_service_register_duplicate_provider():
    service = LspService(None)  # type: ignore

    config1 = LspProviderConfig(
        id="test",
        command=["node", "server.js"],
        extension_to_language={".ts": "typescript"},
    )
    config2 = LspProviderConfig(
        id="test",
        command=["node", "server2.js"],
        extension_to_language={".js": "javascript"},
    )

    service.register_provider(config1)
    with pytest.raises(Exception) as exc_info:
        service.register_provider(config2)

    assert "already registered" in str(exc_info.value)


def test_lsp_service_register_conflicting_extension():
    service = LspService(None)  # type: ignore

    config1 = LspProviderConfig(
        id="test1",
        command=["node", "server.js"],
        extension_to_language={".ts": "typescript"},
    )
    config2 = LspProviderConfig(
        id="test2",
        command=["node", "server2.js"],
        extension_to_language={".ts": "javascript"},  # Same extension
    )

    service.register_provider(config1)
    with pytest.raises(Exception) as exc_info:
        service.register_provider(config2)

    assert "already registered" in str(exc_info.value)


def test_lsp_service_unregister_provider():
    service = LspService(None)  # type: ignore

    config = LspProviderConfig(
        id="test",
        command=["node", "server.js"],
        extension_to_language={".ts": "typescript", ".js": "javascript"},
    )
    service.register_provider(config)
    service.unregister_provider("test")

    assert "test" not in service._providers
    assert ".ts" not in service._routes
    assert ".js" not in service._routes


def test_lsp_service_list_providers():
    service = LspService(None)  # type: ignore

    config = LspProviderConfig(
        id="test",
        command=["node", "server.js"],
        extension_to_language={".ts": "typescript"},
    )
    service.register_provider(config)

    providers = service.list_providers()
    assert len(providers) == 1
    assert providers[0]["id"] == "test"
    assert ".ts" in providers[0]["extensions"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
