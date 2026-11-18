"""Plain-English Telegram path: keyword-first routing, tool execution, refusal.

The claims under test are the ones that touch money:

* commands never depend on a model being reachable, and unmatched text falls
  through to the model instead of returning help text;
* a transaction id the model invented, garbled, or aimed at several pending rows
  produces NO write, and the reason comes back worded so the owner hears the truth
  ("already approved") rather than "not found";
* a decision the model did make is reported even when the provider dies before it
  can narrate it, because silence after a real approval is the worst outcome;
* one turn's cost is bounded in both model round-trips and context bytes, and the
  byte ceiling is enforced by withdrawing tools rather than by sending an oversized
  request and hoping.

A scripted router plays the model, so tool execution runs against an in-memory
store with the same seams the real one exposes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import finance_agent as fa
import llm_router as R

TX_A = "a3f2c1d4-1111-4aaa-8bbb-ccccdddd0001"
TX_B = "b7e4aaaa-2222-4bbb-8ccc-eeeeffff0002"
WRITERS = (fa.TOOL_GET_PENDING, fa.TOOL_APPROVE, fa.TOOL_REJECT)

CHAT_ENV = {
    "TG_LLM_ENABLED": "",
    "NVIDIA_API_KEY": "nk",
    "OPENROUTER_API_KEY": "",
    "LLM_ROUTER_ALLOWED_MODELS": "",
}


class FakeStore:
    """In-memory ledger with the handful of FinanceStore seams the chat path uses."""

    def __init__(self) -> None:
        self.rows = {
            TX_A: {"transaction_id": TX_A, "title": "Ahrefs monthly", "vendor": "ahrefs",
                   "amount_minor": 14900, "currency": "USD", "tier": "tier2",
                   "created_at": "2026-11-18 10:00", "approval_status": "pending",
                   "approved_at": None},
            TX_B: {"transaction_id": TX_B, "title": "Upwork freelancer", "vendor": "upwork",
                   "amount_minor": 32000, "currency": "USD", "tier": "tier2",
                   "created_at": "2026-11-18 11:00", "approval_status": "pending",
                   "approved_at": None},
        }
        self.decisions: list[tuple[str, str]] = []
        self.audits: list[tuple[str, str]] = []

    def spend_totals(self, handle: str, currency: str) -> tuple[int, int]:
        return (123400, 999900)

    def is_dry_run(self) -> bool:
        return True

    def log(self, level: str, event: str, message: str, **kw: Any) -> None:
        self.audits.append((event, message))


class ScriptedRouter:
    """Plays the model: pops one scripted completion per request, records requests."""

    LLMError = R.LLMError
    LLMRequestRejected = R.LLMRequestRejected
    LLMNotConfigured = R.LLMNotConfigured
    AllProvidersFailed = R.AllProvidersFailed

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []

    async def get_completion(self, prompt: str, **kw: Any) -> R.Completion:
        self.requests.append({"messages": list(kw.get("messages") or []),
                              "tools": [t["function"]["name"] for t in (kw.get("tools") or [])],
                              "tool_choice": kw.get("tool_choice")})
        if not self.steps:
            return answer("nothing more to say")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def answer(text: str) -> R.Completion:
    return R.Completion(text=text, model="m", provider="fake", tier="fast",
                        input_tokens=10, output_tokens=5, cost_usd=0.0001,
                        attempts=1, finish_reason="stop")


def call(name: str, arguments: dict[str, Any] | None = None, *, raw: str = "",
         parse_error: str = "", call_id: str = "c1") -> R.Completion:
    return R.Completion(text="", model="m", provider="fake", tier="fast",
                        input_tokens=10, output_tokens=5, cost_usd=0.0001, attempts=1,
                        finish_reason="tool_calls",
                        tool_calls=[R.ToolCall(name=name, arguments=arguments or {}, id=call_id,
                                               raw_arguments=raw, parse_error=parse_error)])


def tool_payloads(request: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(m["content"]) for m in request["messages"] if m.get("role") == "tool"]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    for key, value in CHAT_ENV.items():
        monkeypatch.setenv(key, value)
    R.configure()
    fake = FakeStore()

    def pending_rows(_store: Any, limit: int = 10) -> list[dict[str, Any]]:
        keep = ("transaction_id", "title", "vendor", "amount_minor", "currency", "tier",
                "created_at")
        return [{k: r[k] for k in keep if k in r}
                for r in _store.rows.values() if r["approval_status"] == "pending"]

    def apply_decision(_store: Any, tx_id: str, action: str) -> str:
        row = _store.rows.get(tx_id)
        if row is None or row["approval_status"] != "pending":
            return "duplicate"
        row["approval_status"] = ("approved" if action == fa.TG_ACTION_APPROVE
                                  else "rejected")
        row["approved_at"] = "2026-11-18 12:00"
        _store.decisions.append((tx_id, action))
        return "decided"

    def decided_state(_store: Any, tx_id: str) -> str | None:
        row = _store.rows.get(tx_id)
        return row["approval_status"] if row else None

    monkeypatch.setattr(fa, "_pending_rows", pending_rows)
    monkeypatch.setattr(fa, "_apply_tg_decision", apply_decision)
    monkeypatch.setattr(fa, "_fetch_decision_row",
                        lambda _s, tx: dict(_s.rows.get(tx) or {}))
    monkeypatch.setattr(fa, "_decided_row_state", decided_state)
    monkeypatch.setattr(fa, "_ledger_snapshot",
                        lambda _s: "Ledger context:\n- net position: $4,210.00")
    return fake


# --------------------------------------------------------------- keyword priority
def test_commands_answer_without_a_model(store: FakeStore) -> None:
    assert "Sirius Agent online" in fa.handle_telegram_message(store, "/help")
    assert fa.handle_telegram_message(store, "what is the mood today?") == ""


async def test_status_command_reads_the_ledger_not_a_model(
        store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """/status bypasses the model entirely, and a dead ledger is an error reply.

    `build_status_report` deliberately does not swallow a database failure: it runs
    inside `asyncio.to_thread` and any raise becomes a visible error to the owner,
    so a broken query cannot read as 'zero pending, all calm'.
    """
    real = fa.FinanceStore("postgresql://u:p@127.0.0.1:1/ghostx2")
    monkeypatch.setattr(real, "_connect",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    # /status pulls its limits through get_settings(), which reads the lifespan
    # STATE; point it at the same broken store so the test exercises one failure.
    monkeypatch.setitem(fa.STATE, "store", real)
    with pytest.raises(RuntimeError, match="db down"):
        fa.build_status_report(real, fa.FinanceSettings.from_env())
    # And the keyword path routes /status there synchronously: the marker that
    # proves no model was consulted is that this returns ledger text, not "".
    with pytest.raises(RuntimeError, match="db down"):
        fa.handle_telegram_message(real, "/status")


async def test_unmatched_text_reaches_the_model(store: FakeStore,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_llm(_store: Any, text: str) -> str:
        seen.append(text)
        return "the model answered in prose"

    monkeypatch.setattr(fa, "handle_telegram_plain_english", fake_llm)
    reply = await fa.route_telegram_text(store, "reorder the vendor list nicely")
    assert reply.text == "the model answered in prose"
    # Prose the owner copies commands out of must not arrive as HTML.
    assert reply.parse_mode == ""
    assert seen == ["reorder the vendor list nicely"]


async def test_disabled_flag_restores_the_old_canned_reply(
        store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_LLM_ENABLED", "false")

    async def must_not_run(_store: Any, text: str) -> str:
        raise AssertionError("model must not be called when the flag is off")

    monkeypatch.setattr(fa, "handle_telegram_plain_english", must_not_run)
    reply = await fa.route_telegram_text(store, "hello there friend")
    assert "I don't know" in reply.text
    assert "Sirius Agent online" in reply.text
    # This family is markup the bot built itself, so it keeps HTML parsing.
    assert reply.parse_mode == "HTML"


async def test_no_provider_key_means_no_model_call(
        store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")

    async def must_not_run(_store: Any, text: str) -> str:
        raise AssertionError("no key, no call")

    monkeypatch.setattr(fa, "handle_telegram_plain_english", must_not_run)
    assert "I don't know" in (await fa.route_telegram_text(store, "how are things")).text


async def test_failure_inside_the_model_branch_replies_honestly(
        store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    async def raiser(_store: Any, text: str) -> str:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(fa, "handle_telegram_plain_english", raiser)
    reply = await fa.route_telegram_text(store, "anything at all")
    assert "RuntimeError" in reply.text
    assert "Nothing was changed" in reply.text


# ------------------------------------------------------------------ read-only turn
async def test_read_tool_turn_answers_from_real_rows(store: FakeStore) -> None:
    router = ScriptedRouter(
        call(fa.TOOL_GET_PENDING, {"limit": 5}, raw='{"limit":5}', call_id="c1"),
        answer("Two approvals are waiting: Ahrefs at $149.00 and Upwork at $320.00."),
    )
    reply = await fa.run_chat_turn(store, "what is pending right now?", router=router)

    assert "Ahrefs" in reply
    assert "149" in reply
    first = router.requests[0]
    assert first["messages"][0]["role"] == "system"
    assert "Sirius" in first["messages"][0]["content"]
    assert first["tool_choice"] == "auto"
    # Writers stay out of reach on the conversational path by default.
    assert first["tools"] == [fa.TOOL_GET_PENDING]
    payload = tool_payloads(router.requests[1])[0]
    assert payload["pending_count"] == 2
    assert {t["transaction_id"] for t in payload["transactions"]} == {TX_A, TX_B}
    assert payload["transactions"][0]["amount"].startswith("$")


async def test_writer_prompt_advertises_the_writers_it_has(store: FakeStore) -> None:
    router = ScriptedRouter(answer("ok"))
    await fa.run_chat_turn(store, "can you approve things?", router=router,
                           tools=fa.TOOL_SPECS, specs=WRITERS)
    system = router.requests[0]["messages"][0]["content"]
    assert "approve or reject any pending transaction" in system
    assert "cannot approve or reject anything yourself" not in system


async def test_money_words_are_answered_from_injected_context_not_a_tool(
        store: FakeStore) -> None:
    router = ScriptedRouter(answer("Net position is $4,210.00, dry run on."))
    reply = await fa.run_chat_turn(store, "how much have we banked?", router=router)
    assert "4,210" in reply
    assert tool_payloads(router.requests[0]) == []


# --------------------------------------------------------------------- writer turns
async def test_approve_by_uuid_writes_once_and_audits(store: FakeStore) -> None:
    router = ScriptedRouter(call(fa.TOOL_APPROVE, {"transaction_id": TX_A}),
                            answer("Approved the Ahrefs renewal, $149.00."))
    reply = await fa.run_chat_turn(store, "approve the ahrefs one", router=router,
                                   tools=fa.TOOL_SPECS, specs=WRITERS)
    assert store.decisions == [(TX_A, fa.TG_ACTION_APPROVE)]
    assert "Ahrefs" in reply
    assert any(event == "llm.tool_executed" for event, _ in store.audits)


async def test_reject_by_unique_short_prefix(store: FakeStore) -> None:
    router = ScriptedRouter(call(fa.TOOL_REJECT, {"transaction_id": "b7e4aaaa"}),
                            answer("Rejected the Upwork payout."))
    await fa.run_chat_turn(store, "reject b7e4aaaa", router=router,
                           tools=fa.TOOL_SPECS, specs=WRITERS)
    assert store.decisions == [(TX_B, fa.TG_ACTION_REJECT)]


async def test_hallucinated_id_is_refused_and_never_written(store: FakeStore) -> None:
    router = ScriptedRouter(
        call(fa.TOOL_APPROVE, {"transaction_id": "deadbeef-0000-0000-0000-000000000000"}),
        answer("That one is not in the pending queue."))
    reply = await fa.run_chat_turn(store, "approve deadbeef", router=router,
                                   tools=fa.TOOL_SPECS, specs=WRITERS)
    assert store.decisions == []
    assert tool_payloads(router.requests[1])[0]["ok"] is False
    assert any(event == "llm.tool_refused" for event, _ in store.audits)
    assert reply


async def test_id_shaped_like_a_uuid_but_absent_says_not_found(store: FakeStore) -> None:
    router = ScriptedRouter(
        call(fa.TOOL_APPROVE, {"transaction_id": "11111111-2222-3333-4444-555555555555"}),
        answer("Not found."))
    await fa.run_chat_turn(store, "approve it", router=router,
                           tools=fa.TOOL_SPECS, specs=WRITERS)
    error = tool_payloads(router.requests[1])[0]["error"]
    assert "no pending transaction matches" in error


async def test_already_decided_id_reports_its_real_status(store: FakeStore) -> None:
    """A stale id must not read as 'does not exist' to the owner."""
    store.rows[TX_A]["approval_status"] = "approved"
    router = ScriptedRouter(call(fa.TOOL_APPROVE, {"transaction_id": TX_A}), answer("ok"))
    await fa.run_chat_turn(store, "approve it again", router=router,
                           tools=fa.TOOL_SPECS, specs=WRITERS)
    assert store.decisions == []
    assert "already approved" in tool_payloads(router.requests[1])[0]["error"]


async def test_ambiguous_short_prefix_is_refused(store: FakeStore) -> None:
    """Two pending ids sharing the prefix: the owner picks, the model does not guess."""
    store.rows["a3f2ee00-9999-4aaa-8bbb-ccccdddd0009"] = {
        "transaction_id": "a3f2ee00-9999-4aaa-8bbb-ccccdddd0009", "title": "second a3f2",
        "vendor": "v", "amount_minor": 100, "currency": "USD", "tier": "tier2",
        "created_at": "t", "approval_status": "pending", "approved_at": None}
    router = ScriptedRouter(call(fa.TOOL_APPROVE, {"transaction_id": "a3f2"}), answer("?"))
    await fa.run_chat_turn(store, "approve a3f2", router=router,
                           tools=fa.TOOL_SPECS, specs=WRITERS)
    assert store.decisions == []
    assert "several" in tool_payloads(router.requests[1])[0]["error"]


async def test_non_hex_token_is_not_treated_as_an_id(store: FakeStore) -> None:
    router = ScriptedRouter(call(fa.TOOL_APPROVE, {"transaction_id": "zzzz"}), answer("?"))
    await fa.run_chat_turn(store, "approve zzzz", router=router,
                           tools=fa.TOOL_SPECS, specs=WRITERS)
    assert store.decisions == []
    assert "not a transaction id" in tool_payloads(router.requests[1])[0]["error"]


async def test_truncated_tool_arguments_do_not_write(store: FakeStore) -> None:
    """Half-written arguments must not run as a zero-argument approve."""
    router = ScriptedRouter(
        call(fa.TOOL_APPROVE, {}, raw='{"transaction_id": "a3',
             parse_error="arguments are not valid JSON"),
        answer("I could not read that order."))
    await fa.run_chat_turn(store, "hm", router=router, tools=fa.TOOL_SPECS, specs=WRITERS)
    assert store.decisions == []
    payload = tool_payloads(router.requests[1])[0]
    assert payload["ok"] is False
    assert "malformed" in payload["error"]


async def test_unknown_tool_name_is_refused(store: FakeStore) -> None:
    result = fa.run_tool(store, "drop_all_tables", {})
    assert result["ok"] is False
    assert "no tool named" in result["error"]


async def test_tool_exception_becomes_a_result_not_a_crash(
        store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(_store: Any, args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("connection reset")

    monkeypatch.setitem(fa.TOOLS, fa.TOOL_GET_PENDING, explode)
    result = fa.run_tool(store, fa.TOOL_GET_PENDING, {})
    assert result["ok"] is False
    assert "connection reset" in result["error"]


# --------------------------------------------------------------- degradation honesty
async def test_provider_dead_before_any_action_says_books_untouched(store: FakeStore) -> None:
    router = ScriptedRouter(R.AllProvidersFailed("upstream 502"))
    reply = await fa.run_chat_turn(store, "how are we doing", router=router)
    assert "untouched" in reply
    assert "keyword router" in reply


async def test_provider_dead_after_an_action_still_reports_the_action(
        store: FakeStore) -> None:
    router = ScriptedRouter(call(fa.TOOL_APPROVE, {"transaction_id": TX_A}),
                            R.AllProvidersFailed("connection reset"))
    reply = await fa.run_chat_turn(store, f"approve {TX_A}", router=router,
                                   tools=fa.TOOL_SPECS, specs=WRITERS)
    assert store.decisions == [(TX_A, fa.TG_ACTION_APPROVE)]
    assert "approved" in reply
    assert "149" in reply


async def test_summariser_does_not_invent_errors_from_read_results(store: FakeStore) -> None:
    """A read-only payload is data, not a failure: quoting it as an error lies."""
    reply = fa._summarise_executed([{"name": fa.TOOL_GET_PENDING, "ok": True,
                                     "pending_count": 3}])
    assert "None" not in reply
    assert "nothing" in reply.lower()


# ---------------------------------------------------------------------- loop bounds
async def test_step_budget_bounds_model_requests(store: FakeStore) -> None:
    router = ScriptedRouter(*[call(fa.TOOL_GET_PENDING, {}, call_id=f"c{i}")
                              for i in range(10)])
    await fa.run_chat_turn(store, "and again", router=router)
    assert len(router.requests) == fa.MAX_CHAT_TOOL_STEPS + 1


async def test_context_ceiling_withdraws_tools_and_discloses_trimming(
        store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    wide = [{"transaction_id": f"{i:08d}-0000-0000-0000-00000000000{i}", "title": "t" * 80,
             "amount_minor": 100, "currency": "USD", "tier": "tier2", "created_at": "ts"}
            for i in range(24)]
    monkeypatch.setattr(fa, "_pending_rows", lambda _s, limit=10: [dict(r) for r in wide])
    router = ScriptedRouter(*[call(fa.TOOL_GET_PENDING, {}, call_id=f"c{i}")
                              for i in range(8)])
    reply = await fa.run_chat_turn(store, "list them all", router=router,
                                   context="ctx " * 400)
    sizes = [sum(len(str(m.get("content") or "")) for m in r["messages"])
             for r in router.requests]
    assert max(sizes) <= fa.MAX_CHAT_CONTEXT_CHARS, sizes
    # Tools are withdrawn once the ceiling is near, so the last request cannot grow.
    assert router.requests[-1]["tools"] == []
    assert "context limit" in reply
    assert any("trimmed" in json.dumps(p) for p in tool_payloads(router.requests[1]))


async def test_small_results_keep_tools_available(store: FakeStore) -> None:
    router = ScriptedRouter(*[call(fa.TOOL_GET_PENDING, {}, call_id=f"c{i}")
                              for i in range(8)])
    await fa.run_chat_turn(store, "again", router=router, context="ctx " * 400)
    assert all(r["tools"] for r in router.requests[:-1])


# ------------------------------------------------------------------------- replies
def test_model_html_is_passed_through_untouched() -> None:
    """Superseded expectation, kept as a test so the change is on the record.

    An earlier version escaped model prose, which was the right instinct for markup
    the bot emits itself and the wrong one for prose the owner copies commands out
    of. Angle brackets now travel through unchanged.
    """
    reply = fa._sanitise_reply("Net is **$4,210.00** and dry_run is <b>true</b>.")
    assert "&lt;b&gt;" not in reply
    assert "**" not in reply


def capture_sends(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the JSON bodies this module's Telegram calls would POST.

    Swapping `httpx.AsyncClient` wholesale recursed to death: by the time the
    replacement ran, the name it called was its own. Patching a bound factory that
    closes over the pristine class keeps one construction on the real path.
    """
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        fa.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler),
                                 **{k: v for k, v in kw.items() if k != "transport"}))
    return sent


