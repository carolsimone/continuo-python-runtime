"""A fake warehouse adapter and a runner for the warehouse-read validation ops' unit tests.

The ops ``dependents_check`` and ``probe_views`` each take an ``env`` and a
``discover`` callable, print one result block and return an exit code;
:func:`run_op` drives one of them against :func:`adapter_class` and returns what
it printed.
"""
import contextlib
import io
import json
from types import SimpleNamespace

from continuo_engine_contract import result
from continuo_engine_contract.port import DependentsIntrospection

UNSET = object()


def adapter_class(drop_semantics, *, listing=None, capability=True, connect_error=None,
                   listing_error=None, broken_views=(), required_env=()):
    """A stand-in for the discovered adapter class; ``cls.state`` records what ran.

    ``drop_semantics=UNSET`` declares none. Without ``capability`` the class does
    not subclass ``DependentsIntrospection``.
    """
    state = SimpleNamespace(from_env_calls=0, listing_calls=[], fetched=[], closed=0)

    class _Adapter(*((DependentsIntrospection,) if capability else ())):
        @classmethod
        def required_env(cls):
            return list(required_env)

        @classmethod
        def from_env(cls):
            state.from_env_calls += 1
            if connect_error is not None:
                raise connect_error
            return cls()

        def dependents(self, schema, table):
            state.listing_calls.append((schema, table))
            if listing_error is not None:
                raise listing_error
            return listing

        def fetch(self, sql):
            state.fetched.append(sql)
            for view in broken_views:
                if f'."{view}" ' in sql:
                    raise RuntimeError(f"relation {view} does not exist")
            return None

        def close(self):
            state.closed += 1

    if drop_semantics is not UNSET:
        _Adapter.drop_semantics = drop_semantics
    _Adapter.state = state
    return _Adapter


def run_op(run, env, cls):
    """Run the op function *run* with *env* and a discovery of *cls*; return ``(exit_code, block)``.

    Asserts the block is the one and only block, the last line on stdout, and that
    its body parses as JSON.
    """
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run(dict(env), discover=lambda: ("fake", cls))
    lines = out.getvalue().splitlines()
    assert out.getvalue().count(result.SENTINEL_BEGIN) == 1
    assert lines[-3] == result.SENTINEL_BEGIN and lines[-1] == result.SENTINEL_END
    return code, json.loads(lines[-2])


def with_env(env, **changes):
    """*env* with *changes* applied; a change to ``None`` removes the variable."""
    merged = {**env, **changes}
    return {k: v for k, v in merged.items() if v is not None}
