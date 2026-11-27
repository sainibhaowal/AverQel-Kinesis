# 100% Test Coverage Preparation Document

**Repository**: AverQel KineSis
**Branch**: feature/deepspace-subagentic-parallel-complete
**Current Coverage**: Backend 61% | Frontend 59.53% | **Target**: 100%
**Date**: 2026-11-26

---

## 1. Executive Summary

### Current State
- **Backend**: 61% statement/branch coverage (105,538 statements, 41,348 uncovered)
- **Frontend**: 59.53% statements, 53.12% branches, 55.14% functions, 99.16% lines
- **Tests**: 3,751+ backend unit tests passing | 808 frontend tests passing | 56 desktop shell tests
- **Quality Gates**: All passing (ruff, black, mypy, bandit, pip-audit, ESLint, build)

### Gap to 100%
- **Backend**: ~41,348 uncovered statements across ~50 modules
- **Frontend**: ~9,890 uncovered statements, ~12,500 branches, ~2,500 functions
- **Main Blockers**: Database-dependent modules (need isolated test services), Windows-specific code, Celery workers

---

## 2. What We Already Have (Solid Foundation)

### ✅ Test Infrastructure
| Component | Status | Location |
|-----------|--------|----------|
| Backend pytest config | Complete | `backend/pyproject.toml` |
| Coverage config | Complete | `backend/pyproject.toml` [tool.coverage.run] |
| Frontend vitest config | Complete | `frontend/vitest.config.mts` |
| Test isolation | Documented | `backend/docs/security/isolated-test-runs.md` |
| Test markers | Defined | `unit_no_db`, `db`, `external`, `e2e` |
| CI scripts | Complete | `.github/scripts/run-backend-gate.sh`, `run-local-pr-ci.sh` |
| Pre-commit hooks | Installed | `.pre-commit-config.yaml` |

### ✅ Test Coverage Achieved (>90%)
| Module | Coverage | Test File |
|--------|----------|-----------|
| `openai_compatible_config.py` | 100% | `test_openai_compatible_config.py` (45 tests) |
| `file_permissions.py` | 96% | `test_file_permissions.py` (37 tests) |
| `file_reference_local.py` | 93% | `test_file_reference_local.py` (54 tests) |
| `codex_patch.py` | 95% | `test_codex_patch.py` (64 tests) |
| `agent_instructions.py` | 93% | `test_agent_instructions.py` (18 tests) |
| `jobs/archive.py` | 100% | `test_jobs_archive.py` (3 tests) |
| `file_reference_prompt.py` | 100% | `test_file_reference_prompt.py` (7 tests) |
| `common.py`, `metrics_summary.py` | 100% | `test_system_schemas.py` (8 tests) |
| `openai_compatible.py` | 71% | `test_openai_compatible_provider.py` (88 tests) |

---

## 3. What Needs Work (The 39% Gap)

### 3.1 Backend - Zero Coverage Modules (10 modules, ~2,500 lines)

| Priority | Module | Lines | Type | Blocker | Test Strategy |
|----------|--------|-------|------|---------|---------------|
| **P0** | `auth/repositories/api_keys.py` | 32 | DB Repository | Requires PostgreSQL | Provision isolated PG + test CRUD |
| **P0** | `deepspace/cli/__main__.py` | 9 | CLI Entry | No DB | Mock args, test dispatch |
| **P0** | `deepspace/workers/workflow_tasks.py` | 61 | Celery Task | DB + Celery | Mock DeepSpaceChatService, WorkflowEngine |
| **P0** | `system/services/smtp_client.py` | 46 | SMTP Client | smtplib on Linux | Patch smtplib (see failed attempts) |
| **P0** | `system/services/vitals_service.py` | 64 | Health Checks | DB + HTTP | Mock httpx + DB session |
| **P0** | `query/repositories/workspace.py` | 57 | DB Repository | Requires PostgreSQL | Provision isolated PG |
| **P0** | `documents/workers/tasks_classification.py` | 119 | Celery Worker | DB + Celery | Mock classification service |
| **P0** | `system/workers/tasks_notifications.py` | 179 | Celery Worker | DB + Celery | Mock notification service |
| **P0** | `system/workers/tasks_retention.py` | 76 | Celery Worker | DB + Celery | Mock retention service |
| **P0** | `deepspace/services/tools/fs_win32.py` | 324 | Windows-only | ctypes on Linux | Mock ctypes or skip on non-Windows |

### 3.2 Backend - Low Coverage High-Impact Modules (>500 lines, <20%)

