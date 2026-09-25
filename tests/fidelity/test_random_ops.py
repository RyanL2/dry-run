from __future__ import annotations

import shlex
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import TEST_ROOT, force_rmtree
from tests.fidelity.harness import assert_same, real, shadow_commit, twin

pytestmark = [pytest.mark.sandbox, pytest.mark.slow]

NAMES = ["a", "b", "d/c", "d/e/f", "g h", "d"]
CONTENT = st.text(alphabet="xyz\n", min_size=0, max_size=20)


def render(op) -> str:
    kind, x, y = op
    q, r = shlex.quote(x), shlex.quote(y)
    return {
        "write": f"mkdir -p \"$(dirname {q})\" && printf %s {r} > {q}",
        "append": f"printf %s {r} >> {q}",
        "rm": f"rm -f {q}",
        "rmrf": f"rm -rf {q}",
        "mkdir": f"mkdir -p {q}",
        "symlink": f"mkdir -p \"$(dirname {q})\" && ln -sfn {r} {q}",
        "chmod": f"chmod {'755' if len(y) % 2 else '600'} {q}",
        "mv": f"mv -f {q} {shlex.quote(y if y in NAMES else 'moved')}",
        "truncate": f": > {q}",
    }[kind]


OPS = st.tuples(st.sampled_from(["write", "append", "rm", "rmrf", "mkdir", "symlink", "chmod", "mv", "truncate"]),
                st.sampled_from(NAMES), st.one_of(st.sampled_from(NAMES), CONTENT))


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(OPS, min_size=1, max_size=8))
def test_random_op_sequences(ops):
    scratch = TEST_ROOT / f"fuzz-{uuid.uuid4().hex[:10]}"
    scratch.mkdir(parents=True)
    try:
        a, b = twin(scratch)
        script = "set +e\n" + "\n".join(render(o) for o in ops) + "\ntrue\n"
        real(a, script)
        cs, touched = shadow_commit(b, script, scratch / "state")
        if not cs.committable:
            return  # refused effects are never committed; covered by test_hardlink_is_refused_not_committed
        assert_same(a, b, touched)
    finally:
        force_rmtree(scratch)
