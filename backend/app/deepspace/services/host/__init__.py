"""Host services package for DeepSpace.

Provides the host web server, SPA dist server, directory picker, open-in-app,
plugin inventory, telemetry, and identity services.
"""

from app.deepspace.services.host.directory_picker import (
    BrowseCapability,
    BrowseConfig,
    DirectoryEntry,
    DirectoryListing,
    DirectoryPicker,
    DirectoryPickerCapability,
    DirectoryPickerCapabilityKind,
    DirectoryPickerError,
    DirectoryPickerErrorCode,
    DirectoryPickerService,
    NativeCapability,
    create_auto_directory_picker_service,
    create_browse_directory_picker_service,
    create_native_directory_picker_service,
)
from app.deepspace.services.host.identity.service import (
    AnonymousUserIdService,
    create_anonymous_user_id_service,
)
from app.deepspace.services.host.open_in_app.service import (
    OPEN_IN_APP_CATALOG,
    AppEntry,
    OpenInAppConfig,
    OpenInAppService,
    VerifiedLauncher,
    create_open_in_app_service,
)
from app.deepspace.services.host.plugin_inventory.service import (
    FiberPhase,
    PluginInventoryEntry,
    PluginInventoryService,
    PluginInventorySnapshot,
    PresetComposition,
    PresetPluginEntry,
    create_plugin_inventory_service,
)
from app.deepspace.services.host.spa_dist_server import (
    SPADistConfig,
    SPADistServer,
    create_spa_dist_server,
)
from app.deepspace.services.host.telemetry.service import (
    ProductTelemetryService,
    TelemetryConfig,
    TelemetryEvent,
    create_product_telemetry_service,
)
from app.deepspace.services.host.web_server import (
    WebServerConfig,
    WebServerService,
    WebServerStatus,
    bind_url_host,
    is_loopback_host,
    is_wildcard_host,
    normalize_bind_address,
)

__all__ = [
    # Web Server
    "WebServerConfig",
    "WebServerService",
    "WebServerStatus",
    "is_loopback_host",
    "is_wildcard_host",
    "normalize_bind_address",
    "bind_url_host",
    # SPA Dist Server
    "SPADistServer",
    "SPADistConfig",
    "create_spa_dist_server",
    # Directory Picker
    "DirectoryPicker",
    "DirectoryPickerService",
    "DirectoryPickerCapability",
    "DirectoryPickerCapabilityKind",
    "NativeCapability",
    "BrowseCapability",
    "DirectoryEntry",
    "DirectoryListing",
    "DirectoryPickerError",
    "DirectoryPickerErrorCode",
    "create_native_directory_picker_service",
    "create_browse_directory_picker_service",
    "BrowseConfig",
    "create_auto_directory_picker_service",
    # Open In App
    "OpenInAppService",
    "OpenInAppConfig",
    "AppEntry",
    "VerifiedLauncher",
    "create_open_in_app_service",
    "OPEN_IN_APP_CATALOG",
    # Plugin Inventory
    "PluginInventoryService",
    "PluginInventoryEntry",
    "PresetPluginEntry",
    "PresetComposition",
    "PluginInventorySnapshot",
    "FiberPhase",
    "create_plugin_inventory_service",
    # Telemetry
    "ProductTelemetryService",
    "TelemetryConfig",
    "TelemetryEvent",
    "create_product_telemetry_service",
    # Identity
    "AnonymousUserIdService",
    "create_anonymous_user_id_service",
]
