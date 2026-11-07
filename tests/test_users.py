"""
Tests for multi-brand (users) support. No network, no generation.
Run: python tests/test_users.py
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import users


def test_default_context_is_root():
    ctx = users.get_user("resetworth")
    assert ctx.is_default
    assert ctx.profiles_file.name == "profiles.yaml"
    assert ctx.generator == "resetworth"
    assert "instagram" in ctx.default_networks


def test_unknown_user_falls_back_to_default():
    ctx = users.get_user("ghost_brand_xyz")
    assert ctx.is_default and ctx.name == "resetworth"


def test_create_list_and_scope():
    name = "testbrand_tmp"
    root = users._USERS_DIR / name
    if root.exists():
        shutil.rmtree(root)
    try:
        ctx = users.create_user(name, generator="resetworth",
                                default_networks=["instagram", "tiktok"])
        assert not ctx.is_default
        assert ctx.name == name
        assert ctx.profiles_file == root / "profiles.yaml"
        assert ctx.default_networks == ["instagram", "tiktok"]

        # scaffolding exists
        assert (root / "config.json").exists()
        assert (root / "profiles.yaml").exists()
        assert (root / "state").is_dir()
        assert (root / "output" / "videos").is_dir()

        # profiles.yaml has the providers/platforms shape, scoped to this brand
        import yaml
        prof = yaml.safe_load((root / "profiles.yaml").read_text(encoding="utf-8"))
        assert "ayrshare_main" in prof["providers"]
        assert "instagram" in prof["platforms"] and "tiktok" in prof["platforms"]

        # appears in listing
        assert name in users.list_users()

        # duplicate rejected
        try:
            users.create_user(name)
            assert False, "expected duplicate error"
        except ValueError:
            pass
    finally:
        if root.exists():
            shutil.rmtree(root)


def test_create_rejects_default_name():
    try:
        users.create_user("resetworth")
        assert False
    except ValueError:
        pass


def test_past_subjects_scoped_by_tag(tmp_path=None):
    # Write two fake scripts tagged for different brands into the shared scripts dir,
    # then verify past_subjects only returns the matching brand's.
    sdir = users._SCRIPTS_DIR
    sdir.mkdir(parents=True, exist_ok=True)
    f_a = sdir / "_tmp_userA_test.json"
    f_b = sdir / "_tmp_userB_test.json"
    f_a.write_text(json.dumps({"subject": "brand a topic", "_user": "brandA"}), encoding="utf-8")
    f_b.write_text(json.dumps({"subject": "brand b topic", "_user": "brandB"}), encoding="utf-8")
    try:
        from modules.users import UserContext
        ctx_a = UserContext(name="brandA", is_default=False, root=Path("."),
                            profiles_file=Path("p"),
                            generator="resetworth")
        subs = users.past_subjects(ctx_a)
        assert "brand a topic" in subs
        assert "brand b topic" not in subs
    finally:
        f_a.unlink(missing_ok=True)
        f_b.unlink(missing_ok=True)


def _run_all():
    import inspect
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
        except Exception as e:
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