async def test_model_prose_reaches_telegram_as_plain_text(
        store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through the real sender: prose arrives verbatim, unparsed.

    Two failure modes hide in this one path, and the first one hid in the test:
    patching `telegram_reply` lets a send that never happened, or one that dropped
    the parse mode, pass unseen. So this asserts on the payload `tg_send_message`
    builds. If model prose goes out as parse_mode=HTML, Telegram eats "<b>" as a tag
    and refuses the send outright over a stray "<" — a suggested
    "/approve <id>" would be unsendable, which is worse than ugly.
    """
    monkeypatch.setenv("NVIDIA_API_KEY", "nk")
    sent = capture_sends(monkeypatch)

    async def fake_plain_english(_store: Any, _text: str) -> str:
        return fa._sanitise_reply("Send /approve <id> to confirm.")

    monkeypatch.setattr(fa, "handle_telegram_plain_english", fake_plain_english)
    await fa._telegram_phase2_text(store, "t", 42, "please approve the ahrefs one")

    assert sent, "the model branch never reached the sender"
    payload = sent[0]
    assert "parse_mode" not in payload, payload
    assert payload["text"] == "Send /approve <id> to confirm."
    assert "&lt;" not in payload["text"]


async def test_command_replies_still_ask_telegram_to_parse_html(
        store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: the keyword router's markup must keep rendering.

    Turning parsing off everywhere would silently un-bold every status report and
    print literal `<code>` tags at the owner, so the parse mode is chosen per reply.
    """
    sent = capture_sends(monkeypatch)
    await fa._telegram_phase2_text(store, "t", 42, "/help")
    assert sent[0]["parse_mode"] == "HTML"
    assert "<b>" in sent[0]["text"]


def test_long_model_reply_is_cut_on_a_character_boundary() -> None:
    reply = fa._sanitise_reply("😀" * 2000)
    assert reply
    assert len(reply.encode()) <= 3500 + 4


def test_history_trimming_keeps_the_newest_turns() -> None:
    history = [{"role": "user", "content": "a" * 500}, {"role": "assistant", "content": "ok"},
               {"role": "user", "content": "b" * 500}]
    trimmed = fa._trim_history(history, 600)
    assert trimmed
    assert trimmed[0]["content"].startswith("b")
    assert fa._trim_history([], 100) == []


# ------------------------------------------------------------------------- truncation
def test_tool_result_truncation_drops_whole_rows_and_stays_parseable() -> None:
    big = {"pending_count": 40,
           "transactions": [{"transaction_id": f"{i:08d}-x", "amount": "$1.00"}
                            for i in range(40)],
           "note": "n" * 100}
    text = fa._bounded_tool_result(fa.TOOL_GET_PENDING, big, 900)
    parsed = json.loads(text)
    assert len(text) <= 900
    assert parsed["truncated_rows"] > 0
    assert len(parsed["transactions"]) == 40 - parsed["truncated_rows"]
    assert "trimmed" in parsed["note"]


def test_nested_row_lists_truncate_too() -> None:
    payload = {"data": {"results": [{"transaction_id": str(i)} for i in range(200)]}}
    text = fa._bounded_tool_result("x", payload, 600)
    parsed = json.loads(text)
    assert len(text) <= 600
    assert parsed["truncated_rows"] > 0
    assert parsed["data"]["results"], "keep at least the head of the list"


def test_unsheddable_payload_still_returns_valid_json() -> None:
    """With no list to trim, the fallback must not slice JSON into corruption."""
    text = fa._bounded_tool_result("x", {"ok": True, "error": "e" * 5000}, 120)
    json.loads(text)
    assert len(text) <= 120


def test_small_payload_is_returned_untouched() -> None:
    assert json.loads(fa._bounded_tool_result("x", {"ok": True}, 500)) == {"ok": True}