| Priority | Module | Lines | Current | Target | Key Uncovered Areas |
|----------|--------|-------|---------|--------|---------------------|
| **P1** | `query/services/query_service.py` | 1,672 | 11% | 100% | Query execution, synthesis, caching |
| **P1** | `providers/services/openai_compatible.py` | 1,344 | 71% | 100% | Streaming, reasoning parsing, tool calling |
| **P1** | `providers/services/provider_management_service.py` | 544 | 18% | 100% | CRUD, health checks, model discovery |
| **P1** | `deepspace/api/chats.py` | 1,124 | 23% | 100% | Chat endpoints, streaming, sessions |
| **P1** | `deepspace/api/library.py` | 1,115 | 3% | 100% | Library uploads, search, deliverables |
| **P1** | `deepspace/services/terminals.py` | 1,016 | 6% | 100% | PTY management, shell execution |
| **P1** | `core/config.py` | 1,007 | 64% | 100% | Settings validation, env parsing |
| **P1** | `deepspace/services/chat_service/turns/tool_calls.py` | 930 | 11% | 100% | Tool parsing, execution, results |
| **P1** | `integrations/services/mcp_runtime.py` | 934 | 52% | 100% | MCP server lifecycle, transport |

### 3.3 Frontend - Zero/Low Coverage Components

| Priority | Component | Coverage | Lines | Type |
|----------|-----------|----------|-------|------|
| **P0** | `DeepSpaceLibraryDrawer.tsx` | 0.5% | 830 | Library UI |
| **P0** | `HuggingFaceRerankerHub.tsx` | 1.0% | 105 | Provider UI |
| **P0** | `DeepSpaceSchedulesPanel.tsx` | 2.4% | 41 | Schedules UI |
| **P0** | `MCPDetailDialog.tsx` | 2.9% | 35 | MCP UI |
| **P0** | `lib/exportUtils.ts` | 2.9% | 104 | Utility |
| **P0** | `DeepSpaceLibraryFileWorkspace.tsx` | 4.8% | 63 | Library UI |
| **P1** | `AverQelDialogHost.tsx` | 5.1% | 39 | Dialog |
| **P1** | `library-uploads.ts` | 6.8% | 44 | Library upload |
| **P1** | `InlineMarkdown.tsx` | 8.6% | 35 | Markdown |
| **P1** | `task-progress.ts` | 22.2% | 54 | Task tracking |

---

## 4. Where to Do It (File/Folder Structure)

### Backend Test Files to Create/Extend
```
backend/tests/unit/
├── auth/
│   └── test_api_keys_repository.py          # NEW - for api_keys.py
├── deepspace/
│   ├── cli/
│   │   └── test_main.py                     # NEW - for __main__.py
│   ├── services/
│   │   ├── test_smtp_client.py              # EXTEND - fix mocking
│   │   ├── test_vitals_service.py           # NEW - for vitals_service.py
│   │   ├── test_workflow_tasks.py           # NEW - for workflow_tasks.py
│   │   ├── test_fs_win32.py                 # NEW - with ctypes mocking
│   │   └── tools/
│   │       └── test_fs_win32.py             # NEW - Windows file ops
│   ├── api/
│   │   ├── test_chats.py                    # NEW - for chats.py
│   │   └── test_library.py                  # NEW - for library.py
│   └── workers/
│       ├── test_workflow_tasks.py           # NEW - Celery tasks
├── query/
│   ├── services/
│   │   └── test_query_service.py            # NEW - for query_service.py
│   └── repositories/
│       └── test_workspace_repository.py     # NEW - for workspace.py
├── providers/
│   ├── services/
│   │   ├── test_provider_management.py      # NEW - for provider_management_service.py
│   │   └── test_openai_compatible_ext.py    # EXTEND - add missing coverage
├── system/
│   ├── services/
│   │   ├── test_smtp_client.py              # EXTEND - fix smtplib mocking
│   │   ├── test_vitals_service.py           # NEW
│   └── workers/
│       ├── test_notifications_worker.py     # NEW
│       └── test_retention_worker.py         # NEW
└── documents/
    └── workers/
        └── test_classification_worker.py    # NEW
```

### Frontend Test Files to Create/Extend
```
frontend/tests/
├── components/
│   ├── deepspace/
│   │   ├── DeepSpaceLibraryDrawer.test.tsx          # NEW
│   │   ├── HuggingFaceRerankerHub.test.tsx          # NEW
│   │   ├── DeepSpaceSchedulesPanel.test.tsx         # NEW
│   │   ├── DeepSpaceLibraryFileWorkspace.test.tsx   # NEW
│   │   └── DeepSpaceLibraryPreview.test.tsx         # EXTEND
│   ├── mcp/
│   │   └── MCPDetailDialog.test.tsx                 # NEW
│   ├── ui/
│   │   ├── AverQelDialogHost.test.tsx               # NEW
│   │   └── InlineMarkdown.test.tsx                  # NEW
├── lib/
│   ├── exportUtils.test.ts                          # NEW
│   ├── library-uploads.test.ts                      # EXTEND
│   └── task-progress.test.ts                        # EXTEND
```

