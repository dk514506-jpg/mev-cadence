r"""Structural port of letta-code transcript + in_context id-list bookkeeping
(G-A2, CHARGE-G1-BUILD-ENGINE-V2) — append-only transcript append/trim,
in_context id list add/remove/compact accounting, and the eviction bookkeeping
the compaction flow performs WITHOUT the summarizer.

Sources of record (frozen, byte-verified at port time):
- compaction.ts          sha256_12 61ed085ffdb3
  ($ROOF/reference/letta-code/src/backend/local/compaction.ts)
- local-store.ts         sha256_12 ff72185d95a2
  ($ROOF/reference/letta-code/src/backend/local/local-store.ts)

Supporting modules whose exported helpers implement the id-list bookkeeping
semantics used by the two named sources (imported by local-store.ts /
compaction.ts, not summarizer surface):
- local-transcript.ts    — spliceOutOfContextMessages, messagesMissingFrom,
  residentCoversIds, restrictLocalTranscriptToResidentMessages,
  overlayResidentLocalMessageTail, overlayResidentLocalMessageSuffix,
  localTranscriptRowsResult, localMessagesHaveSameSnapshot
- local-context-estimate.ts — estimateLocalMessageTokens (per-message),
  estimateLocalMessagesTokens (per-array)

SUMMARIZER EXCISION (explicit).  The following surface from compaction.ts is
NOT ported because it invokes the LLM (via `complete`) to generate summary
text — it is summarizer, not bookkeeping:
- summarizeLocalMessagesAll, summarizeLocalMessagesSlidingWindow,
  summarizeLocalMessagesWithPrompt (internal),
  runLocalPressureCompaction (opportunistic wrapper around the summarizer
  call),
- formatLocalMessagesForSummary, localMessagesToSummaryOpenAIDicts,
  textFromUserContent, textFromAssistantContent, toolReturnToText,
  localToolCallArguments, stringifyUnknown, truncateToolReturn,
  middleTruncateText (summarizer formatting helpers),
- LOCAL_ALL_COMPACTION_PROMPT, LOCAL_SLIDING_WINDOW_COMPACTION_PROMPT (LLM
  prompts),
- LOCAL_SUMMARY_TOOL_RETURN_TRUNCATION_CHARS (summarizer formatting constant),
- Fable model-selection fallback logic (compaction model routing).

Reason: these functions generate or format the summary text for the LLM.  The
id-list bookkeeping surface operates on the summary AS INPUT (finishLocal-
FullCompaction consumes `summary` and `packedSummary`; it does not produce
them).  The bookkeeping is what happens BEFORE (planning: planLocalSliding-
WindowCompaction, planLocalAllCompaction) and AFTER (accounting: in_context
id computation, stats, irreducible-context fallback) the summarizer call.

Port surface (binding, charge §2 G-A2):

Functions (16):
  assertLocalCompactionProgress, finishLocalFullCompaction,
  planLocalSlidingWindowCompaction, planLocalAllCompaction,
  estimateLocalMessageTokens, estimateLocalMessagesTokens,
  packageLocalSummaryMessage, isLocalSlidingWindowCompactionPlanningError,
  spliceOutOfContextMessages, messagesMissingFrom, residentCoversIds,
  restrictLocalTranscriptToResidentMessages, overlayResidentLocalMessageTail,
  overlayResidentLocalMessageSuffix, localTranscriptRowsResult,
  localMessagesHaveSameSnapshot

Error classes (2):
  LocalSlidingWindowCompactionPlanningError, LocalIrreducibleContextError

Constants (5):
  LOCAL_DEFAULT_COMPACTION_MODE, LOCAL_DEFAULT_SLIDING_WINDOW_PERCENTAGE,
  IMAGE_TOKEN_ESTIMATE, LOCAL_TRANSCRIPT_LEGACY_MESSAGE_FORMAT,
  LOCAL_TRANSCRIPT_MESSAGE_FORMAT

Recorded adaptations (each carried with a reason; everything else is exact):
1. String lengths that JS measures as `String.prototype.length` (UTF-16 code
   units) are computed via `_utf16_len` in Python for semantic identity with
   the TS source.  This affects `estimateLocalMessageTokens` (textLength /
   jsonLength) and `finishLocalFullCompaction` (systemPrompt/packedWarning
   overhead).
2. `Math.round` (half-away-from-zero for positive inputs) is implemented as
   `_js_round` (`math.floor(x + 0.5)`) because Python's built-in `round` uses
   banker's rounding which diverges at .5 boundaries.
3. `packageLocalSummaryMessage` and the fallback branch of
   `finishLocalFullCompaction` call `new Date().toISOString()`.  The port
   exposes `_now_iso()` (matching JS toISOString format) and the equivalence
   fixtures monkey-patch it on both sides to a fixed timestamp.
4. `console.warn(warning)` in `finishLocalFullCompaction` is omitted (side
   effect only, not part of the return value).
5. `LocalTranscriptRowsResult` uses `Map`/`Set` in TS.  The port uses `dict`
   (insertion-ordered, Python 3.7+) and `set`.  Equivalence canonicalisation
   converts Map->sorted-key dict, Set->sorted list.
6. `compactConversationAccounting` (from `LocalStore.compactConversationAll`,
   local-store.ts lines ~1196-1203) is extracted as a pure function.  The
   original is a method on the stateful `LocalStore` class; the pure
   accounting (compactedMessages, inContextIds, numMessagesBefore/After) is
   carried exactly.  Same for `removeFromInContext` (local-store.ts line
   ~1604: filter one id from in_context_message_ids).
7. `typeof value === "number"` checks use `_is_number` which excludes `bool`
   (Python `bool` subclasses `int`; JS `typeof true === "boolean"`).
8. `Array.prototype.at(-1)` is ported as `messages[-1] if messages else None`
   to match JS returning `undefined` for empty arrays.
"""

