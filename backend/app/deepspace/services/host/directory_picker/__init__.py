"""Directory Picker service package for DeepSpace host.

Provides the directory picker seam with native, browse, and auto backends.
"""

from app.deepspace.services.host.directory_picker.auto import (
    DirectoryPickerAuto,
    create_auto_directory_picker_service,
)
from app.deepspace.services.host.directory_picker.browse import (
    BrowseConfig,
    create_browse_directory_picker_service,
)
from app.deepspace.services.host.directory_picker.native import (
    create_native_directory_picker_service,
)
from app.deepspace.services.host.directory_picker.seam import (
    BrowseCapability,
    DirectoryEntry,
    DirectoryListing,
    DirectoryPicker,
    DirectoryPickerCapability,
    DirectoryPickerCapabilityKind,
    DirectoryPickerError,
    DirectoryPickerErrorCode,
    DirectoryPickerService,
    NativeCapability,
)

__all__ = [
    # Seam
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
    # Backends
    "DirectoryPickerAuto",
    "create_native_directory_picker_service",
    "create_browse_directory_picker_service",
    "BrowseConfig",
    "create_auto_directory_picker_service",
]
