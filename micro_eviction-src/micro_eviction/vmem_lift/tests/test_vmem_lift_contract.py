"""Contract tests for the exported vmem_lift policy (polyguard W2 anchor).

Real tests, not placeholders: exercise the module's own validators and the
sealed E-A8/E-A9 exit-code contract (PASS=0 / FAIL!=0) through the public
surface, mirroring the sealed fixtures in tests/vmem_lift/.
"""
import os
import sys

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
)

from micro_eviction.vmem_lift import policy  # noqa: E402


def _e_a9_pass_package():
    return {
        "query_time": 5.0,
        "residuals": [
            {
                "id": "r1",
                "trusted": True,
                "lifecycle": {
                    "decay": {"strength": 0.8, "eps_prune": 0.1},
                    "interval": None,
                    "stale_flag": False,
                },
            },
            {
                "id": "r2",
                "trusted": True,
                "lifecycle": {
                    "decay": None,
                    "interval": {"t_start": 0.0, "t_end": 10.0},
                    "stale_flag": False,
                },
            },
        ],
    }


def _e_a8_pass_package():
    return {
        "boundary_based_eviction": True,
        "boundaries": [
            {
                "id": "b1",
                "relied_upon_for_eviction": True,
                "signal_name": "recency_days",
                "threshold_value": 31.0,
                "candidate_id": "cand-9",
                "confirmation_record": {
                    "reviewer_id": "rev-1",
                    "date": "2026-11-03",
                    "candidate_ref": "cand-9",
                },
            }
        ],
    }


def test_policy_surface_exists():
    for name in (
        "retention_score",
        "compute_stale_flag",
        "validate_e_a9",
        "e_a9_exit_code",
        "write_residual",
        "validate_e_a8",
        "e_a8_exit_code",
    ):
        assert hasattr(policy, name), f"missing policy surface: {name}"


def test_e_a9_pass_is_zero():
    assert policy.e_a9_exit_code(_e_a9_pass_package()) == 0


def test_e_a9_bare_residual_is_nonzero():
    bare = {
        "query_time": 5.0,
        "residuals": [
            {"id": "r1", "trusted": True, "lifecycle": {"decay": None,
             "interval": None, "stale_flag": False}}
        ],
    }
    assert policy.e_a9_exit_code(bare) != 0


def test_e_a9_stale_trusted_is_nonzero():
    stale_trusted = {
        "query_time": 20.0,
        "residuals": [
            {"id": "r1", "trusted": True, "lifecycle": {
                "decay": None, "interval": {"t_start": 0.0, "t_end": 10.0},
                "stale_flag": True}}
        ],
    }
    assert policy.e_a9_exit_code(stale_trusted) != 0


def test_e_a8_pass_is_zero():
    assert policy.e_a8_exit_code(_e_a8_pass_package()) == 0


def test_e_a8_confirmation_ref_mismatch_is_nonzero():
    pkg = _e_a8_pass_package()
    pkg["boundaries"][0]["confirmation_record"]["candidate_ref"] = "cand-OTHER"
    assert policy.e_a8_exit_code(pkg) != 0


def test_e_a8_boundary_claim_without_boundaries_is_nonzero():
    pkg = {"boundary_based_eviction": True, "boundaries": []}
    assert policy.e_a8_exit_code(pkg) != 0


def test_retention_score_sane():
    s = policy.retention_score(0.5, 0.5, 0.5, 0.5)
    assert 0.0 <= s <= 1.0
    # component dominance order carried from increment 1
    base = s
    d_imp = policy.retention_score(0.6, 0.5, 0.5, 0.5) - base
    d_uni = policy.retention_score(0.5, 0.5, 0.6, 0.5) - base
    assert d_imp > d_uni
