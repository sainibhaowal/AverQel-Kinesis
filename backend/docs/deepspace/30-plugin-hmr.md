# 30. Plugin HMR + Configuration Service

DeepSpace includes a backend Hot Module Reloading (HMR) service for plugins and a configuration-schema editor service. HMR is disabled by default. The current web Settings page exposes the plugin inventory, but does not expose the configuration-schema editor as a user-facing form.

## Use case

- Developers want to iterate on plugins without restarting the server
- Plugin authors want to validate configuration values against a declared schema
- Deployments want to validate plugin configs before applying them

## Why it matters

- Previously, plugin changes required server restart
- Plugin configuration schemas can be registered and validated by the backend service
- The current web client shows plugin inventory and preset availability; configuration editing remains a service/API integration task

## How it works

### Architecture

1. **Configuration**: `deepspace_plugin_hmr_enabled=true` plus watch interval
2. **Watcher**: `PluginWatcher` polls plugin directories for manifest changes
3. **Editor**: `PluginConfigEditor` manages JSON schemas for plugin configuration
4. **Registry**: `PluginHMRService` combines watcher + editor, integrates with `PluginRegistry`

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_plugin_hmr_enabled` | bool | `false` | Master switch |
| `deepspace_plugin_hmr_interval` | int | `5` | Poll interval (seconds) |
| `deepspace_plugin_hmr_debounce_ms` | int | `500` | Debounce rapid changes |
| `deepspace_plugin_hmr_reload` | bool | `true` | Auto-reload on change |
| `deepspace_plugin_hmr_validate` | bool | `true` | Validate manifest on load |
| `deepspace_plugin_hmr_workspace` | bool | `true` | Watch workspace plugins |
| `deepspace_plugin_hmr_deployment` | bool | `true` | Watch deployment plugins |
| `deepspace_plugin_hmr_editor` | bool | `true` | Enable config editor |

### Plugin config schema

Plugins declare their configuration schema:

```python
from app.deepspace.services.plugin_hmr import get_plugin_hmr_service

hmr = await get_plugin_hmr_service(settings)
hmr.register_config_schema(PluginConfigSchema(
    plugin_id="my_plugin",
    schema={
        "type": "object",
        "properties": {
            "api_key": {"type": "string", "format": "password"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
        },
        "required": ["api_key"],
    },
    defaults={"timeout": 30},
    ui_hints={"api_key": {"widget": "password"}},
))
```

### Editor API

```python
hmr = await get_plugin_hmr_service(settings)

# Validate config
valid, errors = hmr.validate_plugin_config("my_plugin", {"api_key": "xxx", "timeout": 60})

# Get defaults
defaults = hmr.get_plugin_defaults("my_plugin")

# Get UI hints
hints = hmr.get_plugin_ui_hints("my_plugin")

# List all schemas
schemas = hmr.list_config_schemas()
```

### Watcher callbacks

```python
async def on_plugins_changed(manifests: list[PluginManifest]):
    print(f"Plugins changed: {[m.id for m in manifests]}")

hmr.watcher.add_callback(on_plugins_changed)
```

### Current web integration

The frontend Settings tab (`/settings/deepspace`) includes a **Plugins** tab that:

- Lists the core runtime and deployment-registered extensions
- Shows preset-scoped extension availability and extension tools
- Lets the user search the displayed inventory and choose the preset used for the inventory view

It does not currently render plugin configuration schemas, save plugin configuration values, or provide a direct HMR control. Those capabilities are available through the backend service methods above when a host integrates them.

## What to watch for

- **File watching**: Polling-based; not instant. Adjust `hmr_interval` for responsiveness vs CPU
- **Validation**: `validate_on_load` prevents broken plugins from loading; disable for development
- **Workspace plugins**: Per-conversation; watcher only scans deployment plugins globally
- **Config editor**: Schemas are JSON Schema Draft 7; use standard keywords. No web form currently consumes these schemas.
- **Security**: Config editor only shows schemas; actual secret values stored in credential store
- **Performance**: Polling scans directories; large plugin directories increase I/O
- **Callbacks**: Watcher callbacks run in watcher task; keep them fast and non-blocking
