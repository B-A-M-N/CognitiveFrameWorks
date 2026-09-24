import hashlib
import json
import tempfile
from pathlib import Path

import pytest


def test_csw_authority_is_shared_and_compare_and_swap(tmp_path):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("cfw_state_runtime", Path(__file__).parents[1] / "scripts" / "runtime.py")
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    CSWStateAuthority = module.CSWStateAuthority

    root = Path("/home/bamn/CognitiveStateWork")
    first = CSWStateAuthority(root, state_path=tmp_path / "subjects.json", evidence_path=tmp_path / "evidence.json")
    second = CSWStateAuthority(root, state_path=tmp_path / "subjects.json", evidence_path=tmp_path / "evidence.json")
    contract_path = root / "gitter" / "transitions.yaml"
    contract_hash = hashlib.sha256(json.dumps(json.loads("{}"), sort_keys=True).encode()).hexdigest()
    import yaml
    contract = yaml.safe_load(contract_path.read_text())
    contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    identity = {"statework_id": "gitter", "subject_ref": "repo:demo"}
    snapshot = first.inspect(statework_id="gitter", subject_ref="repo:demo", contract_hash=contract_hash)
    assert second.inspect(statework_id="gitter", subject_ref="repo:demo", contract_hash=contract_hash)["revision"] == snapshot["revision"]
    evidence = {"ref": "truth-shared", "kind": "repository_truth", "subject_ref": "repo:demo",
                "observed_at": "2026-09-24T00:00:00Z", "issuer": "validator:repository-truth-v1",
                "validator": "repository-truth-v1", "schema": "repository-truth-packet.schema.json"}
    committed = first.commit(statework_id="gitter", subject_ref="repo:demo", contract_hash=contract_hash,
                             expected_revision=snapshot["revision"], output_state="OBSERVED", trigger="observe",
                             evidence_refs=["truth-shared"], evidence_records=[evidence])
    assert committed["revision"] == snapshot["revision"] + 1
    assert second.inspect(statework_id="gitter", subject_ref="repo:demo", contract_hash=contract_hash)["state"] == "OBSERVED"
    with pytest.raises(Exception):
        second.commit(statework_id="gitter", subject_ref="repo:demo", contract_hash=contract_hash,
                      expected_revision=snapshot["revision"], output_state="OBSERVED", trigger="observe",
                      evidence_refs=["truth-shared"], evidence_records=[evidence])