### Database Test Infrastructure Needed
```
backend/
├── tests/
│   ├── conftest.py                      # EXTEND - add PG/Redis/MinIO fixtures
│   ├── docker-compose.test.yml          # NEW - isolated test services
│   └── scripts/
│       └── setup_test_services.sh       # NEW - provision test DBs
```

---

## 5. How to Implement Safely (Step-by-Step)

### Phase 1: Infrastructure Setup (Week 1)
1. **Provision isolated test databases**
   - Create `docker-compose.test.yml` with PostgreSQL (port 5433), Redis (6380), MinIO (9001)
   - Add test credentials with `_test` suffix
   - Update `conftest.py` to use `AVERQEL_TEST_ISOLATED_RUN=external`

2. **Fix smtplib mocking for Linux**
   - Use `patch("app.system.services.smtp_client.smtplib.SMTP")` at module level
   - Start patchers before imports, stop in `pytest_sessionfinish`

3. **Add Windows ctypes mocking**
   - Create fixture that patches `ctypes.WinDLL`, `ctypes.wintypes`
   - Skip fs_win32 tests on non-Windows or mock fully

### Phase 2: Zero-Coverage Modules (Week 1-2)
| Module | Test File | Approach |
|--------|-----------|----------|
| `api_keys.py` | `test_api_keys_repository.py` | Use isolated PG, test CRUD + encryption |
| `__main__.py` | `test_main.py` | Mock argparse, test command dispatch |
| `workflow_tasks.py` | `test_workflow_tasks.py` | Mock DeepSpaceChatService, WorkflowEngine |
| `smtp_client.py` | `test_smtp_client.py` | Module-level patch on smtplib |
| `vitals_service.py` | `test_vitals_service.py` | Mock httpx.AsyncClient + DB session |
| `workspace.py` | `test_workspace_repository.py` | Isolated PG, test CRUD |
| `tasks_classification.py` | `test_classification_worker.py` | Mock classification service |
| `fs_win32.py` | `test_fs_win32.py` | Mock ctypes at module level |

### Phase 3: High-Impact Low-Coverage (Week 2-3)
| Module | Strategy |
|--------|----------|
| `query_service.py` | Test query execution paths, synthesis, caching |
| `provider_management.py` | Test CRUD, health, model discovery with mocked providers |
| `chats.py` | Test chat endpoints, streaming, session mgmt |
| `library.py` | Test upload, search, deliverable generation |
| `terminals.py` | Test PTY creation, shell execution, resize |
| `tool_calls.py` | Test XML/JSON parsing, execution, results |
| `mcp_runtime.py` | Test server lifecycle, stdio/HTTP transport |

### Phase 4: Frontend Components (Week 3)
| Component | Test Strategy |
|-----------|---------------|
| `DeepSpaceLibraryDrawer` | Render, file selection, drag-drop, keyboard nav |
| `HuggingFaceRerankerHub` | Model selection, config, connection test |
| `DeepSpaceSchedulesPanel` | CRUD schedules, cron validation, enable/disable |
| `MCPDetailDialog` | Form validation, connection test, OAuth flow |
| `exportUtils` | CSV/JSON/PDF export, data transformation |
| `AverQelDialogHost` | Portal rendering, focus trap, escape close |
| `InlineMarkdown` | Code blocks, math, mermaid, tables |

---

## 6. Exact Files/Routes/APIs/Components/Schemas Involved

### Backend APIs to Test
| Route | Method | Module | Test Priority |
|-------|--------|--------|---------------|
| `/api/v1/providers` | GET/POST | `providers/api/providers.py` | P1 |
| `/api/v1/chats` | GET/POST | `deepspace/api/chats.py` | P1 |
| `/api/v1/library` | GET/POST | `deepspace/api/library.py` | P1 |
| `/api/v1/terminals` | POST | `deepspace/api/terminals.py` | P1 |
| `/api/v1/workspaces` | GET/POST | `deepspace/api/workspace.py` | P1 |
| `/api/v1/query` | POST | `query/api/queries.py` | P1 |
| `/api/v1/vitals` | GET | `system/api/health.py` | P0 |

### Frontend Routes to Test
| Route | Component | Test Priority |
|-------|-----------|---------------|
| `/dashboard/deepspace/library` | `DeepSpaceLibraryDrawer` | P0 |
| `/dashboard/providers/huggingface` | `HuggingFaceRerankerHub` | P0 |
| `/dashboard/settings/schedules` | `DeepSpaceSchedulesPanel` | P0 |
| `/dashboard/mcp/[id]` | `MCPDetailDialog` | P0 |

