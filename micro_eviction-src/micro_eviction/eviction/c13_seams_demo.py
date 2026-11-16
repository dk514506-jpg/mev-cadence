"""C13 seams demonstration (G-F, CHARGE-G1-BUILD-ENGINE-V2 §2) — the letta
id-list bookkeeping (micro_eviction/letta_bookkeeping.py) survives with the
summarizer excised.

Demonstration artifact, not product code.  It runs ONE compaction/id-list
cycle over a small in-script transcript and asserts the id-list accounting
stays consistent across:

  planLocalAllCompaction / hasPendingLocalToolCall  (pre-summarizer planning)
  packageLocalSummaryMessage                        (summary packaging)
  finishLocalFullCompaction                         (post-summarizer accounting)
  compactConversationAccounting / removeFromInContext (in_context id lists)
  spliceOutOfContextMessages / messagesMissingFrom  (resident-window eviction)
  residentCoversIds / localMessagesHaveSameSnapshot (window/snapshot invariants)

SUMMARIZER EXCISION (explicit): no LLM call exists anywhere in this demo.
The summary text is canned INPUT to the bookkeeping — exactly the port
contract (finishLocalFullCompaction consumes `summary`; it does not produce
it).  `_now_iso` is monkey-patched to a fixed timestamp (recorded adaptation
3) so the captured output is byte-stable.

Machine-checkable final line on stdout:  C13_SEAMS_DEMO: PASS
(Exit 0 on success; the final line is C13_SEAMS_DEMO: FAIL and exit 1 when
any id-list accounting check fails.)
"""

import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # file-mode execution: bootstrap the campaign root
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import micro_eviction.letta_bookkeeping as lb  # noqa: E402

SUMMARY_MESSAGE_ID = "m-sum-1"
CANNED_SUMMARY = (
    "CANNED SUMMARY (summarizer excised): turns 1-7 read src/app.py, "
    "found a TODO, edited the file; verification is pending."
)


def build_transcript():
    """Small in-script transcript (8 messages, ids m1..m8)."""
    return [
        {"id": "m1", "role": "user",
         "content": [{"type": "text",
                      "text": "Please inspect src/app.py and act on TODOs."}]},
        {"id": "m2", "role": "assistant",
         "content": [{"type": "text", "text": "Reading the file."},
                     {"type": "toolCall", "name": "read",
                      "arguments": {"path": "src/app.py"}}]},
        {"id": "m3", "role": "toolResult",
         "content": [{"type": "text", "text": "def main():\n    ..."}]},
        {"id": "m4", "role": "assistant",
         "content": [{"type": "text", "text": "Searching for TODOs."},
                     {"type": "toolCall", "name": "search",
                      "arguments": {"query": "TODO"}}]},
        {"id": "m5", "role": "toolResult",
         "content": [{"type": "text", "text": "TODO: refactor main()"}]},
        {"id": "m6", "role": "assistant",
         "content": [{"type": "text", "text": "Editing the file."},
                     {"type": "toolCall", "name": "edit",
                      "arguments": {"path": "src/app.py"}}]},
        {"id": "m7", "role": "toolResult",
         "content": [{"type": "text", "text": "edit ok"}]},
        {"id": "m8", "role": "assistant",
         "content": [{"type": "text", "text": "Running verification."},
                     {"type": "toolCall", "name": "bash",
                      "arguments": {"command": "pytest"}}]},
    ]


