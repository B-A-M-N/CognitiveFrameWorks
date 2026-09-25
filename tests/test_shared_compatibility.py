import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DP = ROOT.parent / "DigitalPsychology"
sys.path.insert(0, str(DP))
from lib.routing_profiles import BehavioralRoutingProfile
from runtime_contract.routing import applicable_adjustments as cfw_applicable

def test_shared_compatibility_vectors_agree():
    vectors = json.loads((ROOT / "contracts" / "shared-compatibility-vectors.json").read_text())
    profile_doc = vectors["profile"]
    class Profile(BehavioralRoutingProfile):
        pass
    # Build a lightweight validated object through the host API in DP; the
    # vectors use an intentionally minimal profile and test scope matching.
    class Minimal:
        to_dict = lambda self: profile_doc
        sharing_scope = "cross_application"
        subject = profile_doc["subject"]
        context = profile_doc["context"]
        selector_semantics = profile_doc["selector_semantics"]
        profile_hash = profile_doc["profile_hash"]
        routing_adjustments = profile_doc["routing_adjustments"]
    profile = Minimal()
    # Patch validation because the vector is a matching contract, not a deployable artifact.
    import lib.routing_profiles as rp
    original = rp.validate_profile; rp.validate_profile = lambda *args, **kwargs: None
    try:
        for case in vectors["cases"]:
            dp = bool(rp.applicable_adjustments(profile, case["context"]))
            cfw = bool(cfw_applicable(profile_doc, case["context"]))
            assert dp == cfw == case["expected"], (case["id"], dp, cfw)
    finally:
        rp.validate_profile = original
