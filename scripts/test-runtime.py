#!/usr/bin/env python3
"""Regression tests for FrameWorks runtime contracts.

Covers the review's test list for the framework side:
- quick/performance profiles satisfy hard dependencies (requires closure)
- order_after lifecycle: anchor_closeout after fuse/ward/flow/dox_closeout;
  dox_closeout after flow/ward; flow after anchor_open
- StateWork core_requirements merge into the kernel (alias-expanded) without
  changing canonical lifecycle order
- missing required packet routes the producer first; packet_store presence
  skips the producer; optional inputs never recurse
- dependency cycles are detected (PacketDependencyCycle)
- unknown required packet raises PacketDependencyError (never silently
  skipped)
- guard pack schema-validated; semantic hash recomputed; fail-closed scope
- resolve() is a pure library; snapshots write out-of-tree content-addressed
  and never overwrite
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("resolve_runtime", ROOT / "scripts" / "resolve-runtime.py")
resolve = importlib.util.module_from_spec(_spec)
sys.modules["resolve_runtime"] = resolve
_spec.loader.exec_module(resolve)

COW = ROOT.parent / "CognitiveStateWork"


def load_yaml(path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main() -> int:
    failures: list[str] = []

    def check(name, cond, detail=""):
        if cond:
            print(f"  [ok]   {name}")
        else:
            failures.append(name)
            print(f"  [FAIL] {name} {detail}")

    pipeline = load_yaml(ROOT / "shared" / "pipeline.yaml")
    stages = pipeline["stages"]
    aliases = pipeline.get("aliases", {}) or {}
    profiles = pipeline["profiles"]
    stage_by_id = {s["id"]: s for s in stages}

    # hard dependency closure holds for every profile
    for pname, profile in profiles.items():
        active = resolve.requires_closure(resolve.expand(profile, aliases), stage_by_id)
        missing = set()
        for sid in active:
            for req in stage_by_id[sid].get("requires", []):
                if req not in active:
                    missing.add(req)
        check(f"profile {pname} satisfies hard dependencies", not missing,
              f"missing {sorted(missing)}")

    # lifecycle order: anchor_closeout after fuse/ward/flow/dox_closeout
    for pname, profile in profiles.items():
        kernel = resolve.order_kernel(resolve.requires_closure(resolve.expand(profile, aliases), stage_by_id), stage_by_id)
        if "anchor_closeout" in kernel:
            for after in ("anchor_open", "fuse", "ward", "flow", "dox_closeout"):
                if after in kernel:
                    check(f"profile {pname}: anchor_closeout after {after}",
                          kernel.index("anchor_closeout") > kernel.index(after), str(kernel))
        if "dox_closeout" in kernel:
            for after in ("dox_load", "fuse", "ward", "flow"):
                if after in kernel:
                    check(f"profile {pname}: dox_closeout after {after}",
                          kernel.index("dox_closeout") > kernel.index(after), str(kernel))
        if "flow" in kernel and "anchor_open" in kernel:
            check(f"profile {pname}: flow after anchor_open",
                  kernel.index("flow") > kernel.index("anchor_open"), str(kernel))

    # no entropy_delta/intent_weight + banned SISPIS dimensions upstream
    for skill in ["OWL", "ANCHOR", "DOX", "FUSE", "FLOW", "WARD"]:
        for path in (ROOT / skill).rglob("*"):
            if path.is_file() and path.suffix.lower() in {".md", ".yaml", ".yml"}:
                t = path.read_text(encoding="utf-8", errors="replace")
                check(f"no upstream SISPIS math in {skill}",
                      "entropy_delta" not in t and "intent_weight" not in t and
                      not any(d in t for d in resolve.BANNED_SISPIS_DIMENSIONS), path.name)

    # StateWork chain: rebase routes gitter first (producer of truth packet)
    stateworks = resolve.load_stateworks(COW)
    desired, ambiguous = resolve.match_statework(stateworks, ["rebase"])
    check("rebase selects getter deterministically", desired is not None and desired["id"] == "getter", str(desired and desired.get("id")))
    mutation_match, mutation_ambiguous = resolve.match_statework(
        stateworks, ["repository"], phase="mutation")
    observation_match, observation_ambiguous = resolve.match_statework(
        stateworks, ["repository"], phase="observation")
    check("task phase routes repository mutation to getter",
          mutation_match is not None and mutation_match["id"] == "getter" and
          not mutation_ambiguous)
    check("task phase routes repository observation to gitter",
          observation_match is not None and observation_match["id"] == "gitter" and
          not observation_ambiguous)
    packet_reg = resolve.parse_packet_registry(COW)
    chain = resolve.plan_statework_chain(desired, stateworks, packet_reg, packet_store={})
    check("missing truth packet routes gitter producer first",
          [sw["id"] for sw in chain] == ["gitter", "getter"], str([sw["id"] for sw in chain]))
    # only a validated typed packet may skip producer
    valid_fixture = json.loads((COW / "fixtures" / "fixtures-valid.ndjson").read_text().splitlines()[0])
    chain2 = resolve.plan_statework_chain(
        desired, stateworks, packet_reg,
        packet_store={"repository_truth_packet": valid_fixture},
        task_id=valid_fixture["task_id"], subject_ref=valid_fixture["subject_ref"],
        observation_context={"repository_freshness":
                             valid_fixture["payload"]["freshness"]})
    check("present packet skips producer", [sw["id"] for sw in chain2] == ["getter"], str([sw["id"] for sw in chain2]))
    freshness_context = {"repository_freshness": valid_fixture["payload"]["freshness"]}
    fresh_chain = resolve.plan_statework_chain(
        desired, stateworks, packet_reg,
        packet_store={"repository_truth_packet": valid_fixture},
        task_id=valid_fixture["task_id"], subject_ref=valid_fixture["subject_ref"],
        observation_context=freshness_context)
    stale_chain = resolve.plan_statework_chain(
        desired, stateworks, packet_reg,
        packet_store={"repository_truth_packet": valid_fixture},
        task_id=valid_fixture["task_id"], subject_ref=valid_fixture["subject_ref"],
        observation_context={"repository_freshness": dict(
            valid_fixture["payload"]["freshness"],
            working_tree_fingerprint="sha256:changed")})
    check("freshness validator accepts current packet", [sw["id"] for sw in fresh_chain] == ["getter"])
    check("freshness validator rejects stale packet", [sw["id"] for sw in stale_chain] == ["gitter", "getter"])
    subject_chain = resolve.plan_statework_chain(
        desired, stateworks, packet_reg,
        packet_store={"repository_truth_packet": valid_fixture},
        task_id=valid_fixture["task_id"], subject_ref="branch:other",
        observation_context=freshness_context)
    check("subject isolation rejects packet for another branch",
          [sw["id"] for sw in subject_chain] == ["gitter", "getter"])
    cross_chain = resolve.plan_statework_chain(
        desired, stateworks, packet_reg,
        packet_store={"repository_truth_packet": valid_fixture},
        task_id="different-task")
    check("packet for other task rejected",
          "gitter" in [sw["id"] for sw in cross_chain],
          str([sw["id"] for sw in cross_chain]))

    # infrae no longer recurses (optional input)
    desired_i, ambiguous_i = resolve.match_statework(stateworks, ["infrastructure"])
    check("infrae matches", desired_i is not None and desired_i["id"] == "infrae")
    chain_i = resolve.plan_statework_chain(desired_i, stateworks, packet_reg, packet_store={})
    check("infrae no recursion", [sw["id"] for sw in chain_i] == ["infrae"], str([sw["id"] for sw in chain_i]))
    selected_change, _ = resolve.select_statework_runtime(
        desired_i, COW, operation="deploy", trigger="mutation",
        task_shape="implement", domain_tags=["infrastructure"])
    check("machine-readable flow routing selects change",
          selected_change.get("selected_flow", [""])[0].endswith("/change/flow.md"),
          str(selected_change.get("selected_flow")))
    try:
        resolve.select_statework_runtime(desired_i, COW)
        check("ambiguous flow routing fails closed", False, "ambiguous flow accepted")
    except resolve.PacketDependencyError:
        check("ambiguous flow routing fails closed", True)
    infra_request = resolve.TaskRequest(
        task_id="infra-mutation", application_id="cfw-test", subject_ref="cluster:test", shape="implement",
        domain_tags=("infrastructure",), operation="deploy", trigger="mutation",
        observation_context={})
    infra_bundle = resolve.resolve(infra_request)
    check("transition flow requirements activate FUSE/WARD",
          "fuse" in infra_bundle.kernel and "ward" in infra_bundle.kernel and
          any(item.get("selected_flow", [""])[0].endswith("/change/flow.md")
              for item in infra_bundle.stateworks))
    quick_infra = resolve.resolve(resolve.TaskRequest(
        task_id="infra-quick-mutation", application_id="cfw-test", subject_ref="cluster:test",
        phase="domain_state", shape="quick", domain_tags=("infrastructure",),
        operation="deploy", trigger="mutation"))
    check("quick flow requirements are selected before kernel freeze",
          "fuse" in quick_infra.kernel and "ward" in quick_infra.kernel,
          str(quick_infra.kernel))

    # dependency cycle detection
    fake_reg = {"packets": {"p_a": {"producer": "sw_b"}, "p_b": {"producer": "sw_c"}, "p_c": {"producer": "sw_a"}}}
    fakes = [
        {"id": "sw_a", "inputs": {"required": ["p_a"], "optional": []}},
        {"id": "sw_b", "inputs": {"required": ["p_b"], "optional": []}},
        {"id": "sw_c", "inputs": {"required": ["p_c"], "optional": []}},
    ]
    try:
        resolve.plan_statework_chain(fakes[0], fakes, fake_reg, {})
        check("cycle detection", False, "cycle not raised")
    except resolve.PacketDependencyCycle:
        check("cycle detection", True)

    # unknown required packet raises
    try:
        resolve.plan_statework_chain({"id": "sw_x", "inputs": {"required": ["ghost"], "optional": []}},
                                     [{"id": "sw_x", "inputs": {"required": ["ghost"], "optional": []}}],
                                     {"packets": {}}, {}, allow_missing_required=False)
        check("unknown required packet raises", False, "not raised")
    except resolve.PacketDependencyError:
        check("unknown required packet raises", True)

    # StateWork core_requirements merge (getter requires ward)
    merged = resolve.requires_closure(resolve.expand(profiles["implement"], aliases), stage_by_id)
    for sw in chain:
        for req in sw.get("core_requirements", []):
            for concrete in resolve.expand([req], aliases):
                if concrete not in merged:
                    merged.append(concrete)
    kernel = resolve.order_kernel(merged, stage_by_id)
    check("getter core_requirements (ward) merged", "ward" in kernel, str(kernel))
    check("core additions don't reorder base kernel",
          kernel.index("anchor_closeout") > kernel.index("fuse") > kernel.index("anchor_open") > kernel.index("owl"), str(kernel))

    # ambiguous match does not first-match
    fake = [
        {"id": "aa", "name": "Alpha", "version": "1.0.0", "triggers": ["widget state"]},
        {"id": "bb", "name": "Beta", "version": "1.0.0", "triggers": ["widget state"]},
    ]
    hit, amb = resolve.match_statework(fake, ["widget state"])
    check("tied StateWork match reports ambiguity", hit is None and len(amb) == 2, str(amb))

    # guard pack validity + semantic hash stability
    pack = resolve.load_guard_pack()
    check("guard pack status valid", pack.get("guard_pack_status") in ("valid", "framework-kernel-only"), str(pack.get("guard_pack_status")))
    if pack.get("guard_pack_status") == "valid":
        check("semantic hash stable", pack.get("semantic_hash") == pack.get("content_hash"),
              f"{pack.get('semantic_hash')} != {pack.get('content_hash')}")

    # DP/CFW guard compiler conformance uses a JSON-only fixture. This keeps
    # the runtime implementation independent from DP's Python package while
    # proving trigger, nested scope, canary, and enforcement-key behavior.
    conformance = json.loads((ROOT.parent / "DigitalPsychology" / "fixtures" /
                              "guard-conformance.json").read_text(encoding="utf-8"))
    for case in conformance["cases"]:
        actual = resolve.compile_task_guards(
            conformance["guards"], case["domains"], case["shapes"],
            case.get("model"), case.get("harness"), case.get("trigger"),
            case["task_id"], stateworks=case.get("stateworks"))
        check(f"guard conformance: {case['name']}",
              [guard["id"] for guard in actual] == case["expected_active"],
              str([guard["id"] for guard in actual]))

    # fail-closed scope via library resolve
    request = resolve.TaskRequest(task_id="immutable-request", application_id="cfw-test", subject_ref="subject:1",
                                  shape="quick", domain_tags=("read-only",),
                                  observation_context={"trusted": True})
    try:
        request.shape = "implement"
        check("TaskRequest is immutable", False, "mutation succeeded")
    except AttributeError:
        check("TaskRequest is immutable", True)
    b_a = resolve.resolve("t-scope", "implement", ["rebase"], model="model-a",
                          subject_ref="repo:scope")
    b_b = resolve.resolve("t-scope", "implement", ["rebase"], model="model-b",
                          subject_ref="repo:scope")
    guard_ids_a = {g["id"] for g in b_a.guard_pack["guards"]}
    guard_ids_b = {g["id"] for g in b_b.guard_pack["guards"]}
    check("library resolve emits bundle", b_a.bundle_version == "2.0.0")

    # deterministic canary assignment at task composition
    pack2 = resolve.load_guard_pack()
    canary_ids = [g for g in pack2.get("guards", []) if g.get("status") == "canary"]
    if canary_ids:
        r_over = resolve.applicable_guards(pack2, ["implement"], ["implement"],
                                           None, None, None, task_id="task-canary")
        r_other = resolve.applicable_guards(pack2, ["implement"], ["implement"],
                                            None, None, None, task_id="task-not-canary")
        check("canary guard assigned deterministically by task",
              any(g["id"] in {x["id"] for x in r_over} for g in canary_ids),
              f"{[g['id'] for g in canary_ids]} vs {[x['id'] for x in r_over]}")
        check("canary guard excluded for non-assigned task",
              not any(g["id"] in {x["id"] for x in r_other} for g in canary_ids),
              f"{[x['id'] for x in r_other]}")

    # resolve() writes nothing into the source tree: it must never create
    # or touch CognitiveFrameWorks/runtime-bundle.json
    src_bundle = ROOT / "runtime-bundle.json"
    existed = src_bundle.exists()
    b = resolve.resolve("t-lib", "implement", ["rebase"], subject_ref="repo:lib")
    check("resolve() is pure (no source write)", src_bundle.exists() == existed,
          f"existed={existed} after={src_bundle.exists()}")

    quick = resolve.resolve("same-policy", "quick")
    implement = resolve.resolve("same-policy", "implement")
    check("quick and implement policies differ",
          quick.pinned["policy_hash"] != implement.pinned["policy_hash"])
    check("same policy across task ids hashes equally",
          quick.pinned["policy_hash"] == resolve.resolve("other-task", "quick").pinned["policy_hash"])

    pack3 = resolve.load_guard_pack()
    if any(g.get("status") == "canary" for g in pack3.get("guards", [])):
        assigned = resolve.resolve("task-canary", "implement", ["implement", "debug"])
        unassigned = resolve.resolve("task-not-canary", "implement", ["implement", "debug"])
        check("canary selection changes policy hash",
              assigned.pinned["policy_hash"] != unassigned.pinned["policy_hash"])
        check("guard identity remains versioned",
              all("@" in g["key"] for g in assigned.guard_pack["guards"]),
              str([g["key"] for g in assigned.guard_pack["guards"]]))

    scoped_guard = {
        "id": "G-scope", "key": "G-scope@1", "family": "G-scope", "version": "1",
        "status": "active", "rule": "debug-only", "target_behavior": "debug",
        "scope": {"task_shapes": ["debug"]}, "priority": "P1", "evaluator": "x",
    }
    matched = resolve.applicable_guards({"guards": [scoped_guard]}, [], ["debug"],
                                        None, None, None, "task")
    excluded = resolve.applicable_guards({"guards": [scoped_guard]}, [], ["quick"],
                                         None, None, None, "task")
    check("debug-scoped guard excluded from quick", matched and not excluded)

    trigger_guard = dict(scoped_guard, trigger="contradiction")
    trigger_matched = resolve.applicable_guards({"guards": [trigger_guard]}, [], ["debug"],
                                                None, None, "contradiction", "task")
    trigger_excluded = resolve.applicable_guards({"guards": [trigger_guard]}, [], ["debug"],
                                                 None, None, "different", "task")
    check("trigger mismatch excluded", trigger_matched and not trigger_excluded)

    fake_stateworks = [{"id": "quick", "version": "1", "triggers": ["quick"],
                        "inputs": {"required": ["ghost"], "optional": []}}]
    fake_registry = {"packets": {}}
    orig_stateworks, orig_registry = resolve.load_stateworks, resolve.parse_packet_registry
    resolve.load_stateworks = lambda cow_root=COW: list(fake_stateworks)
    resolve.parse_packet_registry = lambda cow_root=COW: dict(fake_registry)
    try:
        resolve.resolve("ghost-input", "quick", ["quick"], packet_store={})
        check("resolver unknown required packet fails closed", False, "resolved anyway")
    except resolve.PacketDependencyError:
        check("resolver unknown required packet fails closed", True)

    resolve.load_stateworks, resolve.parse_packet_registry = orig_stateworks, orig_registry

    runtime_spec = importlib.util.spec_from_file_location("runtime", ROOT / "scripts" / "runtime.py")
    runtime = importlib.util.module_from_spec(runtime_spec)
    sys.modules["runtime"] = runtime
    runtime_spec.loader.exec_module(runtime)
    # Keep this regression run isolated from snapshots created by another
    # process or an earlier test run.
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="cfw-runtime-test-")
    try:
        telemetry_traversal = runtime.HostTelemetry("x/../../outside")
        escaped = telemetry_traversal.store.parent != (
            resolve.runtime_state_dir() / "telemetry")
        check("derived telemetry path contained by design", not escaped,
              str(telemetry_traversal.store))
    except (ValueError, PermissionError):
        check("derived telemetry path traversal rejected", True)
    telemetry_dir = ROOT / "telemetry-test"
    telemetry_dir.mkdir(mode=0o700, exist_ok=True)
    telemetry = runtime.HostTelemetry("schema-test", store=telemetry_dir / "events.ndjson")
    schema_bundle = resolve.resolve("schema-pin", "quick")
    check("telemetry pins exact DP schema hash/version",
          telemetry.schema_hash == schema_bundle.policy.behavior_event_schema_hash and
          telemetry.schema_version == "2.2.0")
    try:
        telemetry.emit({"category": "NOT_A_CATEGORY", "payload": {"secret": "leak"}})
        check("host telemetry schema bypass rejected", False, "invalid event persisted")
    except ValueError:
        check("host telemetry schema bypass rejected", True)
    try:
        telemetry.emit({"category": "observation", "uncertainty": 2.0})
        check("host telemetry exact schema constraints enforced", False, "invalid uncertainty accepted")
    except ValueError:
        check("host telemetry exact schema constraints enforced", True)
    try:
        runtime.HostTelemetry("schema-mismatch", expected_schema_hash="sha256:wrong")
        check("telemetry rejects mismatched pinned schema", False, "mismatched schema accepted")
    except ValueError:
        check("telemetry rejects mismatched pinned schema", True)
    b_host = resolve.resolve("host-gates", "quick")
    session = runtime.start(b_host, telemetry_disabled=True)
    resume_nonce = "resume-regression"
    resumable = runtime.start(b_host, telemetry_disabled=True,
                              session_nonce=resume_nonce)
    resumed_evidence = resumable._host_ingress().record_observation(
        "resume_probe", subject_ref=b_host.policy.subject_ref)
    resumed = runtime.resume_task(b_host, resume_nonce, telemetry_disabled=True)
    check("durable session journal resumes host evidence",
          resumed.ledger.get(resumed_evidence) is not None)
    check("durable session journal preserves analytics session identity",
          resumed.session_id == resumable.session_id and
          resumed.agent_instance_id == resumable.agent_instance_id)
    check("durable session journal has SQLite authority log",
          resumed.journal_db_path is not None and resumed.journal_db_path.exists())
    lifecycle_bundle = resolve.resolve("lifecycle-host", "implement")
    lifecycle_session = runtime.start(lifecycle_bundle, telemetry_disabled=True)
    manifests = list((resolve.runtime_state_dir() / "telemetry").glob("*.manifest.json"))
    check("runtime start writes policy context manifest",
          any(json.loads(path.read_text(encoding="utf-8")).get("task_id") ==
              lifecycle_bundle.task_id for path in manifests))
    check("lifecycle controller activates declared preflight",
          "owl" in lifecycle_session.active_lifecycle)
    initial_lifecycle_context = runtime.context_for(lifecycle_session)
    check("lifecycle keeps action-only stages out of initial model context",
          "fuse" not in lifecycle_session.active_lifecycle and
          "ward" not in lifecycle_session.active_lifecycle and
          "===== FUSE =====" not in initial_lifecycle_context and
          "===== WARD =====" not in initial_lifecycle_context)
    planning_context = lifecycle_session.before_tool_planning({
        "tool_binding_id": "write", "arguments": {"target": "task-output"}})
    check("lifecycle activates planning-time FUSE/WARD before model action",
          "fuse" in lifecycle_session.active_lifecycle and
          "ward" in lifecycle_session.active_lifecycle and
          "===== FUSE =====" in planning_context and
          "===== WARD =====" in planning_context)
    lifecycle_session.before_action({"tool_type": "read"})
    check("lifecycle controller activates per-action fuse",
          "fuse" in lifecycle_session.active_lifecycle)
    lifecycle_session.on_artifact(state_changed=True)
    lifecycle_session.preview_output()
    check("preview output does not activate completion stage",
          "sispis" not in lifecycle_session.active_lifecycle)
    try:
        lifecycle_session.host
        check("agent session does not expose host ingress", False,
              "public host property returned a privileged channel")
    except PermissionError:
        check("agent session does not expose host ingress", True)
    try:
        lifecycle_session.host_context = runtime.HostExecutionContext(principal="operator")
        check("agent cannot replace host authority context", False,
              "host context reassignment accepted")
    except AttributeError:
        check("agent cannot replace host authority context", True)
    try:
        lifecycle_session.ledger = runtime.EvidenceLedger("forged")
        check("agent cannot replace trusted evidence ledger", False,
              "ledger reassignment accepted")
    except AttributeError:
        check("agent cannot replace trusted evidence ledger", True)
    try:
        runtime.start(
            resolve.resolve("action-registry-pinned", "quick"),
            telemetry_disabled=True,
            action_registry={
                "delete": runtime.ActionDescriptor("delete", mutability="read")})
        check("start cannot redefine built-in action authority", False,
              "post-resolution registry override accepted")
    except (ValueError, resolve.SnapshotIntegrityError):
        check("start cannot redefine built-in action authority", True)
    try:
        lifecycle_session.final_output()
        check("final output cannot bypass completion permit", False,
              "public final_output accepted without a permit")
    except PermissionError:
        check("final output cannot bypass completion permit", True)

    segment_bundle = resolve.resolve(
        resolve.TaskRequest(task_id="segment-context", application_id="cfw-test", subject_ref="repo:segment",
                            shape="implement", domain_tags=("repository",),
                            phase="mutation", operation="merge", trigger="mutation",
                            observation_context={}),
        allow_missing_required=True)
    segment_session = runtime.start(segment_bundle, telemetry_disabled=True)
    first_context = runtime.context_for(segment_session)
    check("execution segment exposes only first StateWork",
          "GITTER" in first_context and "GETTER" not in first_context and
          "ACTIVE HANDOFF" in first_context)
    if len(segment_session.execution_segments) > 1:
        try:
            segment_session.advance_segment()
            segment_advance_blocked = False
        except PermissionError:
            segment_advance_blocked = True
        check("execution segment cannot advance without host-attested output",
              segment_advance_blocked)
    second_context = runtime.context_for(segment_session)
    check("execution segment handoff is sequential",
          len(segment_session.execution_segments) < 2 or
          ("GITTER" in second_context and "GETTER" not in second_context))

    b_transition = resolve.resolve(
        resolve.TaskRequest(task_id="transition-runtime", application_id="cfw-test", subject_ref="ui:tuid",
                            shape="implement", domain_tags=("terminal interface",)),
        requested_flows={"tuid": "debug"})
    transition_session = runtime.start(b_transition, telemetry_disabled=True)
    rendered_evidence = transition_session._host_ingress().record_observation(
        "rendered_observation", subject_ref="ui:tuid")
    check("legal StateWork transition allowed",
          transition_session.record_state_transition(
              "tuid", "ui:tuid", "OBSERVED", trigger="discover",
              evidence_refs=[rendered_evidence]) == runtime.GateDecision.ALLOW)
    check("state change is an immutable segment, not policy mutation",
          len(transition_session.state_segments) == 1 and
          transition_session.state_segments[0].output_state == "OBSERVED" and
          transition_session.bundle.policy.stateworks[0].transitions_contract)
    check("illegal StateWork transition blocked",
          transition_session.record_state_transition(
              "tuid", "ui:tuid", "VALIDATED", trigger="validate",
              evidence_refs=["fake"]) == runtime.GateDecision.BLOCK)
    check("unknown StateWork transition fails closed",
          transition_session.record_state_transition(
              "not-active", "ui:tuid", "OBSERVED", trigger="discover",
              evidence_refs=[rendered_evidence]) == runtime.GateDecision.BLOCK)
    state_steps = [
        ("MODELED", "model", "state_model"),
        ("DESIGNED", "design", "interaction_model"),
        ("IMPLEMENTED", "implement", "implementation"),
        ("EXERCISABLE", "interact", "interaction_evidence"),
        ("VALIDATED", "validate", "validation_evidence"),
    ]
    for output_state, trigger, observation_type in state_steps:
        evidence = transition_session._host_ingress().record_observation(
            observation_type, subject_ref="ui:tuid")
        check(f"StateWork transition reaches {output_state}",
              transition_session.record_state_transition(
                  "tuid", "ui:tuid", output_state, trigger=trigger,
                  evidence_refs=[evidence]) == runtime.GateDecision.ALLOW)
    handoff_evidence = transition_session._host_ingress().record_observation(
        "tui_handoff_observed", subject_ref="ui:tuid")
    handoff = {
        "packet_id": "tui-runtime-001", "packet_type": "tui_validation_packet",
        "schema_version": "1.1.0", "producer": "tuid",
        "task_id": b_transition.task_id, "subject_ref": "ui:tuid",
        "observed_at": "2026-09-15T12:00:00Z", "input_state": "EXERCISABLE",
        "output_state": "VALIDATED", "evidence_refs": [handoff_evidence], "unknowns": [],
        "blockers": [], "authority": "task-owner", "invalidated_by": None,
        "recommended_next": "model",
        "payload": {"component": "terminal", "checks": [{"name": "render", "passed": True}],
                    "result": "pass", "freshness_fingerprint": "tui-fingerprint",
                    "terminals": ["xterm"], "notes": None},
    }
    handoff_id = transition_session._host_ingress().publish_handoff(
        handoff, observation_context={"validation_fingerprint": "tui-fingerprint"})
    check("typed handoff publishes through PacketStore",
          transition_session.consume_handoff(
              handoff_id, expected_packet_type="tui_validation_packet",
              expected_subject_ref="ui:tuid",
              observation_context={"validation_fingerprint": "tui-fingerprint"})["packet_id"] == handoff_id)
    try:
        transition_session.record_handoff("tuid", "repository_truth_packet")
        check("metadata-only handoff API rejected", False, "legacy handoff accepted")
    except ValueError:
        check("metadata-only handoff API rejected", True)
    check("asserted wrong origin blocked",
          transition_session._host_ingress().record_observation("state_model", "ui:tuid") and
          transition_session.record_state_transition(
              "tuid", "ui:tuid", "MODELED", input_state="UNKNOWN", trigger="model",
              evidence_refs=[transition_session._host_ingress().record_observation("state_model", "ui:tuid")]) ==
          runtime.GateDecision.REQUIRE_REINSPECTION)
    contradiction_session = runtime.start(resolve.resolve("contradiction", "quick"), telemetry_disabled=True)
    contradiction_id = contradiction_session._host_ingress().record_operator_observation(
        "ui:tuid", "operator says UI is stale")
    check("completion blocked by unresolved contradiction",
          contradiction_session.record_completion_claim("ui:tuid", ["evidence"]) ==
          runtime.GateDecision.REQUIRE_REINSPECTION)
    fresh = contradiction_session._host_ingress().record_tool_result(
        "test", "pass", "ui:tuid", invocation_id="inv-fresh")
    check("contradiction resolution requires fresh ledger evidence",
          contradiction_session.resolve_contradiction(contradiction_id, [fresh]) ==
          runtime.GateDecision.ALLOW)
    contradiction_session._host_ingress().record_tool_result(
        "test", "pass", "ui:tuid", invocation_id="inv-boundary")
    boundary_claim = runtime.CompletionClaim.create(
        claim_id="claim-boundary", task_id=contradiction_session.bundle.task_id,
        subject_ref="ui:tuid", boundary_type="completion", boundary_target="task",
        expected_validator="completion-boundary-v1", invocation_id="inv-boundary")
    boundary = contradiction_session._host_ingress().record_validator_result(
        "completion-boundary-v1", "inv-boundary", {"status": "pass"}, "ui:tuid",
        claim_digest=boundary_claim.claim_digest)
    check("completion requires real boundary evidence",
          contradiction_session.record_completion_claim("ui:tuid", [boundary],
              claim=boundary_claim) ==
          runtime.GateDecision.ALLOW)
    non_boundary = contradiction_session._host_ingress().record_tool_result(
        "test", "pass", "ui:tuid", invocation_id="inv-non-boundary")
    check("completion rejects non-boundary tool evidence",
          contradiction_session.record_completion_claim("ui:tuid", [non_boundary]) ==
          runtime.GateDecision.REQUIRE_REINSPECTION)
    check("invented completion evidence is rejected",
          contradiction_session.record_completion_claim("tuid", ["invented-id"]) ==
          runtime.GateDecision.REQUIRE_REINSPECTION)
    try:
        contradiction_session.record_evidence("boundary_evidence", "ui:tuid")
        check("agent cannot mint boundary evidence", False, "direct evidence accepted")
    except PermissionError:
        check("agent cannot mint boundary evidence", True)
    try:
        contradiction_session.ledger.record_attestation(None)
        check("agent cannot write directly to the evidence ledger", False,
              "ledger accepted a caller attestation")
    except PermissionError:
        check("agent cannot write directly to the evidence ledger", True)
    try:
        contradiction_session.record_operator_contradiction("ui:tuid", "forged operator")
        check("agent cannot mint operator contradiction", False, "direct operator evidence accepted")
    except PermissionError:
        check("agent cannot mint operator contradiction", True)
    try:
        contradiction_session.host_record_tool_result("test", "pass", "ui:tuid")
        check("agent cannot invoke host evidence channel", False,
              "legacy host method accepted a caller assertion")
    except PermissionError:
        check("agent cannot invoke host evidence channel", True)
    try:
        contradiction_session._host_ingress().record_observation("fresh_reinspection", "ui:tuid")
        check("generic observation cannot mint fresh contradiction evidence", False,
              "generic observation accepted as reinspection")
    except ValueError:
        check("generic observation cannot mint fresh contradiction evidence", True)

    b_sispis = resolve.resolve("sispis-runtime", "quick")
    sispis_session = runtime.start(b_sispis, telemetry_disabled=True)
    valid_signal = {"schema_version": "1.1.0", "signal_id": "sig-1", "cause_id": "cause-1",
                    "source": "owl", "signal_type": "owl.approach_failed", "severity": "high",
                    "scope": "task", "evidence_refs": ["evt-1"], "required_action": "recover"}
    sispis_session.emit_signal(valid_signal)
    result = sispis_session.preview_output()
    check("runtime SISPIS calibrates final output",
          result["sispis"]["entropy"] == 2.0 and result["sispis"]["minimum_mode"] == "explanation",
          str(result.get("sispis")))
    try:
        sispis_session.emit_signal(dict(valid_signal, signal_id="sig-2",
                                        signal_type="evil.unregistered"))
        check("runtime rejects unknown SISPIS signal", False, "unknown signal accepted")
    except ValueError:
        check("runtime rejects unknown SISPIS signal", True)
    many = [dict(valid_signal, signal_id=f"s-{i}", cause_id=f"c-{i}") for i in range(100)]
    many_result = runtime._sispis_calibration().calibrate(many)
    check("SISPIS entropy remains bounded", many_result["entropy"] <= 10,
          str(many_result["entropy"]))
    check("structural gate allows safe action",
          session.before_action({"tool_type": "read"}) == runtime.GateDecision.ALLOW)
    check("agent-supplied WARD risk flags are not authority",
          session.before_action({"tool_type": "read", "destructive": True,
                                 "outside_scope": True}) == runtime.GateDecision.ALLOW)
    check("structural gate requires destructive confirmation",
          session.before_action({"tool_type": "delete", "destructive": True}) ==
          runtime.GateDecision.REQUIRE_CONFIRMATION)
    check("structural gate blocks out-of-scope action",
          session.before_action({"tool_type": "deploy", "outside_scope": True}) ==
          runtime.GateDecision.BLOCK)
    configured_state_home = Path(os.environ.get(
        "XDG_STATE_HOME", Path.home() / ".local" / "state"))
    check("structural gate protects trusted control-plane paths",
          session.before_action({"tool_type": "write", "arguments": {
              "target": str(configured_state_home /
                             "digitalpsychology" / "compiled-guard-pack.json")}}) ==
          runtime.GateDecision.BLOCK)
    check("model confirmation and authority fields are ignored",
          session.before_action({"tool_type": "delete", "confirmed": True,
                                 "host_authority": "operator"}) ==
          runtime.GateDecision.REQUIRE_CONFIRMATION)
    delete_request = runtime.AgentActionRequest(
        "delete", {"target": "branch:feature"}, "branch:feature")
    grant = runtime.ConfirmationGrant.create(
        grant_id="grant-1", actor_id="operator", action_digest=delete_request.digest,
        scope="task", issued_at="2026-09-15T00:00:00Z",
        expires_at="2099-01-01T00:00:00Z", task_id=b_host.task_id,
        policy_hash=b_host.pinned["policy_hash"], session_nonce="grant-session")
    authorized = runtime.start(
        b_host, telemetry_disabled=True,
        host_context=runtime.HostExecutionContext(
            principal="operator", task_scope="task", confirmation_grants=(grant,),
            task_id=b_host.task_id, policy_hash=b_host.pinned["policy_hash"],
            session_nonce="grant-session"))
    check("host confirmation grant binds exact normalized action",
          authorized.before_action(delete_request) == runtime.GateDecision.ALLOW)
    check("confirmation grant is single-use",
          authorized.before_action(delete_request) == runtime.GateDecision.REQUIRE_CONFIRMATION)
    altered = runtime.AgentActionRequest(
        "delete", {"target": "branch:other"}, "branch:other")
    check("confirmation grant does not transfer to another target",
          authorized.before_action(altered) == runtime.GateDecision.REQUIRE_CONFIRMATION)
    guard_base = {"id": "G-x", "key": "G-x@1", "family": "G-x", "version": "1",
                  "status": "active", "rule": "r", "target_behavior": "r",
                  "priority": "P1", "estimated_tokens": 20}
    active = resolve.compile_task_guards([guard_base], [], [], None, None, None, "t")
    check("guard compiler retains valid guard", len(active) == 1)
    try:
        conflicting = [guard_base, dict(guard_base, id="G-y", key="G-y@1", family="G-y", conflicts_with=["G-x"])]
        resolve.compile_task_guards(conflicting, [], [], None, None, None, "t")
        check("guard compiler rejects conflicts", False, "conflict compiled")
    except resolve.GuardCompilationError:
        check("guard compiler rejects conflicts", True)
    try:
        unknown_ref = [dict(guard_base, supersedes=["MISSING"])]
        resolve.compile_task_guards(unknown_ref, [], [], None, None, None, "t")
        check("guard compiler rejects unknown supersede refs", False, "unknown reference compiled")
    except resolve.GuardCompilationError:
        check("guard compiler rejects unknown supersede refs", True)
    budget_guards = [
        dict(guard_base, id="Kernel", key="Kernel@1", family="Kernel", priority="P0"),
        dict(guard_base, id="Big", key="Big@1", family="Big", estimated_tokens=500),
        dict(guard_base, id="Small", key="Small@1", family="Small", estimated_tokens=20),
    ]
    budgeted = resolve.compile_task_guards(budget_guards, [], [], None, None, None, "t", token_budget=100)
    check("guard compiler enforces token budget",
          "Small" in {g["id"] for g in budgeted} and "Big" not in {g["id"] for g in budgeted},
          str([g["id"] for g in budgeted]))

    # snapshot writes out-of-tree and never overwrites
    with tempfile.TemporaryDirectory() as tmp:
        import resolve_runtime as _rr  # noqa
        _spec2 = importlib.util.spec_from_file_location("rr2", ROOT / "scripts" / "resolve-runtime.py")
        rr2 = importlib.util.module_from_spec(_spec2)
        sys.modules["rr2"] = rr2
        _spec2.loader.exec_module(rr2)
        orig = rr2.runtime_state_dir
        rr2.runtime_state_dir = lambda: Path(tmp)
        b2 = rr2.resolve("t-snap", "implement", ["rebase"], subject_ref="repo:snap")
        p1 = rr2.write_snapshot(b2)
        p2 = rr2.write_snapshot(b2)
        check("snapshot out-of-tree content-addressed", p1 == p2 and p1.exists())
        check("snapshot 0600 perms", (p1.stat().st_mode & 0o777) == 0o600)
        b2_again = rr2.resolve("t-snap", "implement", ["rebase"], subject_ref="repo:snap")
        check("volatile frozen_at does not fork semantic policy artifact",
              rr2.write_snapshot(b2_again) == p1)

        try:
            b2.profile = "quick"
            check("runtime bundle rejects policy mutation", False, "mutation succeeded")
        except AttributeError:
            check("runtime bundle rejects policy mutation", True)

        stale = resolve.RuntimeBundle(
            bundle_version=b2.bundle_version, task_id=b2.task_id, profile="quick",
            kernel=("sispis",), kernel_instructions=b2.kernel_instructions,
            stateworks=b2.stateworks, handoff_plan=b2.handoff_plan,
            guard_pack=b2.guard_pack, pinned=b2.pinned, policy=b2.policy,
            capsule_hashes=b2.capsule_hashes, injected_capsules=b2.injected_capsules,
            instruction_words=b2.instruction_words,
        )
        rr2.write_snapshot(stale, task_id="stale-task")
        try:
            rr2.write_snapshot(b2, task_id="stale-task")
            check("existing snapshot wrong contents rejected", False, "stale snapshot accepted")
        except rr2.SnapshotIntegrityError:
            check("existing snapshot wrong contents rejected", True)

    print(f"\n{len([1]) and 'pass'}: ", end="")
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all regression tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