def run_c13_seams_demo(*, tamper_delta: int = 0) -> dict:
    """Run the compaction/id-list cycle and check the accounting.

    ``tamper_delta`` seeds a tampered id-list delta into the in_context
    accounting (positive: ghost ids appended; negative: that many ids
    dropped from the tail) so a mutated bookkeeping path returns a failure
    result instead of a silent ok.  Returns
    {"ok": bool, "checks": [...], "failures": [...], "log": [...]}.
    """
    checks = []
    log = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        log.append("C13_SEAMS_DEMO: check %-38s %s %s"
                   % (name, "OK" if ok else "FAIL", detail))
        return ok

    messages = build_transcript()
    in_context_before = [m["id"] for m in messages]
    log.append("transcript=%d messages in_context_before=%s"
               % (len(messages), in_context_before))

    plan = lb.planLocalAllCompaction(messages)
    keep = list(plan["messagesToKeep"])
    summarize = list(plan["messagesToSummarize"])
    log.append("plan mode=all pending_tool_call=%s keep=%s summarize=%d"
               % (lb.hasPendingLocalToolCall(messages[-1]),
                  [m["id"] for m in keep], len(summarize)))
    check("plan_keeps_pending_tool_call_tail",
          [m["id"] for m in keep] == ["m8"] and len(summarize) == 7)

    stats = {
        "messages_count_before": len(messages),
        "messages_count_after": len(keep) + 1,
        "context_tokens_before": lb.estimateLocalMessagesTokens(messages),
        "context_tokens_after": (
            lb.estimateLocalMessagesTokens(keep)
            + lb.estimateLocalMessageTokens(
                {"role": "assistant",
                 "content": [{"type": "text", "text": CANNED_SUMMARY}]})
        ),
        "trigger": "manual",
        "context_window": 128000,
    }

    captured = {}

    def persist(summary, packed_summary, remaining):
        summary_message = {
            "id": SUMMARY_MESSAGE_ID,
            "role": "assistant",
            "content": [{"type": "text", "text": packed_summary}],
        }
        accounting = lb.compactConversationAccounting(
            messages, summary_message, remaining)
        captured["accounting"] = accounting
        captured["summary_message"] = summary_message
        captured["packed_summary"] = packed_summary
        return {
            "numMessagesBefore": accounting["numMessagesBefore"],
            "numMessagesAfter": accounting["numMessagesAfter"],
        }

    prev_now = lb._now_iso
    lb._now_iso = lambda: "2026-11-04T00:00:00.000Z"
    try:
        result = lb.finishLocalFullCompaction({
            "summary": CANNED_SUMMARY,
            "stats": stats,
            "plan": plan,
            "messages": messages,
            "systemPrompt": "You are a careful agent.",
            "persist": persist,
        })
    finally:
        lb._now_iso = prev_now

    accounting = captured["accounting"]
    summary_message = captured["summary_message"]
    compacted = accounting["compactedMessages"]
    in_context_after = list(accounting["inContextIds"])

    # --- the tampered delta (seeded mutation of the id-list accounting) ----
    if tamper_delta > 0:
        in_context_after = in_context_after + [
            "m-ghost-%d" % i for i in range(tamper_delta)]
    elif tamper_delta < 0:
        in_context_after = in_context_after[:tamper_delta]

    log.append("in_context_after=%s tamper_delta=%d"
               % (in_context_after, tamper_delta))
    log.append("compacted=%s" % [m["id"] for m in compacted])

    evicted = lb.messagesMissingFrom(messages, compacted)
    evicted_ids = [m["id"] for m in evicted]
    log.append("evicted=%s" % evicted_ids)

    compacted_ids = [m["id"] for m in compacted]
    check("in_context_ids_match_compacted",
          in_context_after == compacted_ids,
          "in_context=%s compacted=%s" % (in_context_after, compacted_ids))
    check("no_duplicate_in_context_ids",
          len(in_context_after) == len(set(in_context_after)))
    check("counts_before_after_consistent",
          accounting["numMessagesBefore"] == len(messages)
          and accounting["numMessagesAfter"] == len(compacted)
          and accounting["numMessagesAfter"] == len(keep) + 1,
          "before=%d after=%d"
          % (accounting["numMessagesBefore"],
             accounting["numMessagesAfter"]))
    check("finish_result_carries_counts",
          result["numMessagesBefore"] == accounting["numMessagesBefore"]
          and result["numMessagesAfter"] == accounting["numMessagesAfter"])
    check("evicted_ids_never_in_context",
          set(evicted_ids).isdisjoint(set(in_context_after)))
    check("evicted_partition_originals",
          set(evicted_ids) | (set(in_context_after) - {SUMMARY_MESSAGE_ID})
          == set(in_context_before)
          and set(evicted_ids).isdisjoint(
              set(in_context_after) - {SUMMARY_MESSAGE_ID}),
          "evicted=%s kept=%s"
          % (evicted_ids,
             sorted(set(in_context_after) - {SUMMARY_MESSAGE_ID})))

    walked = list(in_context_before)
    for eid in evicted_ids:
        walked = lb.removeFromInContext(walked, eid)
    check("removeFromInContext_walk_equals_kept",
          walked == sorted(set(in_context_after) - {SUMMARY_MESSAGE_ID}),
          "walk=%s" % walked)

    resident = list(messages)
    spliced = lb.spliceOutOfContextMessages(resident, in_context_after, 0)
    spliced_ids = [m["id"] for m in spliced]
    check("splice_evicts_only_out_of_context",
          set(spliced_ids).isdisjoint(set(in_context_after))
          and resident[-1]["id"] == "m8",
          "spliced=%s" % spliced_ids)
    check("resident_covers_in_context",
          lb.residentCoversIds(compacted, in_context_after))
    check("summary_message_is_new_in_context",
          compacted[0]["id"] == SUMMARY_MESSAGE_ID
          and SUMMARY_MESSAGE_ID not in in_context_before)
    check("removeFromInContext_removes_exactly_one",
          lb.removeFromInContext(in_context_after + ["m-x"], "m-x")
          == in_context_after)

    mutated = dict(messages[-1])
    mutated["content"] = [{"type": "text", "text": "different"}]
    check("snapshot_identity_detects_mutation",
          lb.localMessagesHaveSameSnapshot(messages[-1], messages[-1])
          and not lb.localMessagesHaveSameSnapshot(messages[-1], mutated))

    failures = [c["name"] for c in checks if not c["ok"]]
    return {
        "ok": not failures,
        "checks": checks,
        "failures": failures,
        "log": log,
        "in_context_before": in_context_before,
        "in_context_after": in_context_after,
        "evicted_ids": evicted_ids,
    }


def main(argv=None) -> int:
    tamper = 0
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        tamper = int(argv[0])
    result = run_c13_seams_demo(tamper_delta=tamper)
    for line in result["log"]:
        print(line)
    if result["ok"]:
        print("C13_SEAMS_DEMO: PASS")
        return 0
    print("C13_SEAMS_DEMO: FAIL %s" % json.dumps(result["failures"]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
