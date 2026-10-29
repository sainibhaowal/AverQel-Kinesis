# 41. Local SearXNG web search

DeepSpace can run SearXNG as a supervised local process for the standalone
AverQel installation. The app does not require a hosted SearXNG instance or a
Docker service for local web search.

## Use case

Use this path when the app runs on one machine and the user wants the
`web_search`, `web_fetch`, `url_read`, and `image_read` tools to work without
maintaining a separate search server.

## Why it matters

The previous local default used the Docker-only hostname `searxng:8080`.
That name is not resolvable from a normal local Python process. The local
runtime now uses `127.0.0.1:8888`, installs SearXNG into the AverQel home, and
starts it only when the local entrypoint enables the feature.

## How it works

`backend/run_backend.py` enables the local runtime for a non-Docker launch.
On first startup, `SearXNGRuntime`:

1. clones the official SearXNG source with Git;
2. creates a private Python virtual environment under
   `~/.averqel/searxng/venv`;
3. installs SearXNG and its Granian HTTP server;
4. writes a private settings file with HTML and JSON search formats; and
5. starts one loopback-only process on port 8888 and waits for its HTTP health
   response.

The source and runtime directory can be moved by setting `AVERQEL_HOME` or
`AKS_SEARXNG_RUNTIME_DIR`. The source and installation behavior can be
overridden with `AKS_SEARXNG_SOURCE_URL` and `AKS_SEARXNG_SOURCE_REF`.

DeepSpace's model-facing `web_search` calls the existing SearXNG provider.
`web_fetch` and `url_read` continue through the existing safe URL reader, which
handles redirects, private-network blocking, response-size limits, and page
text extraction. SearXNG is the search engine; it is not used as a proxy for
reading arbitrary pages.

The local status route is:

```text
GET /api/v1/deepspace/web-search/status
```

The response reports `disabled`, `external`, `not_installed`, `running`, or
`failed`. The process log is `~/.averqel/searxng/searxng.log`. The manager only
stops a process that it started itself.

Docker deployments keep their explicit `http://searxng:8080` service endpoint;
the local manager is disabled when the API runs inside Docker.

## What to watch for

- The first local startup needs Git, a compatible Python interpreter, internet
  access to fetch the official source and Python packages, and enough disk
  space for the virtual environment. Later starts reuse the installation.
- SearXNG is a metasearch engine. Its result quality and availability depend
  on the public engines configured by SearXNG; an individual engine may rate
  limit or return no result. The provider retries transient HTTP failures and
  preserves a bounded result list, but it cannot make an external engine
  reliable.
- The local process is supervised by the API, so this is Dockerless and does
  not require a user-managed daemon, but it is not literally process-free or
  offline. Web search still requires internet access.
- Set `AKS_SEARXNG_LOCAL_RUNTIME_ENABLED=false` to disable the managed process
  and configure another provider endpoint explicitly.