import json
import math

# --- constants (carried exactly from the TS sources) ----------------------

LOCAL_DEFAULT_COMPACTION_MODE = "sliding_window"
LOCAL_DEFAULT_SLIDING_WINDOW_PERCENTAGE = 0.3

# local-context-estimate.ts: "Matches Pi's `estimateTokens` image cost:
# 4800 chars / 4 = 1200 tokens."
IMAGE_TOKEN_ESTIMATE = 1200

# local-transcript.ts
LOCAL_TRANSCRIPT_LEGACY_MESSAGE_FORMAT = "pi-ai-message-jsonl"
LOCAL_TRANSCRIPT_MESSAGE_FORMAT = "pi-session-entry-jsonl"

# --- error classes (carried from compaction.ts) ---------------------------


class LocalSlidingWindowCompactionPlanningError(Exception):
    """compaction.ts LocalSlidingWindowCompactionPlanningError."""


class LocalIrreducibleContextError(Exception):
    """compaction.ts LocalIrreducibleContextError."""


# --- JS-semantics helpers (recorded adaptations 1, 2, 7) -----------------


def _is_number(value):
    """typeof value === "number" — excludes bool (adaptation 7)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _utf16_len(s):
    """JS String.prototype.length (UTF-16 code units) — adaptation 1."""
    return len(s.encode("utf-16-le")) // 2


def _js_round(x):
    """JS Math.round for non-negative x (half away from zero) — adaptation 2."""
    return math.floor(x + 0.5)


def _now_iso():
    """JS new Date().toISOString() — adaptation 3.  Monkey-patchable for tests."""
    import datetime

    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def _json_length(value):
    """JS JSON.stringify(value).length — UTF-16 code units of compact JSON."""
    try:
        return _utf16_len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


# --- local-context-estimate.ts: token estimation --------------------------


def estimateLocalMessageTokens(message):
    """local-context-estimate.ts estimateLocalMessageTokens (per-message).

    Estimates the context token cost of one LocalMessage by counting chars
    (text, thinking, toolCall name+arguments JSON) at 1 token per 4 chars,
    and images at IMAGE_TOKEN_ESTIMATE tokens (4 chars per token => 4800
    chars each).
    """
    chars = 0
    role = message.get("role")
    content = message.get("content")

    if role == "user":
        if isinstance(content, str):
            chars = _utf16_len(content)
        else:
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        chars += _utf16_len(t)
                elif btype == "image":
                    chars += IMAGE_TOKEN_ESTIMATE * 4
        return math.ceil(chars / 4)

    if role == "assistant":
        for block in content:
            btype = block.get("type")
            if btype == "text":
                t = block.get("text")
                if isinstance(t, str):
                    chars += _utf16_len(t)
            elif btype == "thinking":
                t = block.get("thinking")
                if isinstance(t, str):
                    chars += _utf16_len(t)
            elif btype == "toolCall":
                name = block.get("name")
                if isinstance(name, str):
                    chars += _utf16_len(name)
                chars += _json_length(block.get("arguments"))
        return math.ceil(chars / 4)

    # toolResult and any other role
    for block in content:
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str):
                chars += _utf16_len(t)
        elif btype == "image":
            chars += IMAGE_TOKEN_ESTIMATE * 4
    return math.ceil(chars / 4)


def estimateLocalMessagesTokens(messages):
    """local-context-estimate.ts estimateLocalMessagesTokens (per-array)."""
    return sum(estimateLocalMessageTokens(m) for m in messages)


# --- compaction.ts: internal helpers --------------------------------------


def hasPendingLocalToolCall(message):
    """compaction.ts hasPendingLocalToolCall — assistant with a toolCall block."""
    return (
        message.get("role") == "assistant"
        and isinstance(message.get("content"), list)
        and any(part.get("type") == "toolCall" for part in message["content"])
    )


def normalizedSlidingWindowPercentage(value):
    """compaction.ts normalizedSlidingWindowPercentage."""
    if not _is_number(value) or not math.isfinite(value):
        return LOCAL_DEFAULT_SLIDING_WINDOW_PERCENTAGE
    if value <= 0:
        return 0.1
    if value > 1:
        return 1
    return value


def isValidSlidingWindowCutoff(messages, index, maximumCutoffIndex):
    """compaction.ts isValidSlidingWindowCutoff — module-private."""
    message = messages[index] if 0 <= index < len(messages) else None
    return (
        message is not None
        and message.get("role") == "assistant"
        and index > 0
        and index < maximumCutoffIndex
    )


# --- compaction.ts: bookkeeping exports -----------------------------------


def assertLocalCompactionProgress(stats, mode):
    """compaction.ts assertLocalCompactionProgress.

    Throws if the compaction produced no progress (neither message count nor
    estimated tokens decreased).  mode "all" throws LocalIrreducibleContext-
    Error; anything else throws LocalSlidingWindowCompactionPlanningError.
    """
    after_msgs = stats.get("messages_count_after")
    before_msgs = stats.get("messages_count_before")
    after_tokens = stats.get("context_tokens_after")
    before_tokens = stats.get("context_tokens_before")

    has_progress = (
        (float("inf") if after_msgs is None else after_msgs)
        < (0 if before_msgs is None else before_msgs)
    ) or (
        (float("inf") if after_tokens is None else after_tokens)
        < (0 if before_tokens is None else before_tokens)
    )
    if has_progress:
        return
    if mode == "all":
        raise LocalIrreducibleContextError(
            "Irreducible context: full compaction would not reduce the message count or estimated tokens."
        )
    raise LocalSlidingWindowCompactionPlanningError(
        "Sliding-window compaction would not reduce the message count or estimated tokens."
    )


def planLocalSlidingWindowCompaction(messages, options=None):
    """compaction.ts planLocalSlidingWindowCompaction.

    Computes the sliding-window split: messagesToSummarize (prefix) and
    messagesToKeep (suffix) with the cutoffIndex boundary.
    """
    if options is None:
        options = {}
    if len(messages) < 4:
        raise LocalSlidingWindowCompactionPlanningError(
            "Not enough messages for sliding window compaction."
        )

    percentage = normalizedSlidingWindowPercentage(
        options.get("slidingWindowPercentage")
    )
    last_message = messages[-1] if messages else None
    maximum_cutoff_index = (
        len(messages) - 2
        if last_message and hasPendingLocalToolCall(last_message)
        else len(messages) - 1
    )

    context_window = options.get("contextWindow")
    goal_tokens = (
        (1 - percentage) * context_window
        if _is_number(context_window) and math.isfinite(context_window)
        else None
    )
    approx_token_count = (
        context_window if context_window is not None else float("inf")
    )
    cutoff_index = None

    eviction_percentage = percentage
    while (
        (
            cutoff_index is None
            if goal_tokens is None
            else approx_token_count >= goal_tokens
        )
        and eviction_percentage < 1.0
    ):
        eviction_percentage += 0.1
        message_cutoff_index = min(
            _js_round(eviction_percentage * len(messages)),
            len(messages) - 1,
        )
        cutoff_index = None
        for idx in range(message_cutoff_index, -1, -1):
            if isValidSlidingWindowCutoff(messages, idx, maximum_cutoff_index):
                cutoff_index = idx
                break
        if cutoff_index is None:
            continue
        messages_to_keep = messages[cutoff_index:]
        approx_token_count = estimateLocalMessagesTokens(messages_to_keep)

    if cutoff_index is None or eviction_percentage >= 1.0:
        raise LocalSlidingWindowCompactionPlanningError(
            "No assistant message found for sliding window compaction."
        )

    if cutoff_index >= maximum_cutoff_index:
        raise LocalSlidingWindowCompactionPlanningError(
            "Assistant message index %d is at the end of the message buffer, skipping compaction."
            % cutoff_index
        )

    return {
        "messagesToSummarize": messages[:cutoff_index],
        "messagesToKeep": messages[cutoff_index:],
        "cutoffIndex": cutoff_index,
    }


def planLocalAllCompaction(messages):
    """compaction.ts planLocalAllCompaction.

    Full-compaction split: keep only the trailing message if it has a pending
    tool call; otherwise everything is summarised.
    """
    last_message = messages[-1] if messages else None
    if last_message and hasPendingLocalToolCall(last_message):
        return {
            "messagesToSummarize": messages[:-1],
            "messagesToKeep": [last_message],
        }
    return {"messagesToSummarize": list(messages), "messagesToKeep": []}


def packageLocalSummaryMessage(summary, stats=None, mode=None):
    """compaction.ts packageLocalSummaryMessage.

    Packages a summary string into the system_alert JSON envelope used as the
    compaction summary message.  mode "sliding_window" uses a different
    preamble; stats is included as compaction_stats when truthy.
    """
    if mode and "sliding_window" in mode:
        before = stats.get("messages_count_before") if stats else None
        after = stats.get("messages_count_after") if stats else None
        if before is not None and after is not None:
            num_evicted = before - after
            message = (
                "Note: %d messages from the beginning of the conversation "
                "have been hidden from view due to memory constraints.\n"
                "The following is a summary of the previous messages:\n %s"
                % (num_evicted, summary)
            )
        else:
            message = (
                "Note: prior messages from the beginning of the conversation "
                "have been hidden from view due to conversation memory constraints.\n"
                "The following is a summary of the previous messages:\n %s"
                % summary
            )
    else:
        message = (
            "Note: prior messages have been hidden from view due to conversation "
            "memory constraints.\nThe following is a summary of the previous "
            "messages:\n %s" % summary
        )
    result = {"type": "system_alert", "message": message, "time": _now_iso()}
    if stats is not None:
        result["compaction_stats"] = stats
    return json.dumps(result, separators=(",", ":"), ensure_ascii=False)


def isLocalSlidingWindowCompactionPlanningError(error):
    """compaction.ts isLocalSlidingWindowCompactionPlanningError."""
    return isinstance(error, LocalSlidingWindowCompactionPlanningError)


def finishLocalFullCompaction(input_data):
    """compaction.ts finishLocalFullCompaction.

    Post-summarizer bookkeeping: validates progress, falls back to turn-level
    eviction on irreducible context (context_window_overflow trigger only),
    computes remaining messages and stats, then delegates persistence to the
    `persist` callback.

    Recorded adaptation 3: _now_iso() replaces new Date().toISOString().
    Recorded adaptation 4: console.warn omitted.
    """
    summary = input_data["summary"]
    stats = input_data["stats"]
    remaining = list(input_data["plan"]["messagesToKeep"])
    warning = None

    try:
        assertLocalCompactionProgress(stats, "all")
    except LocalIrreducibleContextError as error:
        if stats.get("trigger") != "context_window_overflow":
            raise error

        messages = input_data["messages"]
        # Preserve the latest user turn, including its tool calls and results.
        latest_turn = -1
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if m.get("role") == "user" and not (m.get("metadata") or {}).get(
                "compaction"
            ):
                latest_turn = i
                break
        if latest_turn < 0:
            latest_turn = 0
            for i, m in enumerate(messages):
                if not (m.get("metadata") or {}).get("compaction"):
                    latest_turn = i
                    break

        cutoff = 0
        warning = (
            "Warning: older messages were dropped because compaction could not "
            "reduce the context. The latest turn was kept."
        )
        packed_warning = packageLocalSummaryMessage(warning)
        overhead = math.ceil(
            (
                _utf16_len(input_data["systemPrompt"])
                + _utf16_len(packed_warning)
            )
            / 4
        )
        while True:
            if cutoff >= latest_turn:
                break
            # Evict whole turns so tool results never outlive their calls.
            cutoff += 1
            while (
                cutoff < latest_turn
                and messages[cutoff].get("role") != "user"
            ):
                cutoff += 1
            cw = stats.get("context_window")
            cw_val = 0 if cw is None else cw
            if not (
                overhead + estimateLocalMessagesTokens(messages[cutoff:])
                >= cw_val
            ):
                break

        remaining = messages[cutoff:]
        stats["messages_count_after"] = len(remaining) + (1 if cutoff > 0 else 0)
        stats["context_tokens_after"] = estimateLocalMessagesTokens(remaining)
        if cutoff == 0:
            return {
                "numMessagesBefore": len(messages),
                "numMessagesAfter": len(messages),
                "summary": "",
                "stats": stats,
            }
        summary = warning
        stats["context_tokens_after"] = (
            stats.get("context_tokens_after", 0) + overhead
        )

    if warning:
        packed_summary = json.dumps(
            {
                "type": "system_alert",
                "message": warning,
                "time": _now_iso(),
                "compaction_stats": stats,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
    else:
        packed_summary = packageLocalSummaryMessage(summary, stats, "all")

    result = input_data["persist"](summary, packed_summary, remaining)
    return {**result, "summary": summary, "stats": stats}


# --- local-transcript.ts: id-list bookkeeping exports ----------------------


def spliceOutOfContextMessages(messages, inContextIds, floor):
    """local-transcript.ts spliceOutOfContextMessages.

    Evict resident messages that left the in-context set, oldest first, while
    the window holds more than `floor` messages.  In-context messages and the
    newest message (appended but not yet recorded as in context) always stay.

    Mutates `messages` in place (removes evicted entries) and returns the
    evicted list.
    """
    in_context = set(inContextIds)
    evicted = []
    index = 0
    while index < len(messages) - 1 and len(messages) > floor:
        message = messages[index]
        if message and message.get("id") not in in_context:
            evicted.append(messages.pop(index))
        else:
            index += 1
    return evicted


def messagesMissingFrom(replaced, next_msgs):
    """local-transcript.ts messagesMissingFrom.

    Messages of a replaced window whose ids the new window no longer holds.
    """
    next_ids = {m.get("id") for m in next_msgs}
    return [m for m in (replaced or []) if m.get("id") not in next_ids]


def residentCoversIds(resident, activeMessageIds):
    """local-transcript.ts residentCoversIds.

    Whether a resident window holds every active id, so readers can serve the
    logical list from it without touching disk.
    """
    if not activeMessageIds:
        return False
    resident_ids = {m.get("id") for m in resident}
    return all(aid in resident_ids for aid in activeMessageIds)


def restrictLocalTranscriptToResidentMessages(transcript, residentMessages):
    """local-transcript.ts restrictLocalTranscriptToResidentMessages.

    Session-entry bookkeeping for a resident/active tail.  The suffix parse
    may contain historical JSONL rows; persist and session maps must not pin
    those.

    Input/output is a LocalTranscriptRowsResult dict with keys:
      messages, entryIds (set), entryIdByMessageId (dict),
      messageById (dict), lastEntryId (str|None), sourceStartIndex (int)
    """
    message_by_id = {}
    entry_id_by_message_id = {}
    entry_ids = set()
    for message in residentMessages:
        mid = message.get("id")
        message_by_id[mid] = transcript.get("messageById", {}).get(mid, message)
        entry_id = transcript.get("entryIdByMessageId", {}).get(mid)
        if entry_id is None:
            continue
        entry_id_by_message_id[mid] = entry_id
        entry_ids.add(entry_id)
    last_entry_id = transcript.get("lastEntryId")
    if last_entry_id:
        entry_ids.add(last_entry_id)
    return {
        "messages": list(residentMessages),
        "entryIds": entry_ids,
        "entryIdByMessageId": entry_id_by_message_id,
        "messageById": message_by_id,
        "lastEntryId": last_entry_id,
        "sourceStartIndex": transcript.get("sourceStartIndex", 0),
    }


def overlayResidentLocalMessageTail(diskMessages, residentTail):
    """local-transcript.ts overlayResidentLocalMessageTail.

    Merge the authoritative on-disk message list with the resident tail
    window.  The resident window is a suffix of the logical message list:
    disk rows not in the window come first, then the window in its own order
    (resident entries win by id).  Order-preserving merge.
    """
    if not residentTail:
        return list(diskMessages)
    disk_ids = {m.get("id") for m in diskMessages}
    merged = []
    resident_index = 0
    for message in diskMessages:
        # Emit resident-only entries that precede this disk row.
        while resident_index < len(residentTail):
            resident = residentTail[resident_index]
            if resident is None:
                break
            rid = resident.get("id")
            if rid == message.get("id") or rid in disk_ids:
                break
            merged.append(resident)
            resident_index += 1
        resident = (
            residentTail[resident_index]
            if resident_index < len(residentTail)
            else None
        )
        if resident is not None and resident.get("id") == message.get("id"):
            merged.append(resident)
            resident_index += 1
            continue
        merged.append(message)
    while resident_index < len(residentTail):
        resident = residentTail[resident_index]
        if resident is not None:
            merged.append(resident)
        resident_index += 1
    return merged


def overlayResidentLocalMessageSuffix(diskSuffix, residentTail, isPersisted):
    """local-transcript.ts overlayResidentLocalMessageSuffix.

    Overlay a resident tail onto a disk *suffix* (not the full transcript).
    Older resident-only ids belong before this suffix and must not be
    appended.  Trailing unpersisted messages still trail.  Overlapping ids
    take the resident snapshot.
    """
    if not residentTail:
        return list(diskSuffix)
    disk_ids = {m.get("id") for m in diskSuffix}
    overlay_tail = []
    seen_persisted = False
    for i in range(len(residentTail) - 1, -1, -1):
        message = residentTail[i]
        if message is None:
            continue
        if isPersisted(message.get("id")):
            seen_persisted = True
        if message.get("id") in disk_ids or not seen_persisted:
            overlay_tail.insert(0, message)
    return overlayResidentLocalMessageTail(diskSuffix, overlay_tail)


# --- local-transcript.ts: row parsing with in_context id filtering ----------


def _is_record(value):
    """JS isRecord — plain object check."""
    return isinstance(value, dict)


def _is_session_message_entry(value):
    return (
        _is_record(value)
        and value.get("type") == "message"
        and isinstance(value.get("id"), str)
        and (value.get("parentId") is None or isinstance(value.get("parentId"), str))
        and isinstance(value.get("timestamp"), str)
        and _is_record(value.get("message"))
        and isinstance(value["message"].get("id"), str)
    )


def _is_compaction_entry(value):
    return (
        _is_record(value)
        and value.get("type") == "compaction"
        and isinstance(value.get("id"), str)
        and (value.get("parentId") is None or isinstance(value.get("parentId"), str))
        and isinstance(value.get("timestamp"), str)
        and isinstance(value.get("summary"), str)
        and (
            value.get("firstKeptEntryId") is None
            or isinstance(value.get("firstKeptEntryId"), str)
        )
        and _is_number(value.get("tokensBefore"))
        and _is_record(value.get("message"))
        and isinstance(value["message"].get("id"), str)
    )


def _is_append_entry(value):
    return _is_session_message_entry(value) or _is_compaction_entry(value)


def _set_latest_local_message(messages_by_id, message):
    """Map#set does not move existing keys — delete first for recency."""
    mid = message.get("id")
    if mid in messages_by_id:
        del messages_by_id[mid]
    messages_by_id[mid] = message


