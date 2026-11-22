# DeepSpace Structured Questions User Guide

**Date:** 2026-11-19

**Audience:** End users answering clarification questions from DeepSpace

---

## Overview

When DeepSpace needs clarification, it uses the **`ask_user_question`** tool to ask structured questions with multiple choice options, text inputs, or both — instead of just asking in plain text. You answer using an interactive card, and DeepSpace receives structured data it can use directly.

---

## The Question Card

When DeepSpace asks a question, you'll see an interactive card:

```
┌─────────────────────────────────────────────────────┐
│  🤖 Your input is needed                           │
│  ⏳ Waiting for you                                │
├─────────────────────────────────────────────────────┤
│ Which format would you like the report in?         │
│                                                     │
│  ○ PDF          Recommended — best for sharing     │
│  ○ Markdown     Good for version control           │
│  ○ HTML         For web publishing                 │
│                                                     │
│  ☐ Include charts?                                 │
│                                                     │
│  [Send answers]                                    │
└─────────────────────────────────────────────────────┤
```

**Card elements:**
- **Question** — The main prompt
- **Options** — Radio buttons (single) or checkboxes (multi-select)
- **Custom input** — Text field for "Other" answers
- **Send answers** — Submits your selections

---

## Question Types

### Single Choice (Radio)
Pick **one** option. First option is often marked **Recommended**.

### Multi-Select (Checkboxes)
Pick **multiple** options. Indicated by "Select all that apply" or similar.

### Free Text
When no options are provided, type your answer in the text area.

### Mixed
Options + optional "Other" text field for custom answers.

---

## How to Answer

### Single / Multi-Select
1. Click the option(s) you want
2. (Optional) Fill in "Other" text field
3. Click **Send answers**

### Free Text
1. Type your answer in the text area
2. Press **Enter** or click **Send answer**

### Keyboard Shortcuts
| Key | Action |
|-----|--------|
| `Enter` | Send answer (text area) |
| `Shift+Enter` | New line (text area) |
| `Tab` | Navigate between options |

---

## What Happens After You Answer

1. Your selections are sent as **structured data** (not plain text)
2. DeepSpace receives:
```json
{
  "answers": [
    {"id": "format", "selected": ["PDF"]},
    {"id": "charts", "selected": []}
  ]
}
```
3. DeepSpace continues immediately using your structured choices
4. The question card disappears from the chat

---

## Skipping Questions

- **Leave unselected** — Empty selection = "skipped"
- **No custom text** — Empty text = "skipped"
- DeepSpace continues without that answer

> ⚠️ **Skipped ≠ Permission** — Skipping a question does NOT grant permission or imply consent.

---

## Timed Questions

Some questions have a **timeout** (shown as countdown). If you don't answer in time:

1. The question becomes **pending** — still answerable
2. DeepSpace continues with other work
3. Your late answer arrives as a **reply** to the pending question
4. DeepSpace resumes the original task with your answer

> ⏱️ **Timeout ≠ Skipped** — A timeout means "no answer yet," not "user skipped."

---

## Pending Questions

If you close the card or navigate away:

- The question stays **active** in the chat history
- Click the question in history to reopen the card
- Answer anytime — no expiration

---

## Keyboard Navigation

| Key | Action |
|-----|--------|
| `↑/↓` | Navigate options |
| `Space` | Toggle checkbox / select radio |
| `Enter` | Submit (when focused on button) |
| `Esc` | Close card (question stays pending) |

---

## Accessibility

- Full keyboard navigation
- Screen reader compatible (ARIA labels)
- High contrast mode support
- Focus indicators visible

---

## Tool Details (For Developers)

The `ask_user_question` tool arguments:

| Field | Type | Description |
|-------|------|-------------|
| `questions` | array (1–10) | Questions to ask |
| `questions[].id` | string (1–128) | Stable id echoed in the answer |
| `questions[].question` | string (1–4000) | The specific question |
| `questions[].header` | string (optional) | Short heading like "Confirm" |
| `questions[].options` | array (1–20) | Optional choices |
| `questions[].options[].label` | string (1–200) | User-facing option label |
| `questions[].options[].description` | string (optional) | Tradeoff/impact explanation |
| `questions[].multi_select` | bool | Allow multiple selections |
| `timeout` | int (optional) | Wait seconds; `-1` = wait forever, omit = blocking |

**Timeout behavior:**
- Omitted → blocking wait until answer
- `-1` → wait forever until answer
- `N` seconds → timed wait; returns `pending` if timeout expires

---

## Tips

1. **Read all options** before selecting — first is often recommended
2. **Use "Other"** for answers not in the list
3. **Multi-select** — Hold `Cmd/Ctrl` to select multiple
4. **Don't worry about mistakes** — You can't "break" anything by answering
5. **Questions persist** — Close the card, answer later from history

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Card didn't appear | Check chat history — pending questions show in message list |
| "Send answers" disabled | Must select at least one option or enter text |
| Answered but nothing happened | Check if DeepSpace is processing — may take a moment |
| Accidentally sent wrong answer | Continue the conversation — you can clarify in next message |

---

## Related

- [DeepSpace Jobs Guide](DEEPSPACE_JOBS_USER_GUIDE.md) — Background tasks
- [DeepSpace Permissions Guide](DEEPSPACE_PERMISSION_USER_GUIDE.md) — Permission modes and sandbox

---

*See also: [DeepSpace Permission Guide](DEEPSPACE_PERMISSION_USER_GUIDE.md)*