### Database Schemas to Test
| Schema | Table | Repository |
|--------|-------|------------|
| `auth` | `api_keys` | `api_keys.py` |
| `deepspace` | `workflow_jobs` | `workflow_tasks.py` |
| `query` | `workspaces` | `workspace.py` |
| `documents` | `connectors` | `tasks_classification.py` |

---

## 7. Safety Rules (Must Not Break)

### Protected Boundaries
| Boundary | Protection |
|----------|------------|
| **Auth/Tenant Isolation** | Never bypass `AuthContext`, always validate `tenant_id` |
| **Encryption/Crypto** | Never log secrets, use `provider_secret_crypto.py` |
| **Database Migrations** | Never run Alembic on test DBs; use `Base.metadata.create_all` |
| **Celery Workers** | Always set `task_always_eager=True` in tests |
| **SMTP/External** | Never connect to real services; always mock |
| **File System** | Use `tempfile` fixtures; never touch real paths |
| **WebSocket/SSE** | Use test clients; never bind real ports |

### Code Patterns to Preserve
| Pattern | Location | Do Not Change |
|---------|----------|---------------|
| `AuthContext` injection | All API deps | Keep dependency injection |
| `get_session_factory()` | All DB access | Use session factory, not direct engine |
| `settings = get_settings()` | Config access | Cache settings, don't re-read env |
| `logger = logging.getLogger(__name__)` | All modules | Keep module-level loggers |
| `frozen=True, slots=True` | Dataclasses | Keep immutability |

---

## 8. Evidence Requirements (Proof of Completion)

### Per Module Completion Checklist
- [ ] Test file created at correct path
- [ ] All functions/branches covered (≥95%)
- [ ] Tests pass in isolation (`pytest test_file.py -v`)
- [ ] Tests pass in full suite (`pytest -m unit_no_db`)
- [ ] No existing tests broken
- [ ] Coverage report shows ≥95% for module
- [ ] Documentation updated (docstrings, README if needed)

### Global Verification Commands
```bash
# Backend
cd backend && .venv/bin/pytest -m unit_no_db --cov=app --cov-branch --cov-report=term-missing
# Must show: TOTAL 100%

# Frontend
cd frontend && pnpm test -- --coverage
# Must show: Statements 100%, Branches 100%, Functions 100%

# Quality Gates
cd backend && .venv/bin/ruff check . && .venv/bin/black --check . && .venv/bin/mypy .
cd frontend && pnpm lint && pnpm build
```

### Documentation Updates Required
| Doc | Location | Update |
|-----|----------|--------|
| Test Guide | `backend/docs/platform/03-testing.md` | Add isolated DB setup |
| CI Scripts | `.github/scripts/run-backend-gate.sh` | Add isolated DB flags |
| Contributing | `CONTRIBUTING.md` | Document test patterns |

---

## 9. Timeline Estimate

| Phase | Duration | Deliverable |
|-------|----------|-------------|
| **Infrastructure** | 2 days | Isolated DBs, fixed mocking |
| **Zero-Coverage (10 modules)** | 3 days | 10 new test files, 100% each |
| **High-Impact (7 modules)** | 4 days | 7 extended test files, >95% each |
| **Frontend (10 components)** | 3 days | 10 new test files, 100% each |
| **Integration & Verification** | 1 day | Full suite passes, docs updated |
| **Total** | **~13 days** | **100% Coverage** |

---

## 10. Risk Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| DB setup fails | Medium | High | Use existing `isolated-test-runs.md` procedure exactly |
| smtplib mocking fails | High | Medium | Use module-level patchers, not fixtures |
| Windows tests on Linux | High | Low | Skip with `@pytest.mark.skipif(sys.platform != "win32")` |
| Celery mocking incomplete | Medium | Medium | Test with `task_always_eager=True`, mock all dependencies |
| Frontend flakiness | Medium | Medium | Use `waitFor` with generous timeouts, test user interactions |

---

## Appendix: Quick Reference Commands

```bash
# Run single test with coverage
cd backend && .venv/bin/pytest tests/unit/test_api_keys_repository.py -v --cov=app/auth/repositories/api_keys --cov-branch

# Run all new tests
cd backend && .venv/bin/pytest tests/unit/test_*.py -v --cov=app --cov-branch --cov-report=term-missing

# Frontend single component
cd frontend && pnpm test -- tests/components/deepspace/DeepSpaceLibraryDrawer.test.tsx

# Check coverage gap
cd backend && .venv/bin/coverage report --rcfile=pyproject.toml | grep -E " 0%|  1%|  2%|  3%|  4%|  5%"
```

---

**Prepared by**: AI Assistant
**Review Required**: Before implementation begins
**Status**: READY FOR IMPLEMENTATION PHASE
