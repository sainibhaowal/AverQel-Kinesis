# 36. Session Feedback

DeepSpace supports session-level feedback with ratings, categories, and comments, recorded as session events.

## Use case

- Users want to rate conversation quality (positive/negative/neutral)
- Teams want to categorize feedback (accuracy, helpfulness, clarity, etc.)
- Operators want aggregate stats for quality monitoring

## Why it matters

- Previously, only message-level feedback existed (thumbs up/down)
- Now session-level feedback captures overall conversation quality
- Feedback recorded as typed session events for audit trail
- Aggregate stats endpoint for quality dashboards

## How it works

### Architecture

1. **Configuration**: `deepspace_feedback_enabled=true` plus category/validation settings
2. **Service**: `SessionFeedbackService` handles submission and retrieval
3. **Events**: Feedback recorded as `feedback/given` session events
4. **Stats**: `get_aggregate_stats()` for quality dashboards

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_feedback_enabled` | bool | `true` | Master switch |
| `deepspace_feedback_require_comment` | bool | `true` | Require comment on negative |
| `deepspace_feedback_max_comment` | int | `2000` | Max comment length |
| `deepspace_feedback_categories` | tuple | 6 categories | Allowed categories |

### Categories

Default categories: `accuracy`, `helpfulness`, `clarity`, `completeness`, `safety`, `other`

### Submission

```python
service = get_session_feedback_service(settings)

feedback = await service.submit_feedback(
    db=db,
    auth=auth,
    conversation_id=conv_id,
    rating="positive",
    categories=["accuracy", "helpfulness"],
    comment="Great job on the refactoring!",
    turn=5,
)
```

### API

```
POST /api/v1/deepspace/chats/{conversation_id}/feedback
{
  "rating": "positive",
  "categories": ["accuracy", "helpfulness"],
  "comment": "Great job!",
  "turn": 5
}
```

Response:
```json
{
  "feedback": {
    "id": "uuid",
    "conversation_id": "uuid",
    "tenant_id": "uuid",
    "user_id": "uuid",
    "rating": "positive",
    "categories": ["accuracy", "helpfulness"],
    "comment": "Great job!",
    "turn": 5,
    "created_at": "2024-01-15T10:30:00Z",
    "metadata": {}
  }
}
```

### Retrieval

```python
feedback_list = await service.get_feedback(
    db=db,
    auth=auth,
    conversation_id=conv_id,
)
```

### Aggregate stats

```python
stats = await service.get_aggregate_stats(
    db=db,
    auth=auth,
    tenant_id=None,  # or specific tenant
)
```

Response:
```json
{
  "total_feedback": 150,
  "positive": 90,
  "negative": 30,
  "neutral": 30,
  "positive_rate": 0.6,
  "categories": {
    "accuracy": 45,
    "helpfulness": 60,
    "clarity": 40,
    "completeness": 35,
    "safety": 10,
    "other": 20
  }
}
```

### Session event

Feedback recorded as `feedback/given` event:

```json
{
  "event_type": "feedback/given",
  "turn": 5,
  "payload": {
    "id": "uuid",
    "rating": "positive",
    "categories": ["accuracy", "helpfulness"],
    "comment": "Great job!",
    "turn": 5,
    "created_at": "2024-01-15T10:30:00Z",
    "metadata": {}
  }
}
```

### UI integration

The DeepSpace message actions include **Helpful**, **Needs Work**, and
**Report** buttons. For a DeepSpace conversation they submit a session-level
positive or negative rating to `/api/v1/deepspace/chats/{conversation_id}/feedback`
with the `helpfulness` category; the negative action supplies the default
comment required by the current configuration. The web client does not expose
the full category picker or aggregate-stats view described by the API.

## What to watch for

- **Validation**: Negative ratings require a comment if `require_comment_on_negative=true`; the current HTTP route also validates ownership and category names
- **Categories**: Only predefined categories accepted; extensible via settings
- **Comment length**: Limited to `max_comment_length` chars
- **Event log**: Feedback events in session log; queryable via session-query API
- **Aggregation**: Stats scoped to tenant by default; filterable
- **Privacy**: Feedback tied to user; not anonymous
- **Turn context**: Optional `turn` links feedback to specific turn
- **Retention**: Feedback events follow session log retention policy