def localTranscriptRowsResult(rows, messageFormat, activeMessageIds=None):
    """local-transcript.ts localTranscriptRowsResult.

    Parses raw transcript rows into a LocalTranscriptRowsResult dict,
    selecting only the messages whose ids appear in `activeMessageIds` (the
    in_context id list).  Legacy format treats rows as LocalMessage objects;
    the new session-entry format extracts messages from typed entries.
    """
    if activeMessageIds is None:
        activeMessageIds = []

    if messageFormat == LOCAL_TRANSCRIPT_LEGACY_MESSAGE_FORMAT:
        all_messages = list(rows)
        message_by_id = {}
        for message in all_messages:
            _set_latest_local_message(message_by_id, message)
        if activeMessageIds:
            active_messages = [
                message_by_id[aid]
                for aid in activeMessageIds
                if aid in message_by_id
            ]
        else:
            active_messages = list(message_by_id.values())
        first_active_id = active_messages[0].get("id") if active_messages else None
        return {
            "messages": active_messages,
            "entryIds": {m.get("id") for m in all_messages},
            "entryIdByMessageId": {m.get("id"): m.get("id") for m in all_messages},
            "messageById": message_by_id,
            "lastEntryId": (
                all_messages[-1].get("id") if all_messages else None
            ),
            "sourceStartIndex": (
                max(0, activeMessageIds.index(first_active_id))
                if first_active_id and first_active_id in activeMessageIds
                else 0
            ),
        }

    # New session-entry format
    entry_ids = set()
    entry_id_by_message_id = {}
    all_messages = []
    message_by_id = {}
    last_entry_id = None

    for row in rows:
        if not _is_append_entry(row):
            continue
        entry_ids.add(row["id"])
        last_entry_id = row["id"]
        entry_id_by_message_id[row["message"]["id"]] = row["id"]
        all_messages.append(row["message"])
        _set_latest_local_message(message_by_id, row["message"])

    if activeMessageIds:
        active_messages = [
            message_by_id[aid] for aid in activeMessageIds if aid in message_by_id
        ]
    else:
        active_messages = list(message_by_id.values())
    first_active_id = active_messages[0].get("id") if active_messages else None

    return {
        "messages": active_messages,
        "entryIds": entry_ids,
        "entryIdByMessageId": entry_id_by_message_id,
        "messageById": message_by_id,
        "lastEntryId": last_entry_id,
        "sourceStartIndex": (
            max(0, activeMessageIds.index(first_active_id))
            if first_active_id and first_active_id in activeMessageIds
            else 0
        ),
    }


def localMessagesHaveSameSnapshot(a, b):
    """local-transcript.ts localMessagesHaveSameSnapshot.

    Snapshot comparison via JSON string equality.  JS JSON.stringify
    preserves key insertion order; Python json.dumps (without sort_keys)
    does too (3.7+).
    """
    return json.dumps(a, separators=(",", ":"), ensure_ascii=False) == json.dumps(
        b, separators=(",", ":"), ensure_ascii=False
    )


# --- local-store.ts: extracted pure accounting (adaptation 6) -------------


def compactConversationAccounting(previousMessages, summaryMessage, remainingMessages):
    """Pure accounting from LocalStore.compactConversationAll (local-store.ts).

    Builds the compacted message list [summaryMessage, ...remainingMessages],
    computes the new in_context id list, and reports before/after counts.
    """
    compacted = [summaryMessage] + list(remainingMessages or [])
    in_context_ids = [m.get("id") for m in compacted]
    return {
        "compactedMessages": compacted,
        "inContextIds": in_context_ids,
        "numMessagesBefore": len(previousMessages),
        "numMessagesAfter": len(compacted),
    }


def removeFromInContext(inContextIds, messageId):
    """Remove one id from the in_context list (local-store.ts line ~1604)."""
    return [i for i in inContextIds if i != messageId]